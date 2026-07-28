"""Folds LLM-judge verdicts into the verification report's Layer-2 (the rubric
layer). Backbone criteria fill the four Layer-2 taxonomy concerns; task-specific
criteria are appended as additional graded (non-gating) results.

Layer 2 is graded-only (never gates) until human/independent calibration promotes
it — consistent with BLOCKER-3's reconciliation (independence substitutes for
human labels; the hard gate stays on the deterministic layers).
"""
from __future__ import annotations

from .judge import JudgeResult
from .rubric import Rubric
from .schemas import CheckResult, CheckStatus, VerificationReport
from .taxonomy import Layer, Owner

_L2_WEIGHT = 1.0
_TS_WEIGHT = 0.5               # task-specific criteria contribute, at lower weight


def _verdict_status(passed: bool) -> CheckStatus:
    return CheckStatus.PASS if passed else CheckStatus.FAIL


def apply_rubric(report: VerificationReport, rubric: Rubric, judge: JudgeResult,
                 *, golden: JudgeResult | None = None,
                 stub: JudgeResult | None = None) -> VerificationReport:
    """Fold candidate verdicts into Layer 2. When golden+stub anchors are given,
    VALIDATE each anchorable criterion (drop those the golden fails or the stub
    passes) and score the candidate on validated criteria only, reporting a GAP vs
    golden. Process criteria (non-anchorable) are scored as-is."""
    # An INCONCLUSIVE candidate judge (empty/unparseable) must NOT become fake fails.
    if not judge.ok:
        report.meta["rubric_inconclusive"] = (
            f"judge returned {judge.raw_verdicts} verdicts, "
            f"{judge.response_chars} chars — not applied")
        for r in report.results:
            if r.owner == Owner.RUBRIC.value and r.status is CheckStatus.PENDING:
                r.status = CheckStatus.NOT_APPLICABLE
                r.summary = "judge inconclusive (empty/unparseable response)"
        return report.finalize()

    validation = None
    if golden is not None and stub is not None:
        from .rubric_anchor import validate_criteria
        validation = validate_criteria(rubric, golden, stub)

    def kept(cid: str) -> bool:
        return validation is None or validation[cid].kept

    def drop_reason(cid: str) -> str:
        return validation[cid].reason if validation else ""

    verdicts = judge.by_id()

    # 1) Layer-2 concerns from backbone verdicts (dropped criterion -> N/A, not scored)
    concern_verdict: dict[str, object] = {}
    concern_dropped: dict[str, str] = {}
    for c in rubric.backbone():
        if not c.concern:
            continue
        if not kept(c.id):
            concern_dropped[c.concern] = drop_reason(c.id)
        elif c.id in verdicts:
            concern_verdict[c.concern] = verdicts[c.id]

    for r in report.results:
        if r.owner != Owner.RUBRIC.value:
            continue
        if r.concern_id in concern_dropped:
            r.status = CheckStatus.NOT_APPLICABLE
            r.summary = f"criterion dropped by anchoring: {concern_dropped[r.concern_id]}"
        elif r.concern_id in concern_verdict:
            v = concern_verdict[r.concern_id]
            r.status = _verdict_status(v.passed)
            r.summary = (v.justification or ("met" if v.passed else "not met"))[:300]
            r.evidence = {"evidence": v.evidence, "judge_model": judge.model,
                          "criterion": v.criterion_id}

    # 2) task-specific criteria (drop invalidated ones entirely)
    dropped: list[dict] = [{"criterion": cid, "reason": rsn}
                           for cid, rsn in concern_dropped.items()]
    for c in rubric.task_specific():
        if not kept(c.id):
            dropped.append({"criterion": c.id, "reason": drop_reason(c.id)})
            continue
        v = verdicts.get(c.id)
        if v is None:
            continue
        report.results.append(CheckResult(
            concern_id=c.id, status=_verdict_status(v.passed), gating=False,
            layer=int(Layer.JUDGMENT), owner=Owner.RUBRIC.value, weight=_TS_WEIGHT,
            summary=(v.justification or "")[:300],
            evidence={"text": c.text, "contract_ref": c.contract_ref, "evidence": v.evidence,
                      "judge_model": judge.model},
            phase="P6", dimension="honesty"))

    # 3) GAP vs golden on validated ANCHORABLE criteria (golden passes all by construction)
    anchorable_validated = [c for c in rubric.criteria
                            if c.anchorable and kept(c.id) and c.id in verdicts]
    n = len(anchorable_validated)
    cand_pass = sum(1 for c in anchorable_validated if verdicts[c.id].passed)
    report.meta["rubric_applied"] = True
    report.meta["judge_model"] = judge.model
    report.meta["rubric_criteria"] = len(rubric.criteria)
    report.meta["rubric_anchored"] = validation is not None
    report.meta["rubric_dropped"] = dropped
    report.meta["rubric_validated_anchorable"] = n
    report.meta["rubric_gap_vs_golden"] = (round(1.0 - cand_pass / n, 4) if n else None)
    return report.finalize()
