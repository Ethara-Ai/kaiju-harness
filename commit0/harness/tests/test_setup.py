from __future__ import annotations

import logging
from unittest.mock import MagicMock, mock_open, patch

import pytest

from commit0.harness.setup import main

MODULE = "commit0.harness.setup"


def _repo_instance(**overrides):
    defaults = {
        "repo": "owner/myrepo",
        "instance_id": "owner/myrepo#1",
        "base_commit": "abc123",
        "reference_commit": "def456",
        "setup": {},
        "test": {},
        "src_dir": "src",
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


class TestSimpleDatasets:
    @pytest.mark.parametrize(
        "name",
        [
            "humaneval",
            "mbpp",
            "bigcodebench",
            "codecontests",
            "HUMANEVAL",
            "my_humaneval_v2",
            "prefix_mbpp_suffix",
            "BigCodeBench_test",
        ],
    )
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_returns_early(self, mock_load, name):
        mock_load.return_value = list([])
        result = main(name, "test", "all", "/base")
        assert result is None
        mock_load.assert_called_once_with(name, split="test")


class TestSweDataset:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_clone_dir_uses_instance_id(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance(instance_id="django__django-12345")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("swe-bench", "test", "all", "/base")
        mock_clone.assert_called_once()
        args = mock_clone.call_args[0]
        assert "django__django-12345" in args[1]
        assert args[2] == "abc123"

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_branch_is_base_commit(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance(base_commit="deadbeef")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("my-swe-dataset", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "deadbeef"

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_repo_split_filters_by_instance_id(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        ex1 = _repo_instance(instance_id="django__django-12345")
        ex2 = _repo_instance(instance_id="flask__flask-99999")
        mock_load.return_value = list([ex1, ex2])
        mock_clone.return_value = _make_repo_mock()
        main("swe-bench", "test", "django", "/base")
        assert mock_clone.call_count == 1
        assert "django__django-12345" in mock_clone.call_args[0][1]

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_repo_split_all_includes_everything(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        ex1 = _repo_instance(instance_id="django__django-12345")
        ex2 = _repo_instance(instance_id="flask__flask-99999")
        mock_load.return_value = list([ex1, ex2])
        mock_clone.return_value = _make_repo_mock()
        main("swe-bench", "test", "all", "/base")
        assert mock_clone.call_count == 2


class TestCommit0Dataset:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_dir_uses_repo_name(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance(repo="wenting/mylib")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        args = mock_clone.call_args[0]
        assert "mylib" in args[1]

    @patch(f"{MODULE}.os.sep", "\\")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_hf_dataset_branch_is_last_segment(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "commit0_combined"

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_json_file_branch_is_commit0_all(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("path/to/dataset.json", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.os.sep", "/")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_path_with_sep_branch_is_commit0_all(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("some/local/path", "test", "all", "/base")
        assert mock_clone.call_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.SPLIT", {"lite": ["myrepo", "other"]})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_repo_split_in_SPLIT_filters(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        ex_in = _repo_instance(repo="owner/myrepo")
        ex_out = _repo_instance(repo="owner/excluded")
        mock_load.return_value = list([ex_in, ex_out])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "lite", "/base")
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.SPLIT", {"lite": ["other"]})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_repo_split_in_SPLIT_repo_not_in_list_skips(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance(repo="owner/myrepo")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "lite", "/base")
        mock_clone.assert_not_called()

    @patch(f"{MODULE}.SPLIT", {})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_repo_split_not_in_SPLIT_filters_by_normalized_name(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance(repo="owner/myrepo")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "unknown_split", "/base")
        assert mock_clone.call_count == 0

    @patch(f"{MODULE}.SPLIT", {})
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_repo_split_not_in_SPLIT_matches_with_normalization(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance(repo="owner/my-repo")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "my_repo", "/base")
        assert mock_clone.call_count == 1


class TestBaseBranch:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_existing_base_branch_deleted(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance()
        mock_load.return_value = list([example])
        repo = _make_repo_mock(has_base_branch=True)
        mock_clone.return_value = repo
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        repo.git.branch.assert_any_call("-D", "commit0")
        repo.git.checkout.assert_called_with("-b", "commit0")

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_base_branch_no_delete(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        example = _repo_instance()
        mock_load.return_value = list([example])
        repo = _make_repo_mock(has_base_branch=False)
        mock_clone.return_value = repo
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        repo.git.branch.assert_not_called()
        repo.git.checkout.assert_called_once_with("-b", "commit0")


class TestGitignore:
    def _run_with_gitignore(self, exists_return, read_content):
        example = _repo_instance()
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
                main("wentingzhao/commit0_combined", "test", "all", "/base")
        return m, repo

    def test_gitignore_not_exists_creates(self):
        m, repo = self._run_with_gitignore(exists_return=False, read_content="")
        handle = m()
        handle.write.assert_called()
        write_calls = "".join(c.args[0] for c in handle.write.call_args_list)
        assert ".aider*" in write_calls
        assert "logs/" in write_calls
        repo.git.add.assert_called_with(".gitignore")
        repo.git.commit.assert_called()

    def test_gitignore_exists_without_entries_appends(self):
        m, repo = self._run_with_gitignore(
            exists_return=True, read_content="*.pyc\n__pycache__\n"
        )
        handle = m()
        handle.write.assert_called()
        write_calls = "".join(c.args[0] for c in handle.write.call_args_list)
        assert ".aider*" in write_calls
        assert "logs/" in write_calls

    def test_gitignore_already_has_entries_skips(self):
        _, repo = self._run_with_gitignore(
            exists_return=True, read_content=".aider*\nlogs/\n"
        )
        repo.git.add.assert_not_called()

    def test_gitignore_partial_entry_appends_missing(self):
        m, repo = self._run_with_gitignore(exists_return=True, read_content=".aider*\n")
        handle = m()
        handle.write.assert_called()
        write_calls = "".join(c.args[0] for c in handle.write.call_args_list)
        assert "logs/" in write_calls
        repo.git.add.assert_called_with(".gitignore")

    def test_gitignore_failure_logs_warning(self):
        example = _repo_instance()
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
            main("wentingzhao/commit0_combined", "test", "all", "/base")
            mock_logger.warning.assert_called_once()
            assert "disk error" in mock_logger.warning.call_args[0][0]


class TestCloneRepoArgs:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_url_format(self, mock_load, mock_clone, mock_abs, mock_exists):
        example = _repo_instance(repo="owner/myrepo")
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        url = mock_clone.call_args[0][0]
        assert url == "https://github.com/owner/myrepo.git"

    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_receives_logger(self, mock_load, mock_clone, mock_abs, mock_exists):
        example = _repo_instance()
        mock_load.return_value = list([example])
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        logger_arg = mock_clone.call_args[0][3]
        assert isinstance(logger_arg, logging.Logger)


class TestMultipleExamples:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.os.path.abspath", side_effect=lambda p: p)
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_processes_all_repos_with_split_all(
        self, mock_load, mock_clone, mock_abs, mock_exists
    ):
        examples = [_repo_instance(repo=f"owner/repo{i}") for i in range(5)]
        mock_load.return_value = list(examples)
        mock_clone.return_value = _make_repo_mock()
        main("wentingzhao/commit0_combined", "test", "all", "/base")
        assert mock_clone.call_count == 5
