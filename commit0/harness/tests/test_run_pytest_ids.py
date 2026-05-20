from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.constants import RepoInstance, SimpleInstance
from commit0.harness.utils import EvaluationError

MODULE = "commit0.harness.run_pytest_ids"


def _default_kwargs(**overrides):
    defaults = dict(
        dataset_name="commit0_combined",
        dataset_split="test",
        base_dir="/base",
        repo_or_repo_dir="/repos/test-repo",
        branch="reference",
        test_ids="test_foo.py::test_bar",
        coverage=False,
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
            "python": "3.12",
            "packages": "requirements.txt",
            "install": "pip install -e .",
        },
        test={
            "test_cmd": "pytest",
            "patch": "diff --git a/f.py",
            "test_patch": "diff --git a/t.py",
        },
        src_dir="src",
    )
    defaults.update(overrides)
    return RepoInstance(**defaults)


def _make_simple_example(**overrides):
    defaults = dict(
        instance_id="humaneval/1",
        prompt="Write hello",
        canonical_solution="print('hello')",
        test="assert True",
    )
    defaults.update(overrides)
    return SimpleInstance(**defaults)


def _make_spec_mock():
    spec = MagicMock()
    spec.eval_script = "bash -c 'pytest {test_ids}{coverage}'"
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
        / "pytest_exit_code.txt"
    )
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text(code)
    test_output = exit_file.parent / "test_output.txt"
    test_output.write_text("test output content")


def _write_exit_code_for(tmp_path, repo_name, branch, code="0"):
    exit_file = (
        tmp_path
        / repo_name
        / branch
        / "abcdef1234567890abcdef"
        / "pytest_exit_code.txt"
    )
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    exit_file.write_text(code)
    test_output = exit_file.parent / "test_output.txt"
    test_output.write_text("test output content")


class TestDatasetTypeDetection:
    @pytest.mark.parametrize(
        "dataset_name,expected_type,instance_id,repo_or_repo_dir",
        [
            ("swe_bench_lite", "swebench", "test/repo", "/repos/test/repo"),
            ("my_swe_dataset", "swebench", "test/repo", "/repos/test/repo"),
            ("humaneval_python", "simple", "humaneval/1", "/repos/humaneval/1"),
            ("mbpp_dataset", "simple", "humaneval/1", "/repos/humaneval/1"),
            ("bigcodebench_v2", "simple", "humaneval/1", "/repos/humaneval/1"),
            ("codecontests_train", "simple", "humaneval/1", "/repos/humaneval/1"),
            ("commit0_combined", "commit0", "test/repo", "/repos/test-repo"),
            ("my_custom_dataset", "commit0", "test/repo", "/repos/test-repo"),
        ],
    )
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dataset_type(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_gen_patch,
        mock_git,
        mock_docker,
        mock_sys,
        dataset_name,
        expected_type,
        instance_id,
        repo_or_repo_dir,
        tmp_path,
    ):
        if expected_type == "simple":
            example = _make_simple_example(instance_id=instance_id)
        else:
            example = _make_repo_example(instance_id=instance_id)

        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
        )
        mock_git.Repo.return_value = MagicMock()
        mock_gen_patch.return_value = "some patch"
        _setup_docker_ctx(mock_docker)

        repo_basename = instance_id if expected_type != "commit0" else "test-repo"
        _write_exit_code_for(tmp_path, repo_basename, "reference")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    dataset_name=dataset_name,
                    repo_or_repo_dir=repo_or_repo_dir,
                )
            )

        mock_make_spec.assert_called_once()
        actual_type = mock_make_spec.call_args[0][1]
        assert actual_type == expected_type


class TestRepoMatching:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_matches_repo_in_basename(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_gen_patch,
        mock_git,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example1 = _make_repo_example(repo="org/other-repo")
        example2 = _make_repo_example(repo="org/test-repo")
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example1, example2],
        )
        mock_git.Repo.return_value = MagicMock()
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(repo_or_repo_dir="/repos/test-repo"))

        assert mock_make_spec.call_args[0][0] == example2

    def test_no_matching_spec_raises(self):
        example = _make_repo_example(repo="org/unrelated-repo")
        with (
            patch(f"{MODULE}.load_dataset_from_config", return_value=list([example])),
            patch(f"{MODULE}.make_spec") as mock_make_spec,
        ):
            mock_make_spec.return_value = None
            from commit0.harness.run_pytest_ids import main

            with pytest.raises(ValueError, match="No spec available"):
                main(**_default_kwargs(repo_or_repo_dir="/repos/test-repo"))


class TestTrailingSlashRemoval:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_trailing_slash_stripped(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_gen_patch,
        mock_git,
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
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(repo_or_repo_dir="/repos/test-repo/"))

        mock_make_spec.assert_called_once()


class TestBranchResolution:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference"))
            mock_sys.exit.assert_called_once_with(0)
        mock_load.return_value = list([example])
        mock_sys.exit.reset_mock()
        mock_gen_patch.reset_mock()
        mock_repo.branches = ["my-branch"]
        mock_repo.commit.return_value.hexsha = "local_hexsha"
        _write_exit_code_for(tmp_path, "test-repo", "my-branch")
        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="my-branch"))

        mock_gen_patch.assert_called_once_with(mock_repo, "abc123", "local_hexsha")

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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
        mock_repo.branches = []
        remote_ref = MagicMock()
        remote_ref.remote_head = "remote-branch"
        remote_ref.name = "origin/remote-branch"
        remote = MagicMock()
        remote.refs = [remote_ref]
        mock_repo.remotes = [remote]
        mock_repo.commit.return_value.hexsha = "remote_hexsha"
        mock_gen_patch.return_value = "patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "test-repo", "remote-branch")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="remote-branch"))

        remote.fetch.assert_called_once()
        mock_gen_patch.assert_called_once_with(mock_repo, "abc123", "remote_hexsha")

    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_branch_not_found_raises(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            with pytest.raises(Exception, match="does not exist locally or remotely"):
                main(**_default_kwargs(branch="nonexistent"))


class TestGitRepoRetry:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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
        mock_git.exc.NoSuchPathError = type("NoSuchPathError", (Exception,), {})
        mock_git.exc.InvalidGitRepositoryError = type(
            "InvalidGitRepositoryError", (Exception,), {}
        )
        mock_git.Repo.side_effect = [
            mock_git.exc.InvalidGitRepositoryError("bad"),
            MagicMock(),
        ]
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs())

        assert mock_git.Repo.call_count == 2

    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            with pytest.raises(Exception, match="are not git directories"):
                main(**_default_kwargs())


class TestSWEDataset:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_reference_uses_example_patches(
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
        example = _make_repo_example(instance_id="test/repo")
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
        _write_exit_code_for(tmp_path, "test/repo", "reference")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    dataset_name="swe_bench",
                    branch="reference",
                    repo_or_repo_dir="/repos/test/repo",
                )
            )

        mock_gen_patch.assert_not_called()
        patch_file = (
            tmp_path
            / "test/repo"
            / "reference"
            / "abcdef1234567890abcdef"
            / "patch.diff"
        )
        content = patch_file.read_text()
        assert "diff --git a/f.py" in content
        assert "diff --git a/t.py" in content

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_swe_non_reference_generates_and_appends_test_patch(
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
        example = _make_repo_example(instance_id="test/repo")
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
        mock_repo.branches = ["dev"]
        mock_repo.commit.return_value.hexsha = "dev_hexsha"
        mock_gen_patch.return_value = "generated_patch"
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "test/repo", "dev")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    dataset_name="swe_bench",
                    branch="dev",
                    repo_or_repo_dir="/repos/test/repo",
                )
            )

        patch_file = (
            tmp_path / "test/repo" / "dev" / "abcdef1234567890abcdef" / "patch.diff"
        )
        content = patch_file.read_text()
        assert "generated_patch" in content
        assert "diff --git a/t.py" in content


class TestCommit0Dataset:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_commit0_generates_patch_only(
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
        mock_gen_patch.return_value = "commit0_patch_content"
        _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference"))

        mock_gen_patch.assert_called_once()
        patch_file = (
            tmp_path
            / "test-repo"
            / "reference"
            / "abcdef1234567890abcdef"
            / "patch.diff"
        )
        assert patch_file.read_text() == "commit0_patch_content"


class TestSimpleDataset:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_simple_reference_uses_prompt_solution_test(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_simple_example()
        spec = _make_spec_mock()
        spec.eval_script = "pytest solution.py"
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
            spec=spec,
        )
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "humaneval/1", "reference")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    dataset_name="humaneval_python",
                    branch="reference",
                    repo_or_repo_dir="/repos/humaneval/1",
                )
            )

        patch_file = (
            tmp_path
            / "humaneval/1"
            / "reference"
            / "abcdef1234567890abcdef"
            / "patch.diff"
        )
        content = patch_file.read_text()
        assert "Write hello" in content
        assert "print('hello')" in content
        assert "assert True" in content

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.extract_code_blocks")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_simple_non_reference_with_code_blocks(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_extract,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_simple_example()
        spec = _make_spec_mock()
        spec.eval_script = "pytest solution.py"
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
            spec=spec,
        )
        mock_extract.return_value = ["block1", "block2"]
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "humaneval/1", "dev")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    dataset_name="humaneval_python",
                    branch="dev",
                    repo_or_repo_dir="/repos/humaneval/1",
                    test_ids="```python\ncode\n```",
                )
            )

        patch_file = (
            tmp_path / "humaneval/1" / "dev" / "abcdef1234567890abcdef" / "patch.diff"
        )
        content = patch_file.read_text()
        assert "block1" in content
        assert "block2" in content
        assert "assert True" in content

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.extract_code_blocks")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_simple_non_reference_no_code_blocks_uses_prompt(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_extract,
        mock_docker,
        mock_sys,
        tmp_path,
    ):
        example = _make_simple_example()
        spec = _make_spec_mock()
        spec.eval_script = "pytest solution.py"
        _setup_base_mocks(
            mock_load,
            mock_make_spec,
            mock_setup_logger,
            mock_close_logger,
            mock_get_hash,
            [example],
            spec=spec,
        )
        mock_extract.return_value = []
        _setup_docker_ctx(mock_docker)
        _write_exit_code_for(tmp_path, "humaneval/1", "dev")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    dataset_name="humaneval_python",
                    branch="dev",
                    repo_or_repo_dir="/repos/humaneval/1",
                    test_ids="raw solution code",
                )
            )

        patch_file = (
            tmp_path / "humaneval/1" / "dev" / "abcdef1234567890abcdef" / "patch.diff"
        )
        content = patch_file.read_text()
        assert "Write hello" in content
        assert "raw solution code" in content


class TestCoverageFlag:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_coverage_true_adds_cov_text(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(coverage=True, branch="reference"))

        eval_file = (
            tmp_path / "test-repo" / "reference" / "abcdef1234567890abcdef" / "eval.sh"
        )
        content = eval_file.read_text()
        assert "--cov=" in content

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_coverage_false_no_cov_text(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(coverage=False, branch="reference"))

        eval_file = (
            tmp_path / "test-repo" / "reference" / "abcdef1234567890abcdef" / "eval.sh"
        )
        content = eval_file.read_text()
        assert "--cov" not in content

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_coverage_adds_coverage_json_to_files_to_collect(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(coverage=True, branch="reference"))

        init_args = mock_docker.call_args
        files_to_collect = init_args[0][6]
        assert "coverage.json" in files_to_collect


class TestBackendSelection:
    @pytest.mark.parametrize(
        "backend_str,expected_cls_name",
        [
            ("local", "Docker"),
            ("LOCAL", "Docker"),
            ("modal", "Modal"),
            ("MODAL", "Modal"),
            ("e2b", "E2B"),
            ("E2B", "E2B"),
        ],
    )
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.E2B")
    @patch(f"{MODULE}.Modal")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_backend_selects_correct_context(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_docker,
        mock_modal,
        mock_e2b,
        mock_sys,
        backend_str,
        expected_cls_name,
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

        for m in [mock_docker, mock_modal, mock_e2b]:
            ctx = MagicMock()
            ctx.exec_run_with_timeout.return_value = ("output", False, 10.0)
            m.return_value.__enter__ = MagicMock(return_value=ctx)
            m.return_value.__exit__ = MagicMock(return_value=False)

        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(backend=backend_str))

        target_map = {"Docker": mock_docker, "Modal": mock_modal, "E2B": mock_e2b}
        target_map[expected_cls_name].assert_called_once()

    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_invalid_backend_raises_value_error(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            with pytest.raises(ValueError):
                main(**_default_kwargs(backend="invalid_backend"))


class TestEvalCommandPath:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.E2B")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_e2b_uses_relative_eval_command(
        self,
        mock_load,
        mock_make_spec,
        mock_setup_logger,
        mock_close_logger,
        mock_get_hash,
        mock_git,
        mock_gen_patch,
        mock_e2b,
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
        ctx = MagicMock()
        ctx.exec_run_with_timeout.return_value = ("output", False, 10.0)
        mock_e2b.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_e2b.return_value.__exit__ = MagicMock(return_value=False)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(backend="e2b"))

        ctx.exec_run_with_timeout.assert_called_once_with("/bin/bash eval.sh")

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_local_uses_absolute_eval_command(
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
        ctx = _setup_docker_ctx(mock_docker)
        _write_exit_code(tmp_path)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(backend="local"))

        ctx.exec_run_with_timeout.assert_called_once_with("/bin/bash /eval.sh")


class TestTimeout:
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            with pytest.raises(EvaluationError):
                main(**_default_kwargs(branch="reference"))


class TestExitCode:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_reads_pytest_exit_code_and_raises_on_failure(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference"))
            mock_sys.exit.assert_called_once_with(5)

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_exit_code_zero_completes_normally(
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
        _write_exit_code(tmp_path, code="0\n")

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference"))


class TestGeneralException:
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_general_exception_raises_runtime_error(
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
        mock_docker.return_value.__enter__ = MagicMock(
            side_effect=TypeError("docker crashed")
        )
        mock_docker.return_value.__exit__ = MagicMock(return_value=False)

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            with pytest.raises(RuntimeError, match="General error"):
                main(**_default_kwargs(branch="reference"))


class TestVerboseOutput:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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
        _write_exit_code(tmp_path)

        with (
            patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path),
            patch("builtins.print") as mock_print,
        ):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference", verbose=1))

        mock_print.assert_called_once()

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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
            patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path),
            patch("builtins.print") as mock_print,
        ):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference", verbose=0))

        mock_print.assert_not_called()


class TestExecutionContextUsage:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_context_manager_called_with_correct_args(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(
                **_default_kwargs(
                    branch="reference",
                    timeout=999,
                    num_cpus=4,
                    rebuild_image=True,
                )
            )

        init_args = mock_docker.call_args
        assert init_args[0][0] is spec
        assert init_args[0][1] is logger
        assert init_args[0][2] == 999
        assert init_args[0][3] == 4
        assert init_args[0][7] is True

    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_close_logger_called_after_context(
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference"))

        mock_close_logger.assert_called_once_with(logger)


class TestFilesWritten:
    @patch(f"{MODULE}.sys")
    @patch(f"{MODULE}.Docker")
    @patch(f"{MODULE}.generate_patch_between_commits", return_value="the_patch")
    @patch(f"{MODULE}.git")
    @patch(f"{MODULE}.get_hash_string")
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}.make_spec")
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

        with patch(f"{MODULE}.RUN_PYTEST_LOG_DIR", tmp_path):
            from commit0.harness.run_pytest_ids import main

            main(**_default_kwargs(branch="reference"))

        log_dir = tmp_path / "test-repo" / "reference" / "abcdef1234567890abcdef"
        assert (log_dir / "patch.diff").exists()
        assert (log_dir / "eval.sh").exists()
        assert "the_patch" in (log_dir / "patch.diff").read_text()
