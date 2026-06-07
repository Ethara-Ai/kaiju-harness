"""Tests for save.py error recovery — silent push failures, credential handling."""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Token / Credential handling
# ---------------------------------------------------------------------------
class TestCredentialHandling:
    def test_no_token_raises(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(EnvironmentError, match="GITHUB_TOKEN is required"):
            main("ds", "test", "all", "/base", "owner", "main", github_token=None)

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_token_embedded_in_url(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token="secret123")

        url_arg = mock_repo.create_remote.call_args[1]["url"]
        assert "x-access-token:secret123@" in url_arg

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_env_token_fallback(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, monkeypatch
    ):
        monkeypatch.setenv("GITHUB_TOKEN", "env-token-456")
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token=None)

        url_arg = mock_repo.create_remote.call_args[1]["url"]
        assert "x-access-token:env-token-456@" in url_arg


# ---------------------------------------------------------------------------
# Push failure (silent continue)
# ---------------------------------------------------------------------------
class TestPushFailureSilentContinue:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_push_failure_continues_to_next_repo(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        """Push failure should log error and continue, not raise."""
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        # Make push raise an exception
        mock_remote = MagicMock()
        mock_remote.push.side_effect = Exception("push failed: auth error")
        mock_repo.remote.return_value = mock_remote

        examples = [_make_example("org/repo1"), _make_example("org/repo2")]
        mock_load.return_value = examples

        # Should NOT raise — the exception is caught and execution continues
        main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        # Both repos should have been attempted (create_repo called twice)
        assert mock_create.call_count == 2

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_push_failure_with_multiple_repos(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        """All repos attempted even if all pushes fail."""
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_remote = MagicMock()
        mock_remote.push.side_effect = Exception("network error")
        mock_repo.remote.return_value = mock_remote

        examples = [_make_example(f"org/repo{i}") for i in range(5)]
        mock_load.return_value = examples

        # Should not raise
        main("ds", "test", "all", "/base", "owner", "main", github_token="token")
        assert mock_create.call_count == 5


# ---------------------------------------------------------------------------
# Branch validation
# ---------------------------------------------------------------------------
class TestBranchValidation:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_nonexistent_branch_raises(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main", "dev"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        with pytest.raises(ValueError, match="does not exist"):
            main(
                "ds",
                "test",
                "all",
                "/base",
                "owner",
                "nonexistent",
                github_token="token",
            )


# ---------------------------------------------------------------------------
# Repo path validation
# ---------------------------------------------------------------------------
class TestRepoPathValidation:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_missing_local_repo_raises(self, mock_load, mock_exists, mock_create):
        mock_load.return_value = [_make_example()]

        with pytest.raises(OSError, match="does not exists"):
            main("ds", "test", "all", "/base", "owner", "main", github_token="token")


# ---------------------------------------------------------------------------
# Dirty repo handling
# ---------------------------------------------------------------------------
class TestDirtyRepoHandling:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dirty_repo_auto_commits(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[], dirty=True)
        mock_repo_cls.return_value = mock_repo

        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        mock_repo.git.add.assert_called_once_with(A=True)
        mock_repo.index.commit.assert_called_once_with("AI generated code.")

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clean_repo_skips_commit(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[], dirty=False)
        mock_repo_cls.return_value = mock_repo

        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        mock_repo.git.add.assert_not_called()
        mock_repo.index.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Remote handling
# ---------------------------------------------------------------------------
class TestRemoteHandling:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_existing_remote_gets_updated(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=["progress-tracker"])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        # Should not create new remote, but update URL
        mock_repo.create_remote.assert_not_called()
        mock_repo.remote.assert_called()

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_new_remote_gets_created(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        mock_repo.create_remote.assert_called_once()
        call_kwargs = mock_repo.create_remote.call_args
        assert call_kwargs[0][0] == "progress-tracker"


class TestSWEDatasetFiltering:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_dataset_filters_by_instance_id(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [
            {"repo": "org/repo1", "instance_id": "org__repo1__abc"},
            {"repo": "org/repo2", "instance_id": "org__repo2__def"},
        ]

        main(
            "swe-bench",
            "test",
            "abc",
            "/base",
            "owner",
            "main",
            github_token="token",
        )

        assert mock_create.call_count == 1

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_dataset_all_split_processes_everything(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [
            {"repo": "org/repo1", "instance_id": "1"},
            {"repo": "org/repo2", "instance_id": "2"},
            {"repo": "org/repo3", "instance_id": "3"},
        ]

        main(
            "swe-bench",
            "test",
            "all",
            "/base",
            "owner",
            "main",
            github_token="token",
        )

        assert mock_create.call_count == 3


class TestRemoteURLUpdate:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_existing_remote_url_updated(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=["progress-tracker"])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        main("ds", "test", "all", "/base", "owner", "main", github_token="tok")

        mock_repo.remote.return_value.set_url.assert_called_once()
        url_arg = mock_repo.remote.return_value.set_url.call_args[0][0]
        assert "x-access-token:tok@" in url_arg


class TestLoggingVerification:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_push_failure_logs_error(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, caplog
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_remote = MagicMock()
        mock_remote.push.side_effect = Exception("auth failed")
        mock_repo.remote.return_value = mock_remote
        mock_load.return_value = [_make_example()]

        import logging

        with caplog.at_level(logging.ERROR, logger="commit0.harness.save"):
            main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        assert any(
            "fails" in r.message.lower() or "push" in r.message.lower()
            for r in caplog.records
        )

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_successful_push_logs_info(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, caplog
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]

        import logging

        with caplog.at_level(logging.INFO, logger="commit0.harness.save"):
            main("ds", "test", "all", "/base", "owner", "main", github_token="token")

        assert any("pushed" in r.message.lower() for r in caplog.records)


class TestNonSWEDatasetFiltering:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_name_match_with_dash_underscore_normalization(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [
            {"repo": "org/my-cool-repo", "instance_id": "1"},
        ]

        main(
            "commit0",
            "test",
            "my_cool_repo",
            "/base",
            "owner",
            "main",
            github_token="token",
        )

        assert mock_create.call_count == 1

    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_name_mismatch_skips_repo(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=[])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [
            {"repo": "org/other-repo", "instance_id": "1"},
        ]

        main(
            "commit0",
            "test",
            "my_cool_repo",
            "/base",
            "owner",
            "main",
            github_token="token",
        )

        mock_create.assert_not_called()


class TestSafeUrlMasking:
    @patch(f"{MODULE}.create_repo_on_github")
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_safe_url_masks_token_in_log(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, caplog
    ):
        mock_repo = _make_mock_repo(heads=["main"], remotes=["progress-tracker"])
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [_make_example()]
        secret = "ghp_super_secret_12345"

        import logging

        with caplog.at_level(logging.INFO, logger="commit0.harness.save"):
            main("ds", "test", "all", "/base", "owner", "main", github_token=secret)

        for record in caplog.records:
            if (
                "replacing" in record.message.lower()
                or "already exists" in record.message.lower()
            ):
                assert secret not in record.message
                assert "***" in record.message
