from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.save import main

MODULE = "commit0.harness.save"


def _make_example(repo="org/myrepo", instance_id="org__myrepo__1"):
    return {"repo": repo, "instance_id": instance_id}


def _make_mock_repo(heads=None, remotes=None, dirty=False):
    repo = MagicMock()
    repo.heads = heads if heads is not None else ["main"]
    repo.is_dirty.return_value = dirty
    remote_objs = []
    for name in remotes or []:
        r = MagicMock()
        r.name = name
        remote_objs.append(r)
    repo.remotes = remote_objs
    push_info = MagicMock()
    repo.remote.return_value = push_info
    push_info.push.return_value = None
    return repo


class TestGithubToken:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config", return_value=[])
    def test_token_none_falls_back_to_env(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", "env-tok-123")
        main("ds", "test", "all", "/base", "owner", "main", github_token=None)
        mock_load.assert_called_once()

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_token_provided_directly(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["dev"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "dev", github_token="direct-tok")
        expected_url = "https://x-access-token:direct-tok@github.com/owner/myrepo.git"
        mock_repo.create_remote.assert_called_once_with(
            "progress-tracker", url=expected_url
        )

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_github_url_contains_token(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "main", github_token="tk_abc")
        url_arg = mock_repo.create_remote.call_args[1]["url"]
        assert "x-access-token:tk_abc@github.com" in url_arg


class TestSweDatasetFiltering:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_repo_split_filters_by_instance_id(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_load.return_value = [
            _make_example(instance_id="match_split_1"),
            _make_example(instance_id="other_thing_2"),
        ]
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        main("swe_bench", "test", "match", "/base", "owner", "main", github_token="t")
        assert mock_repo_cls.call_count == 1

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_repo_split_all_includes_everything(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_load.return_value = [
            _make_example(instance_id="a"),
            _make_example(instance_id="b"),
        ]
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        main("SWE_data", "test", "all", "/base", "owner", "main", github_token="t")
        assert mock_repo_cls.call_count == 2


class TestCommit0DatasetFiltering:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.SPLIT", {"lite": ["myrepo"], "full": ["other"]})
    def test_repo_split_in_split_filters_by_name(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_load.return_value = [
            _make_example(repo="org/myrepo"),
            _make_example(repo="org/notinlite"),
        ]
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        main("commit0", "test", "lite", "/base", "owner", "main", github_token="t")
        assert mock_repo_cls.call_count == 1

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.SPLIT", {"lite": ["myrepo"]})
    def test_repo_split_not_in_split_filters_by_normalized_name(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_load.return_value = [_make_example(repo="org/myrepo")]
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        main(
            "commit0",
            "test",
            "unknown_split",
            "/base",
            "owner",
            "main",
            github_token="t",
        )
        assert mock_repo_cls.call_count == 0

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.SPLIT", {})
    def test_repo_split_not_in_split_matches_with_normalization(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_load.return_value = [_make_example(repo="org/my-repo")]
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        main(
            "commit0",
            "test",
            "my_repo",
            "/base",
            "owner",
            "main",
            github_token="t",
        )
        assert mock_repo_cls.call_count == 1

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_repo_split_all_includes_all_repos(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_load.return_value = [
            _make_example(repo="org/a"),
            _make_example(repo="org/b"),
        ]
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        main("commit0", "test", "all", "/base", "owner", "main", github_token="t")
        assert mock_repo_cls.call_count == 2


class TestLocalRepoPath:
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_path_not_exists_raises_oserror(self, mock_load, mock_exists):
        mock_load.return_value = [_make_example()]
        with pytest.raises(OSError, match="does not exists"):
            main("ds", "test", "all", "/base", "owner", "main", github_token="t")

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_path_exists_loads_git_repo(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "main", github_token="t")
        mock_repo_cls.assert_called_once_with("/base/myrepo")


class TestCreateRepoOnGithub:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_called_with_correct_args(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example(repo="org/myrepo")]
        main("ds", "test", "all", "/base", "the_owner", "main", github_token="tok123")
        mock_create.assert_called_once()
        kwargs = mock_create.call_args[1]
        assert kwargs["organization"] == "the_owner"
        assert kwargs["repo"] == "myrepo"
        assert kwargs["token"] == "tok123"


class TestRemoteManagement:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_remote_not_exists_creates_new(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "main", github_token="t")
        mock_repo.create_remote.assert_called_once()
        assert mock_repo.create_remote.call_args[0][0] == "progress-tracker"

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_remote_exists_updates_url(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=["progress-tracker"])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "main", github_token="t")
        mock_repo.create_remote.assert_not_called()
        mock_repo.remote.assert_any_call(name="progress-tracker")


class TestBranchCheckout:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_branch_exists_checks_out(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["feat"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "feat", github_token="t")
        mock_repo.git.checkout.assert_called_once_with("feat")

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_branch_not_in_heads_raises_valueerror(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        with pytest.raises(ValueError, match="does not exist"):
            main("ds", "test", "all", "/base", "owner", "nope", github_token="t")


class TestDirtyCheck:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_not_dirty_skips_add_and_commit(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[], dirty=False)
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "main", github_token="t")
        mock_repo.git.add.assert_not_called()
        mock_repo.index.commit.assert_not_called()

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dirty_adds_and_commits(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[], dirty=True)
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "main", github_token="t")
        mock_repo.git.add.assert_called_once_with(A=True)
        mock_repo.index.commit.assert_called_once_with("AI generated code.")


class TestPush:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_push_succeeds_logs_info(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        with patch(f"{MODULE}.logger") as mock_logger:
            main("ds", "test", "all", "/base", "owner", "main", github_token="t")
            mock_logger.info.assert_called()
            pushed_msg = [
                c for c in mock_logger.info.call_args_list if "Pushed to" in str(c)
            ]
            assert len(pushed_msg) >= 1

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_push_fails_logs_error_continues(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo1 = _make_mock_repo(heads=["main"], remotes=[])
        remote1 = MagicMock()
        remote1.push.side_effect = Exception("network error")
        mock_repo1.remote.return_value = remote1

        mock_repo2 = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.side_effect = [mock_repo1, mock_repo2]

        mock_load.return_value = [
            _make_example(repo="org/repo1"),
            _make_example(repo="org/repo2"),
        ]
        with patch(f"{MODULE}.logger") as mock_logger:
            main("ds", "test", "all", "/base", "owner", "main", github_token="t")
            error_calls = [
                c for c in mock_logger.error.call_args_list if "fails" in str(c)
            ]
            assert len(error_calls) >= 1
        assert mock_repo_cls.call_count == 2

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_push_uses_correct_refspec(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["feat"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        origin = MagicMock()
        mock_repo.remote.return_value = origin
        mock_load.return_value = [_make_example()]
        main("ds", "test", "all", "/base", "owner", "feat", github_token="t")
        origin.push.assert_called_once_with(refspec="feat:feat")
