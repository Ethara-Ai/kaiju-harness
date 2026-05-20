"""C-specific agent utilities for commit0.

Mirrors agent_utils_go.py but operates on C source files, the
``STUB_PANIC`` marker, and C tooling (clang-tidy, cppcheck).
"""

from __future__ import annotations

import bz2
import logging
import os
import re
import subprocess
from pathlib import Path

import git
import yaml

from agent.agent_utils import get_specification, summarize_specification
from agent.class_types import AgentConfig
from agent.thinking_capture import SummarizerCost
from commit0.harness.constants_c import (
    C_HEADER_EXT,
    C_SKIP_DIRS,
    C_SOURCE_EXT,
    C_STUB_MARKER,
    C_TEST_FILE_GLOBS,
)

logger = logging.getLogger(__name__)


EXCLUDED_DIRS = {
    ".git",
    ".github",
    ".vscode",
    "build",
    "_build",
    "cmake-build-debug",
    "cmake-build-release",
    "node_modules",
} | set(C_SKIP_DIRS)


PROMPT_HEADER = "## Task\n"
REFERENCE_HEADER = "## Reference Information\n"
REPO_INFO_HEADER = "### Repository Structure\n"
TEST_INFO_HEADER = "### Test Information\n"
SPEC_INFO_HEADER = "### Specification\n"
LINT_INFO_HEADER = "### Lint Results\n"

C_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "c_system_prompt.md"


# ---------------------------------------------------------------------------
# Source enumeration
# ---------------------------------------------------------------------------


def _is_test_filename(name: str) -> bool:
    for pattern in C_TEST_FILE_GLOBS:
        if Path(name).match(pattern):
            return True
    return False


def collect_c_files(directory: str) -> list[str]:
    """Collect all .c source files excluding test files and skip-dirs."""
    files: list[str] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in names:
            if not name.endswith(C_SOURCE_EXT):
                continue
            if _is_test_filename(name):
                continue
            files.append(os.path.join(root, name))
    return sorted(files)


def collect_c_test_files(directory: str) -> list[str]:
    """Collect all C test files matching the test-file globs."""
    test_files: list[str] = []
    for root, dirs, names in os.walk(directory):
        # Don't prune into skip-dirs — tests/ IS where the test files live.
        dirs[:] = [d for d in dirs if d not in {".git", "build", "_build"}]
        for name in names:
            if not name.endswith(C_SOURCE_EXT):
                continue
            if _is_test_filename(name):
                test_files.append(os.path.join(root, name))
    return sorted(test_files)


_FUNC_DECL_RE = re.compile(
    r"^\s*(?:static\s+|inline\s+|extern\s+|__attribute__\(\([^)]*\)\)\s*)*"
    r"[\w\s\*]+?\b(\w+)\s*\([^;]*\)\s*\{?\s*$"
)


def extract_c_function_stubs(file_path: str) -> list[dict]:
    """Extract stubbed C functions by scanning for the ``STUB_PANIC`` marker.

    Returns a list of dicts with keys: ``name``, ``file``, ``line``, ``signature``.
    Heuristic — for accurate detection use libclang via cstubber.
    """
    stubs: list[dict] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return stubs

    current_func = None
    current_func_line = 0
    current_func_sig = ""

    for i, line in enumerate(lines, 1):
        m = _FUNC_DECL_RE.match(line)
        if m:
            current_func = m.group(1)
            current_func_line = i
            current_func_sig = line.rstrip()
        if current_func and C_STUB_MARKER in line:
            stubs.append(
                {
                    "name": current_func,
                    "file": file_path,
                    "line": current_func_line,
                    "signature": current_func_sig,
                }
            )
            current_func = None

    return stubs


def get_dir_info(
    base_dir: str,
    src_dir: str = ".",
    max_length: int = 10000,
    show_stubs: bool = False,
) -> str:
    """Build a tree-style directory listing of C source + headers."""
    target = os.path.join(base_dir, src_dir) if src_dir != "." else base_dir
    lines: list[str] = []
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        rel = os.path.relpath(root, base_dir)
        depth = rel.count(os.sep)
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(root)}/")
        for f in sorted(files):
            if not (f.endswith(C_SOURCE_EXT) or f.endswith(C_HEADER_EXT)):
                continue
            if _is_test_filename(f):
                continue
            fpath = os.path.join(root, f)
            sub_indent = "  " * (depth + 1)
            lines.append(f"{sub_indent}{f}")
            if show_stubs and f.endswith(C_SOURCE_EXT):
                for stub in extract_c_function_stubs(fpath):
                    lines.append(f"{sub_indent}  [STUB] {stub['signature']}")

    result = "\n".join(lines)
    if len(result) > max_length:
        result = result[:max_length] + "\n... (truncated)"
    return result


# ---------------------------------------------------------------------------
# Target-edit selection
# ---------------------------------------------------------------------------


def _find_c_files_to_edit(base_dir: str, src_dir: str = ".") -> list[str]:
    target = os.path.join(base_dir, src_dir) if src_dir != "." else base_dir
    return collect_c_files(target)


def get_target_edit_files(
    local_repo: str,
    src_dir: str,
    branch: str,
    reference_commit: str,
) -> list[str]:
    """Files containing STUB_PANIC that differ from the reference commit."""
    repo = git.Repo(local_repo)
    all_c_files = _find_c_files_to_edit(local_repo, src_dir)

    stubbed_files: list[str] = []
    for fpath in all_c_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if C_STUB_MARKER in content:
                stubbed_files.append(fpath)
        except OSError:
            continue

    if not stubbed_files:
        return all_c_files

    try:
        ref_tree = repo.commit(reference_commit).tree
    except Exception:
        return stubbed_files

    target_files: list[str] = []
    for fpath in stubbed_files:
        rel_path = os.path.relpath(fpath, local_repo)
        try:
            ref_blob = ref_tree / rel_path
            ref_content = ref_blob.data_stream.read().decode(
                "utf-8", errors="replace"
            )
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                current_content = f.read()
            if current_content != ref_content:
                target_files.append(fpath)
        except (KeyError, Exception):
            target_files.append(fpath)

    return target_files if target_files else stubbed_files


def get_target_edit_files_from_patch(
    local_repo: str,
    patch: str,
) -> list[str]:
    """Extract C files to edit from a git diff patch."""
    files: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            fpath = line[6:]
            if fpath.endswith(C_SOURCE_EXT) and not _is_test_filename(
                Path(fpath).name
            ):
                full_path = os.path.join(local_repo, fpath)
                if os.path.exists(full_path):
                    files.append(full_path)
    return files


# ---------------------------------------------------------------------------
# Lint dispatch
# ---------------------------------------------------------------------------


_CLI_C_PATH = str(Path(__file__).resolve().parent.parent / "commit0" / "cli_c.py")


def get_c_lint_cmd(repo: str, commit0_config_file: str) -> str:
    return (
        f"python {_CLI_C_PATH} lint {repo} --commit0-config-file {commit0_config_file}"
    )


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def get_c_message(
    agent_config: AgentConfig,
    repo_path: str,
    test_files: list[str],
    commit0_config_file: str = ".commit0.c.yaml",
) -> tuple[str, list[SummarizerCost]]:
    """Build the agent prompt for a C repo. Returns ``(message, spec_costs)``."""
    parts: list[str] = []

    if C_SYSTEM_PROMPT_PATH.exists():
        parts.append(C_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip())
        parts.append("")

    parts.append(PROMPT_HEADER)
    if agent_config.use_user_prompt and agent_config.user_prompt:
        parts.append(agent_config.user_prompt)
    else:
        parts.append(
            "You need to complete the implementations for all stubbed functions "
            "(those whose body calls `STUB_PANIC(\"<name>\")`) and pass the unit "
            "tests.\n"
            "Do not change function signatures or modify any test file "
            "(files under tests/, test/, or matching test_*.c / *_test.c / "
            "check_*.c). Compile-clean code is a hard precondition: if you "
            "cannot finish a function, leave a minimal placeholder that builds."
        )
    parts.append("")

    if agent_config.use_repo_info:
        parts.append(REFERENCE_HEADER)
        parts.append(REPO_INFO_HEADER)
        repo_info = get_dir_info(
            repo_path,
            max_length=agent_config.max_repo_info_length,
            show_stubs=True,
        )
        parts.append(repo_info)
        parts.append("")

    if agent_config.use_unit_tests_info and test_files:
        parts.append(TEST_INFO_HEADER)
        test_info_parts: list[str] = []
        total_len = 0
        for tf in test_files:
            rel = os.path.relpath(tf, repo_path)
            try:
                with open(tf, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                entry = f"### {rel}\n```c\n{content}\n```\n"
                if total_len + len(entry) > agent_config.max_unit_tests_info_length:
                    test_info_parts.append("... (truncated)")
                    break
                test_info_parts.append(entry)
                total_len += len(entry)
            except OSError:
                continue
        parts.append("\n".join(test_info_parts))
        parts.append("")

    if agent_config.use_lint_info and commit0_config_file:
        parts.append(LINT_INFO_HEADER)
        repo_name = os.path.basename(repo_path)
        lint_cmd = get_c_lint_cmd(repo_name, commit0_config_file)
        try:
            result = subprocess.run(
                lint_cmd.split(),
                capture_output=True,
                text=True,
                timeout=120,
                cwd=repo_path,
            )
            lint_output = (result.stdout + result.stderr).strip()
            if len(lint_output) > agent_config.max_lint_info_length:
                lint_output = (
                    lint_output[: agent_config.max_lint_info_length]
                    + "\n... (truncated)"
                )
            parts.append(f"```\n{lint_output}\n```")
        except (subprocess.TimeoutExpired, OSError):
            parts.append("(lint results unavailable)")
        parts.append("")

    spec_costs: list[SummarizerCost] = []
    if agent_config.use_spec_info:
        spec_pdf_path = Path(repo_path) / "spec.pdf"
        spec_bz2_path = Path(repo_path) / "spec.pdf.bz2"
        decompress_failed = False

        if spec_bz2_path.exists() and not spec_pdf_path.exists():
            try:
                _MAX_SPEC = 100 * 1024 * 1024
                with bz2.open(str(spec_bz2_path), "rb") as in_file, open(
                    str(spec_pdf_path), "wb"
                ) as out_file:
                    written = 0
                    while True:
                        chunk = in_file.read(1 << 16)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > _MAX_SPEC:
                            raise ValueError(
                                f"Decompressed spec exceeds "
                                f"{_MAX_SPEC // (1024 * 1024)}MB limit"
                            )
                        out_file.write(chunk)
            except Exception as e:
                logger.warning(
                    "Failed to decompress spec file %s: %s", spec_bz2_path, e
                )
                if spec_pdf_path.exists():
                    try:
                        spec_pdf_path.unlink()
                    except OSError:
                        pass
                decompress_failed = True

        if not decompress_failed and spec_pdf_path.exists():
            try:
                raw_spec = get_specification(specification_pdf_path=spec_pdf_path)
            except Exception as e:
                logger.warning("Failed to read spec PDF %s: %s", spec_pdf_path, e)
                raw_spec = ""
            if raw_spec:
                if len(raw_spec) > int(agent_config.max_spec_info_length * 1.5):
                    try:
                        processed_spec, spec_costs = summarize_specification(
                            spec_text=raw_spec,
                            model=agent_config.model_name,
                            max_tokens=agent_config.spec_summary_max_tokens,
                            max_char_length=agent_config.max_spec_info_length,
                            cache_path=spec_pdf_path.parent
                            / ".spec_summary_cache.json",
                        )
                    except Exception as e:
                        logger.warning(
                            "Spec summarization failed for %s: %s", spec_pdf_path, e
                        )
                        processed_spec = raw_spec[: agent_config.max_spec_info_length]
                        spec_costs = []
                else:
                    processed_spec = raw_spec
                parts.append(SPEC_INFO_HEADER)
                parts.append(processed_spec)
                parts.append("")
        else:
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = Path(repo_path) / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[
                            : agent_config.max_spec_info_length
                        ]
                        parts.append(SPEC_INFO_HEADER)
                        parts.append(readme_text)
                        parts.append("")
                        logger.info(
                            "Using %s as spec fallback for %s",
                            readme_name,
                            repo_path,
                        )
                        break
                    except Exception as e:
                        logger.warning("Failed to read %s: %s", readme_path, e)

    return "\n".join(parts), spec_costs


# ---------------------------------------------------------------------------
# Branch / config helpers
# ---------------------------------------------------------------------------


def create_branch(repo: git.Repo, branch: str, override: bool = False) -> None:
    if branch in repo.heads:
        if override:
            repo.git.checkout(branch)
            repo.git.reset("--hard", "HEAD~0")
        else:
            repo.git.checkout(branch)
    else:
        repo.git.checkout("-b", branch)


def get_changed_files(repo: git.Repo, branch: str) -> list[str]:
    try:
        merge_base = repo.git.merge_base(branch, "HEAD")
        diff_output = repo.git.diff("--name-only", merge_base, branch)
        return [
            f
            for f in diff_output.splitlines()
            if f.endswith(C_SOURCE_EXT) and not _is_test_filename(Path(f).name)
        ]
    except git.GitCommandError:
        return []


def write_agent_config(config_file: str, config: dict) -> None:
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False)
    logger.info(f"Agent config written to {config_file}")


def read_yaml_config(config_file: str) -> dict:
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_agent_config(config_file: str) -> AgentConfig:
    raw = read_yaml_config(config_file)
    return AgentConfig(**raw)
