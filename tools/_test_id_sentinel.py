"""Shared base-validation sentinel for generate_test_ids_* (QC-C6-005).

The V5 gate flags a DEGENERATE stubbed base — one whose base_commit can no
longer ENUMERATE its own tests (so the eval denominator collapses to 0 and every
score is a meaningless 0%). Java/JS/TS encoded this as a NEGATIVE result count
(``results[repo] = -len(test_ids)``); C/Python only logged it and cpp/go/rust
had no ``--validate-base`` at all — so downstream tooling could not uniformly
tell a real repo from a degenerate one.

This module single-sources the ONE canonical rule so every language applies
identical semantics and the contract cannot drift again:

    result_count(test_ids, base_count) -> int

* ``base_count`` is how many tests the STUBBED base_commit can enumerate.
* ``> 0``  -> healthy: return ``+len(test_ids)`` (the canonical test count).
* ``== 0`` -> degenerate: return ``-len(test_ids)`` (negative sentinel).
* ``None`` -> base validation not run: return ``+len(test_ids)`` (unknown-OK).

Downstream: ``count < 0`` means "exclude / investigate — base cannot enumerate".
"""

from __future__ import annotations

from typing import Optional

__all__ = ["result_count", "is_degenerate", "BASE_VALIDATION_FAILED_MSG"]

BASE_VALIDATION_FAILED_MSG = (
    "BASE COMMIT VALIDATION FAILED: the stubbed base_commit is degenerate — it "
    "cannot build/enumerate its own tests, so every eval denominator would be 0 "
    "and the pipeline would report a meaningless 0%% pass rate. Emitting a "
    "negative sentinel (-N) so downstream tooling excludes this repo."
)


def result_count(test_ids, base_count: Optional[int]) -> int:
    """Canonical V5 result count with the degenerate-base negative sentinel.

    Args:
        test_ids: the canonical enumerated test-id list (or its length).
        base_count: tests the stubbed base can enumerate; ``None`` if the
            ``--validate-base`` gate was not run.
    """
    n = test_ids if isinstance(test_ids, int) else len(test_ids)
    if base_count is not None and base_count == 0:
        return -n
    return n


def is_degenerate(count: int) -> bool:
    """True iff a stored result count is the degenerate-base sentinel."""
    return count < 0
