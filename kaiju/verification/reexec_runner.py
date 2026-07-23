"""Independent re-execution backend for `run_independent_reexec`.

The genuinely-independent re-run re-invokes the SAME eval the pipeline used — the
recorded ``stageN_eval_artifacts/<repo>/eval.sh`` — in a fresh process and parses
its stdout (``<name>,<runtime>,<passed>/<total>`` per ``evaluate.py``). Reusing the
exact eval means the comparison is apples-to-apples.

``parse_eval_stdout`` (the format parser) is unit-tested; the execution path is
pipeline-bound (eval.sh references the in-container repo + backend), so it runs in
the environment where the eval itself runs.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .reexec import ReexecResult

# `<name>,<runtime>,<passed>/<total>` — the measured result line (evaluate.py:376).
_RESULT_RE = re.compile(r"^([^,\s][^,]*),(\d+(?:\.\d+)?),(\d+)/(\d+)\s*$")
# status-prefixed lines the eval emits for non-measured outcomes.
_STATUS_TOKENS = ("INVENTORY_MISMATCH", "PYTEST_INFRA_ERROR", "COMPILE_FAILED",
                  "CONTAINER_OR_INFRA_FAILURE", "NOT_RUN", "TEST_SUITE_TIMEOUT",
                  "PATCH_APPLY_FAILED", "INFRA_FAILED", "CHEAT_DETECTED")


def parse_eval_stdout(text: str) -> ReexecResult:
    """Parse the eval's stdout into a ReexecResult. Takes the LAST measured result
    line; if a status token dominates and no measured line exists, reports it."""
    status = "OK"
    measured: tuple[int, int] | None = None
    for line in text.splitlines():
        line = line.strip()
        m = _RESULT_RE.match(line)
        if m:
            measured = (int(m.group(3)), int(m.group(4)))
            continue
        tok = line.split(",", 1)[0].strip().upper()
        if tok in _STATUS_TOKENS:
            status = tok
    if measured is None:
        # No measured result line => the eval did not actually score anything
        # (infra/setup failure), NOT a real 0/N. Mark it so the check is inconclusive.
        return ReexecResult(num_passed=0, num_tests=0,
                            status=(status if status != "OK" else "NO_RESULT"),
                            detail="no measured result line in eval output")
    return ReexecResult(num_passed=measured[0], num_tests=measured[1],
                        status=("OK" if status == "OK" else status))


def _stage3_evalsh(run_dir: Path) -> Path | None:
    for evalsh in sorted(run_dir.glob("stage3_eval_artifacts/*/eval.sh")):
        return evalsh
    return None


def eval_runner_via_evalsh(run_dir: str | Path, *, timeout: int = 1800):
    """Return an ``eval_runner(agent_patch) -> ReexecResult`` that re-runs the
    recorded stage-3 eval.sh fresh. (agent_patch is ignored — eval.sh already
    evaluates the agent's branch; a from-base re-apply would substitute a different
    eval.sh that resets + applies the patch.) Runs where eval.sh's paths resolve."""
    run_dir = Path(run_dir)

    def runner(agent_patch: str) -> ReexecResult:
        evalsh = _stage3_evalsh(run_dir)
        if evalsh is None:
            return ReexecResult(0, 0, "NOT_RUN", "no stage3 eval.sh to re-run")
        try:
            r = subprocess.run(["bash", str(evalsh)], capture_output=True, text=True,
                               timeout=timeout, cwd=str(evalsh.parent))
        except (OSError, subprocess.SubprocessError) as e:
            return ReexecResult(0, 0, "CONTAINER_OR_INFRA_FAILURE", str(e))
        return parse_eval_stdout(r.stdout + "\n" + r.stderr)
    return runner


def default_eval_runner(run_dir: str | Path):
    return eval_runner_via_evalsh(run_dir)
