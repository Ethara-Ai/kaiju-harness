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
from commit0.harness.constants_cpp import (
    CPP_STUB_MARKER,
    CPP_STUB_MARKER_CONSTEXPR,
    CPP_STUB_MARKER_NOEXCEPT,
    CPP_TEST_IDS_DIR,
)

logger = logging.getLogger(__name__)

CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".h"}

_EXCLUDED_DIRS = {
    "build", "cmake-build-debug", "cmake-build-release", "cmake-build-minsizerel",
    "cmake-build-relwithdebinfo", "builddir", ".cache", "_deps", "third_party",
    "vendor", "extern", ".git", "test", "tests", "benchmarks", "examples", "doc",
    "docs", "node_modules",
}

_STUB_MARKERS = (CPP_STUB_MARKER, CPP_STUB_MARKER_CONSTEXPR, CPP_STUB_MARKER_NOEXCEPT)

_FN_PATTERN = re.compile(
    r"("
    r"(?:(?:static|inline|virtual|explicit|constexpr|consteval|friend|extern)\s+)*"
    r"(?:[\w:]+(?:<[^>]*>)?(?:\s*[*&]+)?)\s+"
    r"(?:[\w:]+::)?"
    r"(\w+)"
    r"\s*\([^)]*\)"
    r"(?:\s*(?:const|noexcept|override|final|volatile))*"
    r"(?:\s*->\s*[\w:]+(?:<[^>]*>)?(?:\s*[*&]+)?)?"
    r"\s*)"
    r"\{",
    re.DOTALL,
)


def find_cpp_files_to_edit(src_dir: str) -> list[str]:
    cpp_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CPP_EXTENSIONS:
                continue
            if fname == "CMakeLists.txt":
                continue
            cpp_files.append(os.path.normpath(os.path.join(dirpath, fname)))
    cpp_files.sort()
    return cpp_files


def get_target_edit_files_cpp(
    src_dir: str,
    local_repo: "git.Repo | None" = None,
    base_commit: str | None = None,
) -> list[str]:
    """Return .cpp/.hpp files containing stub markers.

    When ``local_repo`` and ``base_commit`` are provided, membership is derived
    from the BLOBS at ``base_commit`` rather than the current working tree.
    Stage-2/3 fix: after stage 1 fills the stubs, a working-tree scan returns
    zero files and silently kills the refine stages. Mirrors JS/C/Go behavior.
    """
    all_files = find_cpp_files_to_edit(src_dir)

    if local_repo is not None and base_commit:
        target_dir = str(local_repo.working_dir)
        target_files: list[str] = []
        for file_path in all_files:
            rel_path = os.path.relpath(file_path, target_dir)
            try:
                content = local_repo.git.show(f"{base_commit}:{rel_path}")
            except Exception:  # noqa: BLE001
                # File not in base_commit tree (added on branch) or unreadable.
                continue
            if any(marker in content for marker in _STUB_MARKERS):
                target_files.append(file_path)
        # If base_commit yielded stubs, that is the canonical target set —
        # deterministic across all 3 stages regardless of working-tree state.
        if target_files:
            return target_files
        # base scan returned 0 (base blob path drift): fall through to WT scan.

    target_files = []
    for file_path in all_files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            if any(marker in content for marker in _STUB_MARKERS):
                target_files.append(file_path)
        except OSError as exc:
            logger.warning("Could not read %s: %s", file_path, exc)
    return target_files


def extract_cpp_function_stubs(file_path: str) -> list[dict]:
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

        if any(marker in body for marker in _STUB_MARKERS):
            stubs.append({
                "name": fn_name,
                "line": line_number,
                "signature": signature,
            })

    return stubs


def get_cpp_file_dependencies(file_path: str) -> list[str]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except OSError as exc:
        logger.warning("Could not read %s: %s", file_path, exc)
        return []

    deps: set[str] = set()
    for m in re.finditer(r'#include\s+"([^"]+)"', content):
        deps.add(m.group(1))
    return sorted(deps)


_PROMPT_HEADER = ">>> Here is the Task:\n"
_REPO_INFO_HEADER = "\n\n>>> Here is the Repository Information:\n"
_UNIT_TESTS_INFO_HEADER = "\n\n>>> Here are the Unit Tests Information:\n"
_SPEC_INFO_HEADER = "\n\n>>> Here is the Specification Information:\n"

_CPP_TEST_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a test output summarizer for an AI coding agent. "
    "Your job is to compress C++ test output while preserving ALL information "
    "needed to debug test failures.\n\n"
    "PRESERVE (mandatory, never drop):\n"
    "- EVERY failed test name and its full output.\n"
    "- Assertion messages with expected vs actual values.\n"
    "- Compilation errors with full context.\n"
    "- The test result summary line.\n"
    "- Which tests failed.\n\n"
    "OMIT (drop first when budget is tight):\n"
    "- Docker/container setup output.\n"
    "- Passing test details (just keep the count).\n"
    "- Duplicate information.\n"
    "- Warnings unless they indicate why tests fail.\n"
    "- Captured stdout from passing tests.\n\n"
    "FORMAT: Keep tracebacks as code blocks. Be maximally dense."
)


def get_cpp_test_ids(repo_path: str) -> list[str]:
    test_ids: list[str] = []

    build_dir = os.path.join(repo_path, "build")
    if os.path.isdir(build_dir):
        try:
            result = subprocess.run(
                ["ctest", "--test-dir", build_dir, "--show-only=json-v1"],
                capture_output=True,
                text=True,
                cwd=repo_path,
                timeout=60,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                tests = data.get("tests", [])
                test_ids = [t["name"] for t in tests if "name" in t]
                if test_ids:
                    return sorted(test_ids)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            logger.warning("ctest --show-only failed in %s: %s", repo_path, exc)

    repo_name = os.path.basename(os.path.normpath(repo_path))
    cache_path = CPP_TEST_IDS_DIR / f"{repo_name}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                test_ids = [str(t) for t in cached]
                logger.info("Loaded %d cached test IDs for %s", len(test_ids), repo_name)
                return sorted(test_ids)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load cached test IDs from %s: %s", cache_path, exc)

    bz2_cache = CPP_TEST_IDS_DIR / f"{repo_name}.bz2"
    if bz2_cache.exists():
        try:
            raw = bz2.decompress(bz2_cache.read_bytes()).decode()
            test_ids = [line.strip() for line in raw.splitlines() if line.strip()]
            if test_ids:
                logger.info("Loaded %d cached test IDs (bz2) for %s", len(test_ids), repo_name)
                return sorted(test_ids)
        except Exception as exc:
            logger.warning("Failed to load bz2 test IDs from %s: %s", bz2_cache, exc)

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


def get_message_cpp(
    agent_config: AgentConfig,
    repo_path: str,
    test_files: Optional[list[str]] = None,
    target_files: Optional[list[str]] = None,
) -> tuple[str, list[SummarizerCost]]:
    spec_costs: list[SummarizerCost] = []

    template_path = Path(__file__).parent / "prompts" / "cpp_system_prompt.md"
    try:
        template = template_path.read_text(errors="replace")
    except OSError as exc:
        logger.warning("Could not read cpp_system_prompt.md: %s", exc)
        template = agent_config.user_prompt

    repo_name = os.path.basename(os.path.normpath(repo_path))
    if target_files is None:
        target_files = get_target_edit_files_cpp(repo_path)

    function_lines: list[str] = []
    all_dep_content: list[str] = []
    seen_deps: set[str] = set()
    dep_chars = 0
    dep_cap_reached = False

    for fpath in target_files:
        stubs = extract_cpp_function_stubs(fpath)
        rel = os.path.relpath(fpath, repo_path)
        for stub in stubs:
            function_lines.append(
                f"- {stub['name']} ({rel}:{stub['line']}): {stub['signature']}"
            )

        if dep_cap_reached:
            continue
        deps = get_cpp_file_dependencies(fpath)
        base_dir = os.path.dirname(fpath)
        for dep in deps:
            if dep in seen_deps:
                continue
            seen_deps.add(dep)
            dep_file = os.path.join(base_dir, dep)
            if not os.path.isfile(dep_file):
                dep_file = os.path.join(repo_path, dep)
            if os.path.isfile(dep_file):
                try:
                    with open(dep_file, "r", encoding="utf-8", errors="ignore") as fh:
                        dep_lines = fh.readlines()[:200]
                    dep_rel = os.path.relpath(dep_file, repo_path)
                    block = f"// --- {dep_rel} ---\n" + "".join(dep_lines)
                    remaining = _MAX_DEP_CONTEXT_CHARS - dep_chars
                    if remaining <= 0:
                        dep_cap_reached = True
                        break
                    if len(block) > remaining:
                        block = block[:remaining] + "\n// ... dep_context cap reached ...\n"
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
        for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
            readme_path = Path(repo_path) / readme_name
            if readme_path.exists():
                try:
                    readme_text = readme_path.read_text(errors="replace")
                    readme_text = readme_text[: agent_config.max_spec_info_length]
                    spec_info = f"\n{_SPEC_INFO_HEADER} " + readme_text
                    break
                except Exception as e:
                    logger.warning("Failed to read %s: %s", readme_path, e)

    # Lazy import to avoid a top-level circular import with agent.agent_utils.
    # Appended AFTER template fill so the brace-free note can never be
    # reinterpreted as a template placeholder.
    from agent.agent_utils import MODULE_SCOPE_NOTE

    message_to_agent = (
        prompt + repo_info + unit_tests_info + spec_info + MODULE_SCOPE_NOTE
    )
    return message_to_agent, spec_costs


def get_lint_cmd_cpp(
    repo_name: str,
    use_lint_info: bool,
    repo_path: str,
) -> str:
    if not use_lint_info:
        return ""

    compile_db = ""
    for candidate in ["build", "builddir", "cmake-build-release", "cmake-build-debug"]:
        cc_path = os.path.join(repo_path, candidate, "compile_commands.json")
        if os.path.isfile(cc_path):
            compile_db = os.path.join(repo_path, candidate)
            break
    if not compile_db:
        cc_root = os.path.join(repo_path, "compile_commands.json")
        if os.path.isfile(cc_root):
            compile_db = repo_path

    if compile_db:
        return f'clang-tidy -p "{compile_db}"'
    return "clang-format --dry-run --Werror"


def get_changed_files_cpp(
    repo: git.Repo,
    commit1: str,
    commit2: str,
) -> list[str]:
    try:
        commit1_obj = repo.commit(commit1)
        commit2_obj = repo.commit(commit2)
        diff = commit1_obj.diff(commit2_obj)
        changed_files = [item.a_path for item in diff if item.a_path is not None]
        cpp_files = [
            f for f in changed_files
            if os.path.splitext(f)[1].lower() in CPP_EXTENSIONS
        ]
        return cpp_files
    except Exception as e:
        logger.error(
            "Failed to get changed files between %s and %s: %s",
            commit1, commit2, e, exc_info=True,
        )
        return []


def _count_tokens_cpp(text: str, model: str) -> int:
    try:
        import litellm
        return litellm.token_counter(model=model, text=text)
    except Exception:
        return len(text) // 4


def _parse_cpp_test_output(raw: str) -> str:
    lines = raw.split("\n")

    test_start = -1
    for i, line in enumerate(lines):
        if re.match(r"\[\s*=+\s*\]\s*Running", line) or re.match(r"~~~+", line):
            test_start = i
            break
        if "test cases" in line.lower() or "assertions" in line.lower():
            test_start = max(0, i - 5)
            break

    if test_start > 0:
        lines = lines[test_start:]

    text = "\n".join(lines)
    sections: list[str] = []

    failed_lines = [l for l in lines if re.match(r"\[\s*FAILED\s*\]", l.strip())]
    if failed_lines:
        sections.append("\n".join(failed_lines))

    for pattern in [
        r"(\[\s*=+\s*\]\s*\d+\s+tests?\s+from.*)",
        r"(test cases:.*)",
        r"(assertions:.*)",
        r"(\d+\s+tests?\s+ran.*)",
        r"(All tests passed.*)",
    ]:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            sections.append(m.group(1).strip())

    assertion_lines = [l for l in lines if "expected:" in l.lower() or "actual:" in l.lower()
                       or "REQUIRE(" in l or "CHECK(" in l or "EXPECT_" in l or "ASSERT_" in l]
    if assertion_lines:
        sections.append("\n".join(assertion_lines[:50]))

    error_lines = [l for l in lines if l.strip().startswith("error:") or ": error:" in l]
    if error_lines:
        sections.append("\n".join(error_lines[:30]))

    if sections:
        return "\n\n".join(sections)
    return text


def summarize_cpp_test_output(
    raw_output: str,
    max_length: int = 15000,
    model: str = "",
    max_tokens: int = 4000,
) -> tuple[str, list[SummarizerCost]]:
    all_costs: list[SummarizerCost] = []

    max_token_length = (
        _count_tokens_cpp(raw_output[:max_length], model) if model else max_length // 4
    )
    if max_token_length < 1:
        max_token_length = max_length // 4

    raw_tokens = (
        _count_tokens_cpp(raw_output, model) if model else len(raw_output) // 4
    )
    if raw_tokens <= max_token_length:
        return raw_output, all_costs

    parsed = _parse_cpp_test_output(raw_output)
    parsed_tokens = _count_tokens_cpp(parsed, model) if model else len(parsed) // 4
    if parsed_tokens <= max_token_length:
        logger.info(
            "C++ test output summarized (Tier 1 parse): %d -> %d tokens",
            raw_tokens, parsed_tokens,
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
                        _CPP_TEST_SUMMARIZER_SYSTEM_PROMPT
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

        content = response.choices[0].message.content
        if content:
            result = content.strip()
            logger.info(
                "C++ test output summarized (Tier 2 LLM): %d -> %d chars (model=%s)",
                len(raw_output), len(result), model,
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
            "C++ test output summarized (Tier 3 truncation): %d -> %d chars",
            len(raw_output), len(truncated),
        )
        return truncated, all_costs
    return parsed[:max_length], all_costs
