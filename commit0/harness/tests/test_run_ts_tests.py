"""Tests for commit0.harness.run_ts_tests — covers uncovered paths.

Targets:
- Lines 113-122: Git repo fallback (InvalidGitRepositoryError / NoSuchPathError retry)
- Lines 135-151: Remote branch resolution (fetch + remote.refs search)
- Lines 177-250: Post-Docker execution (exit-code reading, close_logger in finally)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.constants import RepoInstance
from commit0.harness.utils import EvaluationError

MODULE = "commit0.harness.run_ts_tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_kwargs(**overrides):
    defaults = dict(
        dataset_name="commit0_combined",
        dataset_split="test",
        base_dir="/base",
        repo_or_repo_dir="/repos/test-repo",
        branch="reference",
        test_ids="src/__tests__/foo.test.ts",
        backend="local",
        timeout=1800,
        num_cpus=1,
        rebuild_image=False,
        verbose=0,
    )
    defaults.update(overrides)
    return defaults


def _make_repo_example(**overrides):
    defaults = dict(
        instance_id="test/repo",
        repo="org/test-repo",
        base_commit="abc123",
        reference_commit="def456",
        setup={
            "node": "20",
            "install": "npm install",
            "packages": [],
            "pre_install": [],
        },
        test={"test_cmd": "npx jest", "test_dir": "__tests__"},
        src_dir="src",
    )
    defaults.update(overrides)
    return RepoInstance(**defaults)


def _make_spec_mock():
    spec = MagicMock()
    spec.eval_script = "#!/bin/bash\nnpx jest --forceExit"
    return spec


def _setup_base_mocks(
    mock_load,
    mock_make_spec,
    mock_setup_logger,
    mock_close_logger,
    mock_get_hash,
    examples,
    spec=None,
    logger=None,
):
    mock_load.return_value = list(examples)
    if spec is None:
        spec = _make_spec_mock()
    mock_make_spec.return_value = spec
    if logger is None:
        logger = MagicMock()
    mock_setup_logger.return_value = logger
    mock_get_hash.return_value = "abcdef1234567890abcdef"
    return spec, logger


def _setup_docker_ctx(mock_docker, timed_out=False):
    ctx = MagicMock()
    ctx.exec_run_with_timeout.return_value = ("output", timed_out, 10.0)
    mock_docker.return_value.__enter__ = MagicMock(return_value=ctx)
    mock_docker.return_value.__exit__ = MagicMock(return_value=False)
    return ctx


def _write_exit_code(tmp_path, code="0"):
    exit_file = (
        tmp_path
        / "test-repo"
        / "reference"
        / "abcdef1234567890abcdef"
        / "test_exit_code.txt"
    )
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text(code)
    test_output = exit_file.parent / "test_output.txt"
    test_output.write_text("test output content")


def _write_exit_code_for(tmp_path, repo_name, branch, code="0"):
    exit_file = (
        tmp_path / repo_name / branch / "abcdef1234567890abcdef" / "test_exit_code.txt"
    )
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text(code)
    test_output = exit_file.parent / "test_output.txt"
    test_output.write_text("test output content")


# ===================================================================
# 1) Git repo fallback  (lines 113-122)
# ===================================================================


class TestGitRepoFallback:
    """When repo_or_repo_dir is not a valid git repo, main() retries with
    os.path.join(base_dir, repo_name).  If that also fails it raises.
    """

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_invalid_git_repo_retries_with_base_dir(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        # First call raises InvalidGitRepositoryError, second succeeds.
        mock_git.exc.NoSuchPathError = type("NoSuchPathError", (Exception,), {})
        mock_git.exc.InvalidGitRepositoryError = type(
            "InvalidGitRepositoryError", (Exception,), {}
        )
        good_repo = MagicMock()
        mock_git.Repo.side_effect = [
            mock_git.exc.InvalidGitRepositoryError("bad"),
            good_repo,
        ]
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        assert mock_git.Repo.call_count == 2
        # Second call should use base_dir/repo_name
        second_call_arg = mock_git.Repo.call_args_list[1][0][0]
        assert second_call_arg == "/base/test-repo"

    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_retry_also_fails_raises(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.exc.NoSuchPathError = type("NoSuchPathError", (Exception,), {})
        mock_git.exc.InvalidGitRepositoryError = type(
            "InvalidGitRepositoryError", (Exception,), {}
        )
        mock_git.Repo.side_effect = [
            mock_git.exc.NoSuchPathError("first"),
            mock_git.exc.NoSuchPathError("second"),
        ]

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            with pytest.raises(Exception, match="are not git directories"):
                main(**_default_kwargs())

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_such_path_retries_with_base_dir(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        """NoSuchPathError (not just InvalidGitRepositoryError) also triggers retry."""
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.exc.NoSuchPathError = type("NoSuchPathError", (Exception,), {})
        mock_git.exc.InvalidGitRepositoryError = type(
            "InvalidGitRepositoryError", (Exception,), {}
        )
        good_repo = MagicMock()
        mock_git.Repo.side_effect = [
            mock_git.exc.NoSuchPathError("not a path"),
            good_repo,
        ]
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        assert mock_git.Repo.call_count == 2


# ===================================================================
# 2) Remote branch resolution  (lines 135-151)
# ===================================================================


class TestRemoteBranchResolution:
    """When the branch is not in local_repo.branches, main() iterates
    remotes, fetches, and searches remote.refs for a match.
    """

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_remote_branch_fetches_and_resolves(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_repo = MagicMock()
        mock_git.Repo.return_value = mock_repo
        mock_repo.branches = []  # branch not local

        remote_ref = MagicMock()
        remote_ref.remote_head = "feat-branch"
        remote_ref.name = "origin/feat-branch"
        remote = MagicMock()
        remote.refs = [remote_ref]
        mock_repo.remotes = [remote]
        mock_repo.commit.return_value.hexsha = "remote_hexsha"
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "test-repo", "feat-branch")

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(branch="feat-branch"))

        remote.fetch.assert_called_once()
        mock_gen_patch.assert_called_once_with(mock_repo, "abc123", "remote_hexsha")

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_remote_branch_found_on_second_remote(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        """Branch not on first remote, found on second."""
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_repo = MagicMock()
        mock_git.Repo.return_value = mock_repo
        mock_repo.branches = []

        # First remote has no matching ref
        remote1 = MagicMock()
        unrelated_ref = MagicMock()
        unrelated_ref.remote_head = "other-branch"
        remote1.refs = [unrelated_ref]

        # Second remote has the target
        remote2 = MagicMock()
        target_ref = MagicMock()
        target_ref.remote_head = "feat-branch"
        target_ref.name = "upstream/feat-branch"
        remote2.refs = [target_ref]

        mock_repo.remotes = [remote1, remote2]
        mock_repo.commit.return_value.hexsha = "upstream_hexsha"
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "test-repo", "feat-branch")

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(branch="feat-branch"))

        remote1.fetch.assert_called_once()
        remote2.fetch.assert_called_once()
        mock_repo.commit.assert_called_with("upstream/feat-branch")

    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_branch_not_found_anywhere_raises(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_repo = MagicMock()
        mock_git.Repo.return_value = mock_repo
        mock_repo.branches = []
        remote = MagicMock()
        remote.refs = []
        mock_repo.remotes = [remote]

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            with pytest.raises(Exception, match="does not exist locally or remotely"):
                main(**_default_kwargs(branch="nonexistent"))

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_local_branch_resolved_without_fetch(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        """When branch IS in local_repo.branches, no remote fetch occurs."""
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_repo = MagicMock()
        mock_git.Repo.return_value = mock_repo
        mock_repo.branches = ["my-local"]
        mock_repo.commit.return_value.hexsha = "local_hexsha"
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "test-repo", "my-local")

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(branch="my-local"))

        # No remote.fetch should have been called
        for remote in mock_repo.remotes:
            remote.fetch.assert_not_called()
        mock_gen_patch.assert_called_once_with(mock_repo, "abc123", "local_hexsha")


# ===================================================================
# 3) Post-Docker execution  (lines 177-250)
# ===================================================================


class TestPostDockerExecution:
    """Exit code file reading (incl. FileNotFoundError / ValueError fallback),
    verbose output, close_logger in finally, and exception propagation.
    """

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_exit_code_read_and_passed_to_sys_exit(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path, code="0")

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        mock_sys.exit.assert_called_once_with(0)

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_exit_code_nonzero(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path, code="5")

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        mock_sys.exit.assert_called_once_with(5)

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_exit_code_file_missing_defaults_to_1(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        """When test_exit_code.txt does not exist, exit code defaults to 1."""
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        # Create log dir but do NOT write exit code file
        log_dir = tmp_path / "test-repo" / "reference" / "abcdef1234567890abcdef"
        log_dir.mkdir(parents=True, exist_ok=True)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        mock_sys.exit.assert_called_once_with(1)

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_exit_code_file_not_int_defaults_to_1(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        """When test_exit_code.txt contains non-integer text, defaults to 1."""
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        # Write non-integer content
        exit_file = (
            tmp_path
            / "test-repo"
            / "reference"
            / "abcdef1234567890abcdef"
            / "test_exit_code.txt"
        )
        exit_file.parent.mkdir(parents=True, exist_ok=True)
        exit_file.write_text("not_a_number")

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        mock_sys.exit.assert_called_once_with(1)

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_close_logger_called_on_success(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _, logger = _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        mock_close_logger.assert_called_once_with(logger)

    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_close_logger_called_on_general_exception(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        tmp_path,
    ):
        """close_logger is called in the finally block even when Docker raises."""
        example = _make_repo_example()
        _, logger = _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        mock_docker.return_value.__enter__ = MagicMock(
            side_effect=TypeError("docker crashed")
        )
        mock_docker.return_value.__exit__ = MagicMock(return_value=False)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            with pytest.raises(RuntimeError, match="General error"):
                main(**_default_kwargs())

        mock_close_logger.assert_called_once_with(logger)

    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_timeout_raises_evaluation_error(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker, timed_out=True)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            with pytest.raises(EvaluationError):
                main(**_default_kwargs())

    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_close_logger_called_on_timeout(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        tmp_path,
    ):
        """close_logger is called even when EvaluationError is raised."""
        example = _make_repo_example()
        _, logger = _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker, timed_out=True)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            with pytest.raises(EvaluationError):
                main(**_default_kwargs())

        mock_close_logger.assert_called_once_with(logger)

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_verbose_prints_test_output(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)  # also writes test_output.txt

        with (
            patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path),
            patch("builtins.print") as mock_print,
        ):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(verbose=1))

        mock_print.assert_called_once()
        assert "test output content" in mock_print.call_args[0][0]

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_verbose_zero_no_print(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with (
            patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path),
            patch("builtins.print") as mock_print,
        ):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(verbose=0))

        mock_print.assert_not_called()

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="the_patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_patch_and_eval_files_written(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs())

        log_dir = tmp_path / "test-repo" / "reference" / "abcdef1234567890abcdef"
        assert (log_dir / "patch.diff").exists()
        assert (log_dir / "eval.sh").exists()
        assert "the_patch" in (log_dir / "patch.diff").read_text()

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_docker_called_with_correct_args(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        spec, logger = _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(timeout=999, num_cpus=4, rebuild_image=True))

        init_args = mock_docker.call_args
        assert init_args[0][0] is spec
        assert init_args[0][1] is logger
        assert init_args[0][2] == 999
        assert init_args[0][3] == 4
        assert init_args[0][7] is True


# ===================================================================
# 4) Misc / edge cases
# ===================================================================


class TestMiscEdgeCases:
    """Non-local backend rejection, no spec, reference branch, _inject_test_ids."""

    def test_no_matching_spec_raises(self):
        example = _make_repo_example(repo="org/unrelated-repo")
        with (
            patch(f"{MODULE}.load_dataset_from_config", return_value=list([example])),
            patch(f"{MODULE}.make_ts_spec") as mock_make_spec,
        ):
            mock_make_spec.return_value = None
            from commit0.harness.run_ts_tests import main

            with pytest.raises(ValueError, match="No spec available"):
                main(**_default_kwargs(repo_or_repo_dir="/repos/test-repo"))

    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_non_local_backend_raises_value_error(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            with pytest.raises(ValueError, match="TS pipeline supports LOCAL"):
                main(**_default_kwargs(backend="modal"))

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_reference_branch_uses_reference_commit(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example(reference_commit="ref_commit_hash")
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_repo = MagicMock()
        mock_git.Repo.return_value = mock_repo
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(branch="reference"))

        # Should use reference_commit as commit_id
        mock_gen_patch.assert_called_once_with(mock_repo, "abc123", "ref_commit_hash")

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_ts_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_trailing_slash_stripped(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_repo_example()
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", tmp_path):
            from commit0.harness.run_ts_tests import main

            main(**_default_kwargs(repo_or_repo_dir="/repos/test-repo/"))

        mock_make_spec.assert_called_once()


# ===================================================================
# 5) _inject_test_ids unit tests
# ===================================================================


class TestInjectTestIds:
    """Unit tests for the _inject_test_ids helper."""

    def test_empty_test_ids_returns_unchanged(self):
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "#!/bin/bash\nnpx jest --forceExit\n"
        assert _inject_test_ids(script, "") == script

    def test_appends_to_force_exit_line(self):
        from commit0.harness.run_ts_tests import _inject_test_ids

        # Only the test-invocation line (identified by its ``>`` output
        # redirect) is rewritten; ids are appended at the end of that line.
        script = "#!/bin/bash\nnpx jest --forceExit > test_output.txt 2>&1\necho done"
        result = _inject_test_ids(script, "src/foo.test.ts")
        assert "> test_output.txt 2>&1 src/foo.test.ts" in result
        assert "echo done" in result

    def test_appends_to_vitest_line(self):
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "#!/bin/bash\nnpx vitest run > test_output.txt 2>&1\necho done"
        result = _inject_test_ids(script, "src/bar.test.ts")
        assert "> test_output.txt 2>&1 src/bar.test.ts" in result

    def test_no_matching_line_returns_unchanged(self):
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "#!/bin/bash\necho hello\n"
        result = _inject_test_ids(script, "test.ts")
        assert result == script
