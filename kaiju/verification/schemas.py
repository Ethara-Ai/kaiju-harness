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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @property
    def counts_toward_score(self) -> bool:
        return self.status in (CheckStatus.PASS, CheckStatus.FAIL)

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
        return self

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
