from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from commit0.harness.constants import RepoInstance

MODULE = "commit0.harness.lint"


def _make_instance(**overrides) -> RepoInstance:
    defaults = dict(
        instance_id="test/repo",
        repo="org/my-lib",
        base_commit="abc",
        reference_commit="def",
        setup={"python": "3.12", "packages": "req.txt", "install": "pip install -e ."},
        test={"test_cmd": "pytest"},
        src_dir="src",
    )
    defaults.update(overrides)
    return RepoInstance(**defaults)


def _dataset(instances=None):
    if instances is None:
        instances = [_make_instance()]
    return iter(instances)


class TestRepoMatching:
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_finds_matching_repo(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.os.path.isdir", return_value=True),
            patch(f"{MODULE}.os.walk", return_value=[("/src", [], ["a.py"])]),
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="ok", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", None, "/base")
        mock_load.assert_called_once_with("ds", split="test")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_empty_dataset_raises(self, mock_load):
        mock_load.return_value = list([])
        from commit0.harness.lint import main

        with pytest.raises(AssertionError, match="No example available"):
            main("ds", "test", "/repos/my-lib", None, "/base")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_matching_repo_uses_last_entry(self, mock_load):
        mock_load.return_value = _dataset([_make_instance(repo="org/other-lib")])
        with patch(f"{MODULE}.os.path.isdir", return_value=False):
            from commit0.harness.lint import main

            with pytest.raises(Exception, match="is a valid path"):
                main("ds", "test", "/repos/no-match", None, "/base")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_trailing_slash_stripped(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.os.path.isdir", return_value=True),
            patch(f"{MODULE}.os.walk", return_value=[("/src", [], ["x.py"])]),
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib/", None, "/base")
            cmd_args = mock_run.call_args[0][0]
            assert "--files" in cmd_args


class TestFileDiscovery:
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_files_none_walks_repo_or_repo_dir(self, mock_load):
        mock_load.return_value = _dataset()
        walk_data = [("/repos/my-lib/src", ["sub"], ["mod.py", "README.md"])]
        with (
            patch(f"{MODULE}.os.path.isdir", return_value=True),
            patch(f"{MODULE}.os.walk", return_value=walk_data),
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", None, "/base")
            cmd_args = mock_run.call_args[0][0]
            file_strs = cmd_args[cmd_args.index("--files") + 1 :]
            assert len(file_strs) == 1
            assert file_strs[0].endswith("mod.py")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_files_none_falls_back_to_base_dir(self, mock_load):
        mock_load.return_value = _dataset()
        walk_data = [("/base/my-lib/src", [], ["f.py"])]

        def isdir_side_effect(path):
            return path != "/repos/my-lib"

        with (
            patch(f"{MODULE}.os.path.isdir", side_effect=isdir_side_effect),
            patch(f"{MODULE}.os.walk", return_value=walk_data),
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", None, "/base")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_files_none_neither_dir_raises(self, mock_load):
        mock_load.return_value = _dataset()
        with patch(f"{MODULE}.os.path.isdir", return_value=False):
            from commit0.harness.lint import main

            with pytest.raises(Exception, match="is a valid path"):
                main("ds", "test", "/repos/my-lib", None, "/base")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_files_provided_directly(self, mock_load):
        mock_load.return_value = _dataset()
        files = [Path("/a/b.py"), Path("/c/d.py")]
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", files, "/base")
            cmd_args = mock_run.call_args[0][0]
            file_strs = cmd_args[cmd_args.index("--files") + 1 :]
            assert file_strs == ["/a/b.py", "/c/d.py"]

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_empty_files_list(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [], "/base")
            cmd_args = mock_run.call_args[0][0]
            file_strs = cmd_args[cmd_args.index("--files") + 1 :]
            assert file_strs == []


class TestConfigFile:
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_config_written_when_missing(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=False) as mock_is_file,
            patch(f"{MODULE}.Path.write_text") as mock_write,
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")
            mock_write.assert_called_once()
            written_content = mock_write.call_args[0][0]
            assert "ruff" in written_content

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_config_always_written(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.Path.write_text") as mock_write,
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")
            mock_write.assert_called_once()


class TestPreCommitBinary:
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_found_in_venv(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.sys.executable", "/venv/bin/python"),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "/venv/bin/pre-commit"

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_fallback_to_shutil_which(self, mock_load):
        mock_load.return_value = _dataset()

        def isfile_side(path):
            if "pre-commit" in str(path) and "venv" in str(path):
                return False
            return True

        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", side_effect=isfile_side),
            patch(f"{MODULE}.sys.executable", "/venv/bin/python"),
            patch(f"{MODULE}.shutil.which", return_value="/usr/bin/pre-commit"),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "/usr/bin/pre-commit"

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_not_found_raises(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=False),
            patch(f"{MODULE}.shutil.which", return_value=None),
        ):
            from commit0.harness.lint import main

            with pytest.raises(FileNotFoundError, match="pre-commit command not found"):
                main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")


class TestSubprocessExecution:
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_success_prints_stdout_and_exits(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit") as mock_exit,
            patch(f"{MODULE}.print") as mock_print,
        ):
            mock_run.return_value = MagicMock(stdout="All passed!", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")
            mock_print.assert_called_once_with("All passed!")
            mock_exit.assert_called_once_with(0)

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_called_process_error_prints_output_and_exits(self, mock_load):
        mock_load.return_value = _dataset()
        err = subprocess.CalledProcessError(1, "pre-commit")
        err.output = "lint failure"
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run", side_effect=err),
            patch(f"{MODULE}.sys.exit") as mock_exit,
            patch(f"{MODULE}.print") as mock_print,
        ):
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")
            mock_print.assert_called_once_with("lint failure")
            mock_exit.assert_called_once_with(1)

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_file_not_found_during_run_raises(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(
                f"{MODULE}.subprocess.run",
                side_effect=FileNotFoundError("no such binary"),
            ),
        ):
            from commit0.harness.lint import main

            with pytest.raises(FileNotFoundError, match="Error running pre-commit"):
                main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_general_exception_during_run_raises(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.subprocess.run", side_effect=RuntimeError("boom")),
        ):
            from commit0.harness.lint import main

            with pytest.raises(Exception, match="unexpected error occurred"):
                main("ds", "test", "/repos/my-lib", [Path("x.py")], "/base")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_subprocess_receives_correct_command(self, mock_load):
        mock_load.return_value = _dataset()
        with (
            patch(f"{MODULE}.Path.is_file", return_value=True),
            patch(f"{MODULE}.os.path.isfile", return_value=True),
            patch(f"{MODULE}.sys.executable", "/env/bin/python"),
            patch(f"{MODULE}.subprocess.run") as mock_run,
            patch(f"{MODULE}.sys.exit"),
        ):
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            from commit0.harness.lint import main

            main("ds", "test", "/repos/my-lib", [Path("a.py"), Path("b.py")], "/base")
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "/env/bin/pre-commit"
            assert cmd[1] == "run"
            assert cmd[2] == "--config"
            assert cmd[3] == ".commit0.pre-commit-config.yaml"
            assert cmd[4] == "--files"
            assert cmd[5:] == ["a.py", "b.py"]
            assert mock_run.call_args[1] == {
                "capture_output": True,
                "text": True,
                "check": True,
            }
