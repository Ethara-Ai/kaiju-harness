"""TypeScript file collection, stub detection, message generation, test output parsing.

Analogue of agent_utils.py for the TS pipeline.
"""

import git
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Optional

from agent.class_types import AgentConfig
from agent.thinking_capture import SummarizerCost
from commit0.harness.constants_ts import (
    TS_SOURCE_EXTS,
    TS_STUB_MARKER,
    TS_TEST_FILE_PATTERNS,
)

logger = logging.getLogger(__name__)

PROMPT_HEADER = ">>> Here is the Task:\n"
REPO_INFO_HEADER = "\n\n>>> Here is the Repository Information:\n"
UNIT_TESTS_INFO_HEADER = "\n\n>>> Here are the Unit Tests Information:\n"
SPEC_INFO_HEADER = "\n\n>>> Here is the Specification Information:\n"

TS_EXCLUDED_DIRS: set[str] = {
    "node_modules",
    "dist",
    "build",
    ".git",
    ".github",
    "coverage",
    "__tests__",
    "test",
    "tests",
    "examples",
    "example",
    "docs",
    "doc",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".tsbuildinfo",
}


def collect_typescript_files(directory: str) -> list[str]:
    """Walk *directory* for .ts/.tsx files, excluding .d.ts, node_modules/, and dist/."""
    ts_files: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "dist", ".git")]
        for file in files:
            if file.endswith(".d.ts"):
                continue
            if any(file.endswith(ext) for ext in TS_SOURCE_EXTS):
                ts_files.append(os.path.join(root, file))
    return ts_files


def collect_ts_test_files(directory: str) -> list[str]:
    """Collect TS test files by pattern (*.test.ts, *.spec.ts, etc.) and test dirs."""
    test_files: list[str] = []
    test_dir_names = {"__tests__", "test", "tests"}

    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "dist", ".git")]
        basename = os.path.basename(root)
        in_test_dir = basename in test_dir_names

        for file in files:
            if not any(file.endswith(ext) for ext in TS_SOURCE_EXTS):
                continue
            if file.endswith(".d.ts"):
                continue
            is_test_pattern = any(
                file.endswith(pat.lstrip("*")) for pat in TS_TEST_FILE_PATTERNS
            )
            if is_test_pattern or in_test_dir:
                test_files.append(os.path.join(root, file))

    return test_files


def has_ts_stubs(file_path: str) -> bool:
    """Check if *file_path* contains the TS stub marker."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return TS_STUB_MARKER in f.read()
    except OSError:
        logger.warning(
            "Cannot read %s for stub detection, treating as no stubs", file_path
        )
        return False


def extract_ts_stubs(file_path: str) -> list[str]:
    """Extract function/method signatures from *file_path* that contain the STUB marker.

    Returns a list of signature strings (the line containing ``function``/``=>``)
    for each function body that includes ``throw new Error("STUB")``.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        logger.warning("Cannot read %s for stub extraction", file_path)
        return []

    if TS_STUB_MARKER not in content:
        return []

    stubs: list[str] = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if TS_STUB_MARKER in line:
            sig_line = _find_enclosing_signature(lines, i)
            if sig_line is not None:
                stubs.append(sig_line.strip())
            else:
                stubs.append(line.strip())
        i += 1
    return stubs


def _find_enclosing_signature(lines: list[str], stub_index: int) -> Optional[str]:
    """Walk backwards from *stub_index* to find the function/method signature."""
    func_pattern = re.compile(
        r"(export\s+)?(async\s+)?function\s+\w+|"
        r"(public|private|protected|static|async)\s+\w+\s*\(|"
        r"\w+\s*[:=]\s*(async\s+)?\(|"
        r"\w+\s*[:=]\s*(async\s+)?function"
    )
    for j in range(stub_index, max(stub_index - 20, -1), -1):
        if func_pattern.search(lines[j]):
            return lines[j]
    return None


def _find_ts_files_to_edit(
    base_dir: str,
    src_dir: str,
    test_dir: str,
) -> list[str]:
    """Identify TS source files to edit (excludes tests, .d.ts, config files)."""
    files = [
        os.path.normpath(f)
        for f in collect_typescript_files(os.path.join(base_dir, src_dir))
    ]

    test_dirs = [d.strip() for d in test_dir.split(",") if d.strip()]
    test_files: set[str] = set()
    for td in test_dirs:
        test_files.update(
            os.path.normpath(f)
            for f in collect_ts_test_files(os.path.join(base_dir, td))
        )
    files = list(set(files) - test_files)

    if src_dir in (".", ""):
        base = Path(base_dir)
        files = [
            f
            for f in files
            if not any(
                part in TS_EXCLUDED_DIRS for part in Path(f).relative_to(base).parts
            )
        ]

    config_patterns = (
        "tsconfig",
        "jest.config",
        "vitest.config",
        "eslint",
        "prettier",
        "webpack.config",
        "rollup.config",
        "vite.config",
        "babel.config",
        "next.config",
    )
    files = [
        f
        for f in files
        if not any(pat in os.path.basename(f).lower() for pat in config_patterns)
    ]
    files = [f for f in files if not f.endswith(".d.ts")]
    return files


def get_target_edit_files_ts(
    local_repo: git.Repo,
    src_dir: str,
    test_dir: str,
    branch: str,
    reference_commit: str,
    base_commit: str | None = None,
) -> tuple[list[str], dict[str, list[str]]]:
    """Find the TS source files that were stubbed at ``base_commit``.

    Stubbed-set membership is a fixed dataset property of the base commit, NOT a
    fact about the current working tree. This set is the agent's target-edit list
    for EVERY stage (draft + refine): stages 2/3 resume on top of stage 1's
    implementation without resetting, so the files must be identified from
    ``base_commit`` (read each candidate's blob there) rather than the live tree.

    Crucially we do NOT additionally require the working tree to differ from
    ``reference_commit``. In a refine stage the working tree already holds stage
    1's implementation, and for a file the model solved that content can match the
    golden reference exactly — an extra "differs from reference" filter would then
    drop the file, empty the set, and trip the caller's degenerate-0-work guard,
    silently killing stages 2 and 3. The reference diff is only used in the
    ``base_commit``-unavailable fallback, where it is the sole stub signal.
    """
    target_dir = str(local_repo.working_dir)
    files = _find_ts_files_to_edit(target_dir, src_dir, test_dir)

    stubbed_at_base: set[str] = set()
    if base_commit:
        for file_path in files:
            rel_path = os.path.relpath(file_path, target_dir)
            try:
                content = local_repo.git.show(f"{base_commit}:{rel_path}")
            except Exception:  # noqa: BLE001
                continue
            if TS_STUB_MARKER in content:
                stubbed_at_base.add(file_path)

    filtered_files: list[str] = []
    if base_commit:
        # Canonical, branch-state-independent target set: every file stubbed at
        # base_commit. No reference-diff filter — see the docstring.
        filtered_files = [f for f in files if f in stubbed_at_base]
    else:
        # base_commit unavailable: fall back to a working-tree stub scan, using the
        # reference diff as the only available "still needs work" signal.
        for file_path in files:
            if not has_ts_stubs(file_path):
                continue
            rel_path = os.path.relpath(file_path, target_dir)
            if local_repo.git.diff(reference_commit, "--", rel_path):
                filtered_files.append(file_path)

    # Last resort: base_commit stub scan found nothing (e.g. path skew between the
    # recorded base and the working tree) but the tree still shows stubs — use them
    # so a legitimately-stubbed repo isn't failed by the caller's guard.
    if not filtered_files and base_commit:
        wt_stubbed = [f for f in files if has_ts_stubs(f)]
        if wt_stubbed:
            logger.warning(
                "get_target_edit_files_ts: base-commit stub scan returned 0, "
                "falling back to working-tree scan (%d files)", len(wt_stubbed),
            )
            filtered_files = wt_stubbed

    result_files = [os.path.relpath(f, target_dir) for f in filtered_files]
    return result_files, {}


def get_message_ts(
    agent_config: AgentConfig,
    repo_path: str,
    test_files: Optional[list[str]] = None,
) -> tuple[str, list[SummarizerCost]]:
    """Build the prompt message for the agent, TS-flavored.

    Same structure as ``get_message`` but references
    ``throw new Error("STUB")`` instead of ``pass``.
    """
    spec_costs: list[SummarizerCost] = []
    prompt = f"{PROMPT_HEADER}" + agent_config.user_prompt

    if agent_config.use_unit_tests_info and test_files:
        unit_tests_info = f"\n{UNIT_TESTS_INFO_HEADER} "
        for test_file in test_files:
            full_path = os.path.join(repo_path, test_file)
            if os.path.exists(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    unit_tests_info += f"\n--- {test_file} ---\n{content}\n"
                except OSError:
                    logger.warning("Cannot read test file %s, skipping", full_path)
        unit_tests_info = unit_tests_info[: agent_config.max_unit_tests_info_length]
    else:
        unit_tests_info = ""

    if agent_config.use_repo_info:
        repo_info = f"\n{REPO_INFO_HEADER} "
        repo_info += _get_ts_dir_tree(Path(repo_path), max_depth=2)
        repo_info = repo_info[: agent_config.max_repo_info_length]
    else:
        repo_info = ""

    if agent_config.use_spec_info:
        spec_info = ""
        repo_p = Path(repo_path)
        bz2_path = repo_p / "spec.pdf.bz2"
        pdf_path = repo_p / "spec.pdf"

        decompress_failed = False
        if bz2_path.exists() and not pdf_path.exists():
            try:
                import bz2 as _bz2

                with _bz2.open(str(bz2_path), "rb") as bf:
                    with open(str(pdf_path), "wb") as out_f:
                        out_f.write(bf.read())
            except Exception as e:
                logger.warning("Failed to decompress spec %s: %s", bz2_path, e)
                if pdf_path.exists():
                    pdf_path.unlink()
                decompress_failed = True

        if not decompress_failed and pdf_path.exists():
            try:
                from agent.agent_utils import get_specification, summarize_specification

                spec_text = get_specification(specification_pdf_path=pdf_path)
                if len(spec_text) > int(agent_config.max_spec_info_length * 1.5):
                    spec_text, s_costs = summarize_specification(
                        spec_text=spec_text,
                        model=agent_config.model_name,
                        max_tokens=agent_config.spec_summary_max_tokens,
                        max_char_length=agent_config.max_spec_info_length,
                        cache_path=pdf_path.parent / ".spec_summary_cache.json",
                        model_short=getattr(agent_config, "model_short", ""),
                    )
                    spec_costs.extend(s_costs)
                spec_text = spec_text[: agent_config.max_spec_info_length]
                spec_info = f"\n{SPEC_INFO_HEADER} " + spec_text
            except Exception as e:
                logger.warning("Failed to extract spec from %s: %s", pdf_path, e)

        if not spec_info:
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = repo_p / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[: agent_config.max_spec_info_length]
                        spec_info = f"\n{SPEC_INFO_HEADER} " + readme_text
                        logger.info(
                            "Using %s as spec fallback for %s", readme_name, repo_path
                        )
                        break
                    except OSError:
                        logger.debug(
                            "Cannot read %s, trying next README variant", readme_path
                        )
    else:
        spec_info = ""

    # Lazy import (mirrors the get_specification import above) to keep the TS
    # module's top-level import surface free of the PyMuPDF-bearing agent_utils.
    from agent.agent_utils import MODULE_SCOPE_NOTE

    message_to_agent = (
        prompt + repo_info + unit_tests_info + spec_info + MODULE_SCOPE_NOTE
    )
    return message_to_agent, spec_costs


def _get_ts_dir_tree(
    dir_path: Path, prefix: str = "", max_depth: int = 10, current_depth: int = 0
) -> str:
    """Minimal directory tree renderer for TS repos."""
    if current_depth >= max_depth:
        return ""
    try:
        contents = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError:
        logger.debug("Cannot list directory %s for tree rendering", dir_path)
        return ""
    contents = [c for c in contents if not c.name.startswith(".")]
    contents = [c for c in contents if c.name not in ("node_modules", "dist", ".git")]

    tree_lines: list[str] = []
    for i, path in enumerate(contents):
        connector = "└── " if i == len(contents) - 1 else "├── "
        tree_lines.append(prefix + connector + path.name)
        if path.is_dir():
            extension = "    " if i == len(contents) - 1 else "│   "
            subtree = _get_ts_dir_tree(
                path,
                prefix=prefix + extension,
                max_depth=max_depth,
                current_depth=current_depth + 1,
            )
            if subtree:
                tree_lines.append(subtree)
    return "\n".join(tree_lines)


def get_changed_ts_files_from_commits(
    repo: git.Repo,
    commit1: str,
    commit2: str,
) -> list[str]:
    """Get changed .ts/.tsx files between two commits."""
    try:
        commit1_obj = repo.commit(commit1)
        commit2_obj = repo.commit(commit2)
        diff = commit1_obj.diff(commit2_obj)
        changed_files: list[str] = [
            item.a_path for item in diff if item.a_path is not None
        ]
        return [
            f for f in changed_files if any(f.endswith(ext) for ext in TS_SOURCE_EXTS)
        ]
    except Exception as e:
        logger.error(
            "Failed to get changed files between %s and %s: %s",
            commit1,
            commit2,
            e,
            exc_info=True,
        )
        return []


def get_ts_lint_cmd(
    repo_name: str,
    use_lint_info: bool,
    commit0_config_file: str,
) -> str:
    """Generate a TS linting command string."""
    if use_lint_info:
        return (
            f"{sys.executable} -m commit0.cli_ts lint "
            f"{shlex.quote(repo_name)} --commit0-config-file {shlex.quote(commit0_config_file)}"
        )
    return ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text.

    Jest/ts-jest output contains terminal color codes ([96m, [91m, etc.)
    that waste ~20-30% of token budget without adding information for the model.
    """
    return _ANSI_RE.sub("", text)


def _deduplicate_ts_errors(text: str) -> str:
    """Deduplicate repeated TypeScript compilation errors.

    When ts-jest encounters compilation failures, it can report the same TS error
    (e.g., TS2339: Property 'x' does not exist on type 'Y') many times across
    different test files or describe blocks. This collapses identical errors to
    save token budget while preserving the unique error information.

    Returns the deduplicated text with a count of removed duplicates.
    """
    lines = text.split("\n")
    # Match TS error pattern: "TS####: message" or "error TS####:"
    ts_error_re = re.compile(r"(TS\d{4}:\s*.+)")

    seen_errors: dict[str, int] = {}
    output_lines: list[str] = []
    skip_until_blank = False

    for line in lines:
        match = ts_error_re.search(line)
        if match:
            error_key = match.group(1).strip()
            if error_key in seen_errors:
                seen_errors[error_key] += 1
                skip_until_blank = True
                continue
            else:
                seen_errors[error_key] = 1
                skip_until_blank = False
        elif skip_until_blank:
            if line.strip() == "":
                skip_until_blank = False
                output_lines.append("")
            continue

        output_lines.append(line)

    total_removed = sum(c - 1 for c in seen_errors.values() if c > 1)
    if total_removed > 0:
        output_lines.append("")
        output_lines.append(
            f"[{total_removed} duplicate TS error(s) removed. "
            f"{len(seen_errors)} unique error(s) remain.]"
        )

    return "\n".join(output_lines)


def _parse_jest_vitest_output(raw: str) -> str:
    """Tier 1 deterministic parser: extract FAIL blocks, assertion errors, summary.

    Extracts the most useful debugging information from raw Jest/Vitest console
    output without using an LLM. Strips ANSI codes and deduplicates TS errors.
    """
    # Strip ANSI escape codes first — saves 20-30% token budget
    raw = _strip_ansi(raw)
    lines = raw.split("\n")

    sections: list[str] = []

    fail_block_lines: list[str] = []
    in_fail_block = False
    for line in lines:
        if line.strip().startswith("FAIL ") or line.strip().startswith("● "):
            in_fail_block = True
        elif in_fail_block and (
            line.strip().startswith("PASS ")
            or line.strip().startswith("Test Suites:")
            or (
                line.strip() == ""
                and len(fail_block_lines) > 0
                and fail_block_lines[-1].strip() == ""
            )
        ):
            in_fail_block = False
        if in_fail_block:
            fail_block_lines.append(line)

    if fail_block_lines:
        sections.append("\n".join(fail_block_lines))

    assertion_errors: list[str] = []
    for line in lines:
        if "expect(" in line or "toBe(" in line or "toEqual(" in line:
            assertion_errors.append(line)
        elif "Expected:" in line or "Received:" in line:
            assertion_errors.append(line)
        elif "AssertionError" in line:
            assertion_errors.append(line)
    if assertion_errors:
        sections.append("\n".join(assertion_errors))

    summary_lines: list[str] = []
    summary_keywords = (
        "Test Suites:",
        "Tests:",
        "Snapshots:",
        "Time:",
        "Ran all test suites",
    )
    for line in lines:
        stripped = line.strip()
        if any(stripped.startswith(kw) for kw in summary_keywords):
            summary_lines.append(line)
        elif "failed" in stripped and (
            "test" in stripped.lower() or "suite" in stripped.lower()
        ):
            summary_lines.append(line)
    if summary_lines:
        sections.append("\n".join(summary_lines))

    if sections:
        result = "\n\n".join(sections)
        return _deduplicate_ts_errors(result)
    return _deduplicate_ts_errors(raw)


def _count_tokens(text: str, model: str) -> int:
    """Count tokens using litellm's tokenizer, with len//4 fallback."""
    try:
        import litellm

        return litellm.token_counter(model=model, text=text)
    except Exception:
        logger.debug("litellm token counter unavailable, using len//4 fallback")
        return len(text) // 4


_TEST_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a test output summarizer for an AI coding agent. "
    "Your job is to compress Jest/Vitest output while preserving ALL information "
    "needed to debug test failures.\n\n"
    "PRESERVE (mandatory, never drop):\n"
    "- EVERY failed test name and its full traceback.\n"
    "- Assertion messages with expected vs actual values.\n"
    "- The test summary section (Test Suites, Tests, Time).\n\n"
    "OMIT (drop first when budget is tight):\n"
    "- Docker/container setup output.\n"
    "- Passing test details (just keep the count).\n"
    "- Console.log output from passing tests.\n\n"
    "FORMAT: Keep tracebacks as code blocks. Be maximally dense."
)


def summarize_test_output_ts(
    raw_output: str,
    max_length: int = 15000,
    model: str = "",
    max_tokens: int = 4000,
    api_base: str = "",
    api_key: str = "",
) -> tuple[str, list[SummarizerCost]]:
    """Hybrid 3-tier test output summarization for Jest/Vitest output.

    Tier 1: Deterministic parsing (_parse_jest_vitest_output).
    Tier 2: LLM summarization if Tier 1 exceeds budget.
    Tier 3: Smart truncation fallback.
    """
    all_costs: list[SummarizerCost] = []

    raw_output = _strip_ansi(raw_output)

    max_token_length = (
        _count_tokens(raw_output[:max_length], model) if model else max_length // 4
    )
    if max_token_length < 1:
        max_token_length = max_length // 4

    raw_tokens = _count_tokens(raw_output, model) if model else len(raw_output) // 4
    if raw_tokens <= max_token_length:
        return raw_output, all_costs

    parsed = _parse_jest_vitest_output(raw_output)
    parsed_tokens = _count_tokens(parsed, model) if model else len(parsed) // 4
    if parsed_tokens <= max_token_length:
        logger.info(
            "Test output summarized (Tier 1 parse): %d -> %d tokens",
            raw_tokens,
            parsed_tokens,
        )
        return parsed, all_costs

    if model:
        try:
            import litellm

            _proxy_kw: dict[str, str] = {}
            if api_base:
                _proxy_kw["api_base"] = api_base
            if api_key:
                _proxy_kw["api_key"] = api_key

            response = litellm.completion(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            _TEST_SUMMARIZER_SYSTEM_PROMPT
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
                **_proxy_kw,
            )

            cost = SummarizerCost()
            usage = getattr(response, "usage", None)
            if usage:
                cost.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                cost.completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            try:
                cost.cost = litellm.completion_cost(completion_response=response)
            except Exception:
                logger.debug("Could not compute LLM completion cost", exc_info=True)
            all_costs.append(cost)

            choices = getattr(response, "choices", None)
            content: Optional[str] = None
            if choices and len(choices) > 0:
                content = getattr(choices[0].message, "content", None)
            if content:
                result = content.strip()
                logger.info(
                    "Test output summarized (Tier 2 LLM): %d -> %d chars (model=%s)",
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
            "Test output summarized (Tier 3 truncation): %d -> %d chars",
            len(raw_output),
            len(truncated),
        )
        return truncated, all_costs
    return parsed[:max_length], all_costs
