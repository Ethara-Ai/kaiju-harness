"""Tests for agent.run_agent_ts — per-repo orchestration and dataset
filtering.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

MODULE = "agent.run_agent_ts"


def _make_example(**overrides: Any) -> dict:
    defaults = {
        "repo": "org/my-lib",
        "instance_id": "commit-0/my-lib",
        "base_commit": "abc123",
        "reference_commit": "def456",
        "src_dir": "src",
        "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
        "setup": {"node": "20", "install": "npm install"},
    }
    defaults.update(overrides)
    return defaults


def _make_agent_config(**overrides: Any) -> MagicMock:
    defaults = {
        "agent_name": "aider",
        "model_name": "test-model",
        "model_short": "test",
        "use_user_prompt": True,
        "user_prompt": "Implement the stubbed functions.",
        "use_topo_sort_dependencies": False,
        "add_import_module_to_context": False,
        "use_repo_info": False,
        "max_repo_info_length": 2000,
        "use_unit_tests_info": True,
        "max_unit_tests_info_length": 5000,
        "use_spec_info": False,
        "max_spec_info_length": 3000,
        "use_lint_info": False,
        "run_entire_dir_lint": False,
        "max_lint_info_length": 2000,
        "pre_commit_config_path": "",
        "run_tests": False,
        "max_iteration": 3,
        "record_test_for_each_commit": False,
        "cache_prompts": True,
        "spec_summary_max_tokens": 4000,
        "max_test_output_length": 15000,
        "capture_thinking": False,
        "trajectory_md": True,
        "output_jsonl": False,
        # These default False in the real AgentConfig. Without explicit values a bare
        # MagicMock returns TRUTHY auto-attributes — in particular a truthy
        # `strip_non_stubs` makes run_agent_ts filter out target files that don't
        # exist on disk (the mocked paths), emptying the loop so _mark_module_done is
        # never called. Pin them to realistic defaults.
        "strip_non_stubs": False,
        "blind_tests": False,
        "blind_lint": False,
        "names_only_tests": False,
        "inject_test_files_readonly": False,
    }
    defaults.update(overrides)
    cfg = MagicMock()
    for k, v in defaults.items():
        setattr(cfg, k, v)
    cfg.keys = MagicMock(return_value=list(defaults.keys()))
    return cfg


# ---------------------------------------------------------------------------
# run_agent_ts_impl — dataset filtering
# ---------------------------------------------------------------------------


class TestRunAgentTsImplFiltering:
    @patch(f"{MODULE}.run_agent_for_repo_ts")
    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_all_split_processes_everything(
        self, mock_load_agent, mock_load_dataset, mock_read_config, mock_run_agent
    ) -> None:
        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/repos",
        }
        examples = [_make_example(repo="org/a"), _make_example(repo="org/b")]
        mock_load_dataset.return_value = examples

        with patch(f"{MODULE}.multiprocessing.Pool") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.apply_async.return_value = MagicMock()
            mock_pool.apply_async.return_value.get.return_value = None
            mock_pool_cls.return_value = mock_pool

            run_agent_ts_impl(
                branch="commit0",
                override_previous_changes=False,
                backend="local",
                agent_config_file=".agent.yaml",
                commit0_config_file=".commit0.ts.yaml",
                log_dir="logs",
                max_parallel_repos=1,
            )

        assert mock_pool.apply_async.call_count == 2

    @patch(f"{MODULE}.run_agent_for_repo_ts")
    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_named_split_filters(
        self, mock_load_agent, mock_load_dataset, mock_read_config, mock_run_agent
    ) -> None:
        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "my-lib",
            "base_dir": "/repos",
        }
        examples = [
            _make_example(repo="org/my-lib"),
            _make_example(repo="org/other"),
        ]
        mock_load_dataset.return_value = examples

        with patch(f"{MODULE}.multiprocessing.Pool") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.apply_async.return_value = MagicMock()
            mock_pool.apply_async.return_value.get.return_value = None
            mock_pool_cls.return_value = mock_pool

            run_agent_ts_impl(
                branch="commit0",
                override_previous_changes=False,
                backend="local",
                agent_config_file=".agent.yaml",
                commit0_config_file=".commit0.ts.yaml",
                log_dir="logs",
                max_parallel_repos=1,
            )

        assert mock_pool.apply_async.call_count == 1

    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_empty_dataset_raises(
        self, mock_load_agent, mock_load_dataset, mock_read_config
    ) -> None:
        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "nonexistent",
            "base_dir": "/repos",
        }
        mock_load_dataset.return_value = []

        with pytest.raises(ValueError, match="No examples matched"):
            run_agent_ts_impl(
                branch="commit0",
                override_previous_changes=False,
                backend="local",
                agent_config_file=".agent.yaml",
                commit0_config_file=".commit0.ts.yaml",
                log_dir="logs",
                max_parallel_repos=1,
            )

    @patch(f"{MODULE}.run_agent_for_repo_ts")
    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_hyphen_underscore_normalization(
        self, mock_load_agent, mock_load_dataset, mock_read_config, mock_run_agent
    ) -> None:
        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "my_lib",
            "base_dir": "/repos",
        }
        examples = [_make_example(repo="org/my-lib")]
        mock_load_dataset.return_value = examples

        with patch(f"{MODULE}.multiprocessing.Pool") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)
            mock_pool.apply_async.return_value = MagicMock()
            mock_pool.apply_async.return_value.get.return_value = None
            mock_pool_cls.return_value = mock_pool

            run_agent_ts_impl(
                branch="commit0",
                override_previous_changes=False,
                backend="local",
                agent_config_file=".agent.yaml",
                commit0_config_file=".commit0.ts.yaml",
                log_dir="logs",
                max_parallel_repos=1,
            )

        assert mock_pool.apply_async.call_count == 1


# ---------------------------------------------------------------------------
# run_agent_for_repo_ts
# ---------------------------------------------------------------------------


class TestRunAgentForRepoTs:
    def test_unsupported_agent_raises(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config(agent_name="unsupported")

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo") as mock_repo_cls:
                mock_repo_cls.return_value = MagicMock()
                with pytest.raises(NotImplementedError, match="not implemented"):
                    run_agent_for_repo_ts(
                        str(tmp_path), agent_config, example, "commit0"
                    )

    def test_invalid_repo_path_raises(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config()

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", side_effect=Exception("not a git repo")):
                with pytest.raises(Exception, match="not a git repo"):
                    run_agent_for_repo_ts(
                        "/nonexistent", agent_config, example, "commit0"
                    )

    def test_aider_agent_created(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config(agent_name="aider")

        # Create the repo subdir so DirContext can chdir into it
        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent = MagicMock()
                                mock_agent.run.return_value = MagicMock()
                                mock_agent_cls.return_value = mock_agent
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=tmp_path,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            run_agent_for_repo_ts(
                                                str(tmp_path),
                                                agent_config,
                                                example,
                                                "commit0",
                                            )

                                mock_agent_cls.assert_called_once()

    def test_dirty_repo_auto_commits(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config()

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = True
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=tmp_path,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            run_agent_for_repo_ts(
                                                str(tmp_path),
                                                agent_config,
                                                example,
                                                "commit0",
                                            )

        mock_repo.git.add.assert_called_once_with(A=True)
        mock_repo.index.commit.assert_called_once()

    def test_override_previous_changes_resets(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config()

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = "different_sha"
        mock_repo.head.commit.hexsha = "different_sha"

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=tmp_path,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            run_agent_for_repo_ts(
                                                str(tmp_path),
                                                agent_config,
                                                example,
                                                "commit0",
                                                override_previous_changes=True,
                                            )

        mock_repo.git.reset.assert_called_once_with("--hard", example["base_commit"])

    # -----------------------------------------------------------------------
    # Shared helper for new coverage tests
    # -----------------------------------------------------------------------

    def _run_with_patches(
        self,
        tmp_path: Path,
        agent_config: MagicMock,
        example: dict,
        *,
        test_ids: list[list[str]] | None = None,
        target_edit_files: list[str] | None = None,
        extra_patches: dict | None = None,
        override_previous_changes: bool = False,
        mock_repo_override: MagicMock | None = None,
        create_test_files: list[str] | None = None,
    ) -> dict:
        """Shared helper that sets up all the boilerplate patches and returns
        the mocks dict so callers can inspect them.

        *create_test_files*: relative paths under ``tmp_path/my-lib`` that
        will be created on disk so that ``Path.exists()`` returns True
        naturally (no monkey-patching needed).
        """
        if test_ids is None:
            test_ids = [[]]
        if target_edit_files is None:
            target_edit_files = []

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        # Materialise test files on disk when requested
        for rel in create_test_files or []:
            fp = repo_dir / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.touch()

        if mock_repo_override is not None:
            mock_repo = mock_repo_override
        else:
            mock_repo = MagicMock()
            mock_repo.is_dirty.return_value = False
            mock_repo.commit.return_value.hexsha = example["base_commit"]
            mock_repo.head.commit.hexsha = example["base_commit"]
            mock_repo.working_dir = str(repo_dir)

        mock_agent = MagicMock()
        mock_agent.run.return_value = MagicMock()

        mocks: dict[str, MagicMock] = {}

        # The log dir must exist on disk for yaml.dump to succeed
        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        base_patches = {
            f"{MODULE}.read_commit0_ts_config_file": {
                "return_value": {"dataset_name": "test.json"}
            },
            f"{MODULE}.Repo": {"return_value": mock_repo},
            f"{MODULE}.create_branch": {},
            f"{MODULE}.get_target_edit_files_ts": {
                "return_value": (target_edit_files, {})
            },
            f"{MODULE}.get_ts_tests": {"return_value": test_ids},
            f"{MODULE}.TsAiderAgents": {"return_value": mock_agent},
            f"{MODULE}._get_stable_log_dir": {"return_value": log_dir},
            f"{MODULE}.get_message_ts": {"return_value": ("test message", [])},
            f"{MODULE}.yaml.dump": {},
            f"{MODULE}._is_module_done": {"return_value": False},
            f"{MODULE}._mark_module_done": {},
            f"{MODULE}.get_ts_lint_cmd": {"return_value": "npx eslint ."},
            f"{MODULE}.get_changed_ts_files_from_commits": {"return_value": []},
        }

        if extra_patches:
            base_patches.update(extra_patches)

        patchers = []
        for target, kwargs in base_patches.items():
            p = patch(target, **kwargs)
            m = p.start()
            patchers.append(p)
            short_name = target.split(".")[-1]
            mocks[short_name] = m

        mocks["repo"] = mock_repo
        mocks["agent"] = mock_agent

        try:
            from agent.run_agent_ts import run_agent_for_repo_ts

            run_agent_for_repo_ts(
                str(tmp_path),
                agent_config,
                example,
                "commit0",
                override_previous_changes=override_previous_changes,
            )
        finally:
            for p in reversed(patchers):
                p.stop()

        return mocks

    # -----------------------------------------------------------------------
    # Test-file path resolution (lines 127-136)
    # -----------------------------------------------------------------------

    def test_test_file_resolves_direct_path(self, tmp_path: Path) -> None:
        """Test file found at repo_path/tf is included directly."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/foo.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/foo.test.ts"],
        )
        mocks["agent"].run.assert_called()

    def test_test_file_resolves_with_test_dir_prefix(self, tmp_path: Path) -> None:
        """Test file found at repo_path/test_dir/tf is resolved with prefix."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        # The test dir from the example is "__tests__".
        # get_ts_tests returns "foo.test.ts" (bare), it does NOT exist at
        # repo_path/foo.test.ts but DOES exist at repo_path/__tests__/foo.test.ts.
        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["foo.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["__tests__/foo.test.ts"],
        )
        mocks["agent"].run.assert_called()

    def test_test_file_not_found_logs_warning(self, tmp_path: Path, caplog) -> None:
        """Missing test files are skipped with a warning."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        import logging

        with caplog.at_level(logging.WARNING, logger="agent.run_agent_ts"):
            mocks = self._run_with_patches(
                tmp_path,
                agent_config,
                example,
                test_ids=[["nonexistent.test.ts"]],
                target_edit_files=["src/index.ts"],
                # No files created on disk -- resolution will fail
            )
        mocks["agent"].run.assert_not_called()
        assert any("Test file not found" in r.message for r in caplog.records)

    # -----------------------------------------------------------------------
    # run_tests branch (lines 145-228)
    # -----------------------------------------------------------------------

    def test_run_tests_branch_calls_agent_with_test_first(self, tmp_path: Path) -> None:
        """When run_tests=True, agent.run is called with test_first=True."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/foo.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/foo.test.ts"],
        )

        mocks["agent"].run.assert_called_once()
        _, kw = mocks["agent"].run.call_args
        assert kw.get("test_first") is True

    def test_run_tests_branch_skips_done_modules(self, tmp_path: Path) -> None:
        """When a test module is already done, it is skipped."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/foo.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/foo.test.ts"],
            extra_patches={
                f"{MODULE}._is_module_done": {"return_value": True},
            },
        )
        mocks["agent"].run.assert_not_called()

    def test_run_tests_branch_marks_module_done(self, tmp_path: Path) -> None:
        """After processing a test module, _mark_module_done is called."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/foo.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/foo.test.ts"],
        )
        mocks["_mark_module_done"].assert_called_once()

    def test_run_tests_branch_calls_get_message_ts(self, tmp_path: Path) -> None:
        """run_tests branch calls get_message_ts with the test file."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/bar.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/bar.test.ts"],
        )
        mocks["get_message_ts"].assert_called()
        call_kwargs = mocks["get_message_ts"].call_args[1]
        assert call_kwargs["test_files"] == ["test/bar.test.ts"]

    def test_run_tests_branch_multiple_test_files(self, tmp_path: Path) -> None:
        """Each resolved test file triggers a separate agent.run call."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/a.test.ts", "test/b.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/a.test.ts", "test/b.test.ts"],
        )
        assert mocks["agent"].run.call_count == 2

    # -----------------------------------------------------------------------
    # run_entire_dir_lint branch (lines 244-291)
    # -----------------------------------------------------------------------

    def test_run_entire_dir_lint_branch(self, tmp_path: Path) -> None:
        """run_entire_dir_lint calls agent.run with lint_first=True per changed file."""
        example = _make_example()
        agent_config = _make_agent_config(run_entire_dir_lint=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
            extra_patches={
                f"{MODULE}.get_changed_ts_files_from_commits": {
                    "return_value": ["src/changed.ts"],
                },
            },
        )

        mocks["agent"].run.assert_called_once()
        _, kw = mocks["agent"].run.call_args
        assert kw.get("lint_first") is True
        # The agent should be called with the specific lint file
        args = mocks["agent"].run.call_args[0]
        assert args[3] == ["src/changed.ts"]

    def test_run_entire_dir_lint_skips_done(self, tmp_path: Path) -> None:
        """Already-linted files are skipped."""
        example = _make_example()
        agent_config = _make_agent_config(run_entire_dir_lint=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
            extra_patches={
                f"{MODULE}.get_changed_ts_files_from_commits": {
                    "return_value": ["src/changed.ts"],
                },
                f"{MODULE}._is_module_done": {"return_value": True},
            },
        )
        mocks["agent"].run.assert_not_called()

    def test_run_entire_dir_lint_marks_done(self, tmp_path: Path) -> None:
        """After linting a file, _mark_module_done is called."""
        example = _make_example()
        agent_config = _make_agent_config(run_entire_dir_lint=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            extra_patches={
                f"{MODULE}.get_changed_ts_files_from_commits": {
                    "return_value": ["src/a.ts", "src/b.ts"],
                },
            },
        )
        assert mocks["_mark_module_done"].call_count == 2

    def test_run_entire_dir_lint_no_changed_files(self, tmp_path: Path) -> None:
        """If no files changed, agent.run is not called."""
        example = _make_example()
        agent_config = _make_agent_config(run_entire_dir_lint=True)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            extra_patches={
                f"{MODULE}.get_changed_ts_files_from_commits": {
                    "return_value": [],
                },
            },
        )
        mocks["agent"].run.assert_not_called()

    # -----------------------------------------------------------------------
    # Default "draft" branch (lines 310-351)
    # -----------------------------------------------------------------------

    def test_draft_branch_iterates_target_edit_files(self, tmp_path: Path) -> None:
        """Default branch iterates target_edit_files and calls agent.run per file."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/a.ts", "src/b.ts"],
        )
        assert mocks["agent"].run.call_count == 2

    def test_draft_branch_skips_done_modules(self, tmp_path: Path) -> None:
        """Done modules are skipped in draft mode."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/a.ts"],
            extra_patches={
                f"{MODULE}._is_module_done": {"return_value": True},
            },
        )
        mocks["agent"].run.assert_not_called()

    def test_draft_branch_marks_done(self, tmp_path: Path) -> None:
        """After drafting a file, _mark_module_done is called."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
        )
        mocks["_mark_module_done"].assert_called_once()

    def test_draft_branch_passes_message(self, tmp_path: Path) -> None:
        """Draft branch passes the message from get_message_ts to agent.run."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
        )
        args = mocks["agent"].run.call_args[0]
        assert args[0] == "test message"

    def test_draft_branch_no_target_files(self, tmp_path: Path) -> None:
        """If no target edit files, agent.run is not called."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=[],
        )
        mocks["agent"].run.assert_not_called()

    def test_draft_branch_uses_lint_cmd(self, tmp_path: Path) -> None:
        """Draft branch passes lint_cmd from get_ts_lint_cmd to agent.run."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
        )
        args = mocks["agent"].run.call_args[0]
        assert args[2] == "npx eslint ."

    def test_draft_branch_current_stage_is_draft(self, tmp_path: Path) -> None:
        """Draft branch passes current_stage='draft' to agent.run."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=False, run_entire_dir_lint=False)

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
        )
        _, kw = mocks["agent"].run.call_args
        assert kw.get("current_stage") == "draft"

    # -----------------------------------------------------------------------
    # Thinking capture finalization (lines 366-388)
    # -----------------------------------------------------------------------

    def test_thinking_capture_writes_trajectory_md(self, tmp_path: Path) -> None:
        """When capture_thinking=True and trajectory_md=True, write_trajectory_md is invoked."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=False,
            run_entire_dir_lint=False,
            capture_thinking=True,
            trajectory_md=True,
        )

        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = []

        MagicMock()

        self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=[],
            extra_patches={
                f"{MODULE}.ThinkingCapture": {"return_value": mock_tc},
            },
        )
        # The function does a local import of write_trajectory_md inside a
        # try block. We cannot easily intercept the local import, but we
        # can verify the ThinkingCapture was used: get_metrics was called.
        mock_tc.get_metrics.assert_called()

    def test_thinking_capture_disabled_no_trajectory(self, tmp_path: Path) -> None:
        """When capture_thinking=False, no trajectory writing occurs."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=False,
            run_entire_dir_lint=False,
            capture_thinking=False,
        )

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
        )
        mocks["agent"].run.assert_called_once()

    def test_run_tests_with_thinking_capture(self, tmp_path: Path) -> None:
        """run_tests + capture_thinking calls agent.run with thinking_capture."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=True,
            capture_thinking=True,
        )

        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = [MagicMock()]
        mock_tc.get_module_metrics.return_value = {}

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            test_ids=[["test/foo.test.ts"]],
            target_edit_files=["src/index.ts"],
            create_test_files=["test/foo.test.ts"],
            extra_patches={
                f"{MODULE}.ThinkingCapture": {"return_value": mock_tc},
                "agent.openhands_formatter.write_module_output_json": {},
            },
        )
        mocks["agent"].run.assert_called_once()
        _, kw = mocks["agent"].run.call_args
        assert kw.get("thinking_capture") is mock_tc

    def test_lint_branch_with_thinking_capture(self, tmp_path: Path) -> None:
        """run_entire_dir_lint + capture_thinking passes thinking_capture to agent.run."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_entire_dir_lint=True,
            capture_thinking=True,
        )

        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = [MagicMock()]
        mock_tc.get_module_metrics.return_value = {}

        mocks = self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            extra_patches={
                f"{MODULE}.get_changed_ts_files_from_commits": {
                    "return_value": ["src/changed.ts"],
                },
                f"{MODULE}.ThinkingCapture": {"return_value": mock_tc},
                "agent.openhands_formatter.write_module_output_json": {},
            },
        )
        mocks["agent"].run.assert_called_once()
        _, kw = mocks["agent"].run.call_args
        assert kw.get("thinking_capture") is mock_tc
        assert kw.get("current_stage") == "lint"

    def test_draft_branch_with_thinking_capture_on_diff(self, tmp_path: Path) -> None:
        """Draft + thinking capture computes git diff when commit SHA changes."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=False,
            run_entire_dir_lint=False,
            capture_thinking=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        # Simulate commit SHA changing after agent.run
        mock_repo.head.commit = MagicMock()
        type(mock_repo.head.commit).hexsha = PropertyMock(
            side_effect=["abc123", "abc123", "new_sha"]
        )
        mock_repo.working_dir = str(repo_dir)
        mock_repo.git.diff.return_value = "diff output"

        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = [MagicMock()]
        mock_tc.get_module_metrics.return_value = {}

        self._run_with_patches(
            tmp_path,
            agent_config,
            example,
            target_edit_files=["src/index.ts"],
            mock_repo_override=mock_repo,
            extra_patches={
                f"{MODULE}.Repo": {"return_value": mock_repo},
                f"{MODULE}.ThinkingCapture": {"return_value": mock_tc},
                "agent.openhands_formatter.write_module_output_json": {},
            },
        )


# ---------------------------------------------------------------------------
# Additional coverage tests (lines 57, 145-147, 199, 249, 387-388, 484, 496)
# ---------------------------------------------------------------------------


class TestDatasetNameValidation:
    """Cover line 57: dataset_name must contain 'commit0' or end with '.json'."""

    def test_invalid_dataset_name_raises(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config()

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "invalid_name",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=MagicMock()):
                with pytest.raises(ValueError, match="dataset_name must contain"):
                    run_agent_for_repo_ts(
                        str(tmp_path), agent_config, example, "commit0"
                    )

    def test_dataset_name_with_commit0_passes(self, tmp_path: Path) -> None:
        """dataset_name containing 'commit0' is accepted."""
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config()

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "wentingzhao/commit0_combined",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=tmp_path,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            run_agent_for_repo_ts(
                                                str(tmp_path),
                                                agent_config,
                                                example,
                                                "commit0",
                                            )


class TestAgentConfigWriteFailure:
    """Cover lines 145-147: OSError when writing agent config YAML."""

    def test_yaml_dump_oserror_raises(self, tmp_path: Path) -> None:
        from agent.run_agent_ts import run_agent_for_repo_ts

        example = _make_example()
        agent_config = _make_agent_config()

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=tmp_path,
                                ):
                                    with patch(
                                        "builtins.open",
                                        side_effect=OSError("disk full"),
                                    ):
                                        with pytest.raises(OSError, match="disk full"):
                                            run_agent_for_repo_ts(
                                                str(tmp_path),
                                                agent_config,
                                                example,
                                                "commit0",
                                            )


class TestThinkingCaptureTrajectoryFailure:
    """Cover lines 387-388: trajectory write failure (OSError caught and warned)."""

    def test_trajectory_write_failure_warns(self, tmp_path: Path, caplog) -> None:
        """When write_trajectory_md raises, a warning is logged but no exception."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=False,
            run_entire_dir_lint=False,
            capture_thinking=True,
            trajectory_md=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        mock_tc = MagicMock()
        mock_tc.turns = [MagicMock(module="m1")]
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 42}
        mock_tc.get_module_turns.return_value = []

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        import logging

        with caplog.at_level(logging.WARNING, logger="agent.run_agent_ts"):
            with patch(
                f"{MODULE}.read_commit0_ts_config_file",
                return_value={
                    "dataset_name": "test.json",
                },
            ):
                with patch(f"{MODULE}.Repo", return_value=mock_repo):
                    with patch(f"{MODULE}.create_branch"):
                        with patch(
                            f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                        ):
                            with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                                with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                    mock_agent_cls.return_value = MagicMock()
                                    with patch(
                                        f"{MODULE}._get_stable_log_dir",
                                        return_value=log_dir,
                                    ):
                                        with patch(
                                            f"{MODULE}.get_message_ts",
                                            return_value=("msg", []),
                                        ):
                                            with patch(f"{MODULE}.yaml.dump"):
                                                with patch(
                                                    f"{MODULE}.ThinkingCapture",
                                                    return_value=mock_tc,
                                                ):
                                                    with patch(
                                                        "agent.trajectory_writer.write_trajectory_md",
                                                        side_effect=OSError(
                                                            "write failed"
                                                        ),
                                                    ):
                                                        from agent.run_agent_ts import (
                                                            run_agent_for_repo_ts,
                                                        )

                                                        run_agent_for_repo_ts(
                                                            str(tmp_path),
                                                            agent_config,
                                                            example,
                                                            "commit0",
                                                        )

        assert any(
            "Failed to write thinking capture" in r.message for r in caplog.records
        )


class TestRunAgentTsImplPoolError:
    """Pool error handling: a worker that raises before returning a status is
    ISOLATED (logged, that repo marked failed) rather than propagated, so one bad
    repo cannot abort the whole batch (parity with the JS worker isolation)."""

    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_pool_worker_exception_is_isolated_not_propagated(
        self, mock_load_agent, mock_load_dataset, mock_read_config, caplog
    ) -> None:
        import logging

        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/repos",
        }
        examples = [_make_example(repo="org/a")]
        mock_load_dataset.return_value = examples

        with patch(f"{MODULE}.multiprocessing.Pool") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool.__exit__ = MagicMock(return_value=False)

            mock_result = MagicMock()
            mock_result.get.side_effect = RuntimeError("worker crashed")
            mock_pool.apply_async.return_value = mock_result
            mock_pool_cls.return_value = mock_pool

            # Must NOT raise — the crashed worker is isolated and logged.
            with caplog.at_level(logging.ERROR, logger="agent.run_agent_ts"):
                run_agent_ts_impl(
                    branch="commit0",
                    override_previous_changes=False,
                    backend="local",
                    agent_config_file=".agent.yaml",
                    commit0_config_file=".commit0.ts.yaml",
                    log_dir="logs",
                    max_parallel_repos=1,
                )
        assert any(
            "isolating" in rec.message.lower() or "worker raised" in rec.message.lower()
            for rec in caplog.records
        ), f"expected an isolation log; got {[r.message for r in caplog.records]}"


class TestMainGuard:
    def test_module_has_main_guard(self) -> None:
        import inspect
        import agent.run_agent_ts as mod

        source = inspect.getsource(mod)
        assert 'if __name__ == "__main__"' in source
        assert "app()" in source

    def test_app_is_typer_instance(self) -> None:
        from agent.run_agent_ts import app as ts_app
        import typer

        assert isinstance(ts_app, typer.Typer)


class TestTsSplitFiltering:
    @patch(f"{MODULE}.run_agent_for_repo_ts")
    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_ts_split_named_split_nonempty(
        self, mock_load_agent, mock_load_dataset, mock_read_config, mock_run_agent
    ) -> None:
        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "custom_split",
            "base_dir": "/repos",
        }
        examples = [_make_example(repo="org/a"), _make_example(repo="org/b")]
        mock_load_dataset.return_value = examples

        with patch(f"{MODULE}.TS_SPLIT", {"all_ts": [], "custom_split": ["a", "b"]}):
            with patch(f"{MODULE}.multiprocessing.Pool") as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool.__enter__ = MagicMock(return_value=mock_pool)
                mock_pool.__exit__ = MagicMock(return_value=False)
                mock_pool.apply_async.return_value = MagicMock()
                mock_pool.apply_async.return_value.get.return_value = None
                mock_pool_cls.return_value = mock_pool

                run_agent_ts_impl(
                    branch="commit0",
                    override_previous_changes=False,
                    backend="local",
                    agent_config_file=".agent.yaml",
                    commit0_config_file=".commit0.ts.yaml",
                    log_dir="logs",
                    max_parallel_repos=1,
                )

            assert mock_pool.apply_async.call_count == 2

    @patch(f"{MODULE}.run_agent_for_repo_ts")
    @patch(f"{MODULE}.read_commit0_ts_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.load_agent_config")
    def test_ts_split_filters_non_matching(
        self, mock_load_agent, mock_load_dataset, mock_read_config, mock_run_agent
    ) -> None:
        from agent.run_agent_ts import run_agent_ts_impl

        mock_load_agent.return_value = _make_agent_config()
        mock_read_config.return_value = {
            "dataset_name": "test.json",
            "dataset_split": "test",
            "repo_split": "custom_split",
            "base_dir": "/repos",
        }
        examples = [_make_example(repo="org/a"), _make_example(repo="org/b")]
        mock_load_dataset.return_value = examples

        with patch(f"{MODULE}.TS_SPLIT", {"custom_split": ["c"]}):
            with pytest.raises(ValueError, match="No examples matched"):
                run_agent_ts_impl(
                    branch="commit0",
                    override_previous_changes=False,
                    backend="local",
                    agent_config_file=".agent.yaml",
                    commit0_config_file=".commit0.ts.yaml",
                    log_dir="logs",
                    max_parallel_repos=1,
                )


class TestWriteModuleOutputJsonLazyImport:
    """Cover lines 145-147 / 151: write_module_output_json lazy import and call."""

    def test_write_module_output_json_called_in_test_branch(
        self, tmp_path: Path
    ) -> None:
        """When capture_thinking is True and module_turns is non-empty,
        write_module_output_json is called for each test module.
        """
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=True,
            capture_thinking=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "test").mkdir(exist_ok=True)
        (repo_dir / "test" / "foo.test.ts").touch()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]
        mock_repo.git.diff.return_value = ""

        mock_tc = MagicMock()
        mock_tc.turns = [MagicMock(module="test__foo.test")]
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 100}
        mock_tc.get_module_turns.return_value = [MagicMock()]  # non-empty
        mock_tc.get_module_metrics.return_value = {"thinking_tokens": 50}

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts",
                        return_value=(["src/index.ts"], {}),
                    ):
                        with patch(
                            f"{MODULE}.get_ts_tests",
                            return_value=[["test/foo.test.ts"]],
                        ):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}.ThinkingCapture",
                                                return_value=mock_tc,
                                            ):
                                                with patch(
                                                    f"{MODULE}._is_module_done",
                                                    return_value=False,
                                                ):
                                                    with patch(
                                                        f"{MODULE}._mark_module_done"
                                                    ):
                                                        with patch(
                                                            f"{MODULE}.get_ts_lint_cmd",
                                                            return_value="npx eslint .",
                                                        ):
                                                            with patch(
                                                                "agent.openhands_formatter.write_module_output_json"
                                                            ) as mock_write:
                                                                with patch(
                                                                    "agent.output_writer.extract_git_patch"
                                                                ):
                                                                    with patch(
                                                                        "agent.output_writer.build_metadata",
                                                                        return_value={},
                                                                    ):
                                                                        from agent.run_agent_ts import (
                                                                            run_agent_for_repo_ts,
                                                                        )

                                                                        run_agent_for_repo_ts(
                                                                            str(
                                                                                tmp_path
                                                                            ),
                                                                            agent_config,
                                                                            example,
                                                                            "commit0",
                                                                        )

                                                            mock_write.assert_called()

    def test_write_module_output_json_called_in_draft_branch(
        self, tmp_path: Path
    ) -> None:
        """When capture_thinking is True in draft branch and module_turns is
        non-empty, write_module_output_json is called.
        """
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=False,
            run_entire_dir_lint=False,
            capture_thinking=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]
        mock_repo.git.diff.return_value = ""

        mock_tc = MagicMock()
        mock_tc.turns = [MagicMock(module="src__index")]
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 100}
        mock_tc.get_module_turns.return_value = [MagicMock()]
        mock_tc.get_module_metrics.return_value = {"thinking_tokens": 50}

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts",
                        return_value=(["src/index.ts"], {}),
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}.ThinkingCapture",
                                                return_value=mock_tc,
                                            ):
                                                with patch(
                                                    f"{MODULE}._is_module_done",
                                                    return_value=False,
                                                ):
                                                    with patch(
                                                        f"{MODULE}._mark_module_done"
                                                    ):
                                                        with patch(
                                                            f"{MODULE}.get_ts_lint_cmd",
                                                            return_value="npx eslint .",
                                                        ):
                                                            with patch(
                                                                "agent.openhands_formatter.write_module_output_json"
                                                            ) as mock_write:
                                                                with patch(
                                                                    "agent.output_writer.extract_git_patch"
                                                                ):
                                                                    with patch(
                                                                        "agent.output_writer.build_metadata",
                                                                        return_value={},
                                                                    ):
                                                                        from agent.run_agent_ts import (
                                                                            run_agent_for_repo_ts,
                                                                        )

                                                                        run_agent_for_repo_ts(
                                                                            str(
                                                                                tmp_path
                                                                            ),
                                                                            agent_config,
                                                                            example,
                                                                            "commit0",
                                                                        )

                                                            mock_write.assert_called()


class TestSpecCostsThinkingCapture:
    """Cover lines 199, 249, 311: spec_costs added to thinking_capture.summarizer_costs."""

    def test_spec_costs_added_in_test_branch(self, tmp_path: Path) -> None:
        """In run_tests branch, spec_costs are added to thinking_capture."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=True,
            capture_thinking=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "test").mkdir(exist_ok=True)
        (repo_dir / "test" / "a.test.ts").touch()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        mock_cost = MagicMock()
        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = []

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts",
                        return_value=(["src/index.ts"], {}),
                    ):
                        with patch(
                            f"{MODULE}.get_ts_tests", return_value=[["test/a.test.ts"]]
                        ):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", [mock_cost]),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}.ThinkingCapture",
                                                return_value=mock_tc,
                                            ):
                                                with patch(
                                                    f"{MODULE}._is_module_done",
                                                    return_value=False,
                                                ):
                                                    with patch(
                                                        f"{MODULE}._mark_module_done"
                                                    ):
                                                        with patch(
                                                            f"{MODULE}.get_ts_lint_cmd",
                                                            return_value="",
                                                        ):
                                                            from agent.run_agent_ts import (
                                                                run_agent_for_repo_ts,
                                                            )

                                                            run_agent_for_repo_ts(
                                                                str(tmp_path),
                                                                agent_config,
                                                                example,
                                                                "commit0",
                                                            )

        mock_tc.summarizer_costs.add.assert_called_with(mock_cost)

    def test_spec_costs_added_in_lint_branch(self, tmp_path: Path) -> None:
        """In run_entire_dir_lint branch, spec_costs are added to thinking_capture."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_entire_dir_lint=True,
            capture_thinking=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        mock_cost = MagicMock()
        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = []

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", [mock_cost]),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}.ThinkingCapture",
                                                return_value=mock_tc,
                                            ):
                                                with patch(
                                                    f"{MODULE}._is_module_done",
                                                    return_value=False,
                                                ):
                                                    with patch(
                                                        f"{MODULE}._mark_module_done"
                                                    ):
                                                        with patch(
                                                            f"{MODULE}.get_ts_lint_cmd",
                                                            return_value="",
                                                        ):
                                                            with patch(
                                                                f"{MODULE}.get_changed_ts_files_from_commits",
                                                                return_value=[
                                                                    "src/a.ts"
                                                                ],
                                                            ):
                                                                from agent.run_agent_ts import (
                                                                    run_agent_for_repo_ts,
                                                                )

                                                                run_agent_for_repo_ts(
                                                                    str(tmp_path),
                                                                    agent_config,
                                                                    example,
                                                                    "commit0",
                                                                )

        mock_tc.summarizer_costs.add.assert_called_with(mock_cost)

    def test_spec_costs_added_in_draft_branch(self, tmp_path: Path) -> None:
        """In default draft branch, spec_costs are added to thinking_capture."""
        example = _make_example()
        agent_config = _make_agent_config(
            run_tests=False,
            run_entire_dir_lint=False,
            capture_thinking=True,
        )

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        mock_cost = MagicMock()
        mock_tc = MagicMock()
        mock_tc.turns = []
        mock_tc.summarizer_costs = MagicMock()
        mock_tc.get_metrics.return_value = {"total_thinking_tokens": 0}
        mock_tc.get_module_turns.return_value = []

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts",
                        return_value=(["src/index.ts"], {}),
                    ):
                        with patch(f"{MODULE}.get_ts_tests", return_value=[[]]):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", [mock_cost]),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}.ThinkingCapture",
                                                return_value=mock_tc,
                                            ):
                                                with patch(
                                                    f"{MODULE}._is_module_done",
                                                    return_value=False,
                                                ):
                                                    with patch(
                                                        f"{MODULE}._mark_module_done"
                                                    ):
                                                        with patch(
                                                            f"{MODULE}.get_ts_lint_cmd",
                                                            return_value="",
                                                        ):
                                                            from agent.run_agent_ts import (
                                                                run_agent_for_repo_ts,
                                                            )

                                                            run_agent_for_repo_ts(
                                                                str(tmp_path),
                                                                agent_config,
                                                                example,
                                                                "commit0",
                                                            )

        mock_tc.summarizer_costs.add.assert_called_with(mock_cost)


class TestTestFileIdParsing:
    """Cover lines 119-124: test ID parsing (colon-separated and space-separated)."""

    def test_test_ids_with_colon_separator(self, tmp_path: Path) -> None:
        """Test IDs with ':' separator extract file path before first ':'."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "test").mkdir(exist_ok=True)
        (repo_dir / "test" / "foo.test.ts").touch()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts",
                        return_value=(["src/index.ts"], {}),
                    ):
                        with patch(
                            f"{MODULE}.get_ts_tests",
                            return_value=[["test/foo.test.ts:TestClass:test_method"]],
                        ):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}._is_module_done",
                                                return_value=False,
                                            ):
                                                with patch(
                                                    f"{MODULE}._mark_module_done"
                                                ):
                                                    with patch(
                                                        f"{MODULE}.get_ts_lint_cmd",
                                                        return_value="",
                                                    ):
                                                        from agent.run_agent_ts import (
                                                            run_agent_for_repo_ts,
                                                        )

                                                        run_agent_for_repo_ts(
                                                            str(tmp_path),
                                                            agent_config,
                                                            example,
                                                            "commit0",
                                                        )

                                mock_agent_cls.return_value.run.assert_called_once()

    def test_test_ids_with_angle_bracket_separator(self, tmp_path: Path) -> None:
        """Test IDs with ' > ' separator extract file path before first ' > '."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)
        (repo_dir / "test").mkdir(exist_ok=True)
        (repo_dir / "test" / "bar.test.ts").touch()

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts",
                        return_value=(["src/index.ts"], {}),
                    ):
                        with patch(
                            f"{MODULE}.get_ts_tests",
                            return_value=[["test/bar.test.ts > describe > test"]],
                        ):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            with patch(
                                                f"{MODULE}._is_module_done",
                                                return_value=False,
                                            ):
                                                with patch(
                                                    f"{MODULE}._mark_module_done"
                                                ):
                                                    with patch(
                                                        f"{MODULE}.get_ts_lint_cmd",
                                                        return_value="",
                                                    ):
                                                        from agent.run_agent_ts import (
                                                            run_agent_for_repo_ts,
                                                        )

                                                        run_agent_for_repo_ts(
                                                            str(tmp_path),
                                                            agent_config,
                                                            example,
                                                            "commit0",
                                                        )

                                mock_agent_cls.return_value.run.assert_called_once()

    def test_empty_test_ids_skipped(self, tmp_path: Path) -> None:
        """Empty/whitespace-only test IDs are filtered out."""
        example = _make_example()
        agent_config = _make_agent_config(run_tests=True)

        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir(exist_ok=True)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_repo.commit.return_value.hexsha = example["base_commit"]
        mock_repo.head.commit.hexsha = example["base_commit"]

        log_dir = tmp_path / "logs"
        log_dir.mkdir(exist_ok=True)

        with patch(
            f"{MODULE}.read_commit0_ts_config_file",
            return_value={
                "dataset_name": "test.json",
            },
        ):
            with patch(f"{MODULE}.Repo", return_value=mock_repo):
                with patch(f"{MODULE}.create_branch"):
                    with patch(
                        f"{MODULE}.get_target_edit_files_ts", return_value=([], {})
                    ):
                        with patch(
                            f"{MODULE}.get_ts_tests", return_value=[["", "  ", ""]]
                        ):
                            with patch(f"{MODULE}.TsAiderAgents") as mock_agent_cls:
                                mock_agent_cls.return_value = MagicMock()
                                with patch(
                                    f"{MODULE}._get_stable_log_dir",
                                    return_value=log_dir,
                                ):
                                    with patch(
                                        f"{MODULE}.get_message_ts",
                                        return_value=("msg", []),
                                    ):
                                        with patch(f"{MODULE}.yaml.dump"):
                                            from agent.run_agent_ts import (
                                                run_agent_for_repo_ts,
                                            )

                                            run_agent_for_repo_ts(
                                                str(tmp_path),
                                                agent_config,
                                                example,
                                                "commit0",
                                            )

                                # No valid test files → agent not called
                                mock_agent_cls.return_value.run.assert_not_called()
