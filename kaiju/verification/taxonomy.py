"""The frozen Concern Taxonomy — the single source of truth for *what* a complete
trajectory verification must cover, and *who* (which layer) owns each concern.

This replaces the "..." placeholder the adversarial review flagged: the set of
concerns is enumerated in full and versioned. Two invariants make the framework's
"complete + non-duplicative" claim machine-checkable (see the registry lint in
``tests/test_verification_core.py``):

  * COMPLETENESS  — every concern in the taxonomy is assigned exactly one owner.
  * NON-DUPLICATION — no concern is owned by two layers; a deterministic check and
    a rubric criterion never target the same concern id.

Each concern carries the *phase* that implements its check. A concern whose phase
has not yet shipped is still registered here (so coverage is provably complete)
and is emitted as ``PENDING`` at evaluation time — honest about what is and isn't
yet enforced, rather than silently uncovered.

Concern id grammar: ``L{layer}.{SHORT_NAME}``.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

TAXONOMY_VERSION = "1.0.0"


class Layer(enum.IntEnum):
    """Verification layer. Deterministic layers gate; the rubric layer scores."""

    LEGITIMACY = 0   # deterministic hard gate: is the trajectory legitimate at all?
    STRUCTURE = 1    # deterministic: per-stage/per-module outcome + structure
    JUDGMENT = 2     # LLM-judge rubric: residual qualitative calls (different model)


class Owner(enum.Enum):
    DETERMINISTIC = "deterministic"
    RUBRIC = "rubric"


# Stage identifiers (match the pipeline's three stages).
STAGE_DRAFT = "stage1"
STAGE_LINT = "stage2"
STAGE_TEST = "stage3"
ALL_STAGES = (STAGE_DRAFT, STAGE_LINT, STAGE_TEST)


@dataclass(frozen=True)
class Concern:
    """One atomic verification concern, owned by exactly one layer."""

    id: str
    title: str
    layer: Layer
    owner: Owner
    gating: bool                 # a FAIL here rejects the trajectory (vs. graded-only)
    phase: str                   # build phase that implements the check ("P1".."P6")
    stages: tuple[str, ...] = ALL_STAGES
    weight: float = 1.0          # contribution to the graded score
    rationale: str = ""

    # NOTE: whether a concern is actually ENFORCED is determined by the presence
    # of a check in ``deterministic.CHECKS`` (single source of truth), not by
    # ``phase`` — ``phase`` is metadata for when the check shipped / is planned.


# --------------------------------------------------------------------------- #
# Layer 0 — legitimacy gate (deterministic). A FAIL here means the trajectory
# is not a trustworthy solve regardless of its score.
# --------------------------------------------------------------------------- #
_LAYER0 = [
    Concern(
        "L0.OUTCOME_SIGNAL_TRUSTWORTHY",
        "Every stage's eval produced a real score (not an infra crash / false-zero)",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="eval_status must be OK, not PYTEST_INFRA_ERROR / INVENTORY_MISMATCH / "
                  "not_run — an infra crash misread as 0/N is not a model failure.",
    ),
    Concern(
        "L0.PIPELINE_COMPLETE",
        "pipeline_results.json present with all three stage records and no fatal error",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="A partial/crashed run is not verifiable as a complete solve.",
    ),
    Concern(
        "L0.MODULES_DONE",
        "Every attempted module completed (.done, no .needs_retry / error.log)",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="A stage with an unresolved module did incomplete work.",
    ),
    Concern(
        "L0.MODULE_ARTIFACTS_PRESENT",
        "Each done module has its artifact quartet (turns/llm_history/output.json/aider.log)",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="A .done module missing output.json means the idempotent backstop "
                  "skipped it with zero turns — a silent gap.",
    ),
    Concern(
        "L0.OUTPUT_JSON_INTEGRITY",
        "Each module's output.json parses and carries cost/metrics",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="Cost aggregation and patch attribution depend on it.",
    ),
    Concern(
        "L0.TEST_FILES_UNTOUCHED",
        "The agent's patch does not modify test files or scoring/config files",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="Editing tests / conftest / pytest config / package.json test script "
                  "is the canonical test-neutering cheat.",
    ),
    Concern(
        "L0.DENOMINATOR_SANITY",
        "Each stage scored against a non-empty frozen test inventory",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="num_tests==0 means the denominator collapsed (no real scoring).",
    ),
    Concern(
        "L0.INDEPENDENT_REEXEC",
        "Outcome re-verified by an independent clean-container re-run of the frozen tests",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P3",
        stages=(STAGE_TEST,),
        rationale="Do not trust the recorded eval; a fabricated 'tests passed' turn or an "
                  "inherited exit-4 must not survive an independent re-run.",
    ),
    Concern(
        "L0.FABRICATED_OBSERVATION",
        "Every recorded observation is caused by a real recorded action (no fabricated result)",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P2",
        rationale="A tampered/fabricated trajectory carries observations with no causing "
                  "action, or an observation whose tool disagrees with its action. Verified "
                  "0 on 782 real modules, so this gates cleanly.",
    ),
    Concern(
        "L0.SIDE_CHANNEL_CLEAN",
        "Agent derived the solution — no golden reference_commit / .kaiju breadcrumb leak",
        Layer.LEGITIMACY, Owner.DETERMINISTIC, gating=True, phase="P3",
        rationale="Agent must derive, not retrieve (research: up to 63% of unhardened 'passes' "
                  "are retrieval). Scans the produced patch + recorded conversation for the "
                  "golden reference_commit SHA and the .kaiju breadcrumb; 0 on real runs. "
                  "Network-egress DENIAL is a harness-config concern (P3-infra), separate from "
                  "this post-hoc detection.",
    ),
]

# --------------------------------------------------------------------------- #
# Layer 1 — per-stage outcome + structure (deterministic).
# --------------------------------------------------------------------------- #
_LAYER1 = [
    Concern(
        "L1.STAGE_DIRS_PRESENT",
        "Each of the three stages ran and produced at least one module",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=True, phase="P1",
    ),
    Concern(
        "L1.DRAFT_MODULES_ADDRESSED",
        "The draft stage produced a non-empty patch for its target modules",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=True, phase="P1",
        stages=(STAGE_DRAFT,),
        rationale="A module left empty was not implemented. (Full match against a "
                  "persisted target-module manifest lands in P2.)",
    ),
    Concern(
        "L1.NO_REGRESSION",
        "Later stages do not reduce the passing-test count (pass_to_pass proxy)",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=True, phase="P1",
        rationale="Lint/test refinement must not regress already-passing behavior.",
    ),
    Concern(
        "L1.TEST_STAGE_OUTCOME",
        "The final (test-refine) stage reached its recorded pass rate",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=False, phase="P1",
        stages=(STAGE_TEST,),
        rationale="Graded, not gating: a legitimate partial solve still verifies as a "
                  "trajectory; the score reflects the outcome.",
    ),
    Concern(
        "L1.STAGE_HANDOFF_CONTINUITY",
        "Each stage built on the previous stage's tree (cumulative, not reset)",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=False, phase="P1",
    ),
    Concern(
        "L1.LINT_MONOTONE",
        "The lint stage reduced targeted lint findings and edited the flagged locations",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=False, phase="P2b",
        stages=(STAGE_LINT,),
        rationale="Needs the structured per-turn lint feedback record (P2b) — same blocker "
                  "as FEEDBACK_CAUSALITY.",
    ),
    Concern(
        "L1.FEEDBACK_CAUSALITY",
        "After a failing test, the next edit touches the implicated code path",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=False, phase="P2b",
        stages=(STAGE_TEST,),
        rationale="Distinguishes methodical debugging from a lucky guess. Blocked on a "
                  "reliable per-turn pass/fail feedback record: the recorded feedback carries "
                  "the test COMMAND but not a language-uniform pass/fail signal, and eval is "
                  "stage-level not per-module — needs the P2b structured feedback record.",
    ),
    Concern(
        "L1.TOOLCALL_CORRECTNESS",
        "Tool calls are well-formed: no hallucinated tools, no missing ids, edits apply",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=False, phase="P2",
        rationale="From the structured OpenHands event log (output.json history): "
                  "BFCL/AgentEvals-style tool-call integrity.",
    ),
    Concern(
        "L1.HELDOUT_GAP",
        "The visible-vs-held-out pass gap is within tolerance (overfit signal)",
        Layer.STRUCTURE, Owner.DETERMINISTIC, gating=False, phase="P5",
        stages=(STAGE_TEST,),
        rationale="Needs generated held-out tests from the mutation engine (P5).",
    ),
]

# --------------------------------------------------------------------------- #
# Layer 2 — judgment (LLM-judge rubric, a DIFFERENT model than the agent). These
# are the residual qualitative calls no rule can decide; owner = RUBRIC so the
# registry lint proves they never duplicate a deterministic concern.
# --------------------------------------------------------------------------- #
_LAYER2 = [
    Concern(
        "L2.INTENT_FIDELITY",
        "The final diff genuinely implements the task intent (not test special-casing)",
        Layer.JUDGMENT, Owner.RUBRIC, gating=False, phase="P6",
    ),
    Concern(
        "L2.REASONING_FAITHFULNESS",
        "Stated reasoning is consistent with the actual edits and feedback (no post-hoc)",
        Layer.JUDGMENT, Owner.RUBRIC, gating=False, phase="P6",
    ),
    Concern(
        "L2.STAGE_LEGITIMACY",
        "Each refinement stage did real, feedback-responsive work",
        Layer.JUDGMENT, Owner.RUBRIC, gating=False, phase="P6",
    ),
    Concern(
        "L2.ORACLE_STRENGTH",
        "The solution is correct beyond the frozen tests (not a degenerate/weak-oracle pass)",
        Layer.JUDGMENT, Owner.RUBRIC, gating=False, phase="P6",
        rationale="The judgment complement to the deterministic held-out gap; the frozen "
                  "tests may under-constrain the spec.",
    ),
]


TAXONOMY: tuple[Concern, ...] = tuple(_LAYER0 + _LAYER1 + _LAYER2)


# --------------------------------------------------------------------------- #
# Scoring DIMENSION — what a check's outcome means for the verdict.
#
# This verifier judges the TRAJECTORY, not the code's correctness (the repo's own
# test suite / commit0 evaluate* owns that). So a check contributes in exactly one
# of three ways:
#   * "legitimacy" — a gating check: a FAIL quarantines the trajectory.
#   * "process"    — trajectory-quality: feeds the graded score.
#   * "honesty"    — the check compares the SOLUTION to the golden purely to expose
#                    GAMING (hardcoded/special-cased outputs) or overfit. It is
#                    non-gating and is NOT folded into the score; it drives a
#                    separate boolean honesty signal. Marking a solution down for
#                    being honestly incomplete is eval's job, not ours.
# --------------------------------------------------------------------------- #
_HONESTY_CONCERNS = frozenset({
    "L1.HELDOUT_GAP",       # visible-vs-held-out generalization (overfit lens)
    "L2.INTENT_FIDELITY",   # genuine implementation vs test special-casing (cheat lens)
    "L2.ORACLE_STRENGTH",   # correct beyond the frozen tests (overfit lens)
})
# concern ids whose FAIL specifically indicates the trajectory GAMED the tests
# (as opposed to generalizing poorly / being incomplete).
GAMING_CONCERNS = frozenset({"L2.INTENT_FIDELITY"})
# concern ids whose FAIL indicates overfit / weak generalization.
OVERFIT_CONCERNS = frozenset({"L1.HELDOUT_GAP", "L2.ORACLE_STRENGTH"})


def dimension_of(concern) -> str:
    """'legitimacy' (gates) | 'process' (scored) | 'honesty' (non-scored lens)."""
    cid = getattr(concern, "id", concern)
    if cid in _HONESTY_CONCERNS:
        return "honesty"
    return "legitimacy" if getattr(concern, "gating", False) else "process"


def by_id(concern_id: str) -> Concern:
    for c in TAXONOMY:
        if c.id == concern_id:
            return c
    raise KeyError(concern_id)
