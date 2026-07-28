"""Orchestration for the non-deterministic half (P4–P6): generate the
pre-registered bundle (TRUTH.md + rubric) BEFORE the trajectory, meta-verify it
with the mutation engine, then run the judge on a produced trajectory and store
the verdicts — all under ``<uuid>/verification/``.

    <uuid>/verification/
      TRUTH.md
      verifiers/rubric/rubric.json
      meta_verification.json
      results/<model>/<run_N>/rubric_results.json
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .digest import build_trajectory_digest
from .judge import JudgeResult, judge_trajectory
from .model_client import (
    ModelClient, default_judge_client, default_generation_client, judge_client_for_run)
from .mutation import MutationReport, generate_mutants, validate_verifiers, Mutant
from .predicates import Predicate, generate_predicates
from .pytest_gen import generate_pytest
from .rubric import Rubric, generate_rubric
from .trajectory import load_trajectory
from .truth import TruthInputs, TruthDoc, generate_truth


def uuid_root_of(run_dir: str | Path) -> Path:
    from . import layout
    return layout.uuid_root_of(run_dir)


from . import layout

# Back-compat alias; the canonical paths live in layout.py.
def verification_dir(uuid_root: str | Path) -> Path:
    return layout.vroot(uuid_root)


@dataclass
class Bundle:
    truth: TruthDoc
    rubric: Rubric
    predicates: list[Predicate] = None  # generated task-specific static checks
    pytest_code: str = ""               # generated EXECUTABLE pytest (real test functions)

    def __post_init__(self):
        if self.predicates is None:
            self.predicates = []


# --------------------------------------------------------------------------- #
# P4 + P6-gen + generated deterministic: build & freeze the pre-registered bundle
# --------------------------------------------------------------------------- #
def build_bundle(inp: TruthInputs, client: ModelClient | None = None) -> Bundle:
    client = client or default_generation_client()
    truth = generate_truth(inp, client)
    rubric = generate_rubric(truth.text, client)
    predicates = generate_predicates(truth.text, client)
    pytest_code = generate_pytest(truth.text, client, stub_files=inp.stub_files)
    return Bundle(truth=truth, rubric=rubric, predicates=predicates, pytest_code=pytest_code)


def freeze_bundle(uuid_root: str | Path, bundle: Bundle) -> dict[str, str]:
    truth_p = layout.truth_path(uuid_root)
    rubric_p = layout.rubric_path(uuid_root)
    pred_p = layout.predicates_path(uuid_root)
    pytest_p = layout.pytest_code_path(uuid_root)
    for p in (truth_p, rubric_p, pred_p, pytest_p):
        p.parent.mkdir(parents=True, exist_ok=True)
    truth_p.write_text(bundle.truth.text, encoding="utf-8")
    rubric_p.write_text(json.dumps(bundle.rubric.to_dict(), indent=2), encoding="utf-8")
    pred_p.write_text(json.dumps([p.to_dict() for p in bundle.predicates], indent=2),
                      encoding="utf-8")
    if bundle.pytest_code.strip():
        pytest_p.write_text(bundle.pytest_code, encoding="utf-8")
    from .coverage import emit_coverage
    cov_p = emit_coverage(uuid_root)
    return {"truth": str(truth_p), "rubric": str(rubric_p), "predicates": str(pred_p),
            "pytest": str(pytest_p), "coverage": str(cov_p)}


def load_pytest_code(uuid_root: str | Path) -> str:
    p = layout.pytest_code_path(uuid_root)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def load_predicates(uuid_root: str | Path) -> list[Predicate]:
    p = layout.predicates_path(uuid_root)
    if not p.exists():
        return []
    return [Predicate.from_dict(d) for d in json.loads(p.read_text(encoding="utf-8"))]


def load_bundle(uuid_root: str | Path) -> tuple[str, Rubric] | None:
    truth_p, rubric_p = layout.truth_path(uuid_root), layout.rubric_path(uuid_root)
    if not (truth_p.exists() and rubric_p.exists()):
        return None
    rubric = Rubric.from_dict(json.loads(rubric_p.read_text(encoding="utf-8")))
    return truth_p.read_text(encoding="utf-8"), rubric


# --------------------------------------------------------------------------- #
# P5: meta-verify the bundle (mutation two-sided kill/spare)
# --------------------------------------------------------------------------- #
def meta_verify(golden_diff: str, truth_md: str, runner: Callable[[Mutant], bool],
                client: ModelClient | None = None, *, n: int = 8) -> MutationReport:
    client = client or default_generation_client()
    mutants = generate_mutants(golden_diff, truth_md, client, n=n)
    return validate_verifiers(mutants, runner)


@dataclass
class MetaVerifyResult:
    bundle: Bundle
    mutation: MutationReport
    regenerations: int
    sound: bool


def closed_meta_verify(inp: TruthInputs,
                       runner: Callable[[Mutant, Bundle], bool],
                       client: ModelClient | None = None, *,
                       max_regen: int = 3, n: int = 8) -> MetaVerifyResult:
    """The closed self-validation loop: author the bundle → mutation-meta-verify it
    (kill-breaking / spare-preserving) → if UNSOUND, regenerate and retry, bounded.
    ``runner(mutant, bundle) -> killed`` is the container-bound mutant executor
    (apply the mutant, run the bundle's generated verifiers). A bundle is only fit
    to freeze when ``sound`` — else hard-flag for human review (never silently ship)."""
    client = client or default_generation_client()
    bundle = None
    report = MutationReport()
    for attempt in range(max_regen + 1):
        bundle = build_bundle(inp, client)
        mutants = generate_mutants(inp.golden_diff, bundle.truth.text, client, n=n)
        report = validate_verifiers(mutants, lambda m, _b=bundle: runner(m, _b))
        if report.sound:
            return MetaVerifyResult(bundle, report, attempt, True)
    return MetaVerifyResult(bundle, report, max_regen, False)


# --------------------------------------------------------------------------- #
# P6-run: judge a produced trajectory against the frozen bundle
# --------------------------------------------------------------------------- #
def judge_run(run_dir: str | Path, client: ModelClient | None = None) -> JudgeResult | None:
    """Load the frozen bundle for this task, build the trajectory digest, judge it,
    and store the verdicts under verification/results/<model>/<run>/."""
    run_dir = Path(run_dir).resolve()
    uuid_root = uuid_root_of(run_dir)
    loaded = load_bundle(uuid_root)
    if loaded is None:
        return None
    truth_md, rubric = loaded
    bundle = load_trajectory(run_dir)
    digest = build_trajectory_digest(bundle)
    # Full candidate solution code (same scope as the golden/stub anchors) so
    # cross-file code criteria are judged fairly (RCA 2).
    try:
        from .build_inputs import _entries, _find_repo
        from .solution_code import read_solution_code
        entry = _entries(uuid_root)
        repo = _find_repo(uuid_root, entry) if entry else None
        if repo is not None:
            st3 = bundle.stages.get("stage3")
            patch = st3.model_changes_diffs[0] if (st3 and st3.model_changes_diffs) else None
            from .rubric_anchor import _manifest_exts
            code = read_solution_code(repo, entry["base_commit"], str(entry.get("src_dir") or "."),
                                      patch=patch, exts=_manifest_exts(uuid_root))
            if code:
                digest = f"## Candidate final solution code\n```\n{code[:20000]}\n```\n\n" + digest
    except Exception:
        pass
    if client is None:
        # CROSS-FAMILY judge: judge a Claude run with GPT/Codex and vice versa,
        # so the judge never shares the agent's model (self-preference bias).
        run_model = str(bundle.pipeline_results.get("model")
                        or bundle.pipeline_results.get("model_short") or "")
        client = judge_client_for_run(run_model)
    # Golden + stub ANCHORS (per-task, cached) — validate the rubric before scoring.
    try:
        from .rubric_anchor import run_and_store_anchors
        run_and_store_anchors(uuid_root, truth_md, rubric, client)
    except Exception as exc:
        pass  # anchoring is optional; candidate is still judged
    result = judge_trajectory(truth_md, rubric, digest, client)
    _store_judge(uuid_root, run_dir, result)
    return result


def _store_judge(uuid_root: Path, run_dir: Path, result: JudgeResult) -> Path:
    out = layout.rubric_results_path(uuid_root, run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return out


def load_judge_result(uuid_root: str | Path, run_dir: str | Path) -> JudgeResult | None:
    p = layout.rubric_results_path(uuid_root, run_dir)
    if not p.exists():
        return None
    return JudgeResult.from_dict(json.loads(p.read_text(encoding="utf-8")))
