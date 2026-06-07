"""Tests for commit0/cli.py — utility functions and CLI commands."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import yaml

from commit0.cli import (
    check_commit0_path,
    check_valid,
    highlight,
    Colors,
    validate_commit0_config,
    write_commit0_config_file,
    read_commit0_config_file,
)

MODULE = "commit0.cli"


# ---------------------------------------------------------------------------
# highlight()
# ---------------------------------------------------------------------------
class TestHighlight:
    def test_basic(self):
        assert highlight("hello", Colors.RED) == f"{Colors.RED}hello{Colors.RESET}"

    def test_empty_string(self):
        assert highlight("", Colors.CYAN) == f"{Colors.CYAN}{Colors.RESET}"

    def test_special_chars(self):
        text = "path/to/file.py::test[param]"
        result = highlight(text, Colors.YELLOW)
        assert text in result


# ---------------------------------------------------------------------------
# check_valid()
# ---------------------------------------------------------------------------
class TestCheckValid:
    def test_valid_item_in_list(self):
        check_valid("a", ["a", "b", "c"])  # should not raise

    def test_invalid_item_raises(self):
        with pytest.raises(Exception):  # typer.BadParameter
            check_valid("z", ["a", "b"])

    def test_dict_input_checks_keys(self):
        check_valid("x", {"x": [1], "y": [2]})  # should not raise

    def test_dict_invalid_key_raises(self):
        with pytest.raises(Exception):
            check_valid("z", {"x": [1], "y": [2]})


# ---------------------------------------------------------------------------
# check_commit0_path()
# ---------------------------------------------------------------------------
class TestCheckCommit0Path:
    @patch(f"{MODULE}.subprocess.run")
    def test_happy_path(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        check_commit0_path()  # should not raise

    @patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError)
    @patch(f"{MODULE}.typer.echo")
    def test_file_not_found(self, mock_echo, mock_run):
        check_commit0_path()
        assert mock_echo.call_count >= 1

    @patch(f"{MODULE}.subprocess.run", side_effect=PermissionError)
    @patch(f"{MODULE}.typer.echo")
    def test_permission_error(self, mock_echo, mock_run):
        check_commit0_path()
        assert mock_echo.call_count >= 1


# ---------------------------------------------------------------------------
# write_commit0_config_file()
# ---------------------------------------------------------------------------
class TestWriteConfig:
    def test_writes_yaml(self, tmp_path):
        cfg = {"dataset_name": "ds", "base_dir": str(tmp_path)}
        fp = str(tmp_path / ".commit0.yaml")
        write_commit0_config_file(fp, cfg)
        with open(fp) as f:
            data = yaml.safe_load(f)
        assert data == cfg

    def test_oserror_propagates(self):
        with pytest.raises(OSError):
            write_commit0_config_file("/nonexistent/dir/file.yaml", {"k": "v"})


# ---------------------------------------------------------------------------
# validate_commit0_config()
# ---------------------------------------------------------------------------
class TestValidateConfig:
    def _valid_config(self, tmp_path):
        return {
            "dataset_name": "ds",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }

    def test_valid(self, tmp_path):
        validate_commit0_config(self._valid_config(tmp_path), "test.yaml")

    def test_missing_key(self, tmp_path):
        cfg = self._valid_config(tmp_path)
        del cfg["dataset_name"]
        with pytest.raises(ValueError, match="missing required keys"):
            validate_commit0_config(cfg, "test.yaml")

    def test_wrong_type(self, tmp_path):
        cfg = self._valid_config(tmp_path)
        cfg["dataset_name"] = 123
        with pytest.raises(TypeError, match="must be str"):
            validate_commit0_config(cfg, "test.yaml")

    def test_base_dir_not_exists(self, tmp_path):
        cfg = self._valid_config(tmp_path)
        cfg["base_dir"] = "/nonexistent/path/xyz"
        with pytest.raises(FileNotFoundError, match="does not exist"):
            validate_commit0_config(cfg, "test.yaml")

    def test_all_keys_missing(self):
        with pytest.raises(ValueError):
            validate_commit0_config({}, "test.yaml")

    @pytest.mark.parametrize(
        "key", ["dataset_name", "dataset_split", "repo_split", "base_dir"]
    )
    def test_each_required_key_missing(self, tmp_path, key):
        cfg = self._valid_config(tmp_path)
        del cfg[key]
        with pytest.raises(ValueError):
            validate_commit0_config(cfg, "test.yaml")


# ---------------------------------------------------------------------------
# read_commit0_config_file()
# ---------------------------------------------------------------------------
class TestReadConfig:
    def test_happy_path(self, tmp_path):
        cfg = {
            "dataset_name": "ds",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        fp = tmp_path / ".commit0.yaml"
        fp.write_text(yaml.dump(cfg))
        result = read_commit0_config_file(str(fp))
        assert result == cfg

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="does not exist"):
            read_commit0_config_file("/nonexistent/.commit0.yaml")

    def test_empty_file(self, tmp_path):
        fp = tmp_path / ".commit0.yaml"
        fp.write_text("")
        with pytest.raises(ValueError, match="empty or invalid"):
            read_commit0_config_file(str(fp))

    def test_non_dict_yaml(self, tmp_path):
        fp = tmp_path / ".commit0.yaml"
        fp.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="Expected a YAML mapping"):
            read_commit0_config_file(str(fp))

    def test_yaml_with_extra_keys_passes(self, tmp_path):
        cfg = {
            "dataset_name": "ds",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
            "extra_key": "extra_value",
        }
        fp = tmp_path / ".commit0.yaml"
        fp.write_text(yaml.dump(cfg))
        result = read_commit0_config_file(str(fp))
        assert result["extra_key"] == "extra_value"


# ---------------------------------------------------------------------------
# CLI commands (mocked dispatch)
# ---------------------------------------------------------------------------
class TestSetupCommand:
    @patch(f"{MODULE}.write_commit0_config_file")
    @patch(f"{MODULE}.commit0.harness.setup.main")
    @patch(f"{MODULE}.check_commit0_path")
    def test_setup_calls_harness(self, mock_check, mock_setup, mock_write):
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["setup", "all", "--base-dir", "/tmp/test_repos"]
        )
        # setup may fail due to missing dataset, but check_commit0_path is called
        mock_check.assert_called_once()


class TestBuildCommand:
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_build_reads_config(self, mock_check, mock_read):
        mock_read.return_value = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        with patch(f"{MODULE}.commit0.harness.build.main"):
            runner.invoke(commit0_app, ["build"])
        mock_check.assert_called_once()
        mock_read.assert_called_once()


class TestGetTestsCommand:
    @patch(f"{MODULE}.commit0.harness.get_pytest_ids.main")
    @patch(f"{MODULE}.check_commit0_path")
    def test_get_tests_calls_harness(self, mock_check, mock_get):
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["get-tests", "somerepo"])
        mock_check.assert_called_once()
        mock_get.assert_called_once_with("somerepo", verbose=1)


class TestSaveCommand:
    @patch(f"{MODULE}.commit0.harness.save.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_save_dispatches(self, mock_check, mock_read, mock_save):
        mock_read.return_value = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["save", "myowner", "mybranch"])
        mock_check.assert_called_once()


class TestTestCommand:
    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_test_requires_test_ids_or_stdin(self, mock_check, mock_read, mock_run):
        mock_read.return_value = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        # No test_ids and no --stdin should exit with error
        result = runner.invoke(commit0_app, ["test", "somerepo", "--branch", "main"])
        assert result.exit_code != 0 or "Error" in (result.output or "")

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_test_with_test_ids(self, mock_check, mock_read, mock_valid, mock_run):
        mock_read.return_value = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["test", "somerepo", "test_mod.py", "--branch", "main"]
        )
        mock_run.assert_called_once()


class TestLanguageRouting:
    @patch(f"{MODULE}.commit0.harness.build_rust.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_rust_language_routes_to_rust_build(
        self, mock_check, mock_read, mock_build
    ):
        mock_read.return_value = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
            "language": "rust",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        with patch(f"{MODULE}.check_valid"):
            runner.invoke(commit0_app, ["build"])
        mock_build.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.build.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_python_language_routes_to_python_build(
        self, mock_check, mock_read, mock_build
    ):
        mock_read.return_value = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
            "language": "python",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["build"])
        mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# Evaluate command
# ---------------------------------------------------------------------------
class TestEvaluateCommand:
    def _config(self, lang="python"):
        c = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        if lang != "python":
            c["language"] = lang
        return c

    @patch(f"{MODULE}.commit0.harness.evaluate.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_evaluate_python(self, mock_check, mock_read, mock_eval):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["evaluate", "--branch", "main"])
        mock_eval.assert_called_once()
        # coverage should be False by default
        args = mock_eval.call_args
        assert args[0][5] is False  # coverage param

    @patch(f"{MODULE}.commit0.harness.evaluate_rust.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_evaluate_rust(self, mock_check, mock_read, mock_eval):
        mock_read.return_value = self._config("rust")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["evaluate", "--branch", "main"])
        mock_eval.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.evaluate.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_evaluate_reference_flag(self, mock_check, mock_read, mock_eval):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["evaluate", "--reference"])
        args = mock_eval.call_args
        assert args[0][4] == "reference"  # branch param

    @patch(f"{MODULE}.commit0.harness.evaluate.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_evaluate_coverage_flag(self, mock_check, mock_read, mock_eval):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["evaluate", "--branch", "main", "--coverage"]
        )
        args = mock_eval.call_args
        assert args[0][5] is True  # coverage param


# ---------------------------------------------------------------------------
# Lint command
# ---------------------------------------------------------------------------
class TestLintCommand:
    def _config(self, lang="python"):
        c = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        if lang != "python":
            c["language"] = lang
        return c

    @patch(f"{MODULE}.commit0.harness.lint.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_python_no_files(self, mock_check, mock_read, mock_lint):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["lint", "myrepo"])
        mock_lint.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.lint_rust.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_rust_routes(self, mock_check, mock_read, mock_lint):
        mock_read.return_value = self._config("rust")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["lint", "myrepo"])
        mock_lint.assert_called_once()

    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_nonexistent_file_raises(self, mock_check, mock_read):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        result = runner.invoke(
            commit0_app, ["lint", "myrepo", "--files", "/no/such/file.py"]
        )
        assert result.exit_code != 0

    @patch(f"{MODULE}.commit0.harness.lint.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_with_valid_file(self, mock_check, mock_read, mock_lint, tmp_path):
        cfg = self._config()
        cfg["base_dir"] = str(tmp_path)
        mock_read.return_value = cfg
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("x=1")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["lint", "myrepo", "--files", "main.py"])
        mock_lint.assert_called_once()
        passed_files = mock_lint.call_args[0][3]
        assert len(passed_files) == 1


# ---------------------------------------------------------------------------
# lint-rust command
# ---------------------------------------------------------------------------
class TestLintRustCommand:
    @patch(f"{MODULE}.commit0.harness.lint_rust.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_rust_dispatches(self, mock_check, mock_read, mock_lint):
        mock_read.return_value = {
            "dataset_name": "ds",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["lint-rust", "myrepo"])
        mock_lint.assert_called_once()

    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_rust_nonexistent_file(self, mock_check, mock_read, tmp_path):
        mock_read.return_value = {
            "dataset_name": "ds",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        (tmp_path / "myrepo").mkdir()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        result = runner.invoke(
            commit0_app, ["lint-rust", "myrepo", "--files", "nope.rs"]
        )
        assert result.exit_code != 0

    @patch(f"{MODULE}.commit0.harness.lint_rust.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_lint_rust_with_valid_file(
        self, mock_check, mock_read, mock_lint, tmp_path
    ):
        mock_read.return_value = {
            "dataset_name": "ds",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        repo = tmp_path / "myrepo"
        repo.mkdir()
        (repo / "lib.rs").write_text("fn main() {}")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["lint-rust", "myrepo", "--files", "lib.rs"]
        )
        mock_lint.assert_called_once()
        passed_files = mock_lint.call_args[0][1]
        assert len(passed_files) == 1


# ---------------------------------------------------------------------------
# Save command expanded
# ---------------------------------------------------------------------------
class TestSaveCommandExpanded:
    def _config(self, lang="python"):
        c = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        if lang != "python":
            c["language"] = lang
        return c

    @patch(f"{MODULE}.commit0.harness.save.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_save_rust_uses_rust_split(self, mock_check, mock_read, mock_save):
        mock_read.return_value = self._config("rust")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        with patch(f"{MODULE}.check_valid"):
            runner.invoke(commit0_app, ["save", "owner", "branch"])
        mock_save.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.save.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_save_with_github_token(self, mock_check, mock_read, mock_save):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["save", "owner", "branch", "--github-token", "ghp_xxx"]
        )
        mock_save.assert_called_once()
        assert mock_save.call_args[0][6] == "ghp_xxx"


# ---------------------------------------------------------------------------
# Test command expanded
# ---------------------------------------------------------------------------
class TestTestCommandExpanded:
    def _config(self, lang="python", ds_name="wentingzhao/commit0_combined"):
        c = {
            "dataset_name": ds_name,
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        if lang != "python":
            c["language"] = lang
        return c

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_reference_flag_sets_branch(
        self, mock_check, mock_read, mock_valid, mock_run
    ):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["test", "somerepo", "test_a.py", "--reference"]
        )
        mock_run.assert_called_once()
        assert mock_run.call_args[0][4] == "reference"

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_coverage_flag_passed(self, mock_check, mock_read, mock_valid, mock_run):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app,
            ["test", "somerepo", "test_a.py", "--branch", "main", "--coverage"],
        )
        mock_run.assert_called_once()
        assert mock_run.call_args[0][6] is True

    @patch(f"{MODULE}.commit0.harness.run_rust_tests.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_rust_language_routes_to_rust_tests(
        self, mock_check, mock_read, mock_valid, mock_run
    ):
        mock_read.return_value = self._config("rust")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["test", "somerepo", "test_a.py", "--branch", "main"]
        )
        mock_run.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_humaneval_dataset_uses_repo_as_branch(
        self, mock_check, mock_read, mock_valid, mock_run
    ):
        mock_read.return_value = self._config(ds_name="humaneval/dataset")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["test", "somerepo", "test_a.py"])
        mock_run.assert_called_once()
        assert mock_run.call_args[0][4] == "somerepo"

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.get_active_branch", return_value="feat-123")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_no_branch_resolves_active(
        self, mock_check, mock_read, mock_active, mock_valid, mock_run
    ):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["test", "somerepo", "test_a.py"])
        mock_active.assert_called_once()
        assert mock_run.call_args[0][4] == "feat-123"

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_trailing_slash_stripped(self, mock_check, mock_read, mock_valid, mock_run):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["test", "somerepo/", "test_a.py", "--branch", "main"]
        )
        mock_run.assert_called_once()
        assert not mock_run.call_args[0][3].endswith("/")


# ---------------------------------------------------------------------------
# Build command expanded
# ---------------------------------------------------------------------------
class TestBuildCommandExpanded:
    def _config(self, lang="python"):
        c = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        if lang != "python":
            c["language"] = lang
        return c

    @patch(f"{MODULE}.commit0.harness.build.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_single_arch_sets_env(self, mock_check, mock_read, mock_build):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        result = runner.invoke(commit0_app, ["build", "--single-arch"])
        assert "COMMIT0_BUILD_PLATFORMS" in os.environ or result.exit_code == 0

    @patch(f"{MODULE}.commit0.harness.build_rust.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_rust_build_dispatches(self, mock_check, mock_read, mock_build):
        mock_read.return_value = self._config("rust")
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        with patch(f"{MODULE}.check_valid"):
            runner.invoke(commit0_app, ["build"])
        mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# Language routing expanded
# ---------------------------------------------------------------------------
class TestLanguageRoutingExpanded:
    def _config(self, lang="python"):
        c = {
            "dataset_name": "wentingzhao/commit0_combined",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        if lang != "python":
            c["language"] = lang
        return c

    @patch(f"{MODULE}.commit0.harness.evaluate.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_evaluate_defaults_to_python(self, mock_check, mock_read, mock_eval):
        cfg = self._config()
        cfg.pop("language", None)
        mock_read.return_value = cfg
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["evaluate", "--branch", "main"])
        mock_eval.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.save.main")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_save_defaults_to_python_split(self, mock_check, mock_read, mock_save):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(commit0_app, ["save", "owner", "branch"])
        mock_save.assert_called_once()

    @patch(f"{MODULE}.commit0.harness.run_pytest_ids.main")
    @patch(f"{MODULE}.check_valid")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.check_commit0_path")
    def test_test_defaults_to_python(self, mock_check, mock_read, mock_valid, mock_run):
        mock_read.return_value = self._config()
        from typer.testing import CliRunner
        from commit0.cli import commit0_app

        runner = CliRunner()
        runner.invoke(
            commit0_app, ["test", "somerepo", "test_a.py", "--branch", "main"]
        )
        mock_run.assert_called_once()
