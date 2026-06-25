import bz2
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

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
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
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
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
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
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
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
    "blocking waiting for file lock",
    "unexpected eof",
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
    import time

    last_result = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
            )
        except FileNotFoundError:
            logger.warning("cargo not found on PATH while running %s", args)
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                "cargo %s attempt %d/%d timed out (cwd=%s)",
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

    message_to_agent = prompt + repo_info + unit_tests_info + spec_info
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
