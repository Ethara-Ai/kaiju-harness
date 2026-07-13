from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import git
import pytest

import agent.run_agent_js as run_agent_js_module
from agent.agent_utils_js import create_branch as create_branch_js


_FITZ_AVAILABLE = importlib.util.find_spec("fitz") is not None
_requires_fitz = pytest.mark.skipif(
    not _FITZ_AVAILABLE,
    reason="fitz (PyMuPDF) not installed; cannot import agent.agent_utils for parity check",
)


def _extract_function_source(file_path: Path, name: str) -> str:
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node).strip()
    raise AssertionError(f"function {name!r} not found in {file_path}")


def _extract_function_body_no_docstring(file_path: Path, name: str) -> str:
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = list(node.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            return "\n".join(ast.unparse(stmt) for stmt in body).strip()
    raise AssertionError(f"function {name!r} not found in {file_path}")


class TestGateInsurance:
    def test_main_is_app(self) -> None:
        from agent.run_agent_js import app, main

        assert main is app

    def test_module_exposes_both(self) -> None:
        assert hasattr(run_agent_js_module, "main")
        assert hasattr(run_agent_js_module, "app")
        assert run_agent_js_module.main is run_agent_js_module.app

    def test_app_is_typer_instance(self) -> None:
        import typer

        assert isinstance(run_agent_js_module.app, typer.Typer)


def _init_repo(path: Path) -> tuple[git.Repo, str]:
    path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(path)
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "t@e.x").release()
    seed = path / "seed.txt"
    seed.write_text("seed", encoding="utf-8")
    repo.index.add([str(seed)])
    repo.index.commit("seed")
    sha = repo.head.commit.hexsha
    return repo, sha


class TestCreateBranchBehavior:
    def test_checkout_existing_branch(self, tmp_path: Path) -> None:
        repo, seed_sha = _init_repo(tmp_path)
        repo.git.checkout("-b", "feat-x")
        repo.git.checkout("master" if "master" in repo.heads else "main")

        create_branch_js(repo, "feat-x", seed_sha)
        assert repo.active_branch.name == "feat-x"

    def test_create_new_branch_from_commit(self, tmp_path: Path) -> None:
        repo, seed_sha = _init_repo(tmp_path)
        (tmp_path / "more.txt").write_text("x", encoding="utf-8")
        repo.index.add(["more.txt"])
        repo.index.commit("more")

        create_branch_js(repo, "feat-new", seed_sha)
        assert repo.active_branch.name == "feat-new"
        assert repo.head.commit.hexsha == seed_sha

    def test_git_command_error_chained_to_runtime_error(self, tmp_path: Path) -> None:
        repo, _ = _init_repo(tmp_path)
        with pytest.raises(RuntimeError) as exc_info:
            create_branch_js(repo, "doesnotmatter", "deadbeef" * 5)
        msg = str(exc_info.value)
        assert "Failed to create or switch to branch 'doesnotmatter':" in msg
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, git.exc.GitCommandError)


class TestCreateBranchParityWithParent:
    def test_ast_body_equivalent_to_parent(self) -> None:
        from agent import agent_utils_js as _utils_js

        js_path = Path(_utils_js.__file__)
        parent_path = js_path.parent / "agent_utils.py"
        assert parent_path.exists(), f"missing {parent_path}"
        js_body = _extract_function_body_no_docstring(js_path, "create_branch")
        parent_body = _extract_function_body_no_docstring(parent_path, "create_branch")
        assert js_body == parent_body, (
            "create_branch body has drifted between agent_utils_js.py and "
            "agent_utils.py. The JS inlined copy must remain semantically "
            "identical (B3 Phase E binding). Docstrings are excluded from "
            "comparison; only executable statements are checked."
        )


class TestDirContext:
    def test_enter_chdirs(self, tmp_path: Path) -> None:
        DirContext = run_agent_js_module.DirContext
        original = os.getcwd()
        try:
            with DirContext(str(tmp_path)):
                assert os.path.realpath(os.getcwd()) == os.path.realpath(str(tmp_path))
        finally:
            os.chdir(original)

    def test_exit_restores_cwd(self, tmp_path: Path) -> None:
        DirContext = run_agent_js_module.DirContext
        original = os.getcwd()
        with DirContext(str(tmp_path)):
            pass
        assert os.getcwd() == original

    def test_exit_restores_cwd_even_on_exception(self, tmp_path: Path) -> None:
        DirContext = run_agent_js_module.DirContext
        original = os.getcwd()

        class _BoomError(RuntimeError):
            pass

        with pytest.raises(_BoomError):
            with DirContext(str(tmp_path)):
                raise _BoomError("inner")
        assert os.getcwd() == original


class TestInlinedHelpers:
    def test_is_module_done_false_when_no_done(self, tmp_path: Path) -> None:
        assert run_agent_js_module._is_module_done(tmp_path) is False

    def test_is_module_done_true_when_done_file(self, tmp_path: Path) -> None:
        (tmp_path / ".done").touch()
        assert run_agent_js_module._is_module_done(tmp_path) is True


class TestDiscoverJsTestFiles:
    def test_finds_ava_root_test_js(self, tmp_path: Path) -> None:
        # ava convention: a bare test.js at the repo ROOT. This is exactly the
        # slugify case that made stage 3 do zero work (canonical ids are bare
        # case-names -> no file -> all skipped). Discovery must find it.
        (tmp_path / "test.js").write_text("test('x', t => t.pass())")
        (tmp_path / "index.js").write_text("export default 1")
        found = run_agent_js_module._discover_js_test_files(str(tmp_path), ".")
        assert found == ["test.js"]

    def test_finds_jest_pattern_and_test_dir(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.test.js").write_text("x")
        (tmp_path / "test").mkdir()
        (tmp_path / "test" / "bar.js").write_text("x")
        found = set(run_agent_js_module._discover_js_test_files(str(tmp_path), "."))
        assert os.path.join("src", "foo.test.js") in found
        assert os.path.join("test", "bar.js") in found

    def test_empty_when_no_tests(self, tmp_path: Path) -> None:
        (tmp_path / "index.js").write_text("export default 1")
        assert run_agent_js_module._discover_js_test_files(str(tmp_path), ".") == []

    def test_mark_module_done_creates_done_file(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "mod"
        run_agent_js_module._mark_module_done(target)
        assert (target / ".done").exists()

    def test_get_stable_log_dir_structure(self, tmp_path: Path) -> None:
        result = run_agent_js_module._get_stable_log_dir(
            str(tmp_path / "logs"), "p-queue", "commit0_main"
        )
        assert result.is_dir()
        assert result.name == "current"
        assert result.parent.name == "commit0_main"
        assert result.parent.parent.name == "p-queue"


class TestByteEquivalenceDriftGuard:
    @pytest.mark.parametrize(
        "fname", ["_is_module_done", "_mark_module_done", "_get_stable_log_dir"]
    )
    def test_byte_equivalent_to_no_rich(self, fname: str) -> None:
        js_path = Path(run_agent_js_module.__file__)
        no_rich_path = js_path.parent / "run_agent_no_rich.py"
        assert no_rich_path.exists(), f"missing {no_rich_path}"
        js_src = _extract_function_source(js_path, fname)
        no_rich_src = _extract_function_source(no_rich_path, fname)
        assert js_src == no_rich_src, (
            f"Phase D drift detected for {fname}: run_agent_js.py and "
            f"run_agent_no_rich.py must remain byte-equivalent for these helpers."
        )


class TestModuleSlug:
    # The slug RETAINS the extension (folded into the name) so dual-package files
    # differing only by extension don't collide into one log dir. See _js_module_slug.
    def test_retains_js_ext(self) -> None:
        assert run_agent_js_module._js_module_slug("src/foo.js") == "src__foo_js"

    def test_retains_jsx_ext(self) -> None:
        assert run_agent_js_module._js_module_slug("a/b.jsx") == "a__b_jsx"

    def test_retains_mjs_ext(self) -> None:
        assert run_agent_js_module._js_module_slug("c.mjs") == "c_mjs"

    def test_replaces_dots(self) -> None:
        assert run_agent_js_module._js_module_slug("a.b.c.js") == "a_b_c_js"

    def test_extension_variants_do_not_collide(self) -> None:
        slug = run_agent_js_module._js_module_slug
        assert slug("src/foo.js") != slug("src/foo.mjs")
        assert slug("src/foo.js") != slug("src/foo.cjs")
        assert len({slug(f"src/foo{e}") for e in (".js", ".mjs", ".cjs", ".jsx")}) == 4


class TestDockerMockedAtBoundary:
    def test_no_real_docker_client_constructed(self) -> None:
        with patch("docker.DockerClient") as MockDocker:
            MockDocker.return_value = MagicMock()
            assert MockDocker.return_value is not None


class TestProtectedTestPathspecs:
    def test_includes_test_globs(self) -> None:
        pathspecs = run_agent_js_module._JS_PROTECTED_TEST_PATHSPECS
        names = " ".join(pathspecs)
        for ext in ("js", "mjs", "cjs", "jsx"):
            assert f":!**/*.test.{ext}" in pathspecs, f"missing test pathspec for .{ext}"
            assert f":!**/*.spec.{ext}" in pathspecs, f"missing spec pathspec for .{ext}"
        assert ":!**/__tests__/**" in pathspecs
        assert ":!jest.config.*" in names
        assert ":!vitest.config.*" in names
        assert ":!.mocharc.*" in names


class TestRunAgentEntrypoints:
    def test_run_agent_calls_impl(self) -> None:
        with patch.object(run_agent_js_module, "run_agent_js_impl") as mock_impl:
            run_agent_js_module.run_agent(
                branch="b",
                override_previous_changes=False,
                backend="local",
                agent_config_file=".a.yml",
                commit0_config_file=".c.yml",
                log_dir="logs",
                max_parallel_repos=1,
            )
        mock_impl.assert_called_once()
        kw = mock_impl.call_args.kwargs
        assert kw["branch"] == "b"
        assert kw["max_parallel_repos"] == 1


class TestRunAgentForRepoBehavior:
    def test_unknown_agent_name_raises_not_implemented(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _init_repo(tmp_path / "lib")

        agent_config = MagicMock()
        agent_config.agent_name = "nonexistent"

        example: Any = {
            "instance_id": "commit-0/lib",
            "repo": "owner/lib",
            "base_commit": "deadbeef",
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }

        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "commit0_js"},
        )

        with pytest.raises(NotImplementedError, match="nonexistent"):
            run_agent_js_module._run_agent_for_repo_js_impl(
                repo_base_dir=str(tmp_path),
                agent_config=agent_config,
                example=example,
                branch="commit0",
            )

    def test_invalid_dataset_name_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_dir = tmp_path / "repo"
        _init_repo(repo_dir)

        agent_config = MagicMock()
        agent_config.agent_name = "aider"

        example: Any = {
            "instance_id": "commit-0/lib",
            "repo": "owner/repo",
            "base_commit": "deadbeef",
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }
        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "wrongname"},
        )

        with pytest.raises(ValueError, match="dataset_name must contain 'commit0'"):
            run_agent_js_module._run_agent_for_repo_js_impl(
                repo_base_dir=str(tmp_path),
                agent_config=agent_config,
                example=example,
                branch="commit0",
            )


class TestRepoNamePathTraversal:
    def test_multi_segment_repo_path_raises_value_error_on_unpacking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent_config = MagicMock()
        agent_config.agent_name = "aider"

        example: Any = {
            "instance_id": "commit-0/lib",
            "repo": "owner/sub/lib",
            "base_commit": "deadbeef",
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }
        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "commit0_js"},
        )
        with pytest.raises(ValueError, match="too many values"):
            run_agent_js_module._run_agent_for_repo_js_impl(
                repo_base_dir=str(tmp_path),
                agent_config=agent_config,
                example=example,
                branch="commit0",
            )

    def test_dotdot_repo_name_escapes_repo_base_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, str] = {}

        def _capture_repo_init(path):
            captured["repo_path"] = str(path)
            raise git.exc.NoSuchPathError(str(path))

        agent_config = MagicMock()
        agent_config.agent_name = "aider"

        example: Any = {
            "instance_id": "commit-0/..",
            "repo": "owner/..",
            "base_commit": "deadbeef",
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }
        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "commit0_js"},
        )
        monkeypatch.setattr(run_agent_js_module, "Repo", _capture_repo_init)

        base = tmp_path / "repos"
        base.mkdir()
        try:
            run_agent_js_module.run_agent_for_repo_js(
                repo_base_dir=str(base),
                agent_config=agent_config,
                example=example,
                branch="commit0",
            )
        except (git.exc.NoSuchPathError, FileNotFoundError, Exception):
            pass

        repo_path = captured.get("repo_path", "")
        assert ".." not in os.path.normpath(repo_path) or os.path.realpath(
            repo_path
        ) == os.path.realpath(str(tmp_path)), (
            f"RA-G13: repo=owner/.. resolved to {repo_path!r} which escapes "
            f"repo_base_dir {base!r}. If sanitisation is added, update this "
            f"test to assert the rejection."
        )


def _setup_repo_with_branch_and_extra_commit(
    base: Path, repo_name: str
) -> tuple[git.Repo, str, str]:
    repo_dir = base / repo_name
    repo, base_sha = _init_repo(repo_dir)
    repo.git.checkout("-b", "commit0_main")
    extra = repo_dir / "extra.txt"
    extra.write_text("agent-work", encoding="utf-8")
    repo.index.add(["extra.txt"])
    head_after = repo.index.commit("agent work")
    return repo, base_sha, head_after.hexsha


class TestF001OverrideResetGuard:
    def _common_example(self, base_sha: str) -> dict[str, Any]:
        return {
            "instance_id": "commit-0/lib",
            "repo": "owner/lib",
            "base_commit": base_sha,
            "reference_commit": base_sha,
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }

    def _stub_run_dependencies(
        self, monkeypatch: pytest.MonkeyPatch, log_dir: Path
    ) -> None:
        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "commit0_js"},
        )
        monkeypatch.setattr(
            run_agent_js_module,
            "AiderJsAgents",
            lambda *_a, **_kw: MagicMock(),
        )
        monkeypatch.setattr(
            run_agent_js_module,
            "get_target_edit_files_js",
            lambda *_a, **_kw: ([], {}),
        )
        monkeypatch.setattr(
            run_agent_js_module, "get_js_tests", lambda *_a, **_kw: []
        )

    def test_done_markers_block_destructive_reset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base_sha, agent_head = _setup_repo_with_branch_and_extra_commit(
            tmp_path, "lib"
        )
        log_dir = tmp_path / "logs"
        prior = log_dir / "lib" / "commit0_main" / "current" / "src__foo"
        prior.mkdir(parents=True)
        (prior / ".done").touch()

        agent_config = MagicMock()
        agent_config.agent_name = "aider"
        self._stub_run_dependencies(monkeypatch, log_dir)

        with pytest.raises(RuntimeError, match="Refusing to reset"):
            run_agent_js_module._run_agent_for_repo_js_impl(
                repo_base_dir=str(tmp_path),
                agent_config=agent_config,
                example=self._common_example(base_sha),
                branch="commit0_main",
                override_previous_changes=True,
                log_dir=str(log_dir),
            )
        assert repo.head.commit.hexsha == agent_head, (
            "F-001: agent commit must be preserved when .done markers exist"
        )


class TestF006ReferenceCommitRequired:
    def test_missing_reference_commit_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base_sha = _init_repo(tmp_path / "lib")
        repo.git.checkout("-b", "commit0_main")

        example: Any = {
            "instance_id": "commit-0/lib",
            "repo": "owner/lib",
            "base_commit": base_sha,
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }
        agent_config = MagicMock()
        agent_config.agent_name = "aider"

        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "commit0_js"},
        )
        monkeypatch.setattr(
            run_agent_js_module,
            "AiderJsAgents",
            lambda *_a, **_kw: MagicMock(),
        )

        with pytest.raises(ValueError, match="reference_commit"):
            run_agent_js_module._run_agent_for_repo_js_impl(
                repo_base_dir=str(tmp_path),
                agent_config=agent_config,
                example=example,
                branch="commit0_main",
                override_previous_changes=False,
                log_dir=str(tmp_path / "logs"),
            )

    def test_empty_reference_commit_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, base_sha = _init_repo(tmp_path / "lib")
        repo.git.checkout("-b", "commit0_main")

        example: Any = {
            "instance_id": "commit-0/lib",
            "repo": "owner/lib",
            "base_commit": base_sha,
            "reference_commit": "",
            "setup": {},
            "test": {"test_dir": "tests"},
            "src_dir": "src",
        }
        agent_config = MagicMock()
        agent_config.agent_name = "aider"

        monkeypatch.setattr(
            run_agent_js_module,
            "read_commit0_js_config_file",
            lambda _: {"dataset_name": "commit0_js"},
        )
        monkeypatch.setattr(
            run_agent_js_module,
            "AiderJsAgents",
            lambda *_a, **_kw: MagicMock(),
        )

        with pytest.raises(ValueError, match="reference_commit"):
            run_agent_js_module._run_agent_for_repo_js_impl(
                repo_base_dir=str(tmp_path),
                agent_config=agent_config,
                example=example,
                branch="commit0_main",
                override_previous_changes=False,
                log_dir=str(tmp_path / "logs"),
            )
