"""Aggregate per-stage warning markers into pipeline_results.json.

Scans a run's log directory for grep-able warning markers emitted by prep
scripts, agent runners, and evaluators. Emits a JSON histogram to stdout for
pipeline_results.json ingestion via `jq`.

Called from every run_pipeline_*.sh at end of prep AND before final results
write, so a large unattended batch can surface silent failures ("300 modules,
12 had F2 low-stub warnings, 3 hit F4 min-content reject") at a glance instead
of forcing per-repo log grep.

Markers scanned (all grep-able, kept in sync with emit sites):
  * F2 low-stub warning        — prepare_repo_java.py, prepare_repo_cpp.py
  * F4 min-content reject       — tools/scrape_pdf.py
  * INSTALL_VERIFICATION_FAILED — commit0/harness/spec_*.py
  * PREP_WARN:<type>            — general prep-time observability marker

Design principles:
  1. Non-fatal — a missing log dir or unreadable file logs to stderr, returns
     empty aggregate, and exits 0. This helper NEVER crashes a pipeline.
  2. Bounded runtime — walks log dir with size cap (KAIJU_WARN_SCAN_MAX_BYTES)
     so a runaway aider.log cannot make aggregation dominate runtime.
  3. Zero deps — pure stdlib (json, os, re, sys, argparse, pathlib) so it can
     be invoked from a container with only the harness venv.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


_WARNING_PATTERNS: dict[str, re.Pattern[str]] = {
    "f2_low_stub": re.compile(r"F2 low-stub warning", re.IGNORECASE),
    "f4_min_content_reject": re.compile(r"F4 min-content reject", re.IGNORECASE),
    "install_verification_failed": re.compile(r"INSTALL_VERIFICATION_FAILED"),
    "prep_warn_generic": re.compile(r"PREP_WARN:(\w+)"),
    "corruption_reverted": re.compile(
        r"stub introduced.*NEW structural parse error|corruption_reverted",
        re.IGNORECASE,
    ),
    "scrape_retry": re.compile(r"retry.*(\d+)/(\d+).*after.*(\d+\.?\d*)s", re.IGNORECASE),
    "readme_fallback": re.compile(r"falling back to README|_readme_spec", re.IGNORECASE),
}

_SCAN_EXTENSIONS = frozenset({".log", ".txt", ".stderr", ".stdout"})
_SCAN_FILENAMES = frozenset({"agent_run.log", "prep.log", "eval.log", "pipeline.log"})

_MAX_FILE_BYTES = int(os.environ.get("KAIJU_WARN_SCAN_MAX_BYTES", str(50 * 1024 * 1024)))
_MAX_TOTAL_BYTES = int(os.environ.get("KAIJU_WARN_SCAN_TOTAL_MAX_BYTES", str(500 * 1024 * 1024)))


def _should_scan(path: Path) -> bool:
    if path.suffix in _SCAN_EXTENSIONS:
        return True
    if path.name in _SCAN_FILENAMES:
        return True
    return False


def _count_warnings_in_file(path: Path, counts: dict[str, int], details: dict[str, list[str]]) -> int:
    """Scan one file for warning markers. Returns bytes read."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size > _MAX_FILE_BYTES:
        logger.debug("skipping %s: %d bytes exceeds per-file cap %d", path, size, _MAX_FILE_BYTES)
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("failed to read %s: %s", path, exc)
        return 0

    for name, pattern in _WARNING_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            n = len(matches)
            counts[name] = counts.get(name, 0) + n
            if name == "prep_warn_generic":
                for m in matches:
                    subtype = f"prep_warn_{m}" if isinstance(m, str) else "prep_warn_unknown"
                    counts[subtype] = counts.get(subtype, 0) + 1
            sample = str(path.relative_to(path.anchor)) if path.is_absolute() else str(path)
            details.setdefault(name, [])
            if len(details[name]) < 5:
                details[name].append(sample)
    return size


def aggregate(log_dir: Path) -> dict[str, object]:
    counts: dict[str, int] = {}
    details: dict[str, list[str]] = {}
    files_scanned = 0
    bytes_scanned = 0

    if not log_dir.exists():
        return {
            "log_dir": str(log_dir),
            "files_scanned": 0,
            "counts": counts,
            "details": details,
            "error": "log_dir does not exist",
        }
    if not log_dir.is_dir():
        return {
            "log_dir": str(log_dir),
            "files_scanned": 0,
            "counts": counts,
            "details": details,
            "error": "log_dir is not a directory",
        }

    for path in sorted(log_dir.rglob("*")):
        if bytes_scanned >= _MAX_TOTAL_BYTES:
            logger.warning(
                "reached total scan cap %d bytes; stopping",
                _MAX_TOTAL_BYTES,
            )
            break
        if not path.is_file():
            continue
        if not _should_scan(path):
            continue
        size = _count_warnings_in_file(path, counts, details)
        if size > 0:
            files_scanned += 1
            bytes_scanned += size

    return {
        "log_dir": str(log_dir),
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "counts": counts,
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggregate prep/agent/eval warnings for pipeline_results.json")
    ap.add_argument("log_dir", type=Path, help="Root log directory to scan recursively")
    ap.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output (default: compact for jq consumption)",
    )
    args = ap.parse_args()

    result = aggregate(args.log_dir)
    if args.pretty:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
