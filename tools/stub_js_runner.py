"""Python wrapper for the Babel-based JavaScript stubbing engine."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
STUBBER_DIR = TOOLS_DIR / "jsstubber"
STUB_JS_PATH = STUBBER_DIR / "stub_js.ts"

MAX_STDERR_LOG_CHARS = 2000
MAX_STDOUT_LOG_CHARS = 500

_HEAP_FLOOR_MB = 4096
_HEAP_CEIL_MB = 12288
_HEAP_SYSTEM_FRACTION = 0.75
_TIMEOUT_FLOOR_S = 300
_TIMEOUT_CEIL_S = 1800
_TIMEOUT_S_PER_100_FILES = 60

_TS_NODE_COMPILER_OPTIONS = json.dumps(
    {
        "module": "commonjs",
        "target": "es2022",
        "moduleResolution": "node",
        "esModuleInterop": True,
        "allowSyntheticDefaultImports": True,
        "resolveJsonModule": True,
        "skipLibCheck": True,
        "strict": False,
    }
)


def _detect_system_ram_mb() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and size > 0:
            return int(pages * size // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return None
    return None


def _compute_node_heap_mb() -> int:
    override = os.environ.get("KAIJU_JS_NODE_HEAP_MB")
    if override:
        try:
            return max(1024, int(override))
        except ValueError:
            logger.warning("Invalid KAIJU_JS_NODE_HEAP_MB=%r, ignoring", override)
    ram_mb = _detect_system_ram_mb()
    if ram_mb is None:
        return 8192
    target = int(ram_mb * _HEAP_SYSTEM_FRACTION)
    return max(_HEAP_FLOOR_MB, min(_HEAP_CEIL_MB, target))


def _count_js_files(*roots: Path) -> int:
    total = 0
    exts = (".js", ".mjs", ".cjs", ".jsx")
    for root in roots:
        try:
            for ext in exts:
                for path in Path(root).rglob(f"*{ext}"):
                    parts = path.parts
                    if "node_modules" in parts or ".git" in parts:
                        continue
                    total += 1
        except (OSError, RuntimeError):
            continue
    return total


def _compute_smart_timeout(src_dir: Path, extra_scan_dirs: list[Path] | None) -> int:
    override = os.environ.get("KAIJU_JS_STUB_TIMEOUT")
    if override:
        try:
            return max(60, int(override))
        except ValueError:
            logger.warning("Invalid KAIJU_JS_STUB_TIMEOUT=%r, ignoring", override)
    roots: list[Path] = [Path(src_dir)]
    if extra_scan_dirs:
        roots.extend(Path(d) for d in extra_scan_dirs)
    n_files = _count_js_files(*roots)
    scaled = _TIMEOUT_FLOOR_S + (n_files // 100) * _TIMEOUT_S_PER_100_FILES
    return max(_TIMEOUT_FLOOR_S, min(_TIMEOUT_CEIL_S, scaled))


def run_stub_js(
    src_dir: Path,
    extra_scan_dirs: list[Path] | None = None,
    mode: str = "all",
    verbose: bool = False,
    timeout: int | None = None,
) -> dict:
    """Run the Babel-based JS stubber and return its JSON report."""
    if not STUB_JS_PATH.exists():
        raise FileNotFoundError(f"JS stubber not found: {STUB_JS_PATH}")

    # Resolve to ABSOLUTE paths. The stubber subprocess runs with cwd=STUBBER_DIR
    # (so `npx` finds the jsstubber-local ts-node), so a RELATIVE --src-dir — which
    # is what a relative --clone-dir produces (e.g. `repos_staging/<repo>`) — would
    # wrongly resolve against tools/jsstubber/ and fail "src-dir does not exist".
    # Absolute paths are CWD-independent.
    src_dir = Path(src_dir).resolve()
    if extra_scan_dirs:
        extra_scan_dirs = [Path(d).resolve() for d in extra_scan_dirs]

    cmd: list[str] = [
        "npx",
        "ts-node",
        str(STUB_JS_PATH),
        "--src-dir",
        str(src_dir),
        "--mode",
        mode,
    ]
    if extra_scan_dirs:
        cmd.extend(["--extra-scan-dirs", ",".join(str(d) for d in extra_scan_dirs)])
    if verbose:
        cmd.append("--verbose")

    env = os.environ.copy()
    heap_mb = _compute_node_heap_mb()
    existing_node_opts = env.get("NODE_OPTIONS", "")
    env["NODE_OPTIONS"] = f"{existing_node_opts} --max-old-space-size={heap_mb}".strip()
    env["TS_NODE_COMPILER_OPTIONS"] = _TS_NODE_COMPILER_OPTIONS
    env["TS_NODE_TRANSPILE_ONLY"] = "true"

    if timeout is None:
        timeout = _compute_smart_timeout(src_dir, extra_scan_dirs)
    logger.info("JS stubber budget: heap=%d MB, timeout=%d s", heap_mb, timeout)
    logger.info("Running JS stubber: %s", " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(STUBBER_DIR),
        env=env,
        check=False,
    )

    if result.returncode != 0:
        logger.error(
            "JS stubber failed (rc=%d):\nstderr: %s\nstdout: %s",
            result.returncode,
            result.stderr[:MAX_STDERR_LOG_CHARS],
            result.stdout[:MAX_STDOUT_LOG_CHARS],
        )
        raise RuntimeError(
            f"JS stubber failed (rc={result.returncode}): "
            f"{result.stderr[:MAX_STDOUT_LOG_CHARS]}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError("JS stubber produced no output on stdout")

    json_start = stdout.find("{")
    json_end = stdout.rfind("}")
    if json_start < 0 or json_end < 0 or json_end <= json_start:
        logger.error(
            "JS stubber output not valid JSON:\n%s", stdout[:MAX_STDOUT_LOG_CHARS]
        )
        raise RuntimeError("JS stubber output contains no JSON object")

    json_str = stdout[json_start : json_end + 1]
    try:
        report = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error("JS stubber output not valid JSON:\n%s", json_str[:MAX_STDOUT_LOG_CHARS])
        raise RuntimeError(f"JS stubber output not JSON: {e}") from e

    logger.info(
        "JS stubbing complete: %d files processed, %d modified, "
        "%d functions stubbed, %d import-time preserved, %d errors",
        report.get("files_processed", 0),
        report.get("files_modified", 0),
        report.get("functions_stubbed", 0),
        report.get("functions_skipped_import_time", 0),
        len(report.get("errors", [])),
    )

    if result.stderr and verbose:
        logger.debug("JS stubber stderr:\n%s", result.stderr[:MAX_STDERR_LOG_CHARS])

    return report
