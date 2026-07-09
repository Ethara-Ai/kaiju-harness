"""Go code stubbing tool — Python wrapper around gostubber binary.

Invokes the gostubber Go binary to replace exported function bodies
with zero-value returns + "STUB: not implemented" string literal.

Preserves:
- All imports and package declarations
- Type definitions, constants, variables
- Unexported functions
- Test files (_test.go)
- Function signatures and comments

Usage:
    python -m tools.stub_go /path/to/repo /path/to/output [--dry-run] [--verbose]

Requires:
    gostubber binary built from tools/gostubber/
    Build: cd tools/gostubber && go build -o gostubber .
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

GOSTUBBER_DIR = Path(__file__).parent / "gostubber"
GOSTUBBER_BIN = GOSTUBBER_DIR / "gostubber"

SKIP_DIRS: set[str] = {"vendor", ".git", "testdata", "node_modules"}


def _ensure_gostubber() -> Path:
    # Rebuild if the binary is MISSING or STALE relative to the source. A stale
    # binary silently corrupts the benchmark: a stubbing/doc-strip fix that lives
    # in the source but not the compiled artifact leaves the agent looking at the
    # crate's full documentation (answer leak). Mirrors ruststubber's
    # _ensure_stubber_fresh.
    newest_src = 0.0
    for f in list(GOSTUBBER_DIR.glob("*.go")) + [
        GOSTUBBER_DIR / "go.mod",
        GOSTUBBER_DIR / "go.sum",
    ]:
        try:
            newest_src = max(newest_src, f.stat().st_mtime)
        except OSError:
            continue
    if GOSTUBBER_BIN.exists() and GOSTUBBER_BIN.stat().st_mtime >= newest_src:
        return GOSTUBBER_BIN

    reason = "missing" if not GOSTUBBER_BIN.exists() else "stale (source newer than binary)"
    logger.info("gostubber binary %s — rebuilding (go build)...", reason)
    try:
        subprocess.run(
            ["go", "build", "-o", str(GOSTUBBER_BIN), "."],
            cwd=str(GOSTUBBER_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error("Failed to build gostubber: %s\n%s", e.stdout, e.stderr)
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Go toolchain not found. Install Go to build gostubber.")
        sys.exit(1)

    return GOSTUBBER_BIN


def stub_go_repo(
    src_dir: Path,
    out_dir: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    gostubber = _ensure_gostubber()

    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(src_dir, out_dir, ignore=shutil.ignore_patterns(*SKIP_DIRS))

    stubbed_count = 0
    for go_file in out_dir.rglob("*.go"):
        rel = go_file.relative_to(out_dir)
        parts = rel.parts
        if any(p in SKIP_DIRS for p in parts):
            continue
        if go_file.name.endswith("_test.go"):
            continue
        if go_file.name == "doc.go":
            continue

        if dry_run:
            logger.info("  [DRY-RUN] Would stub: %s", rel)
            stubbed_count += 1
            continue

        try:
            result = subprocess.run(
                [str(gostubber), str(go_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                stubbed_count += 1
                if verbose:
                    logger.info("  Stubbed: %s", rel)
            else:
                logger.warning("  Failed to stub %s: %s", rel, result.stderr.strip())
        except subprocess.TimeoutExpired:
            logger.warning("  Timeout stubbing %s", rel)

    return stubbed_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Stub Go repo for commit0")
    parser.add_argument("src", type=Path, help="Source Go repo directory")
    parser.add_argument("output", type=Path, help="Output directory for stubbed repo")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.src.is_dir():
        logger.error("Source directory does not exist: %s", args.src)
        sys.exit(1)

    go_mod = args.src / "go.mod"
    if not go_mod.exists():
        logger.error("No go.mod found in %s. Is this a Go repo?", args.src)
        sys.exit(1)

    count = stub_go_repo(args.src, args.output, args.dry_run, args.verbose)
    action = "Would stub" if args.dry_run else "Stubbed"
    logger.info("%s %d Go files in %s -> %s", action, count, args.src, args.output)


if __name__ == "__main__":
    main()
