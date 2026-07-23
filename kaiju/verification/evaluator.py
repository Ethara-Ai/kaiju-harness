"""The deterministic evaluator: runs the P1 Layer-0/Layer-1 checks over a
produced trajectory and emits a ``VerificationReport`` (gate + graded score).

Concerns whose implementing phase has not shipped are emitted as ``PENDING`` so
the report reflects the *full* taxonomy (complete coverage) while being honest
about what is actually enforced today.
"""
from __future__ import annotations

import json
from pathlib import Path

from .deterministic import CHECKS
from .schemas import CheckResult, CheckStatus, VerificationReport
from .taxonomy import TAXONOMY
from .trajectory import load_trajectory


def _result_for(concern, bundle) -> CheckResult:
    base = dict(
        concern_id=concern.id, gating=concern.gating, layer=int(concern.layer),
        owner=concern.owner.value, weight=concern.weight, phase=concern.phase,
    )
    check = CHECKS.get(concern.id)
    if check is None:
        return CheckResult(
            status=CheckStatus.PENDING,
            summary=f"not yet enforced (planned in {concern.phase})", **base)
    try:
        out = check(bundle)
    except Exception as exc:  # a check must never crash the whole run
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"check raised: {type(exc).__name__}: {exc}", **base)
    return CheckResult(
        status=out.status, summary=out.summary, evidence=out.evidence or {}, **base)


def verify_run(run_dir: str | Path, *, out_path: str | Path | None = None,
               apply_rubric: bool = True) -> VerificationReport:
    """Verify one produced trajectory (deterministic layers). When a frozen rubric
    bundle + a stored judge result exist for the task (and *apply_rubric*), the
    Layer-2 judgment results are folded in; otherwise Layer 2 stays PENDING."""
    bundle = load_trajectory(run_dir)
    report = VerificationReport(run_dir=str(Path(run_dir).resolve()))
    report.meta["language"] = bundle.language
    for concern in TAXONOMY:
        report.results.append(_result_for(concern, bundle))
    report.finalize()

    if apply_rubric:
        _maybe_apply_generated(report, bundle, run_dir)

    target = Path(out_path) if out_path else _default_out_path(Path(run_dir))
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        report.meta["report_path"] = str(target)
    return report


def _maybe_apply_generated(report: VerificationReport, bundle, run_dir: str | Path) -> None:
    """Fold in the generated task-specific layer: (a) deterministic PREDICATES if a
    frozen bundle has them; (b) rubric JUDGE verdicts if a stored judge result exists.
    Both optional; never break the deterministic report."""
    try:
        from .orchestrate import (uuid_root_of, load_bundle, load_judge_result,
                                  load_predicates)
        from .rubric_layer import apply_rubric as _apply_rubric
        from .predicate_layer import apply_predicates as _apply_preds
        uuid_root = uuid_root_of(run_dir)
        preds = load_predicates(uuid_root)
        if preds:
            _apply_preds(report, preds, bundle)
        loaded = load_bundle(uuid_root)
        judge = load_judge_result(uuid_root, run_dir)
        if loaded is not None and judge is not None:
            from .rubric_anchor import load_anchors
            anchors = load_anchors(uuid_root)
            g, s = anchors if anchors else (None, None)
            _apply_rubric(report, loaded[1], judge, golden=g, stub=s)
    except Exception as exc:  # generated layer is optional
        report.meta["generated_layer_error"] = f"{type(exc).__name__}: {exc}"


def _default_out_path(run_dir: Path) -> Path | None:
    """Place the report at ``verification/results/<model>/agent/<run>/report.json``
    when the run dir has the consolidated shape, else next to the run dir."""
    from . import layout
    run_dir = Path(run_dir).resolve()
    if "runs" in run_dir.parts:
        return layout.report_path(layout.uuid_root_of(run_dir), run_dir)
    return run_dir / "report.json"
