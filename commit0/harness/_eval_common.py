"""Shared eval-stage helpers: PATCH_APPLY_FAILED sentinel detection + timeout classification.

Every language's `evaluate_*.py` needs to distinguish:

- **Patch-apply failure**: `eval.sh` couldn't apply the model's patch (git apply
  failed). Reported as a `PATCH_APPLY_FAILED` sentinel string in the test output.
  Without this detection the run scores as a legitimate 0/N which is wrong: the
  model's code never ran.

- **Test-suite timeout**: the eval script's `timeout` wrapper killed the test
  runner. Exit codes 124 (GNU timeout), 137 (SIGKILL), 143 (SIGTERM) mark this.
  Without this detection a hung suite scores as legitimate 0/N.

Both are infra failures, not model failures, and must be excluded from scoring
denominators. This helper keeps the detection consistent across 8 languages.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

_PATCH_APPLY_FAILED_MARKER = "PATCH_APPLY_FAILED"
_HEAD_SIZE = 8192
_MIN_TAIL_TRIGGER = 16384
_TIMEOUT_EXIT_CODES = frozenset({124, 137, 143})


def detect_patch_apply_failed(files: Iterable[Path]) -> bool:
    """Return True iff any of ``files`` contains the PATCH_APPLY_FAILED sentinel.

    Searches HEAD and TAIL of each file (8KB windows) so noisy intervening
    output can't hide the sentinel. Missing / unreadable files are ignored
    silently; a real detection is the only positive signal.
    """
    for fp in files:
        try:
            if not fp.exists():
                continue
            size = fp.stat().st_size
            with fp.open("rb") as fh:
                head = fh.read(_HEAD_SIZE).decode("utf-8", errors="replace")
                if _PATCH_APPLY_FAILED_MARKER in head:
                    return True
                if size > _MIN_TAIL_TRIGGER:
                    fh.seek(max(0, size - _HEAD_SIZE))
                    tail = fh.read(_HEAD_SIZE).decode("utf-8", errors="replace")
                    if _PATCH_APPLY_FAILED_MARKER in tail:
                        return True
        except OSError:
            continue
    return False


def detect_timeout(exit_code: int | None) -> bool:
    """Return True iff ``exit_code`` matches the timeout-signal set (124/137/143)."""
    return exit_code in _TIMEOUT_EXIT_CODES


__all__ = [
    "detect_patch_apply_failed",
    "detect_timeout",
]
