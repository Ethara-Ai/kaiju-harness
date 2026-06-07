"""C++ linting via clang-tidy and clang-format."""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__: list[str] = []


def _find_compile_commands(repo_dir: str) -> Optional[str]:
    """Walk upward from repo_dir to find the nearest compile_commands.json or build/."""
    current = Path(repo_dir).resolve()
    while current != current.parent:
        if (current / "compile_commands.json").is_file():
            return str(current)
        if (current / "build" / "compile_commands.json").is_file():
            return str(current)
        if (current / "CMakeLists.txt").is_file():
            return str(current)
        if (current / "meson.build").is_file():
            return str(current)
        current = current.parent
    return None


def _run_clang_tidy(project_dir: str, cpp_files: List[str]) -> Dict[str, Any]:
    """Run clang-tidy and parse diagnostic output.

    Returns dict with keys: warnings, errors, messages (list of parsed diagnostics).
    """
    tidy_bin = shutil.which("clang-tidy")
    if not tidy_bin:
        logger.error("clang-tidy not found in PATH")
        return {"warnings": 0, "errors": 0, "messages": [], "raw_stderr": "clang-tidy not found"}

    if not cpp_files:
        return {"warnings": 0, "errors": 0, "messages": [], "raw_stderr": ""}

    build_dir = os.path.join(project_dir, "build")
    cmd = [tidy_bin]
    if os.path.isdir(build_dir) and os.path.isfile(os.path.join(build_dir, "compile_commands.json")):
        cmd.extend(["-p", build_dir])
    cmd.extend(cpp_files)

    logger.info("Running: %s (in %s)", " ".join(cmd[:5]) + "...", project_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.error("clang-tidy timed out after 300s")
        return {"warnings": 0, "errors": 0, "messages": [], "raw_stderr": "timeout"}
    except FileNotFoundError as exc:
        logger.error("clang-tidy binary not found: %s", exc)
        return {"warnings": 0, "errors": 0, "messages": [], "raw_stderr": str(exc)}

    warnings = 0
    errors = 0
    messages: List[Dict[str, Any]] = []

    diag_pattern = re.compile(
        r"^(.+?):(\d+):(\d+):\s+(warning|error):\s+(.+?)\s+\[(.+?)\]$"
    )

    for line in result.stdout.splitlines():
        match = diag_pattern.match(line)
        if not match:
            continue

        file_path, line_num, col, severity, message, check_name = match.groups()

        if severity == "warning":
            warnings += 1
        elif severity == "error":
            errors += 1

        diagnostic: Dict[str, Any] = {
            "level": severity,
            "message": message,
            "check_name": check_name,
            "spans": [
                {
                    "file": file_path,
                    "line_start": int(line_num),
                    "line_end": int(line_num),
                    "col_start": int(col),
                    "col_end": int(col),
                    "label": check_name,
                }
            ],
        }
        messages.append(diagnostic)

    return {
        "warnings": warnings,
        "errors": errors,
        "messages": messages,
        "returncode": result.returncode,
        "raw_stderr": result.stderr,
    }


def _run_clang_format(project_dir: str, cpp_files: List[str]) -> Dict[str, Any]:
    """Run clang-format --dry-run --Werror and return formatting status.

    Returns dict with keys: formatted (bool), diff (str), returncode (int).
    """
    format_bin = shutil.which("clang-format")
    if not format_bin:
        logger.error("clang-format not found in PATH")
        return {"formatted": False, "diff": "", "returncode": -1}

    if not cpp_files:
        return {"formatted": True, "diff": "", "returncode": 0}

    cmd = [format_bin, "--dry-run", "--Werror"] + cpp_files
    logger.info("Running: %s (in %s)", " ".join(cmd[:5]) + "...", project_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.error("clang-format timed out after 300s")
        return {"formatted": False, "diff": "", "returncode": -1}
    except FileNotFoundError as exc:
        logger.error("clang-format binary not found: %s", exc)
        return {"formatted": False, "diff": "", "returncode": -1}

    formatted = result.returncode == 0
    return {
        "formatted": formatted,
        "diff": result.stderr,
        "returncode": result.returncode,
    }


def _collect_cpp_files(repo_dir: str) -> List[str]:
    """Walk the directory and collect all C++ source and header files."""
    extensions = (".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx", ".c", ".h")
    cpp_files: List[str] = []
    for root, _dirs, files in os.walk(repo_dir):
        if "/build/" in root or "/cmake-build-" in root or "/.cache/" in root:
            continue
        for f in files:
            if f.endswith(extensions):
                cpp_files.append(os.path.join(root, f))
    return sorted(cpp_files)


def get_lint_cmd_cpp(repo_dir: str) -> str:
    """Return the clang-tidy command string for a C++ repository."""
    build_dir = os.path.join(repo_dir, "build")
    if os.path.isdir(build_dir):
        return "clang-tidy -p build *.cpp *.hpp --format-errors-as-warnings"
    return "clang-tidy *.cpp *.hpp --format-errors-as-warnings"


def main(
    repo_or_dir: str,
    files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run C++ linting on a repository or directory.

    Executes two lint stages:
      1. clang-tidy — static analysis with diagnostic parsing
      2. clang-format --dry-run --Werror — formatting verification

    Args:
    ----
        repo_or_dir: Path to a C++ repository or directory containing CMakeLists.txt.
        files: Optional list of specific C++ files to report on.
            If None, all C++ files under repo_or_dir are discovered.

    Returns:
    -------
        A dict with keys:
            tidy: {warnings, errors, messages, returncode, raw_stderr}
            fmt: {formatted, diff, returncode}
            files_checked: list of C++ file paths
            passed: bool — True if zero tidy issues and formatting is clean

    """
    repo_dir = os.path.abspath(repo_or_dir)
    if not os.path.isdir(repo_dir):
        logger.error("Directory does not exist: %s", repo_dir)
        raise FileNotFoundError(f"Directory does not exist: {repo_dir}")

    project_dir = _find_compile_commands(repo_dir)
    if project_dir is None:
        project_dir = repo_dir
        logger.warning(
            "No compile_commands.json or CMakeLists.txt found at or above %s, "
            "using repo_dir directly", repo_dir
        )

    logger.info("C++ lint: project root at %s", project_dir)

    if files is not None:
        cpp_files = [os.path.abspath(f) for f in files]
    else:
        cpp_files = _collect_cpp_files(repo_dir)

    logger.info("Found %d C++ files to check", len(cpp_files))

    logger.info("=== Stage 1: clang-tidy ===")
    tidy_result = _run_clang_tidy(project_dir, cpp_files)
    logger.info(
        "clang-tidy: %d warning(s), %d error(s)",
        tidy_result["warnings"],
        tidy_result["errors"],
    )

    logger.info("=== Stage 2: clang-format --dry-run --Werror ===")
    fmt_result = _run_clang_format(project_dir, cpp_files)
    if fmt_result["formatted"]:
        logger.info("Formatting: OK")
    else:
        logger.warning("Formatting: needs changes")

    passed = (
        tidy_result["warnings"] == 0
        and tidy_result["errors"] == 0
        and fmt_result["formatted"]
    )

    result = {
        "tidy": tidy_result,
        "fmt": fmt_result,
        "files_checked": cpp_files,
        "passed": passed,
    }

    print(f"clang-tidy: {tidy_result['warnings']} warning(s), {tidy_result['errors']} error(s)")
    if tidy_result["messages"]:
        for msg in tidy_result["messages"]:
            loc = ""
            if msg["spans"]:
                s = msg["spans"][0]
                loc = f" [{s['file']}:{s['line_start']}]"
            print(f"  {msg['level'].upper()}: {msg['message']}{loc}")

    print(f"Format: {'OK' if fmt_result['formatted'] else 'NEEDS FORMATTING'}")
    if not fmt_result["formatted"] and fmt_result["diff"]:
        for line in fmt_result["diff"].splitlines()[:20]:
            print(f"  {line}")

    print(f"\nOverall: {'PASSED' if passed else 'FAILED'}")

    return result


lint_cpp_repo = main
