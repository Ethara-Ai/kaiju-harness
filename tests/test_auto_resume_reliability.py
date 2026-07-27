"""Auto-resume reliability — no-limbo markers + progress-aware resume rounds.

Pins the hardening that guarantees a finished trajectory contains ONLY `.done`
modules (or fails loudly):

1. `mark_module_started` writes an in-progress `.needs_retry` the moment a
   module is selected to run, and `_mark_module_done` replaces it with `.done`
   — so a kill at ANY instant leaves one of the two markers. The limbo state
   (neither marker) is impossible by construction, not merely swept after the
   fact.
2. Every language runner calls `mark_module_started` after every
   `_is_module_done` skip check (parity pinned on source text, same idiom as
   the QC bridge-parity tests).
3. Every pipeline script's auto-resume loop is PROGRESS-AWARE: rounds that
   heal at least one module reset the no-progress budget (so large transient
   batches converge instead of being cut off at a fixed round count), pauses
   escalate while stuck, a hard round cap bounds the loop, and leftovers
   strict-block the stage.

CI-safe: no docker, no network, no agent imports beyond `_module_retry`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from agent._module_retry import mark_module_started

REPO = Path(__file__).resolve().parents[1]

RUNNERS = [
    "agent/run_agent.py", "agent/run_agent_no_rich.py", "agent/run_agent_c.py",
    "agent/run_agent_go.py", "agent/run_agent_java.py", "agent/run_agent_js.py",
    "agent/run_agent_ts.py", "agent/run_cpp_agent.py", "agent/run_rust_agent.py",
]

PIPELINES = [
    "run_pipeline.sh", "run_pipeline_c.sh", "run_pipeline_cpp.sh",
    "run_pipeline_go.sh", "run_pipeline_java.sh", "run_pipeline_js.sh",
    "run_pipeline_rust.sh", "run_pipeline_ts.sh",
]


# ---------------------------------------------------------------------------
# 1. mark_module_started semantics
# ---------------------------------------------------------------------------
class TestMarkModuleStarted:
    def test_writes_in_progress_marker(self, tmp_path):
        mod = tmp_path / "mymodule"
        mark_module_started(mod)
        marker = mod / ".needs_retry"
        assert marker.is_file()
        assert "in-progress" in marker.read_text()

    def test_never_overwrites_a_real_error_breadcrumb(self, tmp_path):
        mod = tmp_path / "mymodule"
        mod.mkdir()
        (mod / ".needs_retry").write_text("TransientLLMError: timed out")
        mark_module_started(mod)
        assert (mod / ".needs_retry").read_text() == "TransientLLMError: timed out"

    def test_done_replaces_started_no_ambiguous_state(self, tmp_path):
        # Simulate the full lifecycle: started -> done. _mark_module_done in
        # every runner unlinks .needs_retry before touching .done; verify the
        # python runner's implementation honors that contract.
        from commit0.harness import _optional_dep_stubs  # noqa: F401 — aider stubs
        from agent.run_agent import _mark_module_done
        mod = tmp_path / "mymodule"
        mark_module_started(mod)
        _mark_module_done(mod)
        assert (mod / ".done").is_file()
        assert not (mod / ".needs_retry").exists(), "done module must not stay marked"

    def test_kill_window_always_leaves_a_marker(self, tmp_path):
        # The constructive no-limbo property: at every point in the module
        # lifecycle, at least one marker exists.
        from commit0.harness import _optional_dep_stubs  # noqa: F401
        from agent.run_agent import _mark_module_done
        mod = tmp_path / "mymodule"
        mark_module_started(mod)               # kill here -> .needs_retry ✓
        assert (mod / ".needs_retry").exists()
        (mod / "aider.log").write_text("...")  # kill mid-turn -> .needs_retry ✓
        assert (mod / ".needs_retry").exists()
        _mark_module_done(mod)                 # kill after -> .done ✓
        assert (mod / ".done").exists()


# ---------------------------------------------------------------------------
# 2. cross-language parity (source-text pins, repo idiom)
# ---------------------------------------------------------------------------
class TestParity:
    def test_every_runner_marks_module_start_after_every_done_check(self):
        for f in RUNNERS:
            src = (REPO / f).read_text()
            checks = len(re.findall(r'if .*_is_module_done\(', src))
            marks = src.count("mark_module_started(")
            assert marks == checks and marks > 0, (
                f"{f}: {checks} done-checks but {marks} mark_module_started calls "
                "— every selected module must get the no-limbo marker")
            assert "from agent._module_retry import" in src and "mark_module_started" in src

    def test_every_pipeline_has_progress_aware_auto_resume(self):
        for f in PIPELINES:
            src = (REPO / f).read_text()
            for needle in ("KAIJU_AUTO_RESUME_MAX_ROUNDS", "_noprog", "no-progress",
                           "_sweep_limbo_modules", "FATAL (strict-blocking)"):
                assert needle in src, f"{f} missing {needle!r}"
            # the quoting bug class must never return
            assert '\\"$_ld\\"' not in src and "\\\"$log_dir\\\"" not in src


# ---------------------------------------------------------------------------
# 3. functional test of the progress-aware loop (real shell, stubbed agent)
# ---------------------------------------------------------------------------
class TestAutoResumeLoopFunctional:
    def _run_loop(self, tmp_path, n_modules, heal_per_round, rounds_budget=2,
                  hard_cap=12, go_crazy="false"):
        """Extract the REAL _auto_resume_agent from run_pipeline_rust.sh and
        drive it with a stub agent that heals `heal_per_round` modules per
        invocation. Returns (exit_code, stdout, leftover_count)."""
        ld = tmp_path / "ld"
        for i in range(n_modules):
            m = ld / f"mod{i}"
            m.mkdir(parents=True)
            (m / "aider.log").write_text("x")
            (m / ".needs_retry").write_text("transient")
        fn = subprocess.run(
            ["sed", "-n", "/_auto_resume_agent() {/,/^}/p",
             str(REPO / "run_pipeline_rust.sh")],
            capture_output=True, text=True).stdout
        sweep = subprocess.run(
            ["sed", "-n", "/_sweep_limbo_modules() {/,/^}/p",
             str(REPO / "run_pipeline_rust.sh")],
            capture_output=True, text=True).stdout
        heal = tmp_path / "heal.sh"
        heal.write_text(
            "#!/bin/bash\n"
            f"for m in $(find {ld} -name .needs_retry | sort | head -{heal_per_round}); do\n"
            "  rm -f \"$m\"; touch \"$(dirname $m)/.done\"\n"
            "done\n")
        heal.chmod(0o755)
        script = tmp_path / "driver.sh"
        script.write_text(f"""#!/bin/bash
log() {{ echo "$@"; }}
sleep() {{ :; }}                              # no real waiting in tests
watchdog_run() {{ wait "$1" 2>/dev/null; return $?; }}
INACTIVITY_TIMEOUT=1; STAGE_TIMEOUT=0; MAX_WALL_TIME=0
AGENT_ELAPSED=0; GO_CRAZY={go_crazy}
KAIJU_AUTO_RESUME_ROUNDS={rounds_budget}
KAIJU_AUTO_RESUME_MAX_ROUNDS={hard_cap}
KAIJU_AUTO_RESUME_PAUSE=1
{sweep}
{fn}
_auto_resume_agent "{ld}" "{tmp_path}/agent.log" -- {heal}
echo "LOOP_EXITED_OK"
""")
        script.chmod(0o755)
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                           timeout=60)
        leftover = len(list(ld.rglob(".needs_retry")))
        return r.returncode, r.stdout, leftover

    def test_progress_resets_budget_and_converges(self, tmp_path):
        # 6 broken modules, healing 1/round, no-progress budget of only 2:
        # a fixed-round loop would abort after 2; progress-aware must run 6
        # rounds and converge to zero leftovers.
        rc, out, leftover = self._run_loop(tmp_path, n_modules=6, heal_per_round=1)
        assert leftover == 0, out
        assert rc == 0 and "LOOP_EXITED_OK" in out
        assert "AUTO-RESUME succeeded" in out
        assert out.count("AUTO-RESUME") >= 6

    def test_genuinely_stuck_strict_blocks(self, tmp_path):
        # healing 0/round: after the no-progress budget, strict-block exit 1
        # with the leftover module names in the warning.
        rc, out, leftover = self._run_loop(tmp_path, n_modules=2, heal_per_round=0)
        assert rc == 1 and "LOOP_EXITED_OK" not in out
        assert "FATAL (strict-blocking)" in out
        assert leftover == 2
        assert "mod0" in out and "mod1" in out  # names surfaced for the operator

    def test_go_crazy_bypasses_strict_block(self, tmp_path):
        rc, out, leftover = self._run_loop(tmp_path, n_modules=1, heal_per_round=0,
                                           go_crazy="true")
        assert rc == 0 and "LOOP_EXITED_OK" in out
        assert "WARNING" in out and leftover == 1

    def test_hard_cap_bounds_pathological_progress(self, tmp_path):
        # 30 modules healing 1/round would take 30 rounds; the hard cap (5)
        # must stop it and strict-block.
        rc, out, leftover = self._run_loop(tmp_path, n_modules=30, heal_per_round=1,
                                           hard_cap=5)
        assert rc == 1
        assert leftover == 25
