"""#2 — `L0.INDEPENDENT_REEXEC`: re-run the frozen tests on the agent's patch in a
clean checkout and compare to the RECORDED result, to catch a fabricated pass-claim
or an inherited infra false-zero (never trust the number the run reported).

Two parts:
  * `independent_reexec_check` — the deterministic comparison (recorded vs re-run);
    registered as a check. It reads a stored `reexec_result.json`. When none exists
    (offline verify, no container) it is NOT_APPLICABLE — honest, never a false pass.
  * `run_independent_reexec` — the CONTAINER-BOUND driver that produces the result:
    reset to base_commit → apply the agent patch → run the frozen tests via an
    injected `eval_runner`. The comparison is unit-tested; the driver runs in the
    pipeline (needs the repo + eval harness), so `eval_runner` is injected.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

from .schemas import CheckStatus
from .trajectory import TrajectoryBundle

# Same untrustworthy set as the trust check — a re-run that itself infra-crashed
# tells us nothing (don't overwrite a good recorded result with an infra artifact).
_UNTRUSTWORTHY = {"PYTEST_INFRA_ERROR", "INVENTORY_MISMATCH", "CONTAINER_OR_INFRA_FAILURE",
                  "NOT_RUN", "TEST_SUITE_TIMEOUT", "PATCH_APPLY_FAILED", "INFRA_FAILED",
                  "NO_RESULT", "COMPILE_FAILED"}
_TOLERANCE = 0   # allowed |recorded - rerun| passing-count difference before FAIL


@dataclass
class ReexecResult:
    num_passed: int
    num_tests: int
    status: str = "OK"
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _result_path(run_dir: Path) -> Path:
    from . import layout
    return layout.reexec_path(layout.uuid_root_of(run_dir), run_dir)


def store_reexec_result(run_dir: str | Path, result: ReexecResult) -> Path:
    p = _result_path(Path(run_dir).resolve())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return p


def load_reexec_result(run_dir: str | Path) -> ReexecResult | None:
    p = _result_path(Path(run_dir).resolve())
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return ReexecResult(num_passed=int(d.get("num_passed", 0)),
                        num_tests=int(d.get("num_tests", 0)),
                        status=str(d.get("status") or "OK"), detail=str(d.get("detail") or ""))


def independent_reexec_check(b: TrajectoryBundle):
    from .deterministic import _Outcome
    result = load_reexec_result(b.run_dir)
    if result is None:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        "no independent re-run recorded (run the reexec driver in-container)")
    if result.status.upper() in _UNTRUSTWORTHY or result.num_tests <= 0:
        return _Outcome(CheckStatus.NOT_APPLICABLE,
                        f"independent re-run inconclusive (status={result.status}, "
                        f"tests={result.num_tests})")
    st = b.stages.get("stage3")
    recorded = st.num_passed if st and st.has_score else None
    if recorded is None:
        return _Outcome(CheckStatus.NOT_APPLICABLE, "no recorded test-stage score to compare")
    ev = {"recorded_passed": recorded, "rerun_passed": result.num_passed,
          "rerun_tests": result.num_tests, "rerun_status": result.status}
    if abs(result.num_passed - recorded) > _TOLERANCE:
        return _Outcome(CheckStatus.FAIL,
                        f"recorded pass count ({recorded}) NOT reproduced by an independent "
                        f"re-run ({result.num_passed}) — fabricated/false result", ev)
    return _Outcome(CheckStatus.PASS,
                    f"independent re-run reproduced the recorded result ({recorded} passing)", ev)


# --------------------------------------------------------------------------- #
# Container-bound driver (runs in the pipeline; eval_runner injected)
# --------------------------------------------------------------------------- #
def run_independent_reexec(run_dir: str | Path,
                           eval_runner: Callable[[str], ReexecResult],
                           *, store: bool = True) -> ReexecResult:
    """Produce the independent result. ``eval_runner(agent_patch) -> ReexecResult``
    is the injected backend that, in the pipeline, resets a fresh checkout of
    base_commit, applies the agent's patch, runs the frozen tests, and parses the
    outcome (reusing spec.py/evaluate.py). The agent patch is the trajectory's
    cumulative diff."""
    from .trajectory import load_trajectory
    bundle = load_trajectory(run_dir)
    patches = bundle.all_agent_patches()
    result = eval_runner(patches[0] if patches else "")
    if store:
        store_reexec_result(run_dir, result)
    return result
