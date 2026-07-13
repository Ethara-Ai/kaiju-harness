"""Comprehensive tests for tools.prepare_repo_ts — covers detect_ts_src_dir,
detect_ts_test_dirs, _detect_spec_url, _collect_extra_scan_dirs, fork_repo_ts,
create_ts_stubbed_branch, and prepare_ts_repo.

Complements test_prepare_repo_ts_additions.py which covers detect_package_manager,
detect_test_framework, and generate_setup_dict_ts.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE = "tools.prepare_repo_ts"


# ---------------------------------------------------------------------------
# detect_ts_src_dir
# ---------------------------------------------------------------------------


class TestDetectTsSrcDir:
    def test_src_with_ts_files(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == "src"

    def test_lib_with_ts_files(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "main.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == "lib"

    def test_src_takes_priority_over_lib(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.ts").write_text("")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "b.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == "src"

    def test_root_flat_layout(self, tmp_path: Path) -> None:
        (tmp_path / "tsconfig.json").write_text("{}")
        (tmp_path / "main.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == "."

    def test_root_ignores_d_ts(self, tmp_path: Path) -> None:
        (tmp_path / "types.d.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) != "."

    def test_index_ts_in_child_dir(self, tmp_path: Path) -> None:
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "index.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == "core"

    def test_skips_dot_dirs_and_node_modules(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "index.ts").write_text("")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "index.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == ""

    def test_empty_repo(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == ""

    def test_src_dir_empty_no_ts_files(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "readme.md").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) != "src"

    def test_nested_ts_in_src(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "deep" / "nested").mkdir(parents=True)
        (tmp_path / "src" / "deep" / "nested" / "module.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_src_dir

        assert detect_ts_src_dir(tmp_path) == "src"


# ---------------------------------------------------------------------------
# detect_ts_test_dirs
# ---------------------------------------------------------------------------


class TestDetectTsTestDirs:
    def test_finds_tests_dir(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "foo.test.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        dirs = detect_ts_test_dirs(tmp_path)
        assert len(dirs) == 1
        assert dirs[0].name == "tests"

    def test_finds___tests__(self, tmp_path: Path) -> None:
        tests = tmp_path / "__tests__"
        tests.mkdir()
        (tests / "bar.spec.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        dirs = detect_ts_test_dirs(tmp_path)
        assert len(dirs) == 1
        assert dirs[0].name == "__tests__"

    def test_finds_multiple_test_dirs(self, tmp_path: Path) -> None:
        for name in ["test", "tests", "__tests__"]:
            d = tmp_path / name
            d.mkdir()
            (d / "a.test.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        dirs = detect_ts_test_dirs(tmp_path)
        assert len(dirs) == 3

    def test_ignores_empty_test_dir(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "readme.md").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        assert detect_ts_test_dirs(tmp_path) == []

    def test_finds_js_test_files_too(self, tmp_path: Path) -> None:
        tests = tmp_path / "test"
        tests.mkdir()
        (tests / "legacy.test.js").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        dirs = detect_ts_test_dirs(tmp_path)
        assert len(dirs) == 1

    def test_finds_nested_test_files(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        (tests / "unit").mkdir(parents=True)
        (tests / "unit" / "deep.spec.tsx").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        dirs = detect_ts_test_dirs(tmp_path)
        assert len(dirs) == 1

    def test_case_insensitive_dir_names(self, tmp_path: Path) -> None:
        tests = tmp_path / "Tests"
        tests.mkdir()
        (tests / "foo.test.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        dirs = detect_ts_test_dirs(tmp_path)
        assert len(dirs) == 1

    def test_no_test_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.ts").write_text("")

        from tools.prepare_repo_ts import detect_ts_test_dirs

        assert detect_ts_test_dirs(tmp_path) == []


# ---------------------------------------------------------------------------
# _detect_spec_url
# ---------------------------------------------------------------------------


class TestDetectSpecUrl:
    def test_homepage_field(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://my-lib.dev/docs"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == "https://my-lib.dev/docs"

    def test_docs_field(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "docs": "https://my-lib.dev/api"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == "https://my-lib.dev/api"

    def test_documentation_field(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "documentation": "https://docs.my-lib.dev"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == "https://docs.my-lib.dev"

    def test_homepage_priority_over_docs(self, tmp_path: Path) -> None:
        pkg = {
            "name": "my-lib",
            "homepage": "https://homepage.dev",
            "docs": "https://docs.dev",
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == "https://homepage.dev"

    def test_blocks_github_homepage(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://github.com/owner/repo"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        result = _detect_spec_url(tmp_path)
        assert "github.com" not in result

    def test_blocks_gitlab_homepage(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://gitlab.com/owner/repo"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        result = _detect_spec_url(tmp_path)
        assert "gitlab.com" not in result

    def test_blocks_npmjs_homepage(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://www.npmjs.com/package/my-lib"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        result = _detect_spec_url(tmp_path)
        assert "npmjs.com" not in result

    def test_npm_registry_fallback(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://github.com/owner/repo"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        npm_data = {"homepage": "https://my-lib-docs.io"}

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(npm_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = _detect_spec_url(tmp_path)

        assert result == "https://my-lib-docs.io"

    def test_npm_registry_blocked_falls_to_skypack(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://github.com/owner/repo"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        npm_data = {"homepage": "https://github.com/owner/repo"}

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(npm_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = _detect_spec_url(tmp_path)

        assert result == "https://www.skypack.dev/view/my-lib"

    def test_skypack_fallback_for_no_homepage(self, tmp_path: Path) -> None:
        pkg = {"name": "@scope/my-lib"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("network error")
            result = _detect_spec_url(tmp_path)

        assert result == "https://www.skypack.dev/view/@scope/my-lib"

    def test_no_package_json(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == ""

    def test_invalid_package_json(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("not json")

        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == ""

    def test_empty_homepage(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": ""}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        result = _detect_spec_url(tmp_path)
        assert result != ""

    def test_scoped_package_skypack(self, tmp_path: Path) -> None:
        pkg = {"name": "@babel/core"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen", side_effect=Exception("fail")):
            result = _detect_spec_url(tmp_path)

        assert result == "https://www.skypack.dev/view/@babel/core"

    def test_blocked_docs_field_falls_through(self, tmp_path: Path) -> None:
        pkg = {
            "name": "my-lib",
            "homepage": "https://github.com/me/lib",
            "docs": "https://gitlab.com/docs",
            "documentation": "https://npmjs.com/pkg",
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen", side_effect=Exception("fail")):
            result = _detect_spec_url(tmp_path)

        assert result == "https://www.skypack.dev/view/my-lib"


# ---------------------------------------------------------------------------
# _collect_extra_scan_dirs
# ---------------------------------------------------------------------------


class TestCollectExtraScanDirs:
    def test_includes_test_dirs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "foo.test.ts").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [tests])
        assert tests in result

    def test_includes_sibling_packages(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        sibling = tmp_path / "utils"
        sibling.mkdir()
        (sibling / "helper.ts").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [])
        assert sibling in result

    def test_excludes_src_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.ts").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [])
        assert src not in result

    def test_excludes_node_modules(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "dep.ts").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [])
        assert nm not in result

    def test_excludes_dot_dirs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        hidden = tmp_path / ".config"
        hidden.mkdir()
        (hidden / "setup.ts").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [])
        assert hidden not in result

    def test_excludes_dirs_without_ts_files(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "readme.md").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [])
        assert docs not in result

    def test_no_duplicates_with_test_dirs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "foo.test.ts").write_text("")

        from tools.prepare_repo_ts import _collect_extra_scan_dirs

        result = _collect_extra_scan_dirs(tmp_path, src, [tests])
        assert result.count(tests) == 1


# ---------------------------------------------------------------------------
# fork_repo_ts
# ---------------------------------------------------------------------------


class TestForkRepoTs:
    def test_fork_already_exists(self) -> None:
        from tools.prepare_repo_ts import fork_repo_ts

        with patch(f"{MODULE}.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = fork_repo_ts("owner/repo", "MyOrg")

        assert result == "MyOrg/repo"
        mock_run.assert_called_once()

    def test_fork_created_successfully(self) -> None:
        from tools.prepare_repo_ts import fork_repo_ts

        call_count = [0]

        def mock_run_side_effect(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if "view" in cmd and call_count[0] == 1:
                result.returncode = 1
                return result
            if "fork" in cmd:
                result.returncode = 0
                return result
            result.returncode = 0
            return result

        with patch(f"{MODULE}.subprocess.run", side_effect=mock_run_side_effect):
            with patch("time.sleep"):
                result = fork_repo_ts("owner/repo", "MyOrg")

        assert result == "MyOrg/repo"

    def test_fork_user_account_fallback(self) -> None:
        from tools.prepare_repo_ts import fork_repo_ts

        call_count = [0]

        def mock_run_side_effect(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if "view" in cmd and call_count[0] == 1:
                result.returncode = 1
                return result
            if "fork" in cmd and "--org" in cmd:
                result.returncode = 1
                result.stderr = "login for a user account"
                return result
            if "fork" in cmd:
                result.returncode = 0
                return result
            result.returncode = 0
            return result

        with patch(f"{MODULE}.subprocess.run", side_effect=mock_run_side_effect):
            with patch("time.sleep"):
                result = fork_repo_ts("owner/repo", "MyOrg")

        assert result == "MyOrg/repo"

    def test_fork_timeout(self) -> None:
        from tools.prepare_repo_ts import fork_repo_ts

        call_count = [0]

        def mock_run_side_effect(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock()
            if "fork" in cmd and "--org" in cmd and call_count[0] <= 2:
                result.returncode = 0
                return result
            result.returncode = 1
            return result

        with patch(f"{MODULE}.subprocess.run", side_effect=mock_run_side_effect):
            with patch("time.sleep"):
                with pytest.raises(RuntimeError, match="not available after"):
                    fork_repo_ts("owner/repo", "MyOrg")

    def test_fork_other_error_raises(self) -> None:
        from tools.prepare_repo_ts import fork_repo_ts

        def mock_run_side_effect(cmd, **kwargs):
            result = MagicMock()
            if "view" in cmd:
                result.returncode = 1
                return result
            result.returncode = 1
            result.stderr = "some other error"
            result.args = cmd
            result.stdout = ""
            return result

        with patch(f"{MODULE}.subprocess.run", side_effect=mock_run_side_effect):
            with pytest.raises(subprocess.CalledProcessError):
                fork_repo_ts("owner/repo", "MyOrg")


# ---------------------------------------------------------------------------
# create_ts_stubbed_branch
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _exec_prefix
# ---------------------------------------------------------------------------


class TestExecPrefix:
    def test_npm_default(self) -> None:
        from tools.prepare_repo_ts import _exec_prefix

        assert _exec_prefix("npm") == "npx"

    def test_pnpm(self) -> None:
        from tools.prepare_repo_ts import _exec_prefix

        assert _exec_prefix("pnpm") == "pnpm exec"

    def test_yarn(self) -> None:
        from tools.prepare_repo_ts import _exec_prefix

        assert _exec_prefix("yarn") == "yarn"

    def test_bun(self) -> None:
        from tools.prepare_repo_ts import _exec_prefix

        assert _exec_prefix("bun") == "bunx"

    def test_unknown_falls_to_npx(self) -> None:
        from tools.prepare_repo_ts import _exec_prefix

        assert _exec_prefix("unknown") == "npx"


# ---------------------------------------------------------------------------
# generate_setup_dict_ts — package manager test commands
# ---------------------------------------------------------------------------


class TestGenerateSetupDictTsPkgManager:
    def _write_test_dir(self, tmp_path: Path) -> None:
        tests_dir = tmp_path / "__tests__"
        tests_dir.mkdir()
        (tests_dir / "a.test.ts").write_text("test('x', () => {});")

    def test_npm_uses_npx(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"jest": "*"}})
        )
        (tmp_path / "package-lock.json").write_text("")
        self._write_test_dir(tmp_path)

        from tools.prepare_repo_ts import generate_setup_dict_ts

        setup, test, _ = generate_setup_dict_ts(tmp_path)
        assert setup["install"] == "npm install"
        assert test["test_cmd"] == "npx jest"

    def test_pnpm_uses_pnpm_exec(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"jest": "*"}})
        )
        (tmp_path / "pnpm-lock.yaml").write_text("")
        self._write_test_dir(tmp_path)

        from tools.prepare_repo_ts import generate_setup_dict_ts

        setup, test, _ = generate_setup_dict_ts(tmp_path)
        assert setup["install"] == "pnpm install"
        assert test["test_cmd"] == "pnpm exec jest"

    def test_yarn_uses_yarn(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"vitest": "*"}})
        )
        (tmp_path / "yarn.lock").write_text("")
        self._write_test_dir(tmp_path)

        from tools.prepare_repo_ts import generate_setup_dict_ts

        setup, test, _ = generate_setup_dict_ts(tmp_path)
        assert setup["install"] == "yarn install"
        assert test["test_cmd"] == "yarn vitest run"

    def test_bun_uses_bunx(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"jest": "*"}})
        )
        (tmp_path / "bun.lockb").write_bytes(b"")
        self._write_test_dir(tmp_path)

        from tools.prepare_repo_ts import generate_setup_dict_ts

        setup, test, _ = generate_setup_dict_ts(tmp_path)
        assert setup["install"] == "bun install"
        assert test["test_cmd"] == "bunx jest"


# ---------------------------------------------------------------------------
# create_ts_stubbed_branch — package manager install
# ---------------------------------------------------------------------------


class TestCreateTsStubBranchPkgManager:
    def test_pnpm_install_used(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")

        from tools.prepare_repo_ts import create_ts_stubbed_branch

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:

                def git_side_effect(repo_dir, *args, **kwargs):
                    if args[0] == "status":
                        return "M src/main.ts"
                    if args[0] == "diff":
                        return '+new line\n+  throw new Error("STUB");\n-old line\n'
                    return ""

                mock_git.side_effect = git_side_effect

                with patch(f"{MODULE}.get_head_sha", side_effect=["ref123", "base456"]):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 1,
                            "files_modified": 1,
                            "functions_stubbed": 1,
                            "functions_preserved": 0,
                            "errors": [],
                        },
                    ):
                        with patch(f"{MODULE}.subprocess.run") as mock_subproc:
                            mock_subproc.return_value = MagicMock(returncode=0)
                            create_ts_stubbed_branch(tmp_path, "owner/repo", "src")

        install_call = mock_subproc.call_args
        assert install_call[0][0][0] == "pnpm"
        assert "install" in install_call[0][0]

    def test_bun_install_no_ignore_scripts(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")
        (tmp_path / "bun.lockb").write_bytes(b"")

        from tools.prepare_repo_ts import create_ts_stubbed_branch

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:

                def git_side_effect(repo_dir, *args, **kwargs):
                    if args[0] == "status":
                        return "M src/main.ts"
                    if args[0] == "diff":
                        return '+new line\n+  throw new Error("STUB");\n-old line\n'
                    return ""

                mock_git.side_effect = git_side_effect

                with patch(f"{MODULE}.get_head_sha", side_effect=["ref123", "base456"]):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 1,
                            "files_modified": 1,
                            "functions_stubbed": 1,
                            "functions_preserved": 0,
                            "errors": [],
                        },
                    ):
                        with patch(f"{MODULE}.subprocess.run") as mock_subproc:
                            mock_subproc.return_value = MagicMock(returncode=0)
                            create_ts_stubbed_branch(tmp_path, "owner/repo", "src")

        install_cmd = mock_subproc.call_args[0][0]
        assert install_cmd[0] == "bun"
        assert "--ignore-scripts" not in install_cmd


# ---------------------------------------------------------------------------
# create_ts_stubbed_branch
# ---------------------------------------------------------------------------


class TestCreateTsStubBranch:
    def test_creates_branch_and_commits(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")

        from tools.prepare_repo_ts import create_ts_stubbed_branch

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.return_value = "abc123" * 2
                with patch(
                    f"{MODULE}.get_head_sha", side_effect=["ref123abc", "base456def"]
                ):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 5,
                            "files_modified": 3,
                            "functions_stubbed": 10,
                            "functions_preserved": 2,
                            "errors": [],
                        },
                    ):
                        mock_git.side_effect = None
                        mock_git.return_value = "M src/main.ts"

                        def git_side_effect(repo_dir, *args, **kwargs):
                            if args[0] == "status":
                                return "M src/main.ts"
                            if args[0] == "diff":
                                return '+new line\n+  throw new Error("STUB");\n-old line\n'
                            return ""

                        mock_git.side_effect = git_side_effect

                        base, ref, _, _ = create_ts_stubbed_branch(
                            tmp_path, "owner/repo", "src"
                        )

    def test_raises_on_invalid_src_dir(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git"):
                with patch(f"{MODULE}.get_head_sha", return_value="abc123"):
                    with pytest.raises(ValueError, match="src_dir does not exist"):
                        create_ts_stubbed_branch(tmp_path, "owner/repo", "nonexistent")


# ---------------------------------------------------------------------------
# prepare_ts_repo
# ---------------------------------------------------------------------------


class TestPrepareTsRepo:
    def test_dry_run_skips_fork_and_push(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch(f"{MODULE}.fork_repo_ts") as mock_fork:
            with patch(f"{MODULE}.full_clone", return_value=tmp_path):
                with patch(f"{MODULE}.detect_ts_src_dir", return_value="src"):
                    with patch(
                        f"{MODULE}.generate_setup_dict_ts",
                        return_value=(
                            {
                                "node": "20",
                                "install": "npm install",
                                "packages": [],
                                "pre_install": [],
                                "specification": "",
                            },
                            {"test_cmd": "npx jest", "test_dir": "__tests__"},
                            "jest",
                        ),
                    ):
                        with patch(
                            f"{MODULE}.create_ts_stubbed_branch",
                            return_value=("base123", "ref456", 5),
                        ):
                            with patch(f"{MODULE}.push_to_fork"):
                                result = prepare_ts_repo(
                                    "owner/repo",
                                    tmp_path,
                                    dry_run=True,
                                )

        mock_fork.assert_not_called()
        assert result is not None
        assert result["language"] == "typescript"
        assert result["base_commit"] == "base123"
        assert result["reference_commit"] == "ref456"

    def test_returns_none_on_no_src_dir(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch(f"{MODULE}.fork_repo_ts", return_value="Org/repo"):
            with patch(f"{MODULE}.full_clone", return_value=tmp_path):
                with patch(f"{MODULE}.detect_ts_src_dir", return_value=""):
                    result = prepare_ts_repo(
                        "owner/repo",
                        tmp_path,
                        dry_run=True,
                    )

        assert result is None

    def test_uses_src_dir_override(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch(f"{MODULE}.fork_repo_ts", return_value="Org/repo"):
            with patch(f"{MODULE}.full_clone", return_value=tmp_path):
                with patch(f"{MODULE}.detect_ts_src_dir") as mock_detect:
                    with patch(
                        f"{MODULE}.generate_setup_dict_ts",
                        return_value=(
                            {
                                "node": "20",
                                "install": "npm install",
                                "packages": [],
                                "pre_install": [],
                                "specification": "",
                            },
                            {"test_cmd": "npx jest", "test_dir": "__tests__"},
                            "jest",
                        ),
                    ):
                        with patch(
                            f"{MODULE}.create_ts_stubbed_branch",
                            return_value=("b", "r", 1),
                        ):
                            with patch(f"{MODULE}.push_to_fork"):
                                result = prepare_ts_repo(
                                    "owner/repo",
                                    tmp_path,
                                    src_dir_override="custom_src",
                                    dry_run=True,
                                )

        mock_detect.assert_not_called()
        assert result is not None
        assert result["src_dir"] == "custom_src"

    def test_requires_github_token_for_non_dry_run(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch.dict("os.environ", {}, clear=True):
            with patch("os.environ.get", return_value=None):
                with pytest.raises(EnvironmentError, match="GITHUB_TOKEN"):
                    prepare_ts_repo("owner/repo", tmp_path, dry_run=False)

    def test_output_entry_structure(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch(f"{MODULE}.fork_repo_ts", return_value="Org/repo"):
            with patch(f"{MODULE}.full_clone", return_value=tmp_path):
                with patch(f"{MODULE}.detect_ts_src_dir", return_value="src"):
                    with patch(
                        f"{MODULE}.generate_setup_dict_ts",
                        return_value=(
                            {
                                "node": "20",
                                "install": "npm install",
                                "packages": ["jest"],
                                "pre_install": [],
                                "specification": "https://docs.example.com",
                            },
                            {"test_cmd": "npx jest", "test_dir": "tests"},
                            "jest",
                        ),
                    ):
                        with patch(
                            f"{MODULE}.create_ts_stubbed_branch",
                            return_value=("base_abc", "ref_def", 7),
                        ):
                            result = prepare_ts_repo(
                                "owner/my-lib",
                                tmp_path,
                                dry_run=True,
                            )

        assert result is not None
        assert result["instance_id"] == "commit-0/my-lib"
        assert result["repo"] == "Zahgon/my-lib"
        assert result["original_repo"] == "owner/my-lib"
        assert result["base_commit"] == "base_abc"
        assert result["reference_commit"] == "ref_def"
        assert result["src_dir"] == "src"
        assert result["language"] == "typescript"
        assert result["test_framework"] == "jest"
        assert "setup" in result
        assert "test" in result
        assert result["setup"]["specification"] == "https://docs.example.com"
        assert result["functions_stubbed"] == 7
