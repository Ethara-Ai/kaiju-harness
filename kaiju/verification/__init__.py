"""Trajectory verification framework.

Verifies the correctness and legitimacy of a produced agent trajectory (the
3-stage draft -> lint-refine -> test-refine solve recorded under
``outputs/<uuid>/runs/<model>/agent/run_<N>/``) against a truth-anchored,
pre-registered standard.

Design and rationale live in ``TRAJECTORY_VERIFICATION_PLAN.md`` (Part 13). The
package is built in phases; this module is **P1 — the deterministic evaluator
core** (no LLM): the frozen Concern Taxonomy, the artifact reader, the Layer-0
legitimacy gate + Layer-1 per-stage/per-module structural checks, and the report
writer. Later phases add observability (P2), independent re-execution + coverage
tiers (P3), TRUTH.md authoring (P4), the mutation engine (P5), and the rubric
judge (P6).

Public API:
    from kaiju.verification import verify_run, TAXONOMY
    report = verify_run("outputs/<uuid>/runs/<model>/agent/run_1")
"""
from __future__ import annotations

from .taxonomy import TAXONOMY, Concern, Layer, Owner
from .schemas import CheckResult, CheckStatus, Gate, VerificationReport
from .trajectory import TrajectoryBundle, load_trajectory
from .evaluator import verify_run
from .model_client import ModelClient, MockClient, default_judge_client
from .truth import TruthInputs, generate_truth
from .rubric import Rubric, generate_rubric
from .judge import judge_trajectory, JudgeResult
from .orchestrate import build_bundle, freeze_bundle, load_bundle, judge_run, meta_verify

__all__ = [
    "TAXONOMY", "Concern", "Layer", "Owner",
    "CheckResult", "CheckStatus", "Gate", "VerificationReport",
    "TrajectoryBundle", "load_trajectory", "verify_run",
    # non-deterministic half (P4-P6)
    "ModelClient", "MockClient", "default_judge_client",
    "TruthInputs", "generate_truth", "Rubric", "generate_rubric",
    "judge_trajectory", "JudgeResult",
    "build_bundle", "freeze_bundle", "load_bundle", "judge_run", "meta_verify",
]
