"""Python wrapper for the TypeScript stubbing engine.

Invokes tools/stub_ts.ts via npx ts-node, captures the JSON report from
stdout, and returns it as a dict. Stderr is used for diagnostic logging.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
PROJECT_ROOT = TOOLS_DIR.parent

MAX_STDERR_LOG_CHARS = 2000
MAX_STDOUT_LOG_CHARS = 500

_HEAP_FLOOR_MB = 4096
_HEAP_CEIL_MB = 12288
_HEAP_SYSTEM_FRACTION = 0.75
_TIMEOUT_FLOOR_S = 300
_TIMEOUT_CEIL_S = 1800
_TIMEOUT_S_PER_100_FILES = 60


def _detect_system_ram_mb() -> int | None:
    """Best-effort total physical RAM in MB. Returns None if unknown."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and size > 0:
            return int(pages * size // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass
    return None


def _compute_node_heap_mb() -> int:
    """Resolve V8 heap size: KAIJU_TS_NODE_HEAP_MB > 75% of RAM clamped [4GB,12GB] > 8GB."""
    override = os.environ.get("KAIJU_TS_NODE_HEAP_MB")
    if override:
        try:
            return max(1024, int(override))
        except ValueError:
            logger.warning("Invalid KAIJU_TS_NODE_HEAP_MB=%r, ignoring", override)
    ram_mb = _detect_system_ram_mb()
    if ram_mb is None:
        return 8192
    target = int(ram_mb * _HEAP_SYSTEM_FRACTION)
    return max(_HEAP_FLOOR_MB, min(_HEAP_CEIL_MB, target))


def _count_ts_files(*roots: Path) -> int:
    """Count .ts/.tsx files under given roots (excluding node_modules and .git)."""
    total = 0
    for root in roots:
        try:
            for path in Path(root).rglob("*.ts"):
                parts = path.parts
                if "node_modules" in parts or ".git" in parts:
                    continue
                total += 1
        except (OSError, RuntimeError):
            continue
    return total


def _compute_smart_timeout(src_dir: Path, extra_scan_dirs: list[Path] | None) -> int:
    """Resolve subprocess timeout: KAIJU_TS_STUB_TIMEOUT > scaled by file count > 300s."""
    override = os.environ.get("KAIJU_TS_STUB_TIMEOUT")
    if override:
        try:
            return max(60, int(override))
        except ValueError:
            logger.warning("Invalid KAIJU_TS_STUB_TIMEOUT=%r, ignoring", override)
    roots: list[Path] = [Path(src_dir)]
    if extra_scan_dirs:
        roots.extend(Path(d) for d in extra_scan_dirs)
    n_files = _count_ts_files(*roots)
    scaled = _TIMEOUT_FLOOR_S + (n_files // 100) * _TIMEOUT_S_PER_100_FILES
    return max(_TIMEOUT_FLOOR_S, min(_TIMEOUT_CEIL_S, scaled))

def run_stub_ts(
    src_dir: Path,
    extra_scan_dirs: list[Path] | None = None,
    mode: str = "all",
    verbose: bool = False,
    timeout: int | None = None,
) -> dict:
    """Run the ts-morph stubbing engine via subprocess.

    Args:
    ----
        src_dir: Absolute path to the TypeScript source directory to stub.
        extra_scan_dirs: Additional directories to scan for import-time names
                         (e.g. test dirs, sibling packages). Not stubbed.
        mode: Stubbing mode. Only "all" is supported for TypeScript.
        verbose: Enable debug logging in the TS engine (sent to stderr).
        timeout: Maximum seconds to wait for the subprocess. None (default)
                 = auto-scale by file count. Override via KAIJU_TS_STUB_TIMEOUT.

    Returns:
    -------
        The JSON report dict from stub_ts.ts with keys:
        files_processed, files_modified, functions_stubbed,
        functions_preserved, import_time_names, errors.

    Raises:
    ------
        RuntimeError: If the subprocess fails or returns invalid JSON.

    """
    stub_ts_path = TOOLS_DIR / "stub_ts.ts"
    if not stub_ts_path.exists():
        raise FileNotFoundError(f"TypeScript stubber not found: {stub_ts_path}")

    ts_node_flags: list[str] = []
    probe = Path(src_dir).resolve()
    for _ in range(8):
        pkg = probe / "package.json"
        if pkg.exists():
            try:
                if json.loads(pkg.read_text()).get("type") == "module":
                    ts_node_flags.append("--esm")
            except Exception:
                pass
            break
        if probe.parent == probe:
            break
        probe = probe.parent

    cmd = [
        "npx",
        "ts-node",
        *ts_node_flags,
        str(stub_ts_path),
        "--src-dir",
        str(src_dir),
        "--mode",
        mode,
    ]
    if extra_scan_dirs:
        cmd.extend(
            [
                "--extra-scan-dirs",
                ",".join(str(d) for d in extra_scan_dirs),
            ]
        )
    if verbose:
        cmd.append("--verbose")

    cwd = str(PROJECT_ROOT)

    env = os.environ.copy()
    heap_mb = _compute_node_heap_mb()
    existing_node_opts = env.get("NODE_OPTIONS", "")
    env["NODE_OPTIONS"] = (
        f"{existing_node_opts} --max-old-space-size={heap_mb}".strip()
    )

    if timeout is None:
        timeout = _compute_smart_timeout(src_dir, extra_scan_dirs)
    logger.info(
        "TS stubber resource budget: heap=%d MB, timeout=%d s",
        heap_mb,
        timeout,
    )

    logger.info("Running TS stubber: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env,
    )

    if result.returncode != 0:
        logger.error(
            "TS stubber failed (rc=%d):\nstderr: %s\nstdout: %s",
            result.returncode,
            result.stderr[:MAX_STDERR_LOG_CHARS],
            result.stdout[:MAX_STDOUT_LOG_CHARS],
        )
        raise RuntimeError(
            f"TS stubber failed (rc={result.returncode}): "
            f"{result.stderr[:MAX_STDOUT_LOG_CHARS]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError("TS stubber produced no output on stdout")

    json_start = stdout.find("{")
    json_end = stdout.rfind("}")
    if json_start < 0 or json_end < 0 or json_end <= json_start:
        logger.error(
            "TS stubber output not valid JSON:\n%s", stdout[:MAX_STDOUT_LOG_CHARS]
        )
        raise RuntimeError("TS stubber output contains no JSON object")

    json_str = stdout[json_start : json_end + 1]

    try:
        report = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(
            "TS stubber output not valid JSON:\n%s", json_str[:MAX_STDOUT_LOG_CHARS]
        )
        raise RuntimeError(f"TS stubber output not JSON: {e}") from e

    logger.info(
        "TS stubbing complete: %d files processed, %d modified, "
        "%d functions stubbed, %d import-time preserved, %d errors",
        report.get("files_processed", 0),
        report.get("files_modified", 0),
        report.get("functions_stubbed", 0),
        report.get("functions_preserved", 0),
        len(report.get("errors", [])),
    )

    if result.stderr and verbose:
        logger.debug("TS stubber stderr:\n%s", result.stderr[:MAX_STDERR_LOG_CHARS])

    return report
