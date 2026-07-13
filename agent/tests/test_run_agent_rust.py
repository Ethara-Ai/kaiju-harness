"""Tests for agent/run_agent.py — DirContext, run_eval, run_agent_for_repo."""

from __future__ import annotations

import os
import multiprocessing
from unittest.mock import MagicMock, patch

import pytest

MODULE = "agent.run_agent"


# ---------------------------------------------------------------------------
# DirContext
# ---------------------------------------------------------------------------
class TestDirContext:
    def test_changes_and_restores_dir(self, tmp_path):
        from agent.run_agent import DirContext

        original = os.getcwd()
        target = str(tmp_path)

        with DirContext(target):
            assert os.getcwd() == target

        assert os.getcwd() == original

    def test_restores_on_exception(self, tmp_path):
        from agent.run_agent import DirContext

        original = os.getcwd()
        target = str(tmp_path)

        with pytest.raises(RuntimeError):
            with DirContext(target):
                assert os.getcwd() == target
                raise RuntimeError("test error")

        assert os.getcwd() == original

    def test_stores_original_dir(self, tmp_path):
        from agent.run_agent import DirContext

        ctx = DirContext(str(tmp_path))
        assert ctx.dir == str(tmp_path)
        assert ctx.cwd == os.getcwd()


# ---------------------------------------------------------------------------
# run_eval_after_each_commit
# ---------------------------------------------------------------------------
class TestRunEvalAfterEachCommit:
    @patch(f"{MODULE}.subprocess.run")
    def test_success(self, mock_run):
        from agent.run_agent import run_eval_after_each_commit

        mock_run.return_value = MagicMock(stdout="eval output")
        result = run_eval_after_each_commit("main", "modal", ".commit0.yaml")
        assert result == "eval output"
        mock_run.assert_called_once()
        # Check the command includes expected parts
        cmd = mock_run.call_args[0][0]
        assert "evaluate" in " ".join(cmd)
        assert "--branch" in cmd
        assert "main" in cmd

    @patch(f"{MODULE}.subprocess.run")
    def test_failure_returns_stdout(self, mock_run):
        import subprocess as sp
        from agent.run_agent import run_eval_after_each_commit

        error = sp.CalledProcessError(1, "cmd", output="partial output")
        mock_run.side_effect = error
        result = run_eval_after_each_commit("main", "modal", ".commit0.yaml")
        assert result == "partial output"

    @patch(f"{MODULE}.subprocess.run")
    def test_failure_no_stdout(self, mock_run):
        import subprocess as sp
        from agent.run_agent import run_eval_after_each_commit

        error = sp.CalledProcessError(1, "cmd")
        error.stdout = None
        mock_run.side_effect = error
        result = run_eval_after_each_commit("main", "modal", ".commit0.yaml")
        # Should return str(e) when stdout is None
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# run_agent_for_repo
# ---------------------------------------------------------------------------
class TestRunAgentForRepo:
    def _make_agent_config(self, **overrides):
        cfg = MagicMock()
        cfg.agent_name = "aider"
        cfg.max_iteration = 5
        cfg.model_name = "gpt-4"
        cfg.cache_prompts = False
        cfg.run_tests = False
        cfg.run_entire_dir_lint = False
        cfg.use_topo_sort_dependencies = False
        cfg.use_lint_info = False
        cfg.use_repo_info = False
        cfg.use_unit_tests_info = False
        cfg.use_spec_info = False
        cfg.record_test_for_each_commit = False
        cfg.add_import_module_to_context = False
        cfg.max_test_output_length = 0
        cfg.spec_summary_max_tokens = 4000
        cfg.strip_non_stubs = False
        cfg.blind_tests = False
        cfg.blind_lint = False
        cfg.names_only_tests = False
        cfg.inject_test_files_readonly = False
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def _make_example(self):
        return {
            "repo": "org/myrepo",
            "base_commit": "abc123",
            "reference_commit": "def456",
            "src_dir": "src",
            "test": {"test_dir": "tests", "test_cmd": "pytest"},
        }

    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.get_target_edit_files")
    @patch(f"{MODULE}.get_changed_files_from_commits")
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.DirContext")
    def test_unsupported_agent_raises(
        self,
        mock_dir,
        mock_msg,
        mock_get_tests,
        mock_changed,
        mock_target,
        mock_branch,
        mock_aider,
        mock_repo,
        mock_config,
    ):
        # Test the inner impl directly: the public run_agent_for_repo now ISOLATES
        # (logs + swallows) worker exceptions so one bad repo can't abort the batch;
        # the raise itself lives in _run_agent_for_repo_impl ("raises on failure").
        from agent.run_agent import _run_agent_for_repo_impl

        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        mock_repo_instance = MagicMock()
        mock_repo_instance.is_dirty.return_value = False
        mock_repo.return_value = mock_repo_instance

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config(agent_name="unknown_agent")

        with pytest.raises(NotImplementedError, match="unknown_agent"):
            _run_agent_for_repo_impl(
                "/base",
                agent_config,
                self._make_example(),
                "branch",
                q,
                commit0_config_file="/repos/.commit0.yaml",
            )

    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.Repo")
    def test_not_a_git_repo_raises(self, mock_repo, mock_config):
        # Inner impl raises; the public wrapper isolates (see test above).
        from agent.run_agent import _run_agent_for_repo_impl

        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        mock_repo.side_effect = Exception("Not a git repo")

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config()

        with pytest.raises(Exception, match="[Nn]ot a git repo"):
            _run_agent_for_repo_impl(
                "/base",
                agent_config,
                self._make_example(),
                "branch",
                q,
                commit0_config_file="/repos/.commit0.yaml",
            )

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_message", return_value=("test message", []))
    def test_dirty_repo_auto_commits(
        self,
        mock_msg,
        mock_get_tests,
        mock_changed,
        mock_target,
        mock_branch,
        mock_aider,
        mock_repo,
        mock_config,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = True
        commit_obj = MagicMock()
        commit_obj.hexsha = "abc123"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.0
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        # Verify auto-commit happened
        repo_instance.git.add.assert_called_once_with(A=True)
        repo_instance.index.commit.assert_called_once_with("left from last change")


# ---------------------------------------------------------------------------
# run_agent (orchestrator)
# ---------------------------------------------------------------------------
class TestRunAgent:
    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.TerminalDisplay")
    @patch(f"{MODULE}.multiprocessing.Manager")
    @patch(f"{MODULE}.multiprocessing.Pool")
    def test_empty_dataset_raises(
        self,
        mock_pool,
        mock_manager,
        mock_display,
        mock_load_dataset,
        mock_config,
        mock_agent_config,
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock()
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "nonexistent_split",
        }
        mock_load_dataset.return_value = []

        with pytest.raises(AssertionError, match="No examples available"):
            run_agent(
                "main",
                False,
                "modal",
                ".agent.yaml",
                ".commit0.yaml",
                "logs",
                4,
                4,
            )

    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_all_split_uses_full_dataset(
        self, mock_load_dataset, mock_config, mock_agent_config
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock(add_import_module_to_context=False)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        examples = [
            {"repo": "org/repo1"},
            {"repo": "org/repo2"},
        ]
        mock_load_dataset.return_value = examples

        # We only test the dataset filtering, not the full run
        # Mock the rest to avoid running actual agents
        with (
            patch(f"{MODULE}.TerminalDisplay") as mock_display,
            patch(f"{MODULE}.multiprocessing.Manager") as mock_manager,
            patch(f"{MODULE}.multiprocessing.Pool") as mock_pool_cls,
        ):
            mock_display_inst = MagicMock()
            mock_display.return_value.__enter__ = MagicMock(
                return_value=mock_display_inst
            )
            mock_display.return_value.__exit__ = MagicMock(return_value=False)

            mock_q = MagicMock()
            mock_q.empty.return_value = True
            mock_mgr = MagicMock()
            mock_mgr.Queue.return_value = mock_q
            mock_manager.return_value.__enter__ = MagicMock(return_value=mock_mgr)
            mock_manager.return_value.__exit__ = MagicMock(return_value=False)

            mock_pool = MagicMock()
            mock_result = MagicMock()
            mock_result.ready.return_value = True
            mock_pool.apply_async.return_value = mock_result
            mock_pool_cls.return_value.__enter__ = MagicMock(return_value=mock_pool)
            mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

            run_agent(
                "main", False, "modal", ".agent.yaml", ".commit0.yaml", "logs", 4, 4
            )

            # With "all" split, both examples should be used
            assert mock_pool.apply_async.call_count == 2


# ---------------------------------------------------------------------------
# DirContext edge cases
# ---------------------------------------------------------------------------
class TestDirContextEdgeCases:
    def test_nonexistent_dir_raises(self):
        from agent.run_agent import DirContext

        with pytest.raises((OSError, FileNotFoundError)):
            with DirContext("/nonexistent/path/that/does/not/exist"):
                pass

    def test_nested_context_managers(self, tmp_path):
        from agent.run_agent import DirContext

        original = os.getcwd()
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        with DirContext(str(dir_a)):
            assert os.getcwd() == str(dir_a)
            with DirContext(str(dir_b)):
                assert os.getcwd() == str(dir_b)
            assert os.getcwd() == str(dir_a)
        assert os.getcwd() == original


# ---------------------------------------------------------------------------
# run_eval edge cases
# ---------------------------------------------------------------------------
class TestRunEvalEdgeCases:
    @patch(f"{MODULE}.subprocess.run")
    def test_eval_command_format(self, mock_run):
        from agent.run_agent import run_eval_after_each_commit

        mock_run.return_value = MagicMock(stdout="ok")
        run_eval_after_each_commit("my-branch", "docker", "my-config.yaml")
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "--branch" in cmd_str
        assert "my-branch" in cmd_str
        assert "--backend" in cmd_str
        assert "docker" in cmd_str
        assert "--commit0-config-file" in cmd_str
        assert "my-config.yaml" in cmd_str

    @patch(f"{MODULE}.subprocess.run")
    def test_eval_timeout_value(self, mock_run):
        from agent.run_agent import run_eval_after_each_commit

        mock_run.return_value = MagicMock(stdout="ok")
        run_eval_after_each_commit("main", "modal", ".commit0.yaml")
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "--timeout" in cmd_str
        assert "100" in cmd_str


class TestRunAgentForRepoModes:
    @pytest.fixture(autouse=True)
    def _hermetic_module_done(self):
        # experiment_log_dir lives under the STABLE RUN_AGENT_LOG_DIR (not tmp_path),
        # so a `.done` marker left by a prior run makes _is_module_done skip EVERY
        # module — the loop never calls agent.run (non-hermetic, order-dependent).
        # Force it False so each test drives the loop deterministically.
        with patch(f"{MODULE}._is_module_done", return_value=False):
            yield

    def _make_agent_config(self, **overrides):
        cfg = MagicMock()
        cfg.agent_name = "aider"
        cfg.max_iteration = 5
        cfg.model_name = "gpt-4"
        cfg.cache_prompts = False
        cfg.run_tests = False
        cfg.run_entire_dir_lint = False
        cfg.use_topo_sort_dependencies = False
        cfg.use_lint_info = False
        cfg.use_repo_info = False
        cfg.use_unit_tests_info = False
        cfg.use_spec_info = False
        cfg.record_test_for_each_commit = False
        cfg.add_import_module_to_context = False
        cfg.max_test_output_length = 0
        cfg.spec_summary_max_tokens = 4000
        cfg.strip_non_stubs = False
        cfg.blind_tests = False
        cfg.blind_lint = False
        cfg.names_only_tests = False
        cfg.inject_test_files_readonly = False
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def _make_example(self):
        return {
            "repo": "org/myrepo",
            "base_commit": "abc123",
            "reference_commit": "def456",
            "src_dir": "src",
            "test": {"test_dir": "tests", "test_cmd": "pytest"},
        }

    def _setup_common_mocks(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        tmp_path,
        changed_files=None,
        test_files_return=None,
        target_files=None,
    ):
        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        commit_obj = MagicMock()
        commit_obj.hexsha = "abc123"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.5
        mock_agent_return.test_summarizer_cost = 0.0
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        if target_files is None:
            target_files = ["src/mod.py"]
        mock_target.return_value = (target_files, {})
        mock_changed.return_value = changed_files or []
        mock_get_tests.return_value = test_files_return or []
        spec_cost = MagicMock()
        spec_cost.cost = 0.1
        mock_msg.return_value = ("test message", [spec_cost])
        return repo_instance, mock_agent

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.get_tests")
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_run_tests_mode_iterates_test_files(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        repo_instance, mock_agent = self._setup_common_mocks(
            mock_config,
            mock_repo,
            mock_aider,
            mock_target,
            mock_changed,
            mock_get_tests,
            mock_msg,
            tmp_path,
            test_files_return=[["tests/test_a.py::test_1", "tests/test_b.py::test_2"]],
        )
        (tmp_path / "myrepo" / "tests" / "test_a.py").mkdir(parents=True, exist_ok=True)
        (tmp_path / "myrepo" / "tests" / "test_b.py").touch()

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config(run_tests=True)

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        for c in mock_agent.run.call_args_list:
            assert c.kwargs.get("test_first") is True or c[1].get("test_first") is True
        assert mock_agent.run.call_count >= 1

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message", return_value=("test message", []))
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits")
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_lint_first_mode_iterates_lint_files(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        commit_obj = MagicMock()
        commit_obj.hexsha = "abc123"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.5
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        mock_changed.return_value = ["src/a.py", "src/b.py"]

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config(run_entire_dir_lint=True)

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        assert mock_agent.run.call_count == 2
        for c in mock_agent.run.call_args_list:
            assert c.kwargs.get("lint_first") is True or c[1].get("lint_first") is True

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files")
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_default_mode_iterates_target_files(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        repo_instance, mock_agent = self._setup_common_mocks(
            mock_config,
            mock_repo,
            mock_aider,
            mock_target,
            mock_changed,
            mock_get_tests,
            mock_msg,
            tmp_path,
            target_files=["src/mod.py", "src/utils.py", "src/core.py"],
        )

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        assert mock_agent.run.call_count == 3

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message", return_value=("test message", []))
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_override_previous_changes_resets(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        commit_obj = MagicMock()
        commit_obj.hexsha = "different_commit"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.0
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            override_previous_changes=True,
            commit0_config_file="/repos/.commit0.yaml",
        )

        repo_instance.git.reset.assert_called_once_with("--hard", "abc123")

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message", return_value=("test message", []))
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_no_override_when_same_commit(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        commit_obj = MagicMock()
        commit_obj.hexsha = "abc123"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.0
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            override_previous_changes=True,
            commit0_config_file="/repos/.commit0.yaml",
        )

        repo_instance.git.reset.assert_not_called()

    @patch(f"{MODULE}.run_eval_after_each_commit", return_value="eval_output")
    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_eval_results_written_when_record_test(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        mock_eval,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        repo_instance, mock_agent = self._setup_common_mocks(
            mock_config,
            mock_repo,
            mock_aider,
            mock_target,
            mock_changed,
            mock_get_tests,
            mock_msg,
            tmp_path,
        )

        q = multiprocessing.Queue()
        agent_config = self._make_agent_config(record_test_for_each_commit=True)

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        mock_json_dump.assert_called_once()

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_queue_messages_sent(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        repo_instance, mock_agent = self._setup_common_mocks(
            mock_config,
            mock_repo,
            mock_aider,
            mock_target,
            mock_changed,
            mock_get_tests,
            mock_msg,
            tmp_path,
        )

        q = MagicMock()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        actions = [
            c.args[0][0] for c in q.put.call_args_list if isinstance(c.args[0], tuple)
        ]
        assert "start_repo" in actions
        assert "set_current_file" in actions
        assert "update_money_display" in actions
        assert "finish_repo" in actions

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files")
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_spec_summarizer_cost_added_once(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        repo_instance, mock_agent = self._setup_common_mocks(
            mock_config,
            mock_repo,
            mock_aider,
            mock_target,
            mock_changed,
            mock_get_tests,
            mock_msg,
            tmp_path,
            target_files=["src/a.py", "src/b.py", "src/c.py"],
        )

        q = MagicMock()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        money_msgs = [
            c.args[0]
            for c in q.put.call_args_list
            if isinstance(c.args[0], tuple) and c.args[0][0] == "update_money_display"
        ]
        assert len(money_msgs) == 3
        costs = [m[1][2] for m in money_msgs]
        cost_with_spec = 0.5 + 0.1
        cost_without_spec = 0.5
        assert costs[0] == pytest.approx(cost_with_spec)
        assert costs[1] == pytest.approx(cost_without_spec)
        assert costs[2] == pytest.approx(cost_without_spec)

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message", return_value=("test message", []))
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits")
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_lint_mode_queue_messages(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        commit_obj = MagicMock()
        commit_obj.hexsha = "abc123"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.3
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        mock_changed.return_value = ["src/a.py"]

        q = MagicMock()
        agent_config = self._make_agent_config(run_entire_dir_lint=True)

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        actions = [
            c.args[0][0] for c in q.put.call_args_list if isinstance(c.args[0], tuple)
        ]
        assert "update_money_display" in actions
        assert "finish_repo" in actions

    @patch(f"{MODULE}.run_eval_after_each_commit", return_value="eval_out")
    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message", return_value=("test message", []))
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits")
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_lint_mode_record_test_calls_eval(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        mock_eval,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        (tmp_path / "myrepo").mkdir(parents=True, exist_ok=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
        }
        repo_instance = MagicMock()
        repo_instance.is_dirty.return_value = False
        commit_obj = MagicMock()
        commit_obj.hexsha = "abc123"
        repo_instance.commit.return_value = commit_obj
        mock_repo.return_value = repo_instance

        mock_agent = MagicMock()
        mock_agent_return = MagicMock()
        mock_agent_return.last_cost = 0.0
        mock_agent.run.return_value = mock_agent_return
        mock_aider.return_value = mock_agent

        mock_changed.return_value = ["src/x.py"]

        q = MagicMock()
        agent_config = self._make_agent_config(
            run_entire_dir_lint=True,
            record_test_for_each_commit=True,
        )

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        mock_eval.assert_called_once()
        mock_json_dump.assert_called_once()

    @patch(f"{MODULE}.json.dump")
    @patch(f"{MODULE}.yaml.dump")
    @patch(f"{MODULE}.DirContext")
    @patch(f"{MODULE}.get_message")
    @patch(f"{MODULE}.get_tests", return_value=[])
    @patch(f"{MODULE}.get_changed_files_from_commits", return_value=[])
    @patch(f"{MODULE}.get_target_edit_files", return_value=(["src/mod.py"], {}))
    @patch(f"{MODULE}.create_branch")
    @patch(f"{MODULE}.AiderAgents")
    @patch(f"{MODULE}.Repo")
    @patch(f"{MODULE}.read_commit0_config_file")
    def test_finish_repo_is_last_message(
        self,
        mock_config,
        mock_repo,
        mock_aider,
        mock_branch,
        mock_target,
        mock_changed,
        mock_get_tests,
        mock_msg,
        mock_dir_ctx,
        mock_yaml_dump,
        mock_json_dump,
        tmp_path,
    ):
        from agent.run_agent import run_agent_for_repo

        repo_instance, mock_agent = self._setup_common_mocks(
            mock_config,
            mock_repo,
            mock_aider,
            mock_target,
            mock_changed,
            mock_get_tests,
            mock_msg,
            tmp_path,
        )

        q = MagicMock()
        agent_config = self._make_agent_config()

        run_agent_for_repo(
            str(tmp_path),
            agent_config,
            self._make_example(),
            "branch",
            q,
            commit0_config_file="/repos/.commit0.yaml",
        )

        all_puts = q.put.call_args_list
        last_msg = all_puts[-1].args[0]
        assert last_msg[0] == "finish_repo"
        assert last_msg[1] == "myrepo"


class TestRunAgentFiltering:
    def _make_pool_context(self, mock_pool_cls):
        mock_pool = MagicMock()
        mock_result = MagicMock()
        mock_result.ready.return_value = True
        mock_pool.apply_async.return_value = mock_result
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=mock_pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
        return mock_pool

    def _make_manager_context(self, mock_manager):
        mock_q = MagicMock()
        mock_q.empty.return_value = True
        mock_mgr = MagicMock()
        mock_mgr.Queue.return_value = mock_q
        mock_manager.return_value.__enter__ = MagicMock(return_value=mock_mgr)
        mock_manager.return_value.__exit__ = MagicMock(return_value=False)
        return mock_q

    def _make_display_context(self, mock_display):
        mock_display_inst = MagicMock()
        mock_display.return_value.__enter__ = MagicMock(return_value=mock_display_inst)
        mock_display.return_value.__exit__ = MagicMock(return_value=False)
        return mock_display_inst

    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.TerminalDisplay")
    @patch(f"{MODULE}.multiprocessing.Manager")
    @patch(f"{MODULE}.multiprocessing.Pool")
    @patch(f"{MODULE}.SPLIT", {"lite": ["myrepo", "other"]})
    def test_split_filtering(
        self,
        mock_pool_cls,
        mock_manager,
        mock_display,
        mock_load_dataset,
        mock_config,
        mock_agent_config,
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock(add_import_module_to_context=False)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "lite",
            "base_dir": "/tmp/repos",
        }
        examples = [
            {"repo": "org/myrepo"},
            {"repo": "org/notinthesplit"},
            {"repo": "org/other"},
        ]
        mock_load_dataset.return_value = examples

        self._make_display_context(mock_display)
        self._make_manager_context(mock_manager)
        mock_pool = self._make_pool_context(mock_pool_cls)

        run_agent("main", False, "modal", ".agent.yaml", ".commit0.yaml", "logs", 4, 4)

        assert mock_pool.apply_async.call_count == 2

    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.TerminalDisplay")
    @patch(f"{MODULE}.multiprocessing.Manager")
    @patch(f"{MODULE}.multiprocessing.Pool")
    def test_name_match_filtering(
        self,
        mock_pool_cls,
        mock_manager,
        mock_display,
        mock_load_dataset,
        mock_config,
        mock_agent_config,
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock(add_import_module_to_context=False)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "my-repo",
            "base_dir": "/tmp/repos",
        }
        examples = [
            {"repo": "org/my_repo"},
            {"repo": "org/other"},
        ]
        mock_load_dataset.return_value = examples

        self._make_display_context(mock_display)
        self._make_manager_context(mock_manager)
        mock_pool = self._make_pool_context(mock_pool_cls)

        run_agent("main", False, "modal", ".agent.yaml", ".commit0.yaml", "logs", 4, 4)

        assert mock_pool.apply_async.call_count == 1

    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.TerminalDisplay")
    @patch(f"{MODULE}.multiprocessing.Manager")
    @patch(f"{MODULE}.multiprocessing.Pool")
    def test_unmatched_repo_split_fails_fast(
        self,
        mock_pool_cls,
        mock_manager,
        mock_display,
        mock_load_dataset,
        mock_config,
        mock_agent_config,
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock(add_import_module_to_context=False)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "nonexistent-repo-name",
            "base_dir": "/tmp/repos",
        }
        examples = [
            {"repo": "org/repo1"},
            {"repo": "org/repo2"},
            {"repo": "org/repo3"},
        ]
        mock_load_dataset.return_value = examples

        self._make_display_context(mock_display)
        self._make_manager_context(mock_manager)
        mock_pool = self._make_pool_context(mock_pool_cls)

        # resolve_split documents "no match -> empty list; callers surface a clear
        # error". An unmatched repo_split therefore FAILS FAST rather than silently
        # falling back to running EVERY repo (which could run unintended repos).
        with pytest.raises(AssertionError, match="No examples available"):
            run_agent("main", False, "modal", ".agent.yaml", ".commit0.yaml", "logs", 4, 4)

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.TerminalDisplay")
    @patch(f"{MODULE}.multiprocessing.Manager")
    @patch(f"{MODULE}.multiprocessing.Pool")
    def test_playwright_installed_when_add_import(
        self,
        mock_pool_cls,
        mock_manager,
        mock_display,
        mock_load_dataset,
        mock_config,
        mock_agent_config,
        mock_subprocess_run,
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock(add_import_module_to_context=True)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        examples = [{"repo": "org/repo1"}]
        mock_load_dataset.return_value = examples

        self._make_display_context(mock_display)
        self._make_manager_context(mock_manager)
        self._make_pool_context(mock_pool_cls)

        run_agent("main", False, "modal", ".agent.yaml", ".commit0.yaml", "logs", 4, 4)

        mock_subprocess_run.assert_called_once_with(
            ["playwright", "install", "chromium"], check=True
        )

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.load_agent_config")
    @patch(f"{MODULE}.read_commit0_config_file")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.TerminalDisplay")
    @patch(f"{MODULE}.multiprocessing.Manager")
    @patch(f"{MODULE}.multiprocessing.Pool")
    def test_playwright_not_installed_by_default(
        self,
        mock_pool_cls,
        mock_manager,
        mock_display,
        mock_load_dataset,
        mock_config,
        mock_agent_config,
        mock_subprocess_run,
    ):
        from agent.run_agent import run_agent

        mock_agent_config.return_value = MagicMock(add_import_module_to_context=False)
        mock_config.return_value = {
            "dataset_name": "commit0/test",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/tmp/repos",
        }
        examples = [{"repo": "org/repo1"}]
        mock_load_dataset.return_value = examples

        self._make_display_context(mock_display)
        self._make_manager_context(mock_manager)
        self._make_pool_context(mock_pool_cls)

        run_agent("main", False, "modal", ".agent.yaml", ".commit0.yaml", "logs", 4, 4)

        mock_subprocess_run.assert_not_called()
