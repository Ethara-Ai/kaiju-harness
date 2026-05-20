from __future__ import annotations

import logging
from unittest.mock import MagicMock, mock_open, patch


from commit0.harness.setup_ts import main

MODULE = "commit0.harness.setup_ts"


def _ts_repo_instance(**overrides):
    defaults = {
        "repo": "Zahgon/zod",
        "instance_id": "commit-0/zod",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node": "20",
            "install": "npm install",
            "packages": [],
            "pre_install": [],
            "specification": "",
        },
        "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
        "src_dir": "src",
        "language": "typescript",
    }
    defaults.update(overrides)
    obj = MagicMock()
    obj.__getitem__ = lambda self, key: defaults[key]
    return obj


def _make_repo_mock(has_base_branch=False):
    repo = MagicMock()
    branches = MagicMock()
    branches.__contains__ = MagicMock(return_value=has_base_branch)
    repo.branches = branches
    return repo


class TestTsDatasetLoading:
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_calls_load_dataset(self, mock_load):
        mock_load.return_value = list([])
        main("ts_custom_dataset.json", "test", "all", "/base")
        mock_load.assert_called_once_with("ts_custom_dataset.json", split="test")

    @patch(f"{MODULE}.load_dataset_from_config")
    def test_empty_dataset_no_clone(self, mock_load):
        mock_load.return_value = list([])
        main("ts_custom_dataset.json", "test", "all", "/base")


class TestTsSplitFiltering:
    @patch(f"{MODULE}.TS_SPLIT", {"curated": ["zod", "effect"]})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_split_key_filters(self, mock_load, mock_clone, mock_abs, mock_exists):
        ex_in = _ts_repo_instance(repo="Zahgon/zod")
        ex_out = _ts_repo_instance(repo="Zahgon/excluded")
        mock_load.return_value = list([ex_in, ex_out])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "curated", "/base")
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.TS_SPLIT", {"curated": ["other"]})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_split_key_repo_not_in_list_skips(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance(repo="Zahgon/zod")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "curated", "/base")
        mock_clone.assert_not_called()

    @patch(f"{MODULE}.TS_SPLIT", {})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_unknown_split_filters_by_normalized_name(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance(repo="Zahgon/my-lib")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "my_lib", "/base")
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.TS_SPLIT", {})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_unknown_split_no_match_skips(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance(repo="Zahgon/zod")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "unknown_split", "/base")
        mock_clone.assert_not_called()

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_split_all_processes_everything(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        examples = [_ts_repo_instance(repo=f"Zahgon/lib{i}") for i in range(5)]
        mock_load.return_value = list(examples)
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "all", "/base")
        assert mock_clone.call_count == 5


class TestTsBranch:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_json_dataset_branch_is_commit0_all(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("path/to/ts_dataset.json", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.os.sep", "\\")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_path_with_sep_branch_is_commit0_all(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("some\\local\\path", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.os.sep", "\\")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_hf_dataset_branch_is_last_segment(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("Ethara-Ai/commit0_typescript", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "commit0_typescript"


class TestTsBaseBranch:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_existing_base_branch_deleted(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance()
        mock_load.return_value = list([example])
        repo = _make_repo_mock(has_base_branch=True)
        mock_clone.return_value = repo
        main("ts_custom_dataset.json", "test", "all", "/base")
        repo.git.branch.assert_any_call("-D", "commit0")
        repo.git.checkout.assert_called_with("-b", "commit0")

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_base_branch_no_delete(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance()
        mock_load.return_value = list([example])
        repo = _make_repo_mock(has_base_branch=False)
        mock_clone.return_value = repo
        main("ts_custom_dataset.json", "test", "all", "/base")
        repo.git.branch.assert_not_called()
        repo.git.checkout.assert_called_once_with("-b", "commit0")


class TestTsGitignore:
    def _run_with_gitignore(self, exists_return, read_content):
        example = _ts_repo_instance()
        mock_load = MagicMock(return_value=list([example]))
        repo = _make_repo_mock(has_base_branch=False)
        mock_clone = MagicMock(return_value=repo)

        with (
            patch(f"{MODULE}.load_dataset_from_config", mock_load),
            patch(f"{MODULE}.clone_repo", mock_clone),
            patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p),
            patch(f"{MODULE}.os.path.exists", return_value=exists_return),
            patch(f"{MODULE}.os.path.join", side_effect=lambda *a: "/".join(a)),
        ):
            m = mock_open(read_data=read_content)
            with patch("builtins.open", m):
                main("ts_custom_dataset.json", "test", "all", "/base")
        return m, repo

    def test_no_gitignore_creates_with_node_modules(self):
        m, repo = self._run_with_gitignore(exists_return=False, read_content="")
        handle = m()
        handle.write.assert_called()
        write_calls = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "node_modules/" in write_calls
        assert "dist/" in write_calls
        assert ".aider*" in write_calls
        assert "logs/" in write_calls
        repo.git.add.assert_called_with(".gitignore")
        repo.git.commit.assert_called()

    def test_existing_gitignore_appends_missing(self):
        m, repo = self._run_with_gitignore(
            exists_return=True, read_content="*.pyc\n__pycache__\n"
        )
        handle = m()
        handle.write.assert_called()
        write_calls = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "node_modules/" in write_calls
        assert "dist/" in write_calls

    def test_gitignore_already_complete_skips(self):
        content = "node_modules/\ndist/\n.aider*\nlogs/\n"
        _, repo = self._run_with_gitignore(exists_return=True, read_content=content)
        repo.git.add.assert_not_called()

    def test_gitignore_partial_adds_missing(self):
        m, repo = self._run_with_gitignore(
            exists_return=True, read_content="node_modules/\n.aider*\n"
        )
        handle = m()
        handle.write.assert_called()
        write_calls = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "dist/" in write_calls
        assert "logs/" in write_calls
        assert write_calls.count("node_modules/") == 0
        repo.git.add.assert_called_with(".gitignore")

    def test_gitignore_failure_logs_warning(self):
        example = _ts_repo_instance()
        mock_load = MagicMock(return_value=list([example]))
        repo = _make_repo_mock(has_base_branch=False)
        mock_clone = MagicMock(return_value=repo)

        with (
            patch(f"{MODULE}.load_dataset_from_config", mock_load),
            patch(f"{MODULE}.clone_repo", mock_clone),
            patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p),
            patch(f"{MODULE}.os.path.exists", side_effect=Exception("disk error")),
            patch(f"{MODULE}.os.path.join", side_effect=lambda *a: "/".join(a)),
            patch(f"{MODULE}.logger") as mock_logger,
        ):
            main("ts_custom_dataset.json", "test", "all", "/base")
            mock_logger.warning.assert_called_once()
            assert "disk error" in mock_logger.warning.call_args[0][0]


class TestTsCloneArgs:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_url_format(self, mock_load, mock_clone, mock_abs, mock_exists):
        example = _ts_repo_instance(repo="Zahgon/zod")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "all", "/base")
        url = mock_clone.call_args[0][0]
        assert url == "https://github.com/Zahgon/zod.git"

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_dir_uses_repo_name(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _ts_repo_instance(repo="Zahgon/zod")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "all", "/base")
        clone_dir = mock_clone.call_args[0][1]
        assert "zod" in clone_dir

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_receives_logger(self, mock_load, mock_clone, mock_abs, mock_exists):
        example = _ts_repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("ts_custom_dataset.json", "test", "all", "/base")
        logger_arg = mock_clone.call_args[0][3]
        assert isinstance(logger_arg, logging.Logger)
