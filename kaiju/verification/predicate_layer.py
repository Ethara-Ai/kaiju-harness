"""Evaluates the generated task-specific predicates against a produced trajectory
and appends them as deterministic (Layer-1) results. Graded, not gating by default:
a generated predicate could be imperfect, so it lowers the score rather than
quarantining (promote to gating once mutation-validated)."""
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
            phase="P3gen"))
    report.meta["predicates_applied"] = len(predicates)
    return report.finalize()
