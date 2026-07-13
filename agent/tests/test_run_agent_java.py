from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch


from agent.run_agent_java import (
    DirContext,
    _find_related_tests,
    _find_all_test_files,
    _is_module_done,
    _mark_module_done,
    _get_stable_log_dir,
)


class TestDirContext:
    def test_changes_and_restores_cwd(self, tmp_path: Path) -> None:
        original = os.getcwd()
        with DirContext(str(tmp_path)):
            assert os.getcwd() == str(tmp_path)
        assert os.getcwd() == original


class TestFindRelatedTests:
    def test_finds_test_suffix(self, tmp_path: Path) -> None:
        src = tmp_path / "mod" / "src" / "main" / "java"
        src.mkdir(parents=True)
        source_file = src / "Foo.java"
        source_file.write_text("class Foo {}")
        test_dir = tmp_path / "mod" / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "FooTest.java"
        test_file.write_text("class FooTest {}")

        result = _find_related_tests(str(tmp_path), str(source_file))
        assert len(result) == 1
        assert result[0] == str(test_file)

    def test_finds_test_prefix(self, tmp_path: Path) -> None:
        src = tmp_path / "mod" / "src" / "main" / "java"
        src.mkdir(parents=True)
        source_file = src / "Bar.java"
        source_file.write_text("class Bar {}")
        test_dir = tmp_path / "mod" / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "TestBar.java"
        test_file.write_text("class TestBar {}")

        result = _find_related_tests(str(tmp_path), str(source_file))
        assert len(result) == 1
        assert result[0] == str(test_file)

    def test_no_matches_returns_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "mod" / "src" / "main" / "java"
        src.mkdir(parents=True)
        source_file = src / "Baz.java"
        source_file.write_text("class Baz {}")

        result = _find_related_tests(str(tmp_path), str(source_file))
        assert result == []


class TestFindAllTestFiles:
    def test_finds_in_src_test(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "module" / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        (test_dir / "FooTest.java").write_text("class FooTest {}")
        (test_dir / "BarTests.java").write_text("class BarTests {}")
        (test_dir / "TestBaz.java").write_text("class TestBaz {}")
        (test_dir / "Helper.java").write_text("class Helper {}")

        result = _find_all_test_files(str(tmp_path))
        assert len(result) == 3
        names = [Path(f).name for f in result]
        assert "FooTest.java" in names
        assert "BarTests.java" in names
        assert "TestBaz.java" in names

    def test_finds_in_root_level_src_test(self, tmp_path: Path) -> None:
        # Regression: the common Maven/Gradle layout puts src/test at the REPO
        # ROOT (no leading module dir), e.g. JSON-java's
        # src/test/java/org/json/junit/*Test.java. The old "/src/test/" in rel
        # check missed this (rel has no leading slash), returning 0 test files and
        # silently making Java stage 3 (test-first refine) do zero work.
        test_dir = tmp_path / "src" / "test" / "java" / "org" / "json" / "junit"
        test_dir.mkdir(parents=True)
        (test_dir / "JSONObjectTest.java").write_text("class JSONObjectTest {}")
        (test_dir / "CDLTest.java").write_text("class CDLTest {}")

        result = _find_all_test_files(str(tmp_path))
        names = [Path(f).name for f in result]
        assert "JSONObjectTest.java" in names
        assert "CDLTest.java" in names
        assert len(result) == 2

    def test_empty_repo_returns_empty(self, tmp_path: Path) -> None:
        result = _find_all_test_files(str(tmp_path))
        assert result == []


class TestIsModuleDone:
    def test_true_when_marker_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".done").touch()
        assert _is_module_done(tmp_path) is True

    def test_false_when_missing(self, tmp_path: Path) -> None:
        assert _is_module_done(tmp_path) is False


class TestMarkModuleDone:
    def test_creates_marker_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "deep" / "nested"
        _mark_module_done(log_dir)
        assert (log_dir / ".done").exists() is True


class TestRunJavaAgent:
    @patch("agent.run_agent_java.Repo")
    @patch("agent.run_agent_java.create_branch")
    @patch("agent.run_agent_java.JavaAgents")
    @patch("agent.run_agent_java.collect_java_files")
    @patch("agent.run_agent_java.is_java_stubbed", return_value=True)
    @patch("agent.run_agent_java.count_java_stubs", return_value={"total_stubs": 0})
    @patch("agent.run_agent_java._get_java_message", return_value=("msg", []))
    @patch("agent.run_agent_java.detect_build_system", return_value="maven")
    @patch("agent.run_agent_java.DirContext")
    @patch("agent.run_agent_java._is_module_done", return_value=False)
    @patch("agent.run_agent_java._mark_module_done")
    def _run(
        self,
        mock_mark: MagicMock,
        mock_is_done: MagicMock,
        mock_dir_ctx: MagicMock,
        mock_detect: MagicMock,
        mock_msg: MagicMock,
        mock_count: MagicMock,
        mock_stubbed: MagicMock,
        mock_collect: MagicMock,
        mock_agents_cls: MagicMock,
        mock_create_branch: MagicMock,
        mock_repo_cls: MagicMock,
        *,
        instance: dict | None = None,
        config_overrides: dict | None = None,
        files: list[str] | None = None,
        branch_exists: bool = False,
    ) -> tuple[object, MagicMock, MagicMock, MagicMock]:
        from agent.config_java import JavaAgentConfig
        from agent.run_agent_java import run_java_agent

        mock_dir_ctx.return_value.__enter__ = MagicMock(return_value=None)
        mock_dir_ctx.return_value.__exit__ = MagicMock(return_value=False)

        inst = instance or {"repo": "org/myrepo", "repo_path": "/tmp/myrepo"}
        overrides = {"run_tests": False, **(config_overrides or {})}
        cfg = JavaAgentConfig(**overrides)

        mock_repo = MagicMock()
        mock_repo.is_dirty.return_value = False
        mock_commit = MagicMock()
        mock_commit.hexsha = "abc123"
        mock_repo.commit.return_value = mock_commit
        mock_repo.head.commit.hexsha = "abc123"
        base_branch_mock = MagicMock()
        if branch_exists:
            mock_repo.heads = {"commit0_java": base_branch_mock, "java-agent": MagicMock()}
        else:
            mock_repo.heads = {"commit0_java": base_branch_mock}
        mock_repo_cls.return_value = mock_repo

        mock_collect.return_value = files or ["/tmp/myrepo/Foo.java"]
        mock_agent_instance = MagicMock()
        mock_agent_instance.get_compile_command.return_value = "mvn compile"
        mock_agent_instance.get_test_command.return_value = "mvn test"
        agent_return = MagicMock()
        agent_return.last_cost = 0.05
        agent_return.test_summarizer_cost = 0.0
        mock_agent_instance.run.return_value = agent_return
        mock_agents_cls.return_value = mock_agent_instance

        result = run_java_agent(inst, cfg, log_dir=str(Path("/tmp") / "test_logs"))
        return result, mock_create_branch, mock_agent_instance, mock_repo

    def test_creates_branch(self) -> None:
        _, mock_create_branch, _, _ = self._run()
        mock_create_branch.assert_called_once()

    def test_iterates_stubs(self) -> None:
        files = ["/tmp/myrepo/A.java", "/tmp/myrepo/B.java"]
        result, _, mock_agent, _ = self._run(files=files)
        assert mock_agent.run.call_count == 2

    def test_calls_agent_run(self) -> None:
        _, _, mock_agent, _ = self._run()
        mock_agent.run.assert_called()

    def test_test_driven_mode(self) -> None:
        result, _, mock_agent, _ = self._run(
            config_overrides={"run_tests": True},
        )
        assert result is not None

    def test_lint_mode_uses_lint_first(self) -> None:
        # Stage 2 (run_entire_dir_lint=True) must drive fixes from compile/lint
        # errors via lint_first with an EMPTY message — parity with go/rust/python
        # — NOT re-send the draft "implement stubs" prompt (which no-ops once the
        # stubs are already implemented).
        _, _, mock_agent, _ = self._run(
            config_overrides={"run_entire_dir_lint": True},
        )
        mock_agent.run.assert_called()
        kwargs = mock_agent.run.call_args.kwargs
        assert kwargs.get("lint_first") is True
        assert kwargs.get("current_stage") == "lint"
        assert kwargs.get("message") == ""

    def test_draft_mode_is_not_lint_first(self) -> None:
        # Stage 1 (draft, default) sends the draft prompt, not lint_first.
        _, _, mock_agent, _ = self._run()
        kwargs = mock_agent.run.call_args.kwargs
        assert kwargs.get("current_stage") == "draft"
        assert not kwargs.get("lint_first", False)

    def test_stashes_dirty_repo(self) -> None:
        _, _, _, mock_repo = self._run()
        mock_repo.git.add.assert_not_called()

    def test_skips_if_branch_exists(self) -> None:
        _, mock_create_branch, _, _ = self._run(branch_exists=True)
        mock_create_branch.assert_called_once()

    def test_handles_empty_stubs(self) -> None:
        import pytest
        from agent.config_java import JavaAgentConfig
        from agent.run_agent_java import run_java_agent

        with patch("agent.run_agent_java.Repo") as mock_repo_cls, \
             patch("agent.run_agent_java.create_branch"), \
             patch("agent.run_agent_java.JavaAgents"), \
             patch("agent.run_agent_java.collect_java_files", return_value=["/tmp/myrepo/Foo.java"]), \
             patch("agent.run_agent_java.is_java_stubbed", return_value=False), \
             patch("agent.run_agent_java.detect_build_system", return_value="maven"), \
             patch("agent.run_agent_java.DirContext") as mock_dc:
            mock_dc.return_value.__enter__ = MagicMock(return_value=None)
            mock_dc.return_value.__exit__ = MagicMock(return_value=False)
            mock_repo = MagicMock()
            mock_repo.is_dirty.return_value = False
            mock_commit = MagicMock()
            mock_commit.hexsha = "abc123"
            mock_repo.commit.return_value = mock_commit
            mock_repo.head.commit.hexsha = "abc123"
            mock_repo.heads = {"commit0_java": MagicMock()}
            mock_repo_cls.return_value = mock_repo

            inst = {"repo": "org/myrepo", "repo_path": "/tmp/myrepo"}
            cfg = JavaAgentConfig()
            with pytest.raises(RuntimeError) as exc_info:
                run_java_agent(inst, cfg, log_dir="/tmp/test_logs")
            msg = str(exc_info.value)
            assert "No stubbed Java files" in msg
            assert "myrepo" in msg
            assert "stub_base=abc123" in msg
            assert "degenerate 0-work trajectory" in msg


class TestMatchTestToStub:
    def _setup_repo(self, tmp_path: Path, source_name: str, test_names: list[str]) -> tuple[str, str]:
        src = tmp_path / "mod" / "src" / "main" / "java"
        src.mkdir(parents=True)
        source_file = src / source_name
        source_file.write_text("class Source {}")
        test_dir = tmp_path / "mod" / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        for name in test_names:
            (test_dir / name).write_text("class Test {}")
        return str(tmp_path), str(source_file)

    def test_suffix_test(self, tmp_path: Path) -> None:
        repo, src = self._setup_repo(tmp_path, "Foo.java", ["FooTest.java"])
        result = _find_related_tests(repo, src)
        assert len(result) == 1
        assert "FooTest.java" in result[0]

    def test_suffix_tests(self, tmp_path: Path) -> None:
        repo, src = self._setup_repo(tmp_path, "Foo.java", ["FooTests.java"])
        result = _find_related_tests(repo, src)
        assert len(result) == 1
        assert "FooTests.java" in result[0]

    def test_suffix_it(self, tmp_path: Path) -> None:
        repo, src = self._setup_repo(tmp_path, "Foo.java", ["FooIT.java"])
        result = _find_related_tests(repo, src)
        assert len(result) == 1
        assert "FooIT.java" in result[0]

    def test_prefix_test(self, tmp_path: Path) -> None:
        repo, src = self._setup_repo(tmp_path, "Foo.java", ["TestFoo.java"])
        result = _find_related_tests(repo, src)
        assert len(result) == 1
        assert "TestFoo.java" in result[0]

    def test_no_match_falls_back(self, tmp_path: Path) -> None:
        repo, src = self._setup_repo(tmp_path, "Foo.java", ["Unrelated.java"])
        result = _find_related_tests(repo, src)
        assert result == []

    def test_exact_match_preferred(self, tmp_path: Path) -> None:
        repo, src = self._setup_repo(
            tmp_path, "Foo.java", ["FooTest.java", "FooTests.java", "TestFoo.java"]
        )
        result = _find_related_tests(repo, src)
        assert len(result) == 3
        names = [Path(f).name for f in result]
        assert "FooTest.java" in names


class TestGetStableLogDir:
    def test_creates_directory(self, tmp_path: Path) -> None:
        result = _get_stable_log_dir(str(tmp_path), "myrepo", "main")
        assert result.exists() is True
        assert result.is_dir() is True

    def test_returns_existing(self, tmp_path: Path) -> None:
        expected = tmp_path / "myrepo" / "main" / "current"
        expected.mkdir(parents=True)
        result = _get_stable_log_dir(str(tmp_path), "myrepo", "main")
        assert result == expected
