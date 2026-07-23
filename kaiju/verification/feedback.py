"""#3b — the structured per-turn feedback record + its consumer checks.

The recorded feedback carries the test/lint COMMAND but no language-uniform pass/
fail signal, and eval is stage-level not per-module — so `FEEDBACK_CAUSALITY` and
`LINT_MONOTONE` can't be computed from existing artifacts. This defines a compact,
language-neutral record the harness writes at run time (one entry per feedback turn
with the parsed counts), a writer/loader, and the two checks. The checks are N/A
until the record exists (honest), so nothing breaks on un-instrumented runs.

Harness integration (one call where feedback is injected):
    from kaiju.verification.feedback import FeedbackTurn, append_feedback
    append_feedback(run_dir, FeedbackTurn(stage="stage3", module=mod, turn_index=k,
                    kind="test", passed=p, failed=f))
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path

from .schemas import CheckStatus
from .trajectory import TrajectoryBundle


@dataclass
class FeedbackTurn:
    stage: str                 # "stage2" | "stage3"
    module: str
    turn_index: int
    kind: str                  # "test" | "lint"
    passed: int = 0
    failed: int = 0
    findings: int = 0          # lint findings count (kind == "lint")

    def to_dict(self) -> dict:
        return asdict(self)


def _record_path(run_dir: Path) -> Path:
    return run_dir / "feedback_record.json"


def append_feedback(run_dir: str | Path, turn: FeedbackTurn) -> None:
    p = _record_path(Path(run_dir))
    rows = []
    if p.exists():
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
    rows.append(turn.to_dict())
    p.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def load_feedback_record(run_dir: str | Path) -> list[FeedbackTurn] | None:
    p = _record_path(Path(run_dir).resolve())
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return [FeedbackTurn(stage=str(r.get("stage") or ""), module=str(r.get("module") or ""),
                         turn_index=int(r.get("turn_index") or 0), kind=str(r.get("kind") or ""),
                         passed=int(r.get("passed") or 0), failed=int(r.get("failed") or 0),
                         findings=int(r.get("findings") or 0)) for r in rows]


def _by_module(turns, stage, kind):
    groups: dict[str, list[FeedbackTurn]] = {}
    for t in turns:
        if t.stage == stage and t.kind == kind:
            groups.setdefault(t.module, []).append(t)
    for seq in groups.values():
        seq.sort(key=lambda t: t.turn_index)
    return groups


def feedback_causality_check(b: TrajectoryBundle):
    """After failing tests, the agent must make PROGRESS — a module's per-turn
    failure count must be non-increasing across its test feedback turns (acting on
    feedback, not flailing/regressing)."""
    from .deterministic import _Outcome
    turns = load_feedback_record(b.run_dir)
    if not turns:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        "no structured feedback record (needs P2b harness instrumentation)")
    groups = _by_module(turns, "stage3", "test")
    if not any(len(s) >= 2 for s in groups.values()):
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no module with >=2 test feedback turns")
    regressed = []
    for mod, seq in groups.items():
        for a, c in zip(seq, seq[1:]):
            if c.failed > a.failed:
                regressed.append({"module": mod, "failed": [a.failed, c.failed]})
                break
    if regressed:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(regressed)} module(s) regressed on test feedback (flailing)",
                        {"regressed": regressed[:15]})
    return _Outcome(CheckStatus.PASS, "test failures non-increasing across feedback (progress)")


def lint_monotone_check(b: TrajectoryBundle):
    """The lint stage must reduce (not increase) lint findings across its turns."""
    from .deterministic import _Outcome
    turns = load_feedback_record(b.run_dir)
    if not turns:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        "no structured feedback record (needs P2b harness instrumentation)")
    groups = _by_module(turns, "stage2", "lint")
    if not any(len(s) >= 2 for s in groups.values()):
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no module with >=2 lint feedback turns")
    worse = []
    for mod, seq in groups.items():
        for a, c in zip(seq, seq[1:]):
            if c.findings > a.findings:
                worse.append({"module": mod, "findings": [a.findings, c.findings]})
                break
    if worse:
        return _Outcome(CheckStatus.FAIL,
                        f"{len(worse)} module(s) increased lint findings across the stage",
                        {"worse": worse[:15]})
    return _Outcome(CheckStatus.PASS, "lint findings non-increasing across the stage")
