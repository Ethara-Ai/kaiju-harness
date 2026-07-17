"""QC-C7 regression tests for the ATIF v2 exporter (scripts/commit0_to_atif_v2.py).

Pins:
  C7-001 — tool_calls PREFER edit_capture ground truth (output.json applied edits)
           over the fabrication-prone text-regex parser; fabricated prose edits are
           suppressed; legacy (no output.json) runs fall back to the parser.
  C7-002 — ONE `resolved` definition (ALL-PASS) shared by the per-step trajectory
           flag and the per-model reward.json map (no any-pass vs all-pass drift).
  C7-003 — find_pipeline_for's upward walk is bounded by `stop_at` so the kaiju
           layout (no logs_ marker) cannot escape the run and grab a stray file.
  C7-011 — per-turn Metrics attach to EACH assistant turn that incurred cost (from
           output.json usage); the grand-total fallback is TAGGED, never per-turn.

These are language-agnostic: the single exporter handles all 8 languages, so one
invariant per finding covers every language.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import commit0_to_atif_v2 as A  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
# An assistant turn whose PROSE contains a real-looking SEARCH/REPLACE block that
# the text parser WILL match (path src/foo.py). Ground truth (when present) must win.
_LLM_HISTORY_WITH_EDIT = """\
TO LLM 2026-04-28T06:53:00
SYSTEM You are an expert developer.
USER Please fix the bug.
LLM RESPONSE 2026-04-28T06:53:10
ASSISTANT Here is the change.
src/foo.py
```python
<<<<<<< SEARCH
old code
=======
new code
>>>>>>> REPLACE
```
"""


def _write_unit(tmp: Path, llm: str, out_data: dict | None) -> Path:
    unit = tmp / "current" / "unitmod"
    unit.mkdir(parents=True)
    (unit / "llm_history.txt").write_text(llm)
    if out_data is not None:
        (unit / "output.json").write_text(json.dumps(out_data))
    return unit


def _agent_tool_calls(traj):
    calls = []
    for s in traj.steps:
        if s.source == "agent" and s.tool_calls:
            calls.extend(s.tool_calls)
    return calls


def _convert(unit: Path):
    return A.convert_unit(
        unit, task="t", model="gpt-5.5", stage="draft", module="m",
        reward=1.0, resolved=A._resolved_from_pass_rate(1.0),
        stage_pass_rate={"stage1": 1.0},
    )


# ---------------------------------------------------------------------------
# C7-001 — ground-truth preference
# ---------------------------------------------------------------------------
class TestC7_001_GroundTruthEdits:

    def test_ground_truth_edits_win_over_parser(self, tmp_path):
        """output.json applied edit (src/bar.py) beats the parser's prose (src/foo.py)."""
        out = {
            "history": [
                {"kind": "ActionEvent", "tool_name": "file_editor",
                 "llm_response_id": "r1",
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                           "cost_usd": 0.01, "cache_read_tokens": 0},
                 "action": {"kind": "FileEditorAction", "command": "str_replace",
                            "path": "src/bar.py", "old_str": "a", "new_str": "b"}},
            ],
            "metrics": {},
        }
        unit = _write_unit(tmp_path, _LLM_HISTORY_WITH_EDIT, out)
        traj, st = _convert(unit)
        assert st.edit_source == "ground_truth"
        calls = _agent_tool_calls(traj)
        assert len(calls) == 1
        args = calls[0].arguments
        assert args["path"] == "src/bar.py"          # ground truth, NOT parser's src/foo.py
        assert args["old_str"] == "a" and args["new_str"] == "b"
        assert traj.extra["edit_source"] == "ground_truth"

    def test_fabricated_prose_edit_suppressed(self, tmp_path):
        """Prose looks like an edit but ground truth says ZERO edits -> zero tool_calls."""
        out = {
            "history": [
                {"kind": "ActionEvent", "tool_name": "think", "llm_response_id": "r1",
                 "action": {"kind": "ThinkAction", "thought": "planning"}},
                {"kind": "ActionEvent", "tool_name": "finish",
                 "action": {"kind": "FinishAction", "message": "done"}},
            ],
            "metrics": {},
        }
        unit = _write_unit(tmp_path, _LLM_HISTORY_WITH_EDIT, out)
        traj, st = _convert(unit)
        assert st.edit_source == "ground_truth"
        assert st.n_edits == 0
        assert _agent_tool_calls(traj) == []

    def test_legacy_no_output_json_falls_back_to_parser(self, tmp_path):
        """No output.json -> text parser is used (src/foo.py), edit_source flagged."""
        unit = _write_unit(tmp_path, _LLM_HISTORY_WITH_EDIT, None)
        traj, st = _convert(unit)
        assert st.edit_source == "text_parser_fallback"
        calls = _agent_tool_calls(traj)
        assert len(calls) == 1
        assert calls[0].arguments["path"] == "src/foo.py"
        assert traj.extra["edit_source"] == "text_parser_fallback"

    def test_view_command_is_not_an_edit(self, tmp_path):
        """A 'view' FileEditorAction (file read) must never become a tool_call edit."""
        out = {
            "history": [
                {"kind": "ActionEvent", "tool_name": "file_editor",
                 "action": {"kind": "FileEditorAction", "command": "view",
                            "path": "src/foo.py"}},
            ],
            "metrics": {},
        }
        assert A._extract_ground_truth_edits(out) == []


# ---------------------------------------------------------------------------
# C7-002 — one resolved definition (ALL-PASS)
# ---------------------------------------------------------------------------
class TestC7_002_ResolvedSemantics:

    @pytest.mark.parametrize("pr,expected", [(0.0, 0), (0.5, 0), (0.999, 0), (1.0, 1), (None, None)])
    def test_helper_all_pass(self, pr, expected):
        assert A._resolved_from_pass_rate(pr) == expected

    def test_per_step_and_per_model_agree(self, tmp_path):
        """A partial stage (0.5) must be resolved=0 at BOTH the per-step trajectory
        level and in the per-model reward.json — the C7-002 drift is gone."""
        run_dir = tmp_path / "gpt-5.5" / "agent" / "run_1"
        unit = run_dir / "stage1_draft" / "repo" / "aider-b" / "current" / "mod"
        unit.mkdir(parents=True)
        (unit / "llm_history.txt").write_text(_LLM_HISTORY_WITH_EDIT)
        (run_dir / "pipeline_x_results.json").write_text(json.dumps(
            {"stage1": {"pass_rate": 0.5}, "stage2": {"pass_rate": 1.0},
             "stage3": {"pass_rate": 0.0}}))
        out_root = tmp_path / "out"
        A.convert_task(run_dir, out_root, "task", validate=False, kaiju_mode=True)

        traj = json.loads(next(out_root.rglob("trajectory.json")).read_text())
        reward = json.loads(next(out_root.rglob("reward.json")).read_text())
        # per-step trajectory flag (0.5 -> 0 under ALL-PASS; was 1 under the any-pass bug)
        assert traj["final_metrics"]["extra"]["resolved"] == 0
        # per-model map agrees for the SAME pass_rates
        assert reward["resolved"]["stage1"] == 0
        assert reward["resolved"]["stage2"] == 1
        assert reward["resolved"]["stage3"] == 0


# ---------------------------------------------------------------------------
# C7-003 — bounded pipeline walk
# ---------------------------------------------------------------------------
class TestC7_003_BoundedWalk:

    def test_stop_at_prevents_escaping_run(self, tmp_path):
        root = tmp_path
        (root / "pipeline_stale_results.json").write_text("{}")   # stray at corpus root
        run_dir = root / "model" / "agent" / "run_1"
        unit = run_dir / "stage1_draft" / "repo" / "current" / "mod"
        unit.mkdir(parents=True)
        # No pipeline inside the run -> without stop_at the walk would grab the stray.
        assert A.find_pipeline_for(unit, stop_at=run_dir) is None
        # unbounded (legacy behavior) DOES find the stray — proves the boundary matters.
        assert A.find_pipeline_for(unit) is not None

    def test_pipeline_inside_run_still_found(self, tmp_path):
        run_dir = tmp_path / "model" / "agent" / "run_1"
        unit = run_dir / "stage1_draft" / "repo" / "current" / "mod"
        unit.mkdir(parents=True)
        pl = run_dir / "pipeline_run_results.json"
        pl.write_text("{}")
        assert A.find_pipeline_for(unit, stop_at=run_dir) == pl


# ---------------------------------------------------------------------------
# C7-011 — per-turn metrics
# ---------------------------------------------------------------------------
class TestC7_011_PerTurnMetrics:

    def test_per_turn_metrics_from_output_json(self, tmp_path):
        out = {
            "history": [
                {"kind": "ActionEvent", "tool_name": "file_editor",
                 "llm_response_id": "r1",
                 "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                           "cost_usd": 0.5, "cache_read_tokens": 3},
                 "action": {"kind": "FileEditorAction", "command": "str_replace",
                            "path": "src/bar.py", "old_str": "a", "new_str": "b"}},
            ],
            "metrics": {"total_prompt_tokens": 100, "total_completion_tokens": 20,
                        "total_cost": 0.5},
        }
        unit = _write_unit(tmp_path, _LLM_HISTORY_WITH_EDIT, out)
        traj, st = _convert(unit)
        assert traj.extra["per_step_metrics_available"] is True
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        assert len(agent_steps) == 1
        m = agent_steps[0].metrics
        assert m is not None
        assert m.prompt_tokens == 100 and m.cost_usd == 0.5 and m.cached_tokens == 3
        assert m.extra["metrics_scope"] == "per_turn"

    def test_grand_total_fallback_is_tagged(self, tmp_path):
        """When per-turn usage isn't 1:1 alignable, the terminal grand total is
        attached but TAGGED trajectory_grand_total (never read as per-turn)."""
        out = {
            "history": [
                # FileEditorAction WITHOUT a usage block -> zero usage-bearing turns,
                # so len(usages)=0 != 1 assistant turn -> fallback path.
                {"kind": "ActionEvent", "tool_name": "file_editor",
                 "action": {"kind": "FileEditorAction", "command": "str_replace",
                            "path": "src/bar.py", "old_str": "a", "new_str": "b"}},
            ],
            "metrics": {"total_prompt_tokens": 100, "total_completion_tokens": 20,
                        "cache_hit_tokens": 0, "total_cost": 0.5},
        }
        unit = _write_unit(tmp_path, _LLM_HISTORY_WITH_EDIT, out)
        traj, st = _convert(unit)
        assert traj.extra["per_step_metrics_available"] is False
        agent_steps = [s for s in traj.steps if s.source == "agent"]
        last = agent_steps[-1]
        assert last.metrics is not None
        assert last.metrics.extra["metrics_scope"] == "trajectory_grand_total"
        assert last.metrics.prompt_tokens == 100


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
