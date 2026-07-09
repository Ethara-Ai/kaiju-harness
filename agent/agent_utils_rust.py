import bz2
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

import git

from agent.class_types import AgentConfig
from agent.thinking_capture import SummarizerCost
from commit0.harness.constants_rust import RUST_STUB_MARKER, RUST_TEST_IDS_DIR

logger = logging.getLogger(__name__)

_EXCLUDED_DIRS = {"tests", "benches", "examples", "target", ".git"}

# group(1) = full signature, group(2) = fn name; handles pub/async/unsafe/const/generics/return type
_FN_PATTERN = re.compile(
    r"((?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
    r"fn\s+(\w+)\s*(?:<[^>]*>)?\s*\([^)]*\)(?:\s*->\s*[^{]+?)?\s*)\{",
    re.DOTALL,
)


def _read_source_text(file_path: str) -> str:
    """E14: read a source file, SURFACING decode problems instead of hiding them.

    The old `errors="ignore"` silently DROPPED undecodable bytes from source the
    model has to reproduce verbatim — corrupting the content invisibly. We use
    `errors="replace"` (consistent with the test-file reads) so any bad byte
    becomes a visible U+FFFD, and we WARN when that happens so a genuinely
    non-UTF-8 source file is flagged rather than silently mangled. Raises OSError
    to the caller (callers already handle it)."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    if "�" in content:
        logger.warning(
            "E14: %s contains bytes that are not valid UTF-8 — replaced with U+FFFD. "
            "The model may not reproduce this file exactly.", file_path,
        )
    return content


def find_rust_files_to_edit(src_dir: str) -> list[str]:
    """Walk *src_dir* and collect ``.rs`` files, excluding non-source paths.

    Excluded directories: ``tests``, ``benches``, ``examples``, ``target``, ``.git``.
    Excluded files: ``build.rs`` at any level.

    Returns absolute paths, sorted.
    """
    rs_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for fname in filenames:
            if not fname.endswith(".rs"):
                continue
            if fname == "build.rs":
                continue
            rs_files.append(os.path.normpath(os.path.join(dirpath, fname)))

    rs_files.sort()
    return rs_files


def get_target_edit_files_rust(src_dir: str) -> list[str]:
    """Return the subset of ``.rs`` files that contain the stub marker.

    The stub marker is :data:`commit0.harness.constants_rust.RUST_STUB_MARKER`
    (``panic!("STUB: not implemented")``).
    """
    all_files = find_rust_files_to_edit(src_dir)
    target_files: list[str] = []

    for file_path in all_files:
        try:
            content = _read_source_text(file_path)
            if RUST_STUB_MARKER in content:
                target_files.append(file_path)
        except OSError as exc:
            logger.warning("Could not read %s: %s", file_path, exc)

    return target_files


def extract_rust_function_stubs(file_path: str) -> list[dict]:
    """Find functions whose body contains the stub marker.

    Returns a list of dicts, each with:
      - ``name``  : function name (str)
      - ``line``  : 1-based line number of the ``fn`` keyword (int)
      - ``signature``: full text from qualifiers through the opening ``{`` (str)
    """
    try:
        content = _read_source_text(file_path)
    except OSError as exc:
        logger.warning("Could not read %s: %s", file_path, exc)
        return []

    stubs: list[dict] = []

    for match in _FN_PATTERN.finditer(content):
        fn_name = match.group(2)
        signature = match.group(1).strip()
        line_number = content[: match.start()].count("\n") + 1

        depth = 1
        pos = match.end()
        while pos < len(content) and depth > 0:
            ch = content[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1

        body = content[match.end() : pos - 1] if depth == 0 else ""

        if RUST_STUB_MARKER in body:
            stubs.append(
                {
                    "name": fn_name,
                    "line": line_number,
                    "signature": signature,
                }
            )

    return stubs


def get_rust_file_dependencies(file_path: str) -> list[str]:
    """Parse ``use`` and ``mod`` statements to determine module dependencies.

    Extracts:
      - ``use crate::...`` imports  (returns the crate-relative module path)
      - ``mod name;`` declarations  (external module references, not inline blocks)

    Returns a deduplicated, sorted list of module path strings.
    """
    try:
        content = _read_source_text(file_path)
    except OSError as exc:
        logger.warning("Could not read %s: %s", file_path, exc)
        return []

    deps: set[str] = set()

    for m in re.finditer(r"use\s+crate::(\S+?)\s*[;{]", content):
        path = m.group(1).rstrip(":").rstrip("{")
        if path:
            deps.add(path)

    for m in re.finditer(r"use\s+super::(\S+?)\s*[;{]", content):
        path = m.group(1).rstrip(":").rstrip("{")
        if path:
            deps.add(f"super::{path}")

    for m in re.finditer(r"mod\s+(\w+)\s*;", content):
        deps.add(m.group(1))

    return sorted(deps)


# Section headers (local copies to avoid circular imports with agent_utils)
_PROMPT_HEADER = ">>> Here is the Task:\n"
_REPO_INFO_HEADER = "\n\n>>> Here is the Repository Information:\n"
_UNIT_TESTS_INFO_HEADER = "\n\n>>> Here are the Unit Tests Information:\n"
_SPEC_INFO_HEADER = "\n\n>>> Here is the Specification Information:\n"
_IMPORT_DEPENDENCIES_HEADER = "\n\n>>> Here are the Import Dependencies:\n"

_RUST_TEST_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a test output summarizer for an AI coding agent. "
    "Your job is to compress cargo test output while preserving ALL information "
    "needed to debug test failures.\n\n"
    "PRESERVE (mandatory, never drop):\n"
    "- EVERY failed test name and its full traceback.\n"
    "- Assertion messages with expected vs actual values.\n"
    "- Compilation errors (error[E...] lines) with full context.\n"
    "- The test result summary line.\n"
    "- The failures section listing which tests failed.\n\n"
    "OMIT (drop first when budget is tight):\n"
    "- Docker/container setup output.\n"
    "- Passing test details (just keep the count).\n"
    "- Duplicate information.\n"
    "- Warnings unless they indicate why tests fail.\n"
    "- Captured stdout from passing tests.\n\n"
    "FORMAT: Keep tracebacks as code blocks. Be maximally dense."
)


_TRANSIENT_CARGO_ERRORS = (
    "failed to connect",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "operation timed out",
    "error sending request",
    "503 service unavailable",
    "502 bad gateway",
    "could not connect to",
    "unexpected eof",
    # E3: "blocking waiting for file lock" is intentionally NOT here. cargo prints
    # it while WAITING and normally proceeds once the lock frees — if it instead
    # surfaces in stderr of a FAILED run, a sibling cargo is wedged holding the
    # lock, and retrying just re-blocks on the same dead lock (wasting
    # max_attempts × timeout). The pgid-kill timeout above already reaps any
    # orphan WE created; a persistent foreign lock should fail fast and visibly.
)


def _is_transient_cargo_error(stderr: str) -> bool:
    """Heuristic check whether *stderr* indicates a transient cargo failure."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in _TRANSIENT_CARGO_ERRORS)


def _run_cargo_with_retry(
    args: list[str],
    cwd: str,
    timeout: int = 120,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
):
    """Run a cargo command, retrying on transient (network/lock) failures.

    Retries are gated on stderr matching :data:`_TRANSIENT_CARGO_ERRORS`. Deterministic
    failures (compile errors, missing manifest) return immediately. Backoff is
    ``backoff_base ** attempt`` seconds with a small jitter to spread retries.

    Returns the final :class:`subprocess.CompletedProcess` (success or last
    failure), or ``None`` if cargo could not be launched at all.
    """
    import random
    import signal
    import time

    last_result = None
    for attempt in range(1, max_attempts + 1):
        # E3: cargo spawns rustc grandchildren. `subprocess.run(timeout=)` SIGKILLs
        # only cargo on timeout, orphaning rustc workers that keep holding the
        # build lock and burning CPU. Run cargo in its own session and kill the
        # WHOLE process group on timeout so a hung test can't wedge the next run.
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.warning("cargo not found on PATH while running %s", args)
            return None
        except OSError as exc:
            logger.warning("cargo launch OSError (cwd=%s): %s", cwd, exc)
            if attempt >= max_attempts:
                return None
            time.sleep((backoff_base ** attempt) + random.uniform(0, 0.5))
            continue
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            result = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            logger.warning(
                "cargo %s attempt %d/%d timed out — process group killed (cwd=%s)",
                args[1] if len(args) > 1 else "?",
                attempt,
                max_attempts,
                cwd,
            )
            if attempt >= max_attempts:
                return last_result
            time.sleep((backoff_base ** attempt) + random.uniform(0, 0.5))
            continue
        except OSError as exc:
            logger.warning(
                "cargo %s attempt %d/%d OSError (cwd=%s): %s",
                args[1] if len(args) > 1 else "?",
                attempt,
                max_attempts,
                cwd,
                exc,
            )
            if attempt >= max_attempts:
                return None
            time.sleep((backoff_base ** attempt) + random.uniform(0, 0.5))
            continue

        if result.returncode == 0:
            return result
        if attempt >= max_attempts or not _is_transient_cargo_error(result.stderr):
            return result
        logger.warning(
            "cargo %s attempt %d/%d transient failure rc=%d (cwd=%s); retrying",
            args[1] if len(args) > 1 else "?",
            attempt,
            max_attempts,
            result.returncode,
            cwd,
        )
        last_result = result
        time.sleep((backoff_base ** attempt) + random.uniform(0, 0.5))
    return last_result


def get_rust_test_ids(repo_path: str) -> list[str]:
    """Get Rust test identifiers by running ``cargo test --list``.

    Parses output lines like ``module::test_name: test`` and returns
    the fully qualified test names (without the trailing ``: test``).

    Falls back to cached test IDs in the data directory if cargo is
    unavailable or the command fails.
    """
    test_ids: list[str] = []

    result = _run_cargo_with_retry(
        ["cargo", "test", "--", "--list"],
        cwd=repo_path,
        timeout=120,
    )
    if result is not None and result.returncode == 0:
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.endswith(": test"):
                test_ids.append(line[: -len(": test")])
            elif line.endswith(": benchmark"):
                continue
        if test_ids:
            return sorted(test_ids)
    elif result is not None:
        logger.warning(
            "cargo test --list failed after retries (rc=%d) in %s: %s",
            result.returncode,
            repo_path,
            result.stderr[:500],
        )

    repo_name = os.path.basename(os.path.normpath(repo_path))
    cache_path = RUST_TEST_IDS_DIR / f"{repo_name}.json"
    cache_path_bz2 = RUST_TEST_IDS_DIR / f"{repo_name}.bz2"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                test_ids = [str(t) for t in cached]
                logger.info(
                    "Loaded %d cached test IDs for %s", len(test_ids), repo_name
                )
                return sorted(test_ids)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to load cached test IDs from %s: %s", cache_path, exc
            )
    elif cache_path_bz2.exists():
        try:
            raw = bz2.decompress(cache_path_bz2.read_bytes()).decode("utf-8")
            test_ids = [line.strip() for line in raw.splitlines() if line.strip()]
            if test_ids:
                logger.info(
                    "Loaded %d cached test IDs from bz2 for %s",
                    len(test_ids),
                    repo_name,
                )
                return sorted(test_ids)
        except (OSError, ValueError) as exc:
            logger.warning(
                "Failed to load cached test IDs from %s: %s", cache_path_bz2, exc
            )
    return sorted(test_ids)


def _get_dir_tree(dir_path: str, max_depth: int = 2, _depth: int = 0) -> str:
    if _depth >= max_depth:
        return ""
    try:
        entries = sorted(os.listdir(dir_path))
    except OSError:
        return ""
    lines: list[str] = []
    for entry in entries:
        if entry.startswith("."):
            continue
        full = os.path.join(dir_path, entry)
        indent = "  " * _depth
        if os.path.isdir(full):
            lines.append(f"{indent}{entry}/")
            lines.append(_get_dir_tree(full, max_depth, _depth + 1))
        else:
            lines.append(f"{indent}{entry}")
    return "\n".join(filter(None, lines))


_MAX_DEP_CONTEXT_CHARS = 50_000


def get_message_rust(
    agent_config: AgentConfig,
    repo_path: str,
    test_files: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
) -> tuple[str, list[SummarizerCost]]:
    """Build the agent prompt for a Rust repo.

    Loads ``rust_system_prompt.md`` and fills the ``{repo_name}``,
    ``{function_list}``, and ``{file_context}`` placeholders. Appends optional
    repo info, unit test info, and spec info sections.

    Args:
        agent_config: Agent configuration.
        repo_path: Absolute path to the repo's working directory.
        test_files: Optional list of test file paths for unit_tests_info section.
        target_files: Optional explicit list of stub files to scope
            ``function_list`` and ``file_context`` to. When omitted, all stubs
            in the repo are discovered automatically. Per-file callers should
            pass ``[single_file]`` to keep the prompt small on large crates.

    Returns (message, summarizer_costs).

    """
    spec_costs: list[SummarizerCost] = []

    template_path = Path(__file__).parent / "prompts" / "rust_system_prompt.md"
    try:
        template = template_path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("Could not read rust_system_prompt.md: %s", exc)
        template = agent_config.user_prompt

    repo_name = os.path.basename(os.path.normpath(repo_path))
    if target_files is None:
        target_files = get_target_edit_files_rust(repo_path)

    function_lines: list[str] = []
    all_dep_content: list[str] = []
    seen_deps: set[str] = set()
    dep_chars = 0
    dep_cap_reached = False

    for fpath in target_files:
        stubs = extract_rust_function_stubs(fpath)
        rel = os.path.relpath(fpath, repo_path)
        for stub in stubs:
            function_lines.append(
                f"- {stub['name']} ({rel}:{stub['line']}): {stub['signature']}"
            )

        if dep_cap_reached:
            continue
        deps = get_rust_file_dependencies(fpath)
        base_dir = os.path.dirname(fpath)
        for dep in deps:
            if dep in seen_deps:
                continue
            seen_deps.add(dep)
            if dep.startswith("super::"):
                rel_mod = dep[len("super::") :].replace("::", os.sep)
                dep_file = os.path.join(base_dir, "..", rel_mod + ".rs")
                if not os.path.isfile(dep_file):
                    dep_file = os.path.join(base_dir, "..", rel_mod, "mod.rs")
            else:
                dep_file = os.path.join(base_dir, dep.replace("::", os.sep) + ".rs")
                if not os.path.isfile(dep_file):
                    dep_file = os.path.join(
                        base_dir, dep.replace("::", os.sep), "mod.rs"
                    )
            if os.path.isfile(dep_file):
                try:
                    with open(dep_file, "r", encoding="utf-8", errors="ignore") as fh:
                        lines = fh.readlines()[:200]
                    dep_rel = os.path.relpath(dep_file, repo_path)
                    block = f"// --- {dep_rel} ---\n" + "".join(lines)
                    remaining = _MAX_DEP_CONTEXT_CHARS - dep_chars
                    if remaining <= 0:
                        dep_cap_reached = True
                        break
                    if len(block) > remaining:
                        block = block[:remaining] + "\n// … dep_context cap reached …\n"
                        dep_cap_reached = True
                    all_dep_content.append(block)
                    dep_chars += len(block)
                    if dep_cap_reached:
                        break
                except OSError:
                    pass

    function_list = "\n".join(function_lines) if function_lines else "(none found)"
    file_context = (
        "\n\n".join(all_dep_content) if all_dep_content else "(no dependency context)"
    )

    try:
        filled_template = template.format(
            repo_name=repo_name,
            function_list=function_list,
            file_context=file_context,
        )
    except KeyError as exc:
        logger.warning("Template placeholder error: %s", exc)
        filled_template = template

    prompt = _PROMPT_HEADER + filled_template

    if agent_config.use_unit_tests_info and test_files:
        unit_tests_info = f"\n{_UNIT_TESTS_INFO_HEADER} "
        for test_file in test_files:
            tf_path = Path(os.path.join(repo_path, test_file))
            if tf_path.exists():
                try:
                    unit_tests_info += tf_path.read_text(errors="replace")
                except OSError:
                    pass
        unit_tests_info = unit_tests_info[: agent_config.max_unit_tests_info_length]
    else:
        unit_tests_info = ""

    if agent_config.use_repo_info:
        repo_info = (
            f"\n{_REPO_INFO_HEADER} "
            + _get_dir_tree(repo_path, max_depth=2)[: agent_config.max_repo_info_length]
        )
    else:
        repo_info = ""

    spec_info = ""
    if agent_config.use_spec_info:
        spec_pdf_path = Path(repo_path) / "spec.pdf"
        spec_bz2_path = Path(repo_path) / "spec.pdf.bz2"
        decompress_failed = False
        if spec_bz2_path.exists() and not spec_pdf_path.exists():
            try:
                with bz2.open(str(spec_bz2_path), "rb") as in_file:
                    with open(str(spec_pdf_path), "wb") as out_file:
                        out_file.write(in_file.read())
            except Exception as e:
                logger.warning(
                    "Failed to decompress spec file %s: %s", spec_bz2_path, e
                )
                if spec_pdf_path.exists():
                    spec_pdf_path.unlink()
                decompress_failed = True
        if not decompress_failed and spec_pdf_path.exists():
            try:
                import fitz as _fitz

                raw_spec = ""
                with _fitz.open(spec_pdf_path) as document:
                    for page_num in range(len(document)):
                        page = document.load_page(page_num)
                        raw_spec += str(page.get_text())
            except Exception as exc:
                logger.warning("Failed to extract spec PDF text: %s", exc)
                raw_spec = ""

            if raw_spec:
                if len(raw_spec) > int(agent_config.max_spec_info_length * 1.5):
                    try:
                        from agent.agent_utils import summarize_specification

                        processed_spec, spec_costs = summarize_specification(
                            spec_text=raw_spec,
                            model=agent_config.model_name,
                            max_tokens=agent_config.spec_summary_max_tokens,
                            max_char_length=agent_config.max_spec_info_length,
                            cache_path=spec_pdf_path.parent
                            / ".spec_summary_cache.json",
                            model_short=getattr(agent_config, "model_short", ""),
                        )
                    except Exception as exc:
                        logger.warning("Spec summarization failed: %s", exc)
                        processed_spec = raw_spec[: agent_config.max_spec_info_length]
                else:
                    processed_spec = raw_spec
                spec_info = f"\n{_SPEC_INFO_HEADER} " + processed_spec
        if not spec_info:
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = Path(repo_path) / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[: agent_config.max_spec_info_length]
                        spec_info = f"\n{_SPEC_INFO_HEADER} " + readme_text
                        logger.info(
                            "Using %s as spec fallback for %s",
                            readme_name,
                            repo_path,
                        )
                        break
                    except Exception as e:
                        logger.warning("Failed to read %s: %s", readme_path, e)

    # Imported lazily (like summarize_specification above) to avoid a top-level
    # circular import with agent.agent_utils. Appended AFTER template fill so the
    # brace-free note can never be reinterpreted as a template placeholder.
    from agent.agent_utils import MODULE_SCOPE_NOTE

    message_to_agent = (
        prompt + repo_info + unit_tests_info + spec_info + MODULE_SCOPE_NOTE
    )
    return message_to_agent, spec_costs


def get_lint_cmd_rust(
    repo_name: str,
    use_lint_info: bool,
    repo_path: str,
) -> str:
    """Generate the Rust lint command string.

    When *use_lint_info* is True, returns a ``cargo clippy`` command
    targeting the repo.  Otherwise returns an empty string (lint disabled).

    *repo_name* is accepted for signature parity with the Python
    ``get_lint_cmd`` but is not used directly.

    E17: this is necessarily a WHOLE-CRATE clippy — clippy operates at
    crate/target granularity and cannot lint a single file. The lint stage loops
    per file and runs this command in each file's agent session, so clippy
    re-runs (and each session sees the full-crate warning set, not just its
    file's). That is a known cost×files trade-off; true per-file scoping would
    require running clippy ONCE and filtering diagnostics by path before each
    session. Callers that care about cost should lint once and cache.
    """
    if not use_lint_info:
        return ""
    manifest = os.path.join(repo_path, "Cargo.toml")
    if os.path.isfile(manifest):
        return (
            f'cargo clippy --manifest-path "{manifest}" '
            "--all-targets --message-format=short -- -D warnings"
        )
    return "cargo clippy --all-targets --message-format=short -- -D warnings"


def get_changed_files_rust(
    repo: git.Repo,
    commit1: str,
    commit2: str,
) -> list[str]:
    """Get changed ``.rs`` files between two commits.

    Mirrors :func:`agent.agent_utils.get_changed_files_from_commits` but
    filters for Rust source files instead of Python.
    """
    try:
        commit1_obj = repo.commit(commit1)
        commit2_obj = repo.commit(commit2)
        diff = commit1_obj.diff(commit2_obj)
        changed_files = [item.a_path for item in diff if item.a_path is not None]
        rust_files = [f for f in changed_files if f.endswith(".rs")]
        return rust_files
    except Exception as e:
        logger.error(
            "Failed to get changed files between %s and %s: %s",
            commit1,
            commit2,
            e,
            exc_info=True,
        )
        return []


def _count_tokens_rust(text: str, model: str) -> int:
    try:
        import litellm

        return litellm.token_counter(model=model, text=text)
    except Exception:
        return len(text) // 4


def _parse_cargo_test_output(raw: str) -> str:
    """Tier 1: Deterministic extraction from cargo test output.

    Extracts failures, test result summary line, and error messages.
    """
    lines = raw.split("\n")

    cargo_start = -1
    for i, line in enumerate(lines):
        if re.match(r"running \d+ test", line):
            cargo_start = i
            break

    if cargo_start > 0:
        lines = lines[cargo_start:]

    text = "\n".join(lines)
    sections: list[str] = []

    failures_match = re.search(
        r"(failures:\s*\n.*?)(?=test result:|$)",
        text,
        re.DOTALL,
    )
    if failures_match:
        sections.append(failures_match.group(1).strip())

    result_match = re.search(r"(test result: .+)", text)
    if result_match:
        sections.append(result_match.group(1).strip())

    error_lines = [line for line in lines if re.match(r"error\[E\d+\]", line)]
    if error_lines:
        sections.append("\n".join(error_lines))

    if sections:
        return "\n\n".join(sections)

    return text


def summarize_rust_test_output(
    raw_output: str,
    max_length: int = 15000,
    model: str = "",
    max_tokens: int = 4000,
) -> tuple[str, list[SummarizerCost]]:
    """Hybrid 3-tier Rust test output summarization.

    Mirrors :func:`agent.agent_utils.summarize_test_output` but uses
    Rust-specific parsing for Tier 1 (``cargo test`` output format).

    Returns (summarized_text, list_of_costs).
    """
    all_costs: list[SummarizerCost] = []

    max_token_length = (
        _count_tokens_rust(raw_output[:max_length], model) if model else max_length // 4
    )
    if max_token_length < 1:
        max_token_length = max_length // 4

    raw_tokens = (
        _count_tokens_rust(raw_output, model) if model else len(raw_output) // 4
    )
    if raw_tokens <= max_token_length:
        return raw_output, all_costs

    parsed = _parse_cargo_test_output(raw_output)
    parsed_tokens = _count_tokens_rust(parsed, model) if model else len(parsed) // 4
    if parsed_tokens <= max_token_length:
        logger.info(
            "Rust test output summarized (Tier 1 parse): %d -> %d tokens",
            raw_tokens,
            parsed_tokens,
        )
        return parsed, all_costs

    try:
        import litellm

        response = litellm.completion(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        _RUST_TEST_SUMMARIZER_SYSTEM_PROMPT
                        + "\n- Your summary MUST be under "
                        + str(max_token_length)
                        + " tokens."
                    ),
                },
                {
                    "role": "user",
                    "content": "Summarize this test output:\n\n" + parsed,
                },
            ],
            max_tokens=max_tokens,
        )

        cost = SummarizerCost()
        usage = getattr(response, "usage", None)
        if usage:
            cost.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            cost.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        try:
            cost.cost = litellm.completion_cost(completion_response=response)
        except Exception:
            pass
        all_costs.append(cost)

        content = response.choices[0].message.content  # type: ignore[union-attr]
        if content:
            result = content.strip()
            logger.info(
                "Rust test output summarized (Tier 2 LLM): %d -> %d chars (model=%s)",
                len(raw_output),
                len(result),
                model,
            )
            return result, all_costs
    except Exception:
        logger.warning(
            "LLM test summarization failed, falling back to truncation",
            exc_info=True,
        )

    head = 2000
    tail = 2000
    if max_length >= head + tail + 40:
        truncated = parsed[:head] + "\n\n... [truncated] ...\n\n" + parsed[-tail:]
        logger.info(
            "Rust test output summarized (Tier 3 truncation): %d -> %d chars",
            len(raw_output),
            len(truncated),
        )
        return truncated, all_costs
    return parsed[:max_length], all_costs


# ---------------------------------------------------------------------------
# Per-edit compile gate (opt-in via AgentConfig.per_edit_compile_gate)
# ---------------------------------------------------------------------------
#
# Wrap an aider.run() call so that AFTER each edit, we run `cargo check` and
# REVERT the edit if it broke a file that compiled before. Without this gate,
# bad edits accumulate (4 -> 43 errors observed on virtio-drivers Stage 2).
#
# Design choices (locked-in after user review, see compressed block b1):
#   - Per-file detection: any rustc `error[Exxxx]: ... --> path/to/file.rs:LINE`
#     spans are extracted; we intersect with files edited by this run.
#   - Whole-module revert when retries exhaust: cleaner than cherry-picking
#     individual file reverts, which would risk dangling references.
#   - 2 retries by default: first retry feeds errors back to the LLM; second
#     retry is the agent's last shot before we revert. Configurable via
#     AgentConfig.compile_gate_max_retries.
#   - cargo check on already-broken trees: we record the baseline error set
#     at start; only NEW errors trigger revert. The agent isn't penalised for
#     errors it inherited from a previous module.
#   - Standalone helper (not a class) keeps the import surface flat and the
#     unit tests trivial to write.


_CARGO_ERROR_SPAN_RE = re.compile(
    r"^\s*-->\s+([^\s:]+?\.rs):(?P<line>\d+):(?P<col>\d+)\s*$",
    re.MULTILINE,
    )
# E15: match BOTH long-format (`error[E0308]: ...` at line start) AND short-format
# (`src/foo.rs:12:5: error[E0308]: ...`), since the gate runs cargo with
# `--message-format=short`. The old `^error...` anchor missed every short-format
# diagnostic, so the re-prompt fed the LLM an EMPTY error list on a broken build.
_CARGO_ERROR_LINE_RE = re.compile(
    r"^(?:\S+\.rs:\d+:\d+:\s+)?error(?:\[[A-Z]\d+\])?:\s", re.MULTILINE
    )


# E13: process-level baseline cache, keyed on (repo_realpath, git HEAD sha). A
# given tree state has one cargo-check result, so consecutive modules at the same
# sha reuse it instead of re-running a whole-crate check. Keyed on the exact sha,
# so a changed tree (new sha) always re-checks — never a stale baseline.
_BASELINE_CHECK_CACHE: "dict[tuple[str, str], tuple[int, str]]" = {}


def _run_cargo_check(repo_path: str, timeout: int = 180) -> tuple[int, str]:
    """Run ``cargo check --tests --message-format=short`` and return (rc, stderr).

    Uses ``--message-format=short`` so error locations include the file:line
    we want without the full multi-line span output (which can be huge).
    """
    import subprocess
    import signal
    # C12/E13: cargo spawns rustc grandchildren; a plain `timeout=` SIGKILLs only
    # cargo and leaves rustc holding the build lock, wedging the next check. Run
    # cargo in its own process group and kill the whole group on timeout.
    try:
        proc = subprocess.Popen(
            ["cargo", "check", "--tests", "--all-features", "--message-format=short"],
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"cargo check failed to invoke: {exc}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return 124, f"cargo check timed out after {timeout}s (process group killed)"
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"cargo check failed: {exc}"
    # cargo emits errors on stderr; combine with stdout in case any leaked.
    return proc.returncode, (stderr or "") + (stdout or "")


def _extract_files_with_errors(cargo_output: str, repo_path: str) -> set[str]:
    """Parse cargo's --message-format=short output into a set of absolute file paths.

    Short format emits one line per diagnostic:
        src/foo.rs:42:5: error[E0308]: mismatched types
    Long format (which we may still see on some toolchains) emits multi-line
    spans where the file appears on a `--> path:line:col` line.
    """
    broken: set[str] = set()
    # realpath (not just abspath) so symlinked repo dirs — e.g. macOS
    # /var -> /private/var — canonicalize the same way cargo's cwd-relative
    # diagnostics and git's paths do, otherwise the gate's set intersection
    # misses real regressions.
    repo_path_abs = os.path.realpath(repo_path)
    for line in cargo_output.splitlines():
        # short format: `src/foo.rs:LINE:COL: error...`
        m = re.match(r"^([^\s:]+?\.rs):\d+:\d+:\s*error", line)
        if m:
            rel = m.group(1)
            broken.add(os.path.realpath(os.path.join(repo_path_abs, rel)))
            continue
        # long format span line
        m2 = re.match(r"^\s*-->\s+([^\s:]+?\.rs):\d+:\d+", line)
        if m2:
            rel = m2.group(1)
            broken.add(os.path.realpath(os.path.join(repo_path_abs, rel)))
    return broken


def _extract_file_error_counts(cargo_output: str, repo_path: str) -> dict[str, int]:
    """Map each file to its number of error diagnostics.

    Unlike :func:`_extract_files_with_errors` (presence only), this lets the
    compile gate detect when a file that was ALREADY broken at baseline got
    *worse* — comparing per-file error counts, not just the file set. Without
    it, a pre-broken edited file gets a free pass no matter how badly the agent
    breaks it further (it stays in both baseline and post sets).
    """
    counts: dict[str, int] = {}
    # realpath (not just abspath) so symlinked repo dirs — e.g. macOS
    # /var -> /private/var — canonicalize the same way cargo's cwd-relative
    # diagnostics and git's paths do, otherwise the gate's set intersection
    # misses real regressions.
    repo_path_abs = os.path.realpath(repo_path)
    for line in cargo_output.splitlines():
        # short format: `src/foo.rs:LINE:COL: error...`
        m = re.match(r"^([^\s:]+?\.rs):\d+:\d+:\s*error", line)
        if m:
            f = os.path.realpath(os.path.join(repo_path_abs, m.group(1)))
            counts[f] = counts.get(f, 0) + 1
    return counts


def _format_errors_for_prompt(cargo_output: str, max_chars: int = 4000) -> str:
    """Trim cargo output to the most useful slice for re-prompting the LLM.

    Keeps `error[...]:` lines + their immediately following context lines.
    Caps at ``max_chars`` to keep prompt budget reasonable.
    """
    keep_lines: list[str] = []
    in_error = False
    error_context_remaining = 0
    for line in cargo_output.splitlines():
        if _CARGO_ERROR_LINE_RE.match(line):
            keep_lines.append(line)
            in_error = True
            error_context_remaining = 3
            continue
        if in_error:
            if error_context_remaining > 0:
                keep_lines.append(line)
                error_context_remaining -= 1
            else:
                in_error = False
    out = "\n".join(keep_lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... [truncated]"
    return out


def _files_edited_since(repo_path: str, pre_sha: str) -> set[str]:
    """Return absolute paths of `.rs` files changed since ``pre_sha`` (uncommitted included)."""
    import subprocess
    files: set[str] = set()
    # realpath (not just abspath) so symlinked repo dirs — e.g. macOS
    # /var -> /private/var — canonicalize the same way cargo's cwd-relative
    # diagnostics and git's paths do, otherwise the gate's set intersection
    # misses real regressions.
    repo_path_abs = os.path.realpath(repo_path)
    # Committed changes since pre_sha
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", pre_sha, "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        for rel in r.stdout.splitlines():
            if rel.endswith(".rs"):
                files.add(os.path.realpath(os.path.join(repo_path_abs, rel)))
    except (subprocess.SubprocessError, OSError):
        pass
    # Uncommitted working-tree changes (aider sometimes leaves these)
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
        for rel in r.stdout.splitlines():
            if rel.endswith(".rs"):
                files.add(os.path.realpath(os.path.join(repo_path_abs, rel)))
    except (subprocess.SubprocessError, OSError):
        pass
    return files


def run_with_compile_gate(
    run_aider_call: "Callable[[], Any]",
    *,
    repo_path: str,
    local_repo: "Any",
    pre_sha: str,
    max_retries: int = 2,
    cargo_timeout: int = 180,
    on_revert: "Optional[Callable[[list, str], None]]" = None,
    on_retry: "Optional[Callable[[int, list, str], None]]" = None,
    re_prompt_callback: "Optional[Callable[[str], Any]]" = None,
) -> dict:
    """Invoke ``run_aider_call()`` then verify with cargo check; revert on regression.

    Behaviour:
      1. Capture baseline cargo error file-set BEFORE invoking aider.
      2. Invoke ``run_aider_call()`` (caller wires the actual aider.run with args).
      3. cargo check: if (new broken files - baseline) is empty, KEEP.
      4. Otherwise, retry up to ``max_retries`` by calling
         ``re_prompt_callback(error_text)`` which is expected to call aider again.
      5. If still broken after retries, ``git reset --hard pre_sha`` to revert
         the entire module's edits.

    Returns a dict with keys:
      - ``status``: "kept" | "reverted" | "clean_no_op"
      - ``retries_used``: int
      - ``baseline_broken_files``: list[str]
      - ``final_broken_files``: list[str]
      - ``regressions``: list[str]   # files the agent broke
    """
    # E13: the baseline cargo check is whole-crate and re-run for EVERY module —
    # quadratic across a stage. Cache (repo_path, head_sha) -> (rc, output) so
    # consecutive modules at the same committed tree reuse the result.
    # IMPORTANT: the HEAD sha only captures the COMMITTED tree. aider "sometimes
    # leaves" uncommitted edits (see _files_edited_since), so a dirty working tree
    # at the same sha is a DIFFERENT tree than the cached clean baseline. We
    # therefore ONLY cache/reuse when the tree is CLEAN — a dirty tree always
    # re-runs, so the cache can never serve a stale baseline.
    _head_sha = None
    _tree_clean = False
    try:
        _head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path,
            capture_output=True, text=True, timeout=15,
        ).stdout.strip() or None
        _porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_path,
            capture_output=True, text=True, timeout=15,
        ).stdout
        _tree_clean = (_porcelain.strip() == "")
    except (subprocess.SubprocessError, OSError):
        _head_sha = None
        _tree_clean = False
    _cache_key = (os.path.realpath(repo_path), _head_sha) if (_head_sha and _tree_clean) else None
    _cached = _BASELINE_CHECK_CACHE.get(_cache_key) if _cache_key else None
    if _cached is not None:
        baseline_rc, baseline_out = _cached
        logger.info("CompileGate: reusing cached baseline for HEAD %s (rc=%d)",
                    _head_sha[:8], baseline_rc)
    else:
        # Baseline: errors that already exist BEFORE this module touches anything.
        baseline_rc, baseline_out = _run_cargo_check(repo_path, timeout=cargo_timeout)
        # rc 124 (timeout) / -1 (failed to invoke) mean the check didn't actually
        # run — retry once so we don't proceed with a phantom-empty baseline that
        # would later mis-attribute inherited errors as regressions.
        if baseline_rc in (124, -1):
            logger.warning(
                "CompileGate: baseline cargo check unavailable (rc=%d); retrying once",
                baseline_rc,
            )
            baseline_rc, baseline_out = _run_cargo_check(repo_path, timeout=cargo_timeout)
        # Only cache a baseline that actually RAN (don't memoize a timeout/failure).
        if _cache_key is not None and baseline_rc not in (124, -1):
            _BASELINE_CHECK_CACHE[_cache_key] = (baseline_rc, baseline_out)
    baseline_unavailable = baseline_rc in (124, -1)
    baseline_broken = _extract_files_with_errors(baseline_out, repo_path)
    baseline_counts = _extract_file_error_counts(baseline_out, repo_path)
    logger.info(
        "CompileGate: baseline cargo check rc=%d, %d files with errors%s",
        baseline_rc, len(baseline_broken),
        " (baseline UNAVAILABLE — verification degraded)" if baseline_unavailable else "",
    )

    # Step 1: initial aider call
    run_aider_call()

    for attempt in range(max_retries + 1):
        rc, out = _run_cargo_check(repo_path, timeout=cargo_timeout)
        if rc == 0:
            return {
                "status": "kept",
                "retries_used": attempt,
                "baseline_broken_files": sorted(baseline_broken),
                "final_broken_files": [],
                "regressions": [],
            }

        # rc 124/-1 means cargo check didn't actually run (timeout / launch
        # failure). The error text contains no `file:line:col: error`, so naively
        # we'd compute zero regressions and report "kept" as if verified clean.
        # Surface it as "unverified" instead and keep the edits (reverting on a
        # flaky timeout would destroy good work).
        if rc in (124, -1):
            logger.warning(
                "CompileGate: post-edit cargo check unavailable (rc=%d); "
                "keeping edits UNVERIFIED", rc,
            )
            return {
                "status": "unverified",
                "retries_used": attempt,
                "baseline_broken_files": sorted(baseline_broken),
                "final_broken_files": [],
                "regressions": [],
            }

        broken_now = _extract_files_with_errors(out, repo_path)
        now_counts = _extract_file_error_counts(out, repo_path)
        edited = _files_edited_since(repo_path, pre_sha)
        # "Regressions" = files we edited that have MORE errors than at baseline.
        # Counting (not set membership) catches files that were already broken
        # and got worse — a per-file-presence check would give those a free pass.
        regressions = sorted(
            f for f in edited
            if now_counts.get(f, 0) > baseline_counts.get(f, 0)
        )

        if not regressions:
            # cargo unhappy, but not from anything we touched. Keep the edits.
            logger.info(
                "CompileGate: %d broken files but none are our edits; keeping. "
                "(broken=%d, baseline=%d, edited=%d)",
                len(broken_now), len(broken_now), len(baseline_broken), len(edited),
            )
            return {
                "status": "kept",
                "retries_used": attempt,
                "baseline_broken_files": sorted(baseline_broken),
                "final_broken_files": sorted(broken_now),
                "regressions": [],
            }

        # We have regressions. If retries remain, re-prompt; else revert.
        if attempt < max_retries and re_prompt_callback is not None:
            err_text = _format_errors_for_prompt(out)
            logger.warning(
                "CompileGate: %d regressions from our edits (%s); retrying %d/%d",
                len(regressions),
                ", ".join(os.path.relpath(p, repo_path) for p in regressions[:3]),
                attempt + 1, max_retries,
            )
            if on_retry is not None:
                try:
                    on_retry(attempt + 1, regressions, err_text)
                except Exception:  # noqa: BLE001
                    logger.exception("CompileGate on_retry callback raised")
            try:
                re_prompt_callback(err_text)
            except Exception:  # noqa: BLE001
                logger.exception("CompileGate re_prompt_callback raised; treating as failed retry")
            continue

        # No retries left -> revert entire module's edits.
        logger.error(
            "CompileGate: %d regressions remain after %d retries; reverting to %s",
            len(regressions), max_retries, pre_sha[:8],
        )
        if on_revert is not None:
            try:
                on_revert(regressions, _format_errors_for_prompt(out))
            except Exception:  # noqa: BLE001
                logger.exception("CompileGate on_revert callback raised")
        try:
            local_repo.git.reset("--hard", pre_sha)
        except Exception:  # noqa: BLE001
            logger.exception("CompileGate: failed to git reset; module left in broken state")
        return {
            "status": "reverted",
            "retries_used": attempt,
            "baseline_broken_files": sorted(baseline_broken),
            "final_broken_files": sorted(broken_now),
            "regressions": regressions,
        }

    # Loop fell through (shouldn't happen)
    return {
        "status": "clean_no_op",
        "retries_used": max_retries,
        "baseline_broken_files": sorted(baseline_broken),
        "final_broken_files": [],
        "regressions": [],
    }

