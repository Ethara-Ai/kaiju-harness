"""P5 — the mutation engine: the human-free oracle that validates the generated
verifiers (ACH / AdverTest two-sided formulation).

A verifier set is sound iff it KILLS semantics-BREAKING mutants of the golden and
SPARES semantics-PRESERVING ones (valid alternative implementations). Killing all
breaking mutants proves discriminating power; sparing the preserving ones proves
path-agnosticism (a different-but-correct solution is not rejected).

Mutant generation (LLM) and mutant execution (apply mutant → run the generated
pytest/judge) are abstracted: generation via ModelClient, execution via an injected
``runner`` callable — so the two-sided aggregation and soundness verdict here are
fully unit-testable, while the execution backend is wired where the container is.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .model_client import ModelClient

BREAKING = "breaking"
PRESERVING = "preserving"


@dataclass
class Mutant:
    id: str
    kind: str                 # BREAKING | PRESERVING
    description: str
    diff: str = ""

    @property
    def must_be_killed(self) -> bool:
        return self.kind == BREAKING


@dataclass
class MutantOutcome:
    mutant: Mutant
    killed: bool              # did the verifier set REJECT this mutant?

    @property
    def correct(self) -> bool:
        # breaking must be killed; preserving must be spared (not killed)
        return self.killed if self.mutant.must_be_killed else (not self.killed)


@dataclass
class MutationReport:
    outcomes: list[MutantOutcome] = field(default_factory=list)

    def _split(self):
        b = [o for o in self.outcomes if o.mutant.kind == BREAKING]
        p = [o for o in self.outcomes if o.mutant.kind == PRESERVING]
        return b, p

    @property
    def mutation_score(self) -> float | None:
        b, _ = self._split()
        return round(sum(o.killed for o in b) / len(b), 4) if b else None

    @property
    def survived_breaking(self) -> list[Mutant]:
        return [o.mutant for o in self.outcomes if o.mutant.kind == BREAKING and not o.killed]

    @property
    def false_killed_preserving(self) -> list[Mutant]:
        return [o.mutant for o in self.outcomes if o.mutant.kind == PRESERVING and o.killed]

    @property
    def sound(self) -> bool:
        """Verifiers are sound iff every breaking mutant is killed AND every
        preserving mutant is spared."""
        return not self.survived_breaking and not self.false_killed_preserving

    def to_dict(self) -> dict:
        return {"sound": self.sound, "mutation_score": self.mutation_score,
                "survived_breaking": [m.id for m in self.survived_breaking],
                "false_killed_preserving": [m.id for m in self.false_killed_preserving],
                "n_mutants": len(self.outcomes)}


_SYSTEM = (
    "You generate MUTANTS of a golden solution to test a verifier's discriminating power.\n"
    "Produce two groups:\n"
    "- BREAKING: small semantic changes that make the solution WRONG (off-by-one, dropped "
    "guard, inverted condition, wrong bound). A good verifier MUST catch these.\n"
    "- PRESERVING: semantics-PRESERVING rewrites that are still CORRECT (equivalent idiom, "
    "iterative<->recursive, renamed locals, reordered independent statements). A good "
    "verifier MUST accept these — they represent valid alternative solutions.\n"
    'Return ONLY JSON: {"breaking": [{"id":"b1","description":"...","diff":"..."}], '
    '"preserving": [{"id":"p1","description":"...","diff":"..."}]}.'
)


def build_mutant_prompt(golden_diff: str, truth_md: str, *, n: int = 8) -> tuple[str, str]:
    user = (f"# TRUTH.md\n{truth_md}\n\n# Golden solution diff\n```diff\n{golden_diff[:14000]}\n```\n\n"
            f"Produce about {n} BREAKING and {n} PRESERVING mutants. Return the JSON.")
    return _SYSTEM, user


def parse_mutants(text: str) -> list[Mutant]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return []
    out: list[Mutant] = []
    for kind, key in ((BREAKING, "breaking"), (PRESERVING, "preserving")):
        for i, item in enumerate(d.get(key) or []):
            if not isinstance(item, dict):
                continue
            out.append(Mutant(id=str(item.get("id") or f"{kind[0]}{i}"), kind=kind,
                              description=str(item.get("description") or ""),
                              diff=str(item.get("diff") or "")))
    return out


def generate_mutants(golden_diff: str, truth_md: str, client: ModelClient, *,
                     n: int = 8, max_tokens: int = 6144) -> list[Mutant]:
    system, user = build_mutant_prompt(golden_diff, truth_md, n=n)
    return parse_mutants(client.complete(system, user, max_tokens=max_tokens).text)


def validate_verifiers(mutants: list[Mutant],
                       runner: Callable[[Mutant], bool]) -> MutationReport:
    """Run each mutant through the verifier set. ``runner(mutant) -> killed`` applies
    the mutant to the golden and returns whether the verifiers REJECTED it."""
    return MutationReport(outcomes=[MutantOutcome(m, bool(runner(m))) for m in mutants])
