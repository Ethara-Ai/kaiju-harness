"""Tests for Rust-specific harness modules.

Covers: build_rust, setup_rust, evaluate_rust, lint_rust, run_rust_tests.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# build_rust — _load_datasets, main
# ---------------------------------------------------------------------------
BUILD_MODULE = "commit0.harness.build_rust"


class TestLoadDatasets:
    """Tests for build_rust._load_datasets."""

    def test_load_single_json_file(self, tmp_path):
        from commit0.harness.build_rust import _load_datasets

        data = [{"repo": "a", "image": "img_a"}]
        f = tmp_path / "test_rust_dataset.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert result == data

    def test_load_dict_json_converts_to_list(self, tmp_path):
        from commit0.harness.build_rust import _load_datasets

        data = {"key1": {"repo": "a"}, "key2": {"repo": "b"}}
        f = tmp_path / "repos.json"
        f.write_text(json.dumps(data))
        result = _load_datasets(f)
        assert len(result) == 2

    def test_load_from_directory(self, tmp_path):
        from commit0.harness.build_rust import _load_datasets

        f1 = tmp_path / "alpha_rust_dataset.json"
        f2 = tmp_path / "beta_rust_dataset.json"
        f1.write_text(json.dumps([{"repo": "a"}]))
        f2.write_text(json.dumps([{"repo": "b"}]))
        result = _load_datasets(tmp_path)
        assert len(result) == 2

    def test_empty_directory_exits(self, tmp_path):
        from commit0.harness.build_rust import _load_datasets

        with pytest.raises(SystemExit):
            _load_datasets(tmp_path)

    def test_nonexistent_path_exits(self, tmp_path):
        from commit0.harness.build_rust import _load_datasets

        with pytest.raises(SystemExit):
            _load_datasets(tmp_path / "nope")


class TestBuildRustMain:
    """Tests for build_rust.main."""

    @patch(f"{BUILD_MODULE}.build_rust_repo_images")
    @patch(f"{BUILD_MODULE}.docker_client")
    def test_success(self, mock_docker, mock_build, tmp_path):
        from commit0.harness.build_rust import main

        f = tmp_path / "d.json"
        f.write_text(json.dumps([{"repo": "x"}]))
        mock_build.return_value = (["img1"], [])
        main(str(f), num_workers=1, verbose=0)
        mock_build.assert_called_once()

    @patch(f"{BUILD_MODULE}.build_rust_repo_images")
    @patch(f"{BUILD_MODULE}.docker_client")
    def test_failed_build_exits(self, mock_docker, mock_build, tmp_path):
        from commit0.harness.build_rust import main

        f = tmp_path / "d.json"
        f.write_text(json.dumps([{"repo": "x"}]))
        mock_build.return_value = ([], ["img_fail"])
        with pytest.raises(SystemExit):
            main(str(f), num_workers=1, verbose=0)


# ---------------------------------------------------------------------------
# setup_rust — main
# ---------------------------------------------------------------------------
SETUP_MODULE = "commit0.harness.setup_rust"


class TestSetupRustMain:
    @patch(f"{SETUP_MODULE}.clone_repo")
    @patch(f"{SETUP_MODULE}.load_dataset_from_config")
    def test_skips_repo_not_in_split(self, mock_load, mock_clone):
        from commit0.harness.setup_rust import main

        mock_load.return_value = [{"repo": "org/not_in_split"}]
        with patch.dict(f"{SETUP_MODULE}.RUST_SPLIT", {"lite": ["org/other_repo"]}):
            main("dataset", "split", "lite", "/tmp/base")
        mock_clone.assert_not_called()

    @patch(f"{SETUP_MODULE}.clone_repo")
    @patch(f"{SETUP_MODULE}.load_dataset_from_config")
    def test_clones_matching_repo_by_name(self, mock_load, mock_clone):
        from commit0.harness.setup_rust import main

        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_repo.git.checkout = MagicMock()
        mock_clone.return_value = mock_repo
        mock_load.return_value = [{"repo": "org/my_repo"}]
        main("dataset", "split", "my_repo", "/tmp/base")
        mock_clone.assert_called_once()


# ---------------------------------------------------------------------------
# lint_rust — _find_cargo_toml, _run_cargo_clippy, _run_cargo_fmt, _collect_rs_files, main
# ---------------------------------------------------------------------------
LINT_MODULE = "commit0.harness.lint_rust"


class TestFindCargoToml:
    def test_finds_in_current_dir(self, tmp_path):
        from commit0.harness.lint_rust import _find_cargo_toml

        (tmp_path / "Cargo.toml").touch()
        result = _find_cargo_toml(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_finds_in_parent(self, tmp_path):
        from commit0.harness.lint_rust import _find_cargo_toml

        (tmp_path / "Cargo.toml").touch()
        sub = tmp_path / "src"
        sub.mkdir()
        result = _find_cargo_toml(str(sub))
        assert result == str(tmp_path.resolve())

    def test_returns_none_when_not_found(self, tmp_path):
        from commit0.harness.lint_rust import _find_cargo_toml

        sub = tmp_path / "deep" / "nested"
        sub.mkdir(parents=True)
        result = _find_cargo_toml(str(sub))
        assert result is None


class TestRunCargoClippy:
    # B5-post-hoc: _run_cargo_clippy now calls _bust_clippy_cache first;
    # tests that mock subprocess.run at the top level make the cache-bust
    # emit ValueError on empty stdout (json.loads('')), returning rc=-2.
    # Bypass to isolate _run_cargo_clippy behavior.
    @pytest.fixture(autouse=True)
    def _bypass_cache_bust(self):
        with patch(f"{LINT_MODULE}._bust_clippy_cache", return_value=True):
            yield
    @patch(f"{LINT_MODULE}.shutil")
    def test_cargo_not_found(self, mock_shutil):
        from commit0.harness.lint_rust import _run_cargo_clippy

        mock_shutil.which.return_value = None
        result = _run_cargo_clippy("/some/dir")
        assert result["warnings"] == 0
        assert result["errors"] == 0
        assert "cargo not found" in result["raw_stderr"]

    @patch(f"{LINT_MODULE}.shutil")
    def test_timeout(self, mock_shutil):
        import subprocess as real_subprocess
        from commit0.harness.lint_rust import _run_cargo_clippy

        mock_shutil.which.return_value = "/usr/bin/cargo"
        with patch(
            f"{LINT_MODULE}.subprocess.run",
            side_effect=real_subprocess.TimeoutExpired("cmd", 300),
        ):
            result = _run_cargo_clippy("/some/dir")
        assert "timeout" in result["raw_stderr"]

    @patch(f"{LINT_MODULE}.subprocess")
    @patch(f"{LINT_MODULE}.shutil")
    def test_parses_warnings_and_errors(self, mock_shutil, mock_subprocess):
        from commit0.harness.lint_rust import _run_cargo_clippy

        mock_shutil.which.return_value = "/usr/bin/cargo"
        warning_line = json.dumps(
            {
                "reason": "compiler-message",
                "message": {"level": "warning", "message": "unused var", "spans": []},
            }
        )
        error_line = json.dumps(
            {
                "reason": "compiler-message",
                "message": {"level": "error", "message": "type mismatch", "spans": []},
            }
        )
        mock_result = MagicMock()
        mock_result.stdout = warning_line + "\n" + error_line + "\n"
        mock_result.stderr = ""
        mock_result.returncode = 1
        mock_subprocess.run.return_value = mock_result
        result = _run_cargo_clippy("/some/dir")
        assert result["warnings"] == 1
        assert result["errors"] == 1
        assert len(result["messages"]) == 2


class TestRunCargoFmt:
    @patch(f"{LINT_MODULE}.shutil")
    def test_cargo_not_found(self, mock_shutil):
        from commit0.harness.lint_rust import _run_cargo_fmt

        mock_shutil.which.return_value = None
        result = _run_cargo_fmt("/some/dir")
        assert result["formatted"] is False
        assert result["returncode"] == -1


class TestCollectRsFiles:
    def test_collects_rs_files(self, tmp_path):
        from commit0.harness.lint_rust import _collect_rs_files

        (tmp_path / "main.rs").touch()
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "lib.rs").touch()
        (sub / "readme.md").touch()
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 2
        assert all(f.endswith(".rs") for f in result)

    def test_empty_dir(self, tmp_path):
        from commit0.harness.lint_rust import _collect_rs_files

        result = _collect_rs_files(str(tmp_path))
        assert result == []


class TestLintRustMain:
    def test_nonexistent_dir_raises(self, tmp_path):
        from commit0.harness.lint_rust import main

        with pytest.raises(FileNotFoundError, match="does not exist"):
            main(str(tmp_path / "nope"))

    def test_no_cargo_toml_raises(self, tmp_path):
        from commit0.harness.lint_rust import main

        with pytest.raises(FileNotFoundError, match="Cargo.toml"):
            main(str(tmp_path))

    @patch(f"{LINT_MODULE}._run_cargo_fmt")
    @patch(f"{LINT_MODULE}._run_cargo_clippy")
    @patch(f"{LINT_MODULE}._find_cargo_toml")
    def test_passes_when_clean(self, mock_find, mock_clippy, mock_fmt, tmp_path):
        from commit0.harness.lint_rust import main

        mock_find.return_value = str(tmp_path)
        mock_clippy.return_value = {
            "warnings": 0,
            "errors": 0,
            "messages": [],
            "raw_stderr": "",
            "returncode": 0,
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert result["passed"] is True

    @patch(f"{LINT_MODULE}._run_cargo_fmt")
    @patch(f"{LINT_MODULE}._run_cargo_clippy")
    @patch(f"{LINT_MODULE}._find_cargo_toml")
    def test_fails_when_clippy_errors(self, mock_find, mock_clippy, mock_fmt, tmp_path):
        from commit0.harness.lint_rust import main

        mock_find.return_value = str(tmp_path)
        mock_clippy.return_value = {
            "warnings": 0,
            "errors": 2,
            "messages": [{"level": "error", "message": "bad", "spans": []}],
            "raw_stderr": "",
            "returncode": 1,
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# evaluate_rust — _aggregate_rust_results
# ---------------------------------------------------------------------------
EVAL_MODULE = "commit0.harness.evaluate_rust"


class TestAggregateRustResults:
    def test_missing_test_output(self, tmp_path):
        from commit0.harness.evaluate_rust import _aggregate_rust_results

        out = []
        _aggregate_rust_results(str(tmp_path), "myrepo", out)
        assert len(out) == 1
        assert out[0]["num_tests"] == 0
        assert out[0]["num_passed"] == 0

    def test_nextest_json_parsed(self, tmp_path):
        from commit0.harness.evaluate_rust import _aggregate_rust_results

        lines = []
        for name in ["test_a", "test_b"]:
            lines.append(
                json.dumps(
                    {
                        "type": "test",
                        "event": "ok",
                        "name": name,
                        "exec_time": 0.5,
                        "stdout": "",
                    }
                )
            )
        (tmp_path / "test_output.txt").write_text("\n".join(lines))
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["num_passed"] == 2
        assert out[0]["num_tests"] == 2

    def test_cargo_test_fallback(self, tmp_path):
        from commit0.harness.evaluate_rust import _aggregate_rust_results

        # libtest text output: the parser counts the per-test `... ok/FAILED`
        # lines and cross-checks them against the `test result:` summary. A bare
        # summary line alone is intentionally NOT scored (forged-summary guard),
        # so a genuine run must include the per-test lines it summarises.
        content = (
            "running 4 tests\n"
            "test mod::a ... ok\n"
            "test mod::b ... ok\n"
            "test mod::c ... ok\n"
            "test mod::d ... FAILED\n"
            "test result: FAILED. 3 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
        )
        (tmp_path / "test_output.txt").write_text(content)
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["num_passed"] == 3
        assert out[0]["num_tests"] == 4

    def test_no_summary_line(self, tmp_path):
        from commit0.harness.evaluate_rust import _aggregate_rust_results

        (tmp_path / "test_output.txt").write_text("running 1 test\nsome output\n")
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["num_tests"] == 0
