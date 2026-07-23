"""Evaluates the generated task-specific predicates against a produced trajectory
and appends them as deterministic (Layer-1) results. These check the SOLUTION's
structure against the golden's mechanisms (e.g. "uses unidecode, not a hardcoded
map"), so they belong to the non-gating HONESTY dimension: they flag gaming /
hardcoding, and never lower the trajectory-process score."""
from __future__ import annotations

from .predicates import Predicate, evaluate_predicate, added_code
from .schemas import CheckResult, CheckStatus, VerificationReport
from .taxonomy import Layer, Owner
from .trajectory import TrajectoryBundle

_PRED_WEIGHT = 0.5


def apply_predicates(report: VerificationReport, predicates: list[Predicate],
                     bundle: TrajectoryBundle) -> VerificationReport:
    if not predicates:
        return report
    code = added_code(bundle.all_agent_patches())
    for p in predicates:
        passed, detail = evaluate_predicate(p, code)
        report.results.append(CheckResult(
            concern_id=p.id,
            status=CheckStatus.PASS if passed else CheckStatus.FAIL,
            gating=False, layer=int(Layer.STRUCTURE), owner=Owner.DETERMINISTIC.value,
            weight=_PRED_WEIGHT, summary=f"{p.type}: {detail}",
            evidence={"target": p.target, "truth_ref": p.truth_ref,
                      "description": p.description},
            phase="P3gen", dimension="honesty"))
    report.meta["predicates_applied"] = len(predicates)
    return report.finalize()
