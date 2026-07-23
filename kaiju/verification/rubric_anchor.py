"""Golden + stub ANCHORING for the rubric (Prosa/OpenRubrics/SWE-bench methodology).

Judge the reference-correct (golden) and unimplemented (stub) solutions against the
rubric, then VALIDATE each anchorable criterion:
  * a criterion the GOLDEN fails is unsound (too strict / wrong / or a golden defect) -> DROP;
  * a criterion the STUB passes is non-discriminating (asserts nothing) -> DROP.
The candidate is then scored ONLY on validated criteria, reported as a GAP vs golden
(cancels the judge's strictness/leniency offset). Process criteria (reasoning, stage
legitimacy) are candidate-only — bare golden code has no trajectory to anchor them.

Anchors are per-TASK (cached under <uuid>/verification/), not per-run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .judge import JudgeResult, judge_trajectory
from .model_client import ModelClient
from .rubric import Rubric, Criterion


def _code_digest(code: str, label: str) -> str:
    return (f"## {label}\nThe following is the solution's implementation code. "
            f"Judge ONLY the code-and-behavior criteria against it.\n\n```\n{code[:16000]}\n```")


def build_anchor_code(uuid_root: str | Path) -> tuple[str, str] | None:
    """(golden_code, stub_code) = the FULL solution src_dir at reference vs base.
    Full context (not just the stubbed file) so cross-file criteria — e.g. CLI
    routing in __main__.py — can actually be verified (RCA 2)."""
    from .build_inputs import _entries, _find_repo
    from .solution_code import read_solution_code
    uuid_root = Path(uuid_root)
    entry = _entries(uuid_root)
    if not entry:
        return None
    repo = _find_repo(uuid_root, entry)
    base, ref = entry.get("base_commit"), entry.get("reference_commit")
    if repo is None or not base or not ref:
        return None
    src_dir = str(entry.get("src_dir") or ".")
    golden = read_solution_code(repo, ref, src_dir)
    stub = read_solution_code(repo, base, src_dir)
    if not golden:
        return None
    return golden, stub


def _anchors_path(uuid_root):
    from . import layout
    return layout.rubric_anchors_path(uuid_root)


def run_and_store_anchors(uuid_root: str | Path, truth_md: str, rubric: Rubric,
                          client: ModelClient) -> tuple[JudgeResult, JudgeResult] | None:
    """Judge golden + stub code against the rubric; cache as one anchors.json per task
    (symmetric with pytest/anchors.json)."""
    existing = load_anchors(uuid_root)
    if existing is not None:
        return existing
    codes = build_anchor_code(uuid_root)
    if codes is None:
        return None
    golden_code, stub_code = codes
    # Anchor only the anchorable (code-and-behavior) criteria — process criteria have
    # no trajectory to judge against bare code, so they'd only pollute the anchor log
    # with meaningless golden fails.
    anchor_rubric = Rubric(criteria=[c for c in rubric.criteria if c.anchorable])
    golden = judge_trajectory(truth_md, anchor_rubric,
                              _code_digest(golden_code, "GOLDEN reference solution (correct)"), client)
    stub = judge_trajectory(truth_md, anchor_rubric,
                            _code_digest(stub_code, "STUB solution (unimplemented)"), client)
    ap = _anchors_path(uuid_root)
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(json.dumps({"golden": golden.to_dict(), "stub": stub.to_dict()}, indent=2),
                  encoding="utf-8")
    return golden, stub


def load_anchors(uuid_root: str | Path) -> tuple[JudgeResult, JudgeResult] | None:
    ap = _anchors_path(uuid_root)
    if not ap.exists():
        return None
    try:
        d = json.loads(ap.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return JudgeResult.from_dict(d.get("golden") or {}), JudgeResult.from_dict(d.get("stub") or {})


@dataclass
class CriterionValidation:
    criterion_id: str
    kept: bool
    reason: str


def validate_criteria(rubric: Rubric, golden: JudgeResult,
                      stub: JudgeResult) -> dict[str, CriterionValidation]:
    """Keep an anchorable criterion iff golden passes it AND stub fails it. Process
    (non-anchorable) criteria are always kept (can't be golden-anchored). Requires
    both anchor judgements to be OK; otherwise everything is kept (anchoring skipped)."""
    gv, sv = golden.by_id(), stub.by_id()
    anchors_ok = golden.ok and stub.ok
    out: dict[str, CriterionValidation] = {}
    for c in rubric.criteria:
        if not c.anchorable or not anchors_ok:
            out[c.id] = CriterionValidation(c.id, True,
                                            "process criterion" if not c.anchorable
                                            else "anchors unavailable")
            continue
        g = gv.get(c.id)
        s = sv.get(c.id)
        if g is not None and not g.passed:
            out[c.id] = CriterionValidation(c.id, False, "golden FAILS it (unsound/too-strict)")
        elif s is not None and s.passed:
            out[c.id] = CriterionValidation(c.id, False, "stub PASSES it (non-discriminating)")
        else:
            out[c.id] = CriterionValidation(c.id, True, "validated (golden-pass ∧ stub-fail)")
    return out
