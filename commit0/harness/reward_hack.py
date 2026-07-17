"""Shared reward-hacking / scoring-integrity helpers for every language's
``evaluate_*.py`` aggregator.

Historically each language hand-rolled its own micro-average line and its own
notion of "which rows count", which drifted: Rust/Go/C excluded infra/timeout/
patch-apply/cheat rows from the mean, while C++/TS/JS/Python summed the FULL row
list so a forged (``CHEAT_DETECTED``), crashed (``SUITE_CRASHED``), patch-failed
or inventory-mismatch row silently dragged the reported pass rate down (or, for
the Python ``passed=None`` sentinel, raised ``TypeError`` and aborted the batch).

``average_pass_rate`` is the single source of truth for that formula so the
defense cannot drift again. A row is EXCLUDED from the mean when any of:

* its ``passed`` value is ``None`` (an explicit "not scored" sentinel), or
* its status is in the caller-supplied ``excluded_statuses`` set (infra /
  timeout / patch-apply-failed / compile-failed / forged-cheat / inventory
  mismatch — not a measured model score), or
* the caller-supplied ``exclude_if(row)`` predicate returns True (for languages
  such as JS that flag exclusion with a boolean field, not a status string).

Excluded rows are reported (count returned) so a run that is mostly infra-broken
cannot masquerade as a genuine low score.

``detect_stdout_result_injection`` is the structural forgery check for languages
that count passes from RAW STDOUT (a claim of "all passed" while the test process
exited non-zero is impossible for a genuine run). It is the language-agnostic
form of the guard already inlined in ``evaluate_cpp.py`` / ``evaluate_rust.py``.
"""
from __future__ import annotations

from typing import Callable, Iterable, Mapping, Optional, Tuple

# Canonical status strings that are NEVER a measured model score. Every
# evaluator draws its per-language excluded set from these (plus its own
# language-specific spellings) so the exclusion semantics stay identical.
CHEAT_DETECTED = "CHEAT_DETECTED"
INVENTORY_MISMATCH = "INVENTORY_MISMATCH"

__all__ = [
    "CHEAT_DETECTED",
    "INVENTORY_MISMATCH",
    "average_pass_rate",
    "detect_stdout_result_injection",
]


def average_pass_rate(
    rows: Iterable[Mapping],
    excluded_statuses: Iterable[str] = frozenset(),
    *,
    status_key: str = "status",
    passed_key: str = "passed",
    exclude_if: Optional[Callable[[Mapping], bool]] = None,
) -> Tuple[float, int, int]:
    """Micro-average ``row[passed_key]`` over SCORED rows only.

    Returns ``(averaged_passed, num_excluded, num_scored)``. ``averaged_passed``
    is ``0.0`` when no row is scored. A row is excluded (never summed) when its
    ``passed`` is ``None``, its status is in ``excluded_statuses``, or
    ``exclude_if(row)`` is truthy. This mirrors the canonical Rust/Go/C pattern
    (``scored = [x for x in out if x.get("status") not in _EXCLUDED_STATUSES]``)
    and additionally drops the ``passed=None`` sentinel so aggregators that emit
    it (Python's ``INVENTORY_MISMATCH``) cannot ``TypeError`` on the sum.
    """
    excluded_set = set(excluded_statuses)
    total = 0.0
    scored = 0
    excluded = 0
    for row in rows:
        passed = row.get(passed_key)
        status = row.get(status_key)
        drop = (
            passed is None
            or status in excluded_set
            or (exclude_if is not None and bool(exclude_if(row)))
        )
        if drop:
            excluded += 1
            continue
        total += float(passed)
        scored += 1
    averaged = total / scored if scored else 0.0
    return averaged, excluded, scored


def detect_stdout_result_injection(
    num_passed: int,
    num_tests: int,
    exit_code: Optional[int],
) -> bool:
    """Return True iff a stdout-counted run's claim is structurally impossible.

    For frameworks whose pass count is scraped from RAW STDOUT (GTest ``[ OK ]``,
    Catch2, libtest, ...), a model can print fake pass lines. The test *process*
    exits 0 IFF every test passed, so "claims >= all tests passed" together with
    a NON-ZERO exit is impossible for a genuine run -> forged output. ``exit_code``
    of 0 or ``None`` (unknown) is never flagged; ``num_tests <= 0`` is never
    flagged (nothing to forge). This is the language-agnostic core of the guard
    inlined in ``evaluate_cpp.py`` and ``evaluate_rust.py``.
    """
    if exit_code in (0, None):
        return False
    if num_tests <= 0:
        return False
    return num_passed >= num_tests
