"""Executes the generated pytest against the golden, stub, and agent solutions and
feeds the result into `L1.HELDOUT_GAP` — the generated suite is the held-out oracle
that tests behavior beyond the frozen tests, so a solution that passes the frozen
tests but FAILS the generated ones is overfit / exploiting a weak oracle.

Container/runtime-bound (needs the repo's deps installed); the comparison check
reads the stored per-run results.json and is N/A when absent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .schemas import CheckStatus
from .trajectory import TrajectoryBundle

_GOLDEN_MIN = 0.8      # golden must pass most generated tests (else tests unsound)
_STUB_MAX = 0.2        # stub must fail most (else tests don't discriminate)
_OVERFIT_TOL = 0.1     # solution may trail golden by at most this before it's a gap


def load_pytest_results(run_dir: str | Path) -> dict | None:
    from . import layout
    p = layout.pytest_results_path(layout.uuid_root_of(run_dir), run_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _run_one(repo, commit, patch, code, python, log_path: Path) -> dict:
    from .pytest_runner import run_pytest_in_dir, checkout_solution, remove_worktree
    wt = checkout_solution(repo, commit, patch=patch)
    if wt is None:
        return {"status": "INFRA", "detail": "worktree failed", "per_test": {}}
    res = run_pytest_in_dir(wt, code, python=python)
    remove_worktree(repo, wt)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(res.output, encoding="utf-8")
    d = res.to_dict(); d["log"] = str(log_path); d["per_test"] = res.per_test
    return d


def _load_or_run_anchors(uuid_root, repo, base, ref, code, python) -> dict:
    """Golden+stub per-test outcomes — per TASK, cached at pytest/anchors.json."""
    from . import layout
    ap = layout.pytest_anchors_path(uuid_root)
    if ap.exists():
        try:
            return json.loads(ap.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    logs = layout.pytest_task_logs_dir(uuid_root)
    anchors = {
        "golden": _run_one(repo, ref, None, code, python, logs / "golden.log"),
        "stub": _run_one(repo, base, None, code, python, logs / "stub.log"),
    }
    ap.parent.mkdir(parents=True, exist_ok=True)
    ap.write_text(json.dumps(anchors, indent=2), encoding="utf-8")
    return anchors


def run_generated_pytest(run_dir: str | Path, *, python: str = "python") -> dict | None:
    """Golden+stub ANCHORS (per task, cached) + the SOLUTION run (per run), then the
    per-test sound-subset analysis. Stores results/pytest/<run>/results.json."""
    from . import layout
    from .build_inputs import _entries, _find_repo
    from .orchestrate import load_pytest_code
    from .trajectory import load_trajectory

    run_dir = Path(run_dir).resolve()
    uuid_root = layout.uuid_root_of(run_dir)
    code = load_pytest_code(uuid_root)
    entry = _entries(uuid_root)
    if not code.strip() or not entry:
        return None
    repo = _find_repo(uuid_root, entry)
    if repo is None:
        return None
    base, ref = entry.get("base_commit"), entry.get("reference_commit")

    anchors = _load_or_run_anchors(uuid_root, repo, base, ref, code, python)
    patch = ""
    st3 = load_trajectory(run_dir).stages.get("stage3")
    if st3 and st3.model_changes_diffs:
        patch = st3.model_changes_diffs[0]
    solution = _run_one(repo, base, patch or None, code, python,
                        layout.pytest_logs_dir(uuid_root, run_dir) / "solution.log")

    per_test = {"golden": (anchors.get("golden") or {}).get("per_test") or {},
                "stub": (anchors.get("stub") or {}).get("per_test") or {},
                "solution": solution.get("per_test") or {}}
    out = {"solution": {k: v for k, v in solution.items() if k != "per_test"},
           "anchors": {"golden": {k: v for k, v in (anchors.get("golden") or {}).items()
                                  if k != "per_test"},
                       "stub": {k: v for k, v in (anchors.get("stub") or {}).items()
                                if k != "per_test"}},
           "analysis": _analyze(per_test)}
    rp = layout.pytest_results_path(uuid_root, run_dir)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def store_pytest_results(run_dir: str | Path, results: dict) -> Path:
    """Test hook: write a results.json at the canonical per-run path."""
    from . import layout
    p = layout.pytest_results_path(layout.uuid_root_of(run_dir), run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return p


def _analyze(per_test: dict) -> dict:
    """Per-test golden/stub filtering (EvalPlus/SWE-bench methodology):
      sound subset S = tests that PASS on golden AND FAIL on stub;
      wrong-oracle  = tests that do NOT pass on golden (bad expected value);
      non-discriminating = tests that PASS on stub (assert nothing meaningful).
    The candidate is scored ONLY on S, and reported as a gap vs golden (=1.0 on S)."""
    golden = per_test.get("golden") or {}
    stub = per_test.get("stub") or {}
    solution = per_test.get("solution") or {}
    all_tests = set(golden) | set(stub) | set(solution)

    # discriminating on the stub = the test does NOT pass there (fail OR error — a
    # stub that raises makes the test error, which still proves it discriminates).
    sound = sorted(t for t in all_tests
                   if golden.get(t) == "pass" and stub.get(t) in ("fail", "error"))
    wrong_oracle = sorted(t for t in all_tests if golden.get(t) != "pass")
    non_discriminating = sorted(t for t in all_tests if stub.get(t) == "pass")

    sol_pass = sum(1 for t in sound if solution.get(t) == "pass")
    sol_rate = (sol_pass / len(sound)) if sound else None
    return {
        "n_total": len(all_tests),
        "n_sound": len(sound),
        "sound_subset": sound,
        "wrong_oracle_dropped": wrong_oracle,      # e.g. tests that fail on golden
        "non_discriminating_dropped": non_discriminating,
        "solution_pass_on_sound": sol_rate,        # candidate score on validated tests
        "gap_vs_golden": (round(1.0 - sol_rate, 4) if sol_rate is not None else None),
    }


_MIN_SOUND = 3   # need at least this many sound tests to certify anything


def heldout_gap_check(b: TrajectoryBundle):
    from .deterministic import _Outcome
    results = load_pytest_results(b.run_dir)
    if not results:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        "no generated-pytest results (run the pytest executor in-runtime)")
    a = results.get("analysis") or {}
    n_sound = a.get("n_sound") or 0
    sol_rate = a.get("solution_pass_on_sound")
    ev = {"n_total": a.get("n_total"), "n_sound": n_sound,
          "wrong_oracle_dropped": a.get("wrong_oracle_dropped"),
          "non_discriminating_dropped": a.get("non_discriminating_dropped"),
          "solution_pass_on_sound": sol_rate, "gap_vs_golden": a.get("gap_vs_golden")}
    # the suite can only certify if enough tests survived the golden+stub soundness gate
    if n_sound < _MIN_SOUND or sol_rate is None:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        f"only {n_sound} sound test(s) (passes-golden ∧ fails-stub); "
                        "too weak to certify — inconclusive", ev)
    # candidate is scored ONLY on the sound subset, as a gap vs golden (golden==1.0)
    if a.get("gap_vs_golden", 0) > _OVERFIT_TOL:
        return _Outcome(CheckStatus.FAIL,
                        f"solution passes {sol_rate:.2f} of {n_sound} SOUND held-out tests "
                        f"(gap {a.get('gap_vs_golden')} vs golden) — overfit / incomplete", ev)
    return _Outcome(CheckStatus.PASS,
                    f"solution passes {sol_rate:.2f} of {n_sound} sound held-out tests "
                    "(matches golden)", ev)
