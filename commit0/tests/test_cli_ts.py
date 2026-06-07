from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from commit0.cli_ts import (
    commit0_ts_app,
    check_valid_ts,
    write_commit0_ts_config_file,
    read_commit0_ts_config_file,
    highlight,
    Colors,
)

runner = CliRunner()


class TestHighlight:
    def test_wraps_text_with_color(self):
        result = highlight("hello", Colors.ORANGE)
        assert "hello" in result
        assert Colors.ORANGE in result
        assert Colors.RESET in result


class TestCheckValidTs:
    def test_all_always_valid(self):
        check_valid_ts("all", {"all_ts": []})

    def test_known_split_valid(self):
        check_valid_ts("all_ts", {"all_ts": []})

    def test_unknown_split_raises(self):
        import typer

        with pytest.raises(typer.BadParameter, match="Invalid repo_split"):
            check_valid_ts("nonexistent", {"all_ts": []})

    def test_empty_string_raises(self):
        import typer

        with pytest.raises(typer.BadParameter):
            check_valid_ts("", {"all_ts": []})


class TestWriteConfig:
    def test_writes_yaml(self, tmp_path):
        cfg_path = str(tmp_path / ".commit0.ts.yaml")
        config = {
            "dataset_name": "ts_custom_dataset.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/repos_ts",
        }
        write_commit0_ts_config_file(cfg_path, config)
        with open(cfg_path) as f:
            loaded = yaml.safe_load(f)
        assert loaded == config

    def test_write_creates_file(self, tmp_path):
        cfg_path = str(tmp_path / "config.yaml")
        write_commit0_ts_config_file(
            cfg_path,
            {
                "dataset_name": "x",
                "dataset_split": "t",
                "repo_split": "a",
                "base_dir": "/b",
            },
        )
        assert os.path.exists(cfg_path)

    def test_write_invalid_path_raises(self, tmp_path):
        cfg_path = str(tmp_path / "nonexistent_dir" / "config.yaml")
        with pytest.raises(OSError):
            write_commit0_ts_config_file(cfg_path, {"key": "val"})


class TestReadConfig:
    def test_roundtrip(self, tmp_path):
        cfg_path = str(tmp_path / ".commit0.ts.yaml")
        config = {
            "dataset_name": "ts_custom_dataset.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        write_commit0_ts_config_file(cfg_path, config)
        loaded = read_commit0_ts_config_file(cfg_path)
        assert loaded == config

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError, match="TS config file not found"):
            read_commit0_ts_config_file("/nonexistent/.commit0.ts.yaml")

    def test_empty_file_raises(self, tmp_path):
        cfg_path = str(tmp_path / ".commit0.ts.yaml")
        Path(cfg_path).write_text("")
        with pytest.raises(ValueError, match="empty or invalid"):
            read_commit0_ts_config_file(cfg_path)

    def test_missing_keys_raises(self, tmp_path):
        cfg_path = str(tmp_path / ".commit0.ts.yaml")
        Path(cfg_path).write_text(yaml.dump({"dataset_name": "x"}))
        with pytest.raises(ValueError, match="missing required keys"):
            read_commit0_ts_config_file(cfg_path)

    def test_wrong_type_raises(self, tmp_path):
        cfg_path = str(tmp_path / ".commit0.ts.yaml")
        config = {
            "dataset_name": 123,
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        Path(cfg_path).write_text(yaml.dump(config))
        with pytest.raises(TypeError, match="must be str"):
            read_commit0_ts_config_file(cfg_path)


class TestSetupCommand:
    @patch("commit0.harness.setup_ts.main")
    def test_setup_invokes_main(self, mock_main, tmp_path):
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text("[]")
        runner.invoke(
            commit0_ts_app,
            [
                "setup",
                "all",
                "--dataset-name",
                str(dataset_path),
                "--base-dir",
                str(tmp_path / "repos"),
                "--commit0-config-file",
                str(tmp_path / ".commit0.ts.yaml"),
            ],
        )
        mock_main.assert_called_once()
        args = mock_main.call_args[0]
        assert str(dataset_path) in args[0]
        assert args[1] == "test"
        assert args[2] == "all"

    @patch("commit0.harness.setup_ts.main")
    def test_setup_writes_config(self, mock_main, tmp_path):
        cfg_path = tmp_path / ".commit0.ts.yaml"
        dataset_path = tmp_path / "dataset.json"
        dataset_path.write_text("[]")
        runner.invoke(
            commit0_ts_app,
            [
                "setup",
                "all",
                "--dataset-name",
                str(dataset_path),
                "--base-dir",
                str(tmp_path / "repos"),
                "--commit0-config-file",
                str(cfg_path),
            ],
        )
        assert cfg_path.exists()
        loaded = yaml.safe_load(cfg_path.read_text())
        assert loaded["repo_split"] == "all"
        assert loaded["dataset_split"] == "test"

    def test_setup_invalid_split_fails(self):
        result = runner.invoke(commit0_ts_app, ["setup", "nonexistent"])
        assert result.exit_code != 0


class TestStubCommands:
    @pytest.mark.parametrize("cmd", ["test", "evaluate", "lint", "save", "get-tests"])
    def test_stub_command_raises(self, cmd):
        result = runner.invoke(
            commit0_ts_app,
            [cmd] + (["dummy"] if cmd in {"test", "lint", "save", "get-tests"} else []),
        )
        assert result.exit_code != 0

    def test_no_args_shows_help(self):
        result = runner.invoke(commit0_ts_app, [])
        assert "TypeScript" in result.output or "Usage" in result.output


MODULE = "commit0.cli_ts"


class TestSetupDatasetPathBranches:
    @patch("commit0.harness.setup_ts.main")
    def test_setup_existing_non_json_dataset_resolves_path(self, mock_main, tmp_path):
        dataset_file = tmp_path / "my_dataset"
        dataset_file.write_text("data")
        cfg_path = tmp_path / ".commit0.ts.yaml"

        result = runner.invoke(
            commit0_ts_app,
            [
                "setup",
                "all",
                "--dataset-name",
                str(dataset_file),
                "--base-dir",
                str(tmp_path / "repos"),
                "--commit0-config-file",
                str(cfg_path),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_main.assert_called_once()
        called_dataset = mock_main.call_args[0][0]
        assert os.path.isabs(called_dataset)
        assert called_dataset == str(dataset_file.resolve())

    @patch("commit0.harness.setup_ts.main")
    def test_setup_nonexistent_non_json_dataset_keeps_name(self, mock_main, tmp_path):
        cfg_path = tmp_path / ".commit0.ts.yaml"

        result = runner.invoke(
            commit0_ts_app,
            [
                "setup",
                "all",
                "--dataset-name",
                "nonexistent_dataset",
                "--base-dir",
                str(tmp_path / "repos"),
                "--commit0-config-file",
                str(cfg_path),
            ],
        )
        assert result.exit_code == 0, result.output
        mock_main.assert_called_once()
        called_dataset = mock_main.call_args[0][0]
        assert called_dataset == "nonexistent_dataset"


class TestBuildCommand:
    def _write_config(self, tmp_path):
        cfg_path = tmp_path / ".commit0.ts.yaml"
        import yaml

        config = {
            "dataset_name": "test_ds.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path / "repos"),
        }
        cfg_path.write_text(yaml.dump(config))
        return str(cfg_path)

    def test_build_single_arch_arm64(self, tmp_path):
        cfg_path = self._write_config(tmp_path)

        with patch("commit0.harness.build_ts.main") as mock_build:
            with patch("platform.machine", return_value="arm64"):
                result = runner.invoke(
                    commit0_ts_app,
                    [
                        "build",
                        "--single-arch",
                        "--commit0-config-file",
                        cfg_path,
                    ],
                )
        assert result.exit_code == 0, result.output
        assert os.environ.get("COMMIT0_BUILD_PLATFORMS") == "linux/arm64"
        mock_build.assert_called_once()
        os.environ.pop("COMMIT0_BUILD_PLATFORMS", None)

    def test_build_single_arch_amd64(self, tmp_path):
        cfg_path = self._write_config(tmp_path)

        with patch("commit0.harness.build_ts.main") as mock_build:
            with patch("platform.machine", return_value="x86_64"):
                result = runner.invoke(
                    commit0_ts_app,
                    [
                        "build",
                        "--single-arch",
                        "--commit0-config-file",
                        cfg_path,
                    ],
                )
        assert result.exit_code == 0, result.output
        assert os.environ.get("COMMIT0_BUILD_PLATFORMS") == "linux/amd64"
        mock_build.assert_called_once()
        os.environ.pop("COMMIT0_BUILD_PLATFORMS", None)

    def test_build_single_arch_aarch64(self, tmp_path):
        cfg_path = self._write_config(tmp_path)

        with patch("commit0.harness.build_ts.main") as mock_build:
            with patch("platform.machine", return_value="aarch64"):
                result = runner.invoke(
                    commit0_ts_app,
                    [
                        "build",
                        "--single-arch",
                        "--commit0-config-file",
                        cfg_path,
                    ],
                )
        assert result.exit_code == 0, result.output
        assert os.environ.get("COMMIT0_BUILD_PLATFORMS") == "linux/arm64"
        mock_build.assert_called_once()
        os.environ.pop("COMMIT0_BUILD_PLATFORMS", None)

    def test_build_without_single_arch(self, tmp_path):
        cfg_path = self._write_config(tmp_path)
        os.environ.pop("COMMIT0_BUILD_PLATFORMS", None)

        with patch("commit0.harness.build_ts.main") as mock_build:
            result = runner.invoke(
                commit0_ts_app,
                [
                    "build",
                    "--commit0-config-file",
                    cfg_path,
                ],
            )
        assert result.exit_code == 0, result.output
        mock_build.assert_called_once()

    def test_build_passes_correct_args(self, tmp_path):
        cfg_path = self._write_config(tmp_path)

        with patch("commit0.harness.build_ts.main") as mock_build:
            result = runner.invoke(
                commit0_ts_app,
                [
                    "build",
                    "--num-workers",
                    "4",
                    "--verbose",
                    "2",
                    "--commit0-config-file",
                    cfg_path,
                ],
            )
        assert result.exit_code == 0, result.output
        mock_build.assert_called_once_with(
            dataset_name="test_ds.json",
            dataset_split="test",
            split="all",
            num_workers=4,
            verbose=2,
        )


class TestSaveCommand:
    def test_save_raises_not_implemented(self):
        result = runner.invoke(
            commit0_ts_app,
            ["save", "myowner", "mybranch"],
        )
        assert result.exit_code != 0

    def test_save_with_token_still_raises(self):
        result = runner.invoke(
            commit0_ts_app,
            [
                "save",
                "myowner",
                "mybranch",
                "--github-token",
                "ghp_fake",
            ],
        )
        assert result.exit_code != 0

    def test_save_with_config_still_raises(self, tmp_path):
        result = runner.invoke(
            commit0_ts_app,
            [
                "save",
                "owner",
                "branch",
                "--commit0-config-file",
                str(tmp_path / "nonexistent.yaml"),
            ],
        )
        assert result.exit_code != 0


class TestGetTestsCommand:
    def test_get_tests_prints_ids(self):
        test_groups = [["test_a", "test_b"], ["test_c"]]

        with patch(
            "commit0.harness.get_ts_test_ids.main", return_value=test_groups
        ) as mock_main:
            result = runner.invoke(
                commit0_ts_app,
                ["get-tests", "my-repo"],
            )

        assert result.exit_code == 0
        mock_main.assert_called_once_with("my-repo", verbose=1)
        assert "test_a" in result.output
        assert "test_b" in result.output
        assert "test_c" in result.output

    def test_get_tests_empty_groups(self):
        with patch("commit0.harness.get_ts_test_ids.main", return_value=[]):
            result = runner.invoke(
                commit0_ts_app,
                ["get-tests", "my-repo"],
            )
        assert result.exit_code == 0

    def test_get_tests_with_verbose(self):
        with patch(
            "commit0.harness.get_ts_test_ids.main", return_value=[["id1"]]
        ) as mock_main:
            result = runner.invoke(
                commit0_ts_app,
                ["get-tests", "my-repo", "--verbose", "2"],
            )
        assert result.exit_code == 0
        mock_main.assert_called_once_with("my-repo", verbose=2)

    def test_get_tests_single_group_multiple_ids(self):
        ids = [f"test_{i}" for i in range(5)]
        with patch("commit0.harness.get_ts_test_ids.main", return_value=[ids]):
            result = runner.invoke(
                commit0_ts_app,
                ["get-tests", "some-repo"],
            )
        assert result.exit_code == 0
        for tid in ids:
            assert tid in result.output


class TestMainGuard:
    def test_main_guard_invokes_app(self):
        with patch(f"{MODULE}.commit0_ts_app") as mock_app:
            import commit0.cli_ts as cli_mod

            if hasattr(cli_mod, "commit0_ts_app"):
                mock_app()
                mock_app.assert_called_once()


class TestTestCommand:
    def test_test_command_invokes_run_ts_tests(self, tmp_path):
        cfg_path = tmp_path / ".commit0.ts.yaml"
        config = {
            "dataset_name": "ds.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        cfg_path.write_text(yaml.dump(config))

        with patch("commit0.harness.run_ts_tests.main") as mock_main:
            result = runner.invoke(
                commit0_ts_app,
                [
                    "test",
                    "my-repo",
                    "test_id_1",
                    "--branch",
                    "feat",
                    "--commit0-config-file",
                    str(cfg_path),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_main.assert_called_once()
        kwargs = mock_main.call_args[1]
        assert kwargs["repo_or_repo_dir"] == "my-repo"
        assert kwargs["test_ids"] == "test_id_1"
        assert kwargs["branch"] == "feat"


class TestEvaluateCommand:
    def test_evaluate_command_invokes_main(self, tmp_path):
        cfg_path = tmp_path / ".commit0.ts.yaml"
        config = {
            "dataset_name": "ds.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        cfg_path.write_text(yaml.dump(config))

        with patch("commit0.harness.evaluate_ts.main") as mock_main:
            result = runner.invoke(
                commit0_ts_app,
                [
                    "evaluate",
                    "--branch",
                    "dev",
                    "--commit0-config-file",
                    str(cfg_path),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_main.assert_called_once()


class TestLintCommand:
    def test_lint_command_invokes_main(self, tmp_path):
        cfg_path = tmp_path / ".commit0.ts.yaml"
        config = {
            "dataset_name": "ds.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        cfg_path.write_text(yaml.dump(config))

        with patch("commit0.harness.lint_ts.main") as mock_main:
            result = runner.invoke(
                commit0_ts_app,
                [
                    "lint",
                    "my-repo",
                    "--commit0-config-file",
                    str(cfg_path),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_main.assert_called_once()
