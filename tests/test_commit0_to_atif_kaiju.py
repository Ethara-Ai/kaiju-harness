"""Unit tests for the kaiju integration in commit0_to_atif_v2.py.

Tests cover:
  1. load_pipeline_rewards() — flat (all non-C) and nested (C) schemas
  2. _KAIJU_STAGE_MAP — coverage and correctness
  3. discover_units_kaiju() — glob, model/stage/module extraction, edge cases
  4. find_pipeline_for() — upward walk, stop-at-logs_*, closest-wins
  5. CLI args — --kaiju-mode, --pipeline via main()

harbor is not installed in kaiju's venv; we stub it before importing the script.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

# ---------------------------------------------------------------------------
# Stub harbor so the top-level `from harbor.models.trajectories import ...`
# doesn't blow up when the package is absent.
# ---------------------------------------------------------------------------

def _install_harbor_stubs():
    harbor = types.ModuleType("harbor")
    harbor_models = types.ModuleType("harbor.models")
    harbor_traj = types.ModuleType("harbor.models.trajectories")
    harbor_utils = types.ModuleType("harbor.utils")
    harbor_tv = types.ModuleType("harbor.utils.trajectory_validator")

    class _Stub:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    harbor_traj.Agent = _Stub
    harbor_traj.FinalMetrics = _Stub
    harbor_traj.Step = _Stub
    harbor_traj.ToolCall = _Stub
    harbor_traj.Trajectory = _Stub

    class _TV:
        def validate(self, *a, **kw):
            return True

    harbor_tv.TrajectoryValidator = _TV

    harbor.models = harbor_models
    harbor_models.trajectories = harbor_traj
    harbor.utils = harbor_utils
    harbor_utils.trajectory_validator = harbor_tv

    for name, stub_mod in [
        ("harbor", harbor),
        ("harbor.models", harbor_models),
        ("harbor.models.trajectories", harbor_traj),
        ("harbor.utils", harbor_utils),
        ("harbor.utils.trajectory_validator", harbor_tv),
    ]:
        sys.modules.setdefault(name, stub_mod)


_install_harbor_stubs()

# Add the scripts directory to sys.path so we can import the module directly.
_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import commit0_to_atif_v2 as mod  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _make_unit(run_dir: Path, stage: str, repo: str, branch: str, file_name: str) -> Path:
    """Create the kaiju log directory tree and a minimal llm_history.txt."""
    unit_dir = run_dir / stage / repo / branch / "current" / file_name
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "llm_history.txt").write_text(
        "TO LLM 2026-01-01T00:00:00\nSYSTEM Hello\nUSER World\n"
        "LLM RESPONSE 2026-01-01T00:00:01\nASSISTANT Done\n"
    )
    return unit_dir


# ===========================================================================
# 1. load_pipeline_rewards
# ===========================================================================

class TestLoadPipelineRewards(unittest.TestCase):

    def test_flat_all_stages_present(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "stage1": {"pass_rate": 0.8},
                "stage2": {"pass_rate": 1.0},
                "stage3": {"pass_rate": 0.5},
            })
            r = mod.load_pipeline_rewards(p)
        self.assertAlmostEqual(r["stage1"], 0.8)
        self.assertAlmostEqual(r["stage2"], 1.0)
        self.assertAlmostEqual(r["stage3"], 0.5)

    def test_flat_missing_stage_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "stage1": {"pass_rate": 0.6},
                "stage3": {"pass_rate": 0.9},
                # stage2 absent
            })
            r = mod.load_pipeline_rewards(p)
        self.assertAlmostEqual(r["stage1"], 0.6)
        self.assertIsNone(r["stage2"])
        self.assertAlmostEqual(r["stage3"], 0.9)

    def test_flat_none_pass_rate(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "stage1": {"pass_rate": None},
                "stage2": {"pass_rate": 0.9},
                "stage3": {},  # missing key entirely
            })
            r = mod.load_pipeline_rewards(p)
        self.assertIsNone(r["stage1"])
        self.assertAlmostEqual(r["stage2"], 0.9)
        self.assertIsNone(r["stage3"])

    def test_flat_with_extra_fields_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "model": "opus47",
                "branch": "aider-opus47-pexpect",
                "stage1": {"name": "Draft", "pass_rate": 1.0, "num_passed": 5},
                "stage2": {"name": "Lint",  "pass_rate": 0.8, "num_passed": 4},
                "stage3": {"name": "Test",  "pass_rate": 0.0, "num_passed": 0},
            })
            r = mod.load_pipeline_rewards(p)
        self.assertEqual(r, {"stage1": 1.0, "stage2": 0.8, "stage3": 0.0})

    # -- C pipeline nested schema --

    def test_c_nested_all_stages(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "stages": {
                    "stage1_draft": {"cost_usd": 0.1,  "pass_rate": 0.7},
                    "stage2_lint":  {"cost_usd": 0.05, "pass_rate": 1.0},
                    "stage3_test":  {"cost_usd": 0.08, "pass_rate": 0.0},
                }
            })
            r = mod.load_pipeline_rewards(p)
        self.assertAlmostEqual(r["stage1"], 0.7)
        self.assertAlmostEqual(r["stage2"], 1.0)
        self.assertAlmostEqual(r["stage3"], 0.0)

    def test_c_nested_partial_stages(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "stages": {
                    "stage1_draft": {"pass_rate": 0.5},
                    # stage2_lint absent
                    "stage3_test":  {"pass_rate": 0.9},
                }
            })
            r = mod.load_pipeline_rewards(p)
        self.assertAlmostEqual(r["stage1"], 0.5)
        self.assertIsNone(r["stage2"])
        self.assertAlmostEqual(r["stage3"], 0.9)

    def test_c_nested_none_pass_rate(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write_json(Path(td) / "pipeline.json", {
                "stages": {
                    "stage1_draft": {"pass_rate": None},
                    "stage2_lint":  {"pass_rate": 1.0},
                    "stage3_test":  {"pass_rate": 0.3},
                }
            })
            r = mod.load_pipeline_rewards(p)
        self.assertIsNone(r["stage1"])
        self.assertAlmostEqual(r["stage2"], 1.0)
        self.assertAlmostEqual(r["stage3"], 0.3)

    def test_c_nested_triggers_on_stages_key_presence(self):
        """The nested branch fires IFF the JSON has a top-level 'stages' key."""
        with tempfile.TemporaryDirectory() as td:
            # This has 'stages' key → C branch → reads from stages.*
            p = _write_json(Path(td) / "pipeline.json", {
                "stages": {
                    "stage1_draft": {"pass_rate": 0.42},
                    "stage2_lint":  {"pass_rate": 0.0},
                    "stage3_test":  {"pass_rate": 1.0},
                },
                # also has flat keys — must NOT read these
                "stage1": {"pass_rate": 0.99},
            })
            r = mod.load_pipeline_rewards(p)
        # C branch wins: reads from stages.stage1_draft, not stage1
        self.assertAlmostEqual(r["stage1"], 0.42)


# ===========================================================================
# 2. _KAIJU_STAGE_MAP
# ===========================================================================

class TestKaijuStageMap(unittest.TestCase):

    def test_all_expected_keys_present(self):
        m = mod._KAIJU_STAGE_MAP
        for key in ("stage1_draft", "stage2_lint", "stage3_tests", "stage3_test"):
            with self.subTest(key=key):
                self.assertIn(key, m)

    def test_values_correct(self):
        m = mod._KAIJU_STAGE_MAP
        self.assertEqual(m["stage1_draft"], "draft")
        self.assertEqual(m["stage2_lint"],  "lint")
        self.assertEqual(m["stage3_tests"], "test")
        self.assertEqual(m["stage3_test"],  "test")

    def test_c_alias_and_plural_map_to_same_value(self):
        m = mod._KAIJU_STAGE_MAP
        self.assertEqual(m["stage3_test"], m["stage3_tests"])

    def test_unknown_key_not_in_map(self):
        self.assertNotIn("stage4_unknown", mod._KAIJU_STAGE_MAP)


# ===========================================================================
# 3. discover_units_kaiju
# ===========================================================================

class TestDiscoverUnitsKaiju(unittest.TestCase):

    def test_single_unit_extracted_correctly(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "mymodel" / "run_1"
            run_dir.mkdir(parents=True)
            unit = _make_unit(run_dir, "stage1_draft", "pexpect", "aider-br", "src__pexpect__FSM")

            result = mod.discover_units_kaiju(run_dir)

        self.assertEqual(len(result), 1)
        unit_path, model, stage, module = result[0]
        self.assertEqual(unit_path, unit)
        self.assertEqual(model,  "mymodel")
        self.assertEqual(stage,  "draft")
        self.assertEqual(module, "src__pexpect__FSM")

    def test_all_three_stages_found(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "opus47" / "run_0"
            run_dir.mkdir(parents=True)
            _make_unit(run_dir, "stage1_draft", "repo", "br", "src__repo__mod")
            _make_unit(run_dir, "stage2_lint",  "repo", "br", "src__repo__mod")
            _make_unit(run_dir, "stage3_tests", "repo", "br", "src__repo__mod")

            result = mod.discover_units_kaiju(run_dir)

        stages = {r[2] for r in result}
        self.assertEqual(stages, {"draft", "lint", "test"})
        self.assertEqual(len(result), 3)

    def test_c_stage3_test_alias(self):
        """C uses stage3_test (no 's') — must map to 'test'."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "c_model" / "run_1"
            run_dir.mkdir(parents=True)
            _make_unit(run_dir, "stage3_test", "libfoo", "br", "tests__foo__main")

            result = mod.discover_units_kaiju(run_dir)

        self.assertEqual(len(result), 1)
        _, _, stage, _ = result[0]
        self.assertEqual(stage, "test")

    def test_model_is_run_dir_parent_name(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "kimi-k2.5" / "run_3"
            run_dir.mkdir(parents=True)
            _make_unit(run_dir, "stage2_lint", "somerepo", "br", "src__mod")

            result = mod.discover_units_kaiju(run_dir)

        _, model, _, _ = result[0]
        self.assertEqual(model, "kimi-k2.5")

    def test_multiple_modules_same_stage(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "opus47" / "run_0"
            run_dir.mkdir(parents=True)
            _make_unit(run_dir, "stage1_draft", "repo", "br", "src__repo__alpha")
            _make_unit(run_dir, "stage1_draft", "repo", "br", "src__repo__beta")

            result = mod.discover_units_kaiju(run_dir)

        modules = {r[3] for r in result}
        self.assertEqual(modules, {"src__repo__alpha", "src__repo__beta"})

    def test_multiple_repos_and_stages(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "model" / "run_0"
            run_dir.mkdir(parents=True)
            _make_unit(run_dir, "stage1_draft", "repo_a", "br", "src__a__mod1")
            _make_unit(run_dir, "stage2_lint",  "repo_a", "br", "src__a__mod1")
            _make_unit(run_dir, "stage1_draft", "repo_b", "br", "src__b__mod2")

            result = mod.discover_units_kaiju(run_dir)

        self.assertEqual(len(result), 3)
        stages = {r[2] for r in result}
        self.assertIn("draft", stages)
        self.assertIn("lint", stages)

    def test_directory_without_llm_history_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "model" / "run_0"
            run_dir.mkdir(parents=True)
            # create the structure but omit llm_history.txt
            (run_dir / "stage1_draft" / "repo" / "br" / "current" / "src__mod").mkdir(parents=True)

            result = mod.discover_units_kaiju(run_dir)

        self.assertEqual(result, [])

    def test_empty_run_dir_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "model" / "run_0"
            run_dir.mkdir(parents=True)
            self.assertEqual(mod.discover_units_kaiju(run_dir), [])

    def test_unknown_stage_label_maps_to_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "model" / "run_0"
            run_dir.mkdir(parents=True)
            unit_dir = run_dir / "stage9_future" / "repo" / "br" / "current" / "src__mod"
            unit_dir.mkdir(parents=True)
            (unit_dir / "llm_history.txt").write_text("dummy\n")

            result = mod.discover_units_kaiju(run_dir)

        self.assertEqual(len(result), 1)
        _, _, stage, _ = result[0]
        self.assertEqual(stage, "unknown")

    def test_module_name_is_file_dir_name(self):
        """module should equal the sanitized file name, e.g. src__pexpect__FSM."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "model" / "run_0"
            run_dir.mkdir(parents=True)
            expected_module = "src__marshmallow__schema"
            _make_unit(run_dir, "stage1_draft", "marshmallow", "br", expected_module)

            result = mod.discover_units_kaiju(run_dir)

        _, _, _, module = result[0]
        self.assertEqual(module, expected_module)


# ===========================================================================
# 4. find_pipeline_for
# ===========================================================================

class TestFindPipelineFor(unittest.TestCase):

    def test_finds_pipeline_at_direct_parent(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pipeline = base / "pipeline_abc123_results.json"
            pipeline.write_text("{}")
            unit_dir = base / "unit"
            unit_dir.mkdir()

            result = mod.find_pipeline_for(unit_dir)

        self.assertEqual(result, pipeline)

    def test_finds_pipeline_multiple_levels_up(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            pipeline = base / "pipeline_run1_results.json"
            pipeline.write_text("{}")
            unit_dir = base / "a" / "b" / "c" / "unit"
            unit_dir.mkdir(parents=True)

            result = mod.find_pipeline_for(unit_dir)

        self.assertEqual(result, pipeline)

    def test_returns_none_when_no_pipeline_exists(self):
        with tempfile.TemporaryDirectory() as td:
            unit_dir = Path(td) / "deep" / "nested" / "unit"
            unit_dir.mkdir(parents=True)
            self.assertIsNone(mod.find_pipeline_for(unit_dir))

    def test_stops_at_logs_ancestor(self):
        """Must not walk past a directory whose name starts with logs_."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # pipeline exists ABOVE logs_model — must not be found
            above_pipeline = base / "pipeline_above_results.json"
            above_pipeline.write_text("{}")
            logs_dir = base / "logs_model"
            logs_dir.mkdir()
            unit_dir = logs_dir / "stage1" / "unit"
            unit_dir.mkdir(parents=True)

            result = mod.find_pipeline_for(unit_dir)

        self.assertIsNone(result)

    def test_pipeline_inside_logs_dir_is_found(self):
        """Pipeline INSIDE the logs_* subtree should be found."""
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            logs_dir = base / "logs_model"
            logs_dir.mkdir()
            pipeline = logs_dir / "pipeline_run1_results.json"
            pipeline.write_text("{}")
            unit_dir = logs_dir / "stage1" / "unit"
            unit_dir.mkdir(parents=True)

            result = mod.find_pipeline_for(unit_dir)

        self.assertEqual(result, pipeline)

    def test_returns_alphabetically_first_when_multiple_at_same_level(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            p_b = base / "pipeline_bbb_results.json"
            p_a = base / "pipeline_aaa_results.json"
            p_b.write_text("{}")
            p_a.write_text("{}")
            unit_dir = base / "unit"
            unit_dir.mkdir()

            result = mod.find_pipeline_for(unit_dir)

        self.assertEqual(result, p_a)

    def test_closer_pipeline_wins_over_farther(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            far = base / "pipeline_far_results.json"
            far.write_text("{}")
            near_dir = base / "sub"
            near_dir.mkdir()
            near = near_dir / "pipeline_near_results.json"
            near.write_text("{}")
            unit_dir = near_dir / "unit"
            unit_dir.mkdir()

            result = mod.find_pipeline_for(unit_dir)

        self.assertEqual(result, near)


# ===========================================================================
# 5. CLI argument parsing (via real main() with convert_task patched)
# ===========================================================================

class TestCLIArgs(unittest.TestCase):
    """Verify the new --kaiju-mode and --pipeline flags are wired into main()."""

    def _run_main(self, extra_args: list[str], pipeline_data: dict | None = None):
        """
        Run main() with a minimal temp directory and a patched convert_task that
        records what kwargs it was called with, then returns a minimal report.
        """
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "model" / "run_0"
            run_dir.mkdir(parents=True)
            out_dir = Path(td) / "out"
            out_dir.mkdir()

            captured = {}

            def fake_convert_task(task_dir, out_root, task_name, **kw):
                captured.update(kw)
                captured["task_dir"] = task_dir
                captured["task_name"] = task_name
                # write the report file that main() expects
                report = {"units": 0, "converted": 0, "with_errors": 0,
                          "validated_ok": 0, "total_edits": 0,
                          "assistant_search_markers": 0, "edit_format_ok": True,
                          "edit_parse_rate": None, "task": task_name}
                (out_root / f"{task_name}_v2_report.json").write_text(json.dumps(report))
                return report

            with patch.object(mod, "convert_task", side_effect=fake_convert_task):
                argv = [str(run_dir), str(out_dir)] + extra_args
                rc = mod.main(argv)

        return rc, captured

    def test_kaiju_mode_false_by_default(self):
        rc, kw = self._run_main([])
        self.assertEqual(rc, 0)
        self.assertFalse(kw.get("kaiju_mode"))

    def test_kaiju_mode_flag_sets_true(self):
        rc, kw = self._run_main(["--kaiju-mode"])
        self.assertEqual(rc, 0)
        self.assertTrue(kw.get("kaiju_mode"))

    def test_pipeline_none_by_default(self):
        rc, kw = self._run_main([])
        self.assertIsNone(kw.get("pipeline_override"))

    def test_pipeline_arg_passed_as_path(self):
        with tempfile.TemporaryDirectory() as td:
            pl = Path(td) / "pipeline_run1_results.json"
            pl.write_text("{}")
            rc, kw = self._run_main(["--pipeline", str(pl)])
        self.assertIsNotNone(kw.get("pipeline_override"))
        self.assertEqual(kw["pipeline_override"], pl)

    def test_kaiju_mode_and_pipeline_together(self):
        with tempfile.TemporaryDirectory() as td:
            pl = Path(td) / "pipeline_results.json"
            pl.write_text("{}")
            rc, kw = self._run_main(["--kaiju-mode", "--pipeline", str(pl)])
        self.assertTrue(kw.get("kaiju_mode"))
        self.assertEqual(kw["pipeline_override"], pl)

    def test_no_validate_flag_passes_through(self):
        rc, kw = self._run_main(["--no-validate"])
        self.assertFalse(kw.get("validate", True))

    def test_limit_flag_passes_through(self):
        rc, kw = self._run_main(["--limit", "5"])
        self.assertEqual(kw.get("limit"), 5)


# ===========================================================================
# 6. convert_task routes discovery correctly based on kaiju_mode
# ===========================================================================

class TestConvertTaskKaijuRouting(unittest.TestCase):
    """convert_task must call discover_units_kaiju when kaiju_mode=True,
    discover_units when kaiju_mode=False."""

    def test_kaiju_mode_false_uses_discover_units(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            out_dir = Path(td) / "out"
            out_dir.mkdir()

            with patch.object(mod, "discover_units", return_value=[]) as mock_du, \
                 patch.object(mod, "discover_units_kaiju", return_value=[]) as mock_duk:
                mod.convert_task(run_dir, out_dir, "mytask", kaiju_mode=False)

            mock_du.assert_called_once()
            mock_duk.assert_not_called()

    def test_kaiju_mode_true_uses_discover_units_kaiju(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            out_dir = Path(td) / "out"
            out_dir.mkdir()

            with patch.object(mod, "discover_units", return_value=[]) as mock_du, \
                 patch.object(mod, "discover_units_kaiju", return_value=[]) as mock_duk:
                mod.convert_task(run_dir, out_dir, "mytask", kaiju_mode=True)

            mock_duk.assert_called_once()
            mock_du.assert_not_called()

    def test_pipeline_override_used_instead_of_find_pipeline_for(self):
        """When pipeline_override is set, find_pipeline_for must not be called."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            pipeline = Path(td) / "pipeline.json"
            pipeline.write_text(json.dumps({
                "stage1": {"pass_rate": 1.0},
                "stage2": {"pass_rate": 1.0},
                "stage3": {"pass_rate": 1.0},
            }))
            # produce one unit that convert_unit can skip gracefully
            unit_dir = run_dir / "unit"
            unit_dir.mkdir()
            fake_unit = (unit_dir, "model", "draft", "mod")

            with patch.object(mod, "discover_units_kaiju", return_value=[fake_unit]), \
                 patch.object(mod, "find_pipeline_for") as mock_fpf, \
                 patch.object(mod, "load_pipeline_rewards") as mock_lpr, \
                 patch.object(mod, "convert_unit") as mock_cu:
                mock_lpr.return_value = {"stage1": 1.0, "stage2": 1.0, "stage3": 1.0}
                mock_cu.return_value = (None, MagicMock(
                    skipped=True, skip_reason="no_llm_history", errors=[],
                    instance_id="", reward=None, resolved=None,
                    real_timestamps=False, out_data_missing=False,
                    n_edits=0, assistant_search_markers=0, md_edit_count=0,
                ))
                mod.convert_task(run_dir, out_dir, "t", kaiju_mode=True,
                                 pipeline_override=pipeline)

            mock_fpf.assert_not_called()
            mock_lpr.assert_called_once_with(pipeline)


    def test_batch_mode_passes_kaiju_mode(self):
        """--batch --kaiju-mode must forward kaiju_mode=True to each convert_task call."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            task_dir = root / "mytask"
            task_dir.mkdir()
            (task_dir / "logs_model").mkdir()
            out_dir = Path(td) / "out"
            out_dir.mkdir()

            captured = {}

            def fake_convert_task(task_dir, out_root, task_name, **kw):
                captured.update(kw)
                report = {"units": 0, "converted": 0, "with_errors": 0,
                           "validated_ok": 0, "total_edits": 0,
                           "assistant_search_markers": 0, "edit_format_ok": True,
                           "edit_parse_rate": None, "task": task_name}
                (out_root / f"{task_name}_v2_report.json").write_text(json.dumps(report))
                return report

            with patch.object(mod, "convert_task", side_effect=fake_convert_task):
                rc = mod.main([str(root), str(out_dir), "--batch", "--kaiju-mode"])

        self.assertEqual(rc, 0)
        self.assertTrue(captured.get("kaiju_mode"))
        self.assertIsNone(captured.get("pipeline_override"))

    def test_batch_mode_passes_pipeline_override(self):
        """--batch --pipeline must forward pipeline_override=Path(...) to each convert_task call."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            task_dir = root / "mytask"
            task_dir.mkdir()
            (task_dir / "logs_model").mkdir()
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            pipeline_path = Path(td) / "pipeline_run1_results.json"
            pipeline_path.write_text("{}")

            captured = {}

            def fake_convert_task(task_dir, out_root, task_name, **kw):
                captured.update(kw)
                report = {"units": 0, "converted": 0, "with_errors": 0,
                           "validated_ok": 0, "total_edits": 0,
                           "assistant_search_markers": 0, "edit_format_ok": True,
                           "edit_parse_rate": None, "task": task_name}
                (out_root / f"{task_name}_v2_report.json").write_text(json.dumps(report))
                return report

            with patch.object(mod, "convert_task", side_effect=fake_convert_task):
                rc = mod.main([str(root), str(out_dir), "--batch", "--kaiju-mode",
                               "--pipeline", str(pipeline_path)])

        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("pipeline_override"), pipeline_path)

class TestBranchSuffixDisambiguation(unittest.TestCase):
    """branch_suffix must include the repo directory when aider_parts are identical across repos."""

    def _compute_suffix(self, unit: "Path") -> str:
        from pathlib import Path
        unit = Path(unit)
        aider_parts = [p for p in unit.parts if p.startswith("aider-")]
        if not aider_parts:
            return ""
        aider_idx = next(i for i, p in enumerate(unit.parts) if p.startswith("aider-"))
        repo_part = unit.parts[aider_idx - 1] if aider_idx > 0 else ""
        return "__".join(filter(None, [repo_part] + aider_parts))

    def test_different_repos_produce_distinct_suffixes(self):
        """Two repos with same module and identical aider suffix must yield distinct branch_suffix values."""
        from pathlib import Path
        unit_a = Path("/base/stage1_1/repo-a/aider-opus4.6-mybatch/current/utils/llm_history.txt")
        unit_b = Path("/base/stage1_1/repo-b/aider-opus4.6-mybatch/current/utils/llm_history.txt")
        suffix_a = self._compute_suffix(unit_a)
        suffix_b = self._compute_suffix(unit_b)
        self.assertNotEqual(suffix_a, suffix_b)
        self.assertIn("repo-a", suffix_a)
        self.assertIn("repo-b", suffix_b)

    def test_suffix_retains_aider_component(self):
        """branch_suffix must still include the aider-* component for traceability."""
        from pathlib import Path
        unit = Path("/base/stage1_1/repo-a/aider-opus4.6-mybatch/current/utils/llm_history.txt")
        suffix = self._compute_suffix(unit)
        self.assertIn("aider-opus4.6-mybatch", suffix)


# ===========================================================================
# Run
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
