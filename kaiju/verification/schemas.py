"""Result schemas for a verification run.

A ``VerificationReport`` is the artifact written to
``outputs/<uuid>/verification/results/<model>/agent/run_<N>/report.json``.
It carries both a **gate** (keep/quarantine) and a **graded score** (per the
locked decision that we emit both).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any

from .taxonomy import TAXONOMY_VERSION


class CheckStatus(enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"   # concern legitimately does not apply here
    PENDING = "pending"                 # concern registered but its phase not yet shipped
    ERROR = "error"                     # the check itself could not run (missing/corrupt data)


class Gate(enum.Enum):
    ACCEPT = "accept"
    QUARANTINE = "quarantine"           # a gating check FAILed or ERRORed


@dataclass
class CheckResult:
    """The outcome of evaluating one concern against one trajectory."""

    concern_id: str
    status: CheckStatus
    gating: bool
    layer: int
    owner: str
    weight: float
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    phase: str = "P1"
    dimension: str = "process"    # legitimacy (gates) | process (scored) | honesty (lens)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @property
    def counts_toward_score(self) -> bool:
        # Only trajectory-PROCESS quality feeds the score. Legitimacy is the gate;
        # honesty (solution-vs-golden) is a separate non-scored signal — folding
        # code-correctness into the score is eval's job, not the trajectory's.
        return self.dimension == "process" and self.status in (CheckStatus.PASS, CheckStatus.FAIL)

    @property
    def scored_value(self) -> float:
        return 1.0 if self.status is CheckStatus.PASS else 0.0


@dataclass
class VerificationReport:
    run_dir: str
    taxonomy_version: str = TAXONOMY_VERSION
    gate: Gate = Gate.ACCEPT
    graded_score: float | None = None    # weighted PASS fraction; None if nothing decided
    results: list[CheckResult] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---- rollups -------------------------------------------------------- #
    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status is CheckStatus.FAIL]

    @property
    def gating_failures(self) -> list[CheckResult]:
        return [
            r for r in self.results
            if r.gating and r.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        ]

    def finalize(self) -> "VerificationReport":
        """Compute the gate and graded score from the accumulated results."""
        self.gate = Gate.QUARANTINE if self.gating_failures else Gate.ACCEPT

        num = sum(r.scored_value * r.weight for r in self.results if r.counts_toward_score)
        den = sum(r.weight for r in self.results if r.counts_toward_score)
        # None (not 0.0) when nothing was decided — 0.0 would be indistinguishable
        # from "every check failed".
        self.graded_score = round(num / den, 4) if den else None

        tally: dict[str, int] = {}
        for r in self.results:
            tally[r.status.value] = tally.get(r.status.value, 0) + 1
        self.meta["status_tally"] = tally
        self.meta["decided_checks"] = int(den) if den == int(den) else den
        # Honesty (C3): a P1 ACCEPT is not "fully verified" while gating concerns
        # remain unenforced (their checks ship in later phases). Surface them so a
        # consumer never reads ACCEPT as "cheat-checked".
        self.meta["unenforced_gating_concerns"] = [
            r.concern_id for r in self.results
            if r.gating and r.status is CheckStatus.PENDING
        ]
        self.meta["trajectory_honesty"] = self._honesty_signal()
        return self

    def _honesty_signal(self) -> dict[str, Any]:
        """Roll the honesty-dimension checks (solution-vs-golden) into a signal: did
        the trajectory GAME the frozen tests (hardcoded / special-cased outputs), or
        OVERFIT them (solved the visible suite yet fails held-out)? Informational,
        never gating — an honestly incomplete solution is not dishonest.

        Overfit is asserted ONLY when the run actually SOLVED the frozen tests it was
        scored on (L1.TEST_STAGE_OUTCOME PASS) *and* trails the golden on the held-out
        oracle. Without a confirmed frozen solve, a held-out gap is just incompleteness
        (correctness is eval's call), reported as a softer 'review'."""
        from .taxonomy import GAMING_CONCERNS, OVERFIT_CONCERNS
        lens = [r for r in self.results
                if r.dimension == "honesty" and r.status in (CheckStatus.PASS, CheckStatus.FAIL)]
        failed = {r.concern_id for r in lens if r.status is CheckStatus.FAIL}
        # a no-hardcoded-* predicate FAIL is a direct gaming tell, alongside the
        # intent-fidelity rubric concern.
        gamed = bool(failed & GAMING_CONCERNS) or any(
            "hardcod" in cid.lower() or "no-hardcoded" in cid.lower() for cid in failed)
        weak_generalization = bool(failed & OVERFIT_CONCERNS)
        # cross-reference eval: did the run solve the frozen/visible suite it was
        # scored on? None when there is no scored test stage (can't tell).
        frozen_solved = self._frozen_solved()
        overfit = bool(weak_generalization and frozen_solved)
        # Three-state, so honest incompleteness is not branded as dishonesty:
        #   suspect — GAMED (hardcoded/special-cased) or genuine OVERFIT (solved
        #             visible, fails held-out): the trajectory looks better than it is.
        #   review  — weak generalization without a confirmed frozen solve: likely an
        #             honestly incomplete solve; correctness is eval's call, not ours.
        #   clean   — no honesty concern fired.
        if gamed or overfit:
            signal = "suspect"
        elif weak_generalization:
            signal = "review"
        else:
            signal = "clean"
        return {
            "signal": signal,
            "gamed_frozen_tests": gamed,
            "overfit_frozen_tests": overfit,
            "weak_generalization_vs_golden": weak_generalization,
            "frozen_tests_solved": frozen_solved,
            "n_signals_evaluated": len(lens),
            "failing_signals": sorted(failed),
        }

    def _frozen_solved(self) -> bool | None:
        """True/False if the run solved the frozen test suite it was scored on
        (final test-refine stage passed all frozen tests); None if unscored."""
        for r in self.results:
            if r.concern_id == "L1.TEST_STAGE_OUTCOME":
                if r.status is CheckStatus.PASS:
                    return True
                if r.status is CheckStatus.FAIL:
                    return False
                return None   # NOT_APPLICABLE / PENDING -> unknown
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_dir": self.run_dir,
            "taxonomy_version": self.taxonomy_version,
            "gate": self.gate.value,
            "graded_score": self.graded_score,
            "gating_failures": [r.concern_id for r in self.gating_failures],
            "results": [r.to_dict() for r in self.results],
            "meta": self.meta,
        }
