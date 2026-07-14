"""Rust linting via cargo clippy and cargo fmt."""

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__: list[str] = []


def _find_cargo_toml(repo_dir: str) -> Optional[str]:
    """Walk upward from repo_dir to find the nearest Cargo.toml."""
    current = Path(repo_dir).resolve()
    while current != current.parent:
        candidate = current / "Cargo.toml"
        if candidate.is_file():
            return str(current)
        current = current.parent
    return None


def _bust_clippy_cache(cargo_dir: str, cargo_bin: str) -> bool:
    """Force clippy to re-lint local crates by cleaning just the workspace
    packages (deps stay cached). Returns True on success, False if any step
    failed — callers MUST refuse to trust clippy output when this returns False,
    because a warm cache would let clippy silently return 0/0 diagnostics on
    unrepresented local changes (false pass).

    clippy shares the ``cargo check`` cache: on a warm cache nothing recompiles
    and clippy emits ZERO diagnostics, so real warnings silently vanish and the
    lint stage falsely passes. Cleaning only the local packages (via
    ``cargo metadata --no-deps``) keeps dependency build artefacts so this is
    cheap, while guaranteeing the local code is actually re-linted.
    """
    try:
        meta = subprocess.run(
            [cargo_bin, "metadata", "--no-deps", "--format-version", "1"],
            capture_output=True, text=True, cwd=cargo_dir, timeout=60,
        )
        if meta.returncode != 0:
            logger.error(
                "clippy cache-bust: cargo metadata failed (rc=%d); refusing to run clippy on stale cache.\n"
                "--- stderr ---\n%s", meta.returncode, meta.stderr,
            )
            return False
        names = [p.get("name") for p in json.loads(meta.stdout).get("packages", [])]
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.error("clippy cache-bust: metadata error (%s); refusing to run clippy on stale cache.", exc)
        return False

    any_clean_failed = False
    for name in filter(None, names):
        try:
            clean = subprocess.run(
                [cargo_bin, "clean", "-p", name],
                capture_output=True, text=True, cwd=cargo_dir, timeout=60,
            )
            if clean.returncode != 0:
                logger.warning(
                    "clippy cache-bust: clean -p %s failed (rc=%d): %s",
                    name, clean.returncode, clean.stderr.strip(),
                )
                any_clean_failed = True
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("clippy cache-bust: clean -p %s errored: %s", name, exc)
            any_clean_failed = True

    return not any_clean_failed


def _run_cargo_clippy(cargo_dir: str) -> Dict[str, Any]:
    """Run cargo clippy and parse JSON diagnostics.

    Returns dict with keys: warnings, errors, messages, returncode, raw_stderr.
    `returncode` is ALWAYS present so callers can distinguish "0 warnings" from
    "clippy never ran" (timeout / missing binary / launch failure).
    """
    clippy_bin = shutil.which("cargo")
    if not clippy_bin:
        logger.error("cargo not found in PATH")
        return {"warnings": 0, "errors": 0, "messages": [], "returncode": -1,
                "raw_stderr": "cargo not found"}

    if not _bust_clippy_cache(cargo_dir, clippy_bin):
        logger.error(
            "clippy cache-bust failed \u2014 refusing to run clippy on potentially "
            "stale artifacts (which would silently pass with 0 diagnostics).",
        )
        return {"warnings": 0, "errors": 0, "messages": [], "returncode": -2,
                "raw_stderr": "clippy cache-bust failed; refused to run to avoid stale-cache false pass"}

    cmd = [
        clippy_bin,
        "clippy",
        "--all-targets",
        "--message-format=json",
        "--",
        "-D",
        "warnings",
    ]
    logger.info("Running: %s (in %s)", " ".join(cmd), cargo_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cargo_dir,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.error("cargo clippy timed out after 300s")
        return {"warnings": 0, "errors": 0, "messages": [], "returncode": 124,
                "raw_stderr": "timeout"}
    except FileNotFoundError as exc:
        logger.error("cargo binary not found: %s", exc)
        return {"warnings": 0, "errors": 0, "messages": [], "returncode": -1,
                "raw_stderr": str(exc)}

    warnings = 0
    errors = 0
    messages: List[Dict[str, Any]] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("reason") != "compiler-message":
            continue

        msg = obj.get("message", {})
        level = msg.get("level", "")
        text = msg.get("message", "")
        spans = msg.get("spans", [])

        if level == "warning":
            warnings += 1
        elif level == "error":
            errors += 1
        else:
            continue

        diagnostic: Dict[str, Any] = {
            "level": level,
            "message": text,
            "spans": [],
        }
        for span in spans:
            diagnostic["spans"].append(
                {
                    "file": span.get("file_name", ""),
                    "line_start": span.get("line_start", 0),
                    "line_end": span.get("line_end", 0),
                    "col_start": span.get("column_start", 0),
                    "col_end": span.get("column_end", 0),
                    "label": span.get("label", ""),
                }
            )
        messages.append(diagnostic)

    return {
        "warnings": warnings,
        "errors": errors,
        "messages": messages,
        "returncode": result.returncode,
        "raw_stderr": result.stderr,
    }


def _run_cargo_fmt(cargo_dir: str) -> Dict[str, Any]:
    """Run cargo fmt --check and return formatting status.

    Returns dict with keys: formatted (bool), diff (str), returncode (int).
    """
    cargo_bin = shutil.which("cargo")
    if not cargo_bin:
        logger.error("cargo not found in PATH")
        return {"formatted": False, "diff": "", "returncode": -1}

    cmd = [cargo_bin, "fmt", "--all", "--", "--check"]
    logger.info("Running: %s (in %s)", " ".join(cmd), cargo_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cargo_dir,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("cargo fmt timed out after 120s")
        return {"formatted": False, "diff": "", "returncode": -1}
    except FileNotFoundError as exc:
        logger.error("cargo binary not found: %s", exc)
        return {"formatted": False, "diff": "", "returncode": -1}

    formatted = result.returncode == 0
    return {
        "formatted": formatted,
        "diff": result.stdout,
        "returncode": result.returncode,
    }


_SKIP_WALK_DIRS = {"target", ".git", "node_modules", "vendor"}


def _collect_rs_files(repo_dir: str) -> List[str]:
    """Walk the directory and collect all .rs files, skipping build artefacts
    and vendored deps (target/, .git/, ...) which would otherwise list thousands
    of irrelevant generated files."""
    rs_files: List[str] = []
    for root, dirs, files in os.walk(repo_dir):
        # Prune in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if d not in _SKIP_WALK_DIRS]
        for f in files:
            if f.endswith(".rs"):
                rs_files.append(os.path.join(root, f))
    return sorted(rs_files)


def main(
    repo_or_dir: str,
    files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run Rust linting on a repository or directory.

    Executes two lint stages:
      1. cargo clippy — static analysis with JSON diagnostics
      2. cargo fmt --check — formatting verification

    Args:
    ----
        repo_or_dir: Path to a Rust repository or directory containing Cargo.toml.
        files: Optional list of specific .rs files to report on.
            If None, all .rs files under repo_or_dir are discovered.

    Returns:
    -------
        A dict with keys:
            clippy: {warnings, errors, messages, returncode, raw_stderr}
            fmt: {formatted, diff, returncode}
            files_checked: list of .rs file paths
            passed: bool — True if zero clippy issues and formatting is clean

    """
    repo_dir = os.path.abspath(repo_or_dir)
    if not os.path.isdir(repo_dir):
        logger.error("Directory does not exist: %s", repo_dir)
        raise FileNotFoundError(f"Directory does not exist: {repo_dir}")

    cargo_dir = _find_cargo_toml(repo_dir)
    if cargo_dir is None:
        logger.error("No Cargo.toml found at or above %s", repo_dir)
        raise FileNotFoundError(
            f"No Cargo.toml found at or above {repo_dir}. "
            "Ensure this is a valid Rust project."
        )

    logger.info("Rust lint: project root at %s", cargo_dir)

    if files is not None:
        rs_files = [os.path.abspath(f) for f in files]
    else:
        rs_files = _collect_rs_files(repo_dir)

    logger.info("Found %d .rs files to check", len(rs_files))

    logger.info("=== Stage 1: cargo clippy ===")
    clippy_result = _run_cargo_clippy(cargo_dir)
    logger.info(
        "Clippy: %d warning(s), %d error(s)",
        clippy_result["warnings"],
        clippy_result["errors"],
    )

    logger.info("=== Stage 2: cargo fmt --check ===")
    fmt_result = _run_cargo_fmt(cargo_dir)
    if fmt_result["formatted"]:
        logger.info("Formatting: OK")
    else:
        logger.warning("Formatting: needs changes")

    # Require clippy to have actually completed cleanly (returncode 0). Without
    # this, a timeout / missing-binary / manifest-parse failure (which emits no
    # `compiler-message` JSON) yields warnings==errors==0 and would falsely PASS.
    clippy_rc = clippy_result.get("returncode")
    passed = (
        clippy_rc == 0
        and clippy_result["warnings"] == 0
        and clippy_result["errors"] == 0
        and fmt_result["formatted"]
    )
    if clippy_rc != 0 and clippy_result["warnings"] == 0 and clippy_result["errors"] == 0:
        logger.warning(
            "Clippy exited rc=%s with no parsed diagnostics — treating as FAILED "
            "(build/manifest/timeout failure, not a clean pass)", clippy_rc,
        )

    result = {
        "clippy": clippy_result,
        "fmt": fmt_result,
        "files_checked": rs_files,
        "passed": passed,
    }

    print(f"Clippy: {clippy_result['warnings']} warning(s), {clippy_result['errors']} error(s)")
    if clippy_result["messages"]:
        for msg in clippy_result["messages"]:
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
