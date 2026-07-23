"""Mutant execution backends for `closed_meta_verify` / `validate_verifiers`.

A "runner" decides whether the generated verifier set REJECTS (kills) a mutant.
Two backends:

  * ``predicate_mutant_runner`` — applies the mutant to the golden code and runs
    the bundle's generated deterministic PREDICATES. killed == any predicate fails.
    Fully self-contained (no container) and unit-tested — this alone validates the
    discriminating power of the predicate layer.
  * ``combined_mutant_runner`` — additionally runs the frozen tests on the mutated
    code via an injected ``test_runner`` (container-bound); killed if predicates
    fail OR tests fail. Use in the pipeline for the strongest signal.
"""
from __future__ import annotations

import re
from typing import Callable

from .mutation import Mutant
from .predicates import Predicate, evaluate_predicate


def _apply_mutant(golden_code: str, mutant: Mutant) -> str:
    """Apply a mutant to the golden code. Supports a unified-diff-ish `diff`
    (replace each removed line with its added counterpart) and, failing that,
    treats the diff body as the mutated snippet appended. Best-effort but
    deterministic — the mutant only needs to perturb the code the predicates read."""
    diff = mutant.diff or ""
    if not diff:
        return golden_code
    removed = [ln[1:] for ln in diff.splitlines()
               if ln.startswith("-") and not ln.startswith("---")]
    added = [ln[1:] for ln in diff.splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    code = golden_code
    # pair removed->added line replacements
    for rem, add in zip(removed, added):
        if rem.strip() and rem in code:
            code = code.replace(rem, add, 1)
    # pure deletions (removed with no paired addition) are dropped
    for rem in removed[len(added):]:
        if rem.strip():
            code = code.replace(rem, "", 1)
    # pure additions (added with no paired removal) are appended
    if len(added) > len(removed):
        code += "\n" + "\n".join(added[len(removed):])
    if not removed and not added:
        code += "\n" + diff
    return code


def predicate_mutant_runner(golden_code: str,
                            predicates: list[Predicate]) -> Callable[[Mutant], bool]:
    """runner(mutant) -> killed: the mutated code fails at least one predicate."""
    def runner(mutant: Mutant) -> bool:
        mutated = _apply_mutant(golden_code, mutant)
        for p in predicates:
            passed, _ = evaluate_predicate(p, mutated)
            if not passed:
                return True   # a verifier rejected it -> killed
        return False
    return runner


def combined_mutant_runner(golden_code: str, predicates: list[Predicate],
                           test_runner: Callable[[str], bool]
                           ) -> Callable[[Mutant], bool]:
    """As above, plus the frozen tests. ``test_runner(mutated_code) -> tests_pass``
    is the container-bound backend (apply mutant, run frozen tests). killed if the
    predicates reject OR the tests fail."""
    pred = predicate_mutant_runner(golden_code, predicates)

    def runner(mutant: Mutant) -> bool:
        if pred(mutant):
            return True
        mutated = _apply_mutant(golden_code, mutant)
        return not bool(test_runner(mutated))
    return runner
