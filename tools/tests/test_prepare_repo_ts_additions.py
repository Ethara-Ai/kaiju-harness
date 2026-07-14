import json
from pathlib import Path

import pytest

from tools.prepare_repo_ts import (
    detect_package_manager,
    detect_test_framework,
    generate_setup_dict_ts,
)


def test_detect_package_manager_npm(tmp_path: Path) -> None:
    assert detect_package_manager(tmp_path) == "npm"


def test_detect_package_manager_yarn(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").touch()
    assert detect_package_manager(tmp_path) == "yarn"


def test_detect_package_manager_pnpm(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").touch()
    assert detect_package_manager(tmp_path) == "pnpm"


def test_detect_test_framework_jest_dep(tmp_path: Path) -> None:
    pkg = {"devDependencies": {"jest": "^29.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_test_framework(tmp_path) == "jest"


def test_detect_test_framework_vitest_dep(tmp_path: Path) -> None:
    pkg = {"devDependencies": {"vitest": "^1.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_test_framework(tmp_path) == "vitest"


def test_detect_test_framework_config_file(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({}))
    (tmp_path / "vitest.config.ts").touch()
    assert detect_test_framework(tmp_path) == "vitest"


def test_detect_test_framework_default(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({}))
    assert detect_test_framework(tmp_path) == "jest"


def test_detect_test_framework_both(tmp_path: Path) -> None:
    pkg = {"devDependencies": {"vitest": "^1.0.0", "jest": "^29.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    assert detect_test_framework(tmp_path) == "vitest"


def test_generate_setup_dict_ts(tmp_path: Path) -> None:
    pkg = {
        "devDependencies": {
            "vitest": "^1.0.0",
            "@vitest/coverage-v8": "^1.0.0",
            "typescript": "^5.0.0",
        }
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg))
    (tmp_path / "yarn.lock").touch()
    test_dir = tmp_path / "__tests__"
    test_dir.mkdir()
    (test_dir / "app.test.ts").touch()

    setup_dict, test_dict, test_framework = generate_setup_dict_ts(tmp_path)

    assert test_framework == "vitest"
    assert setup_dict["node_version"] == "20"
    assert setup_dict["install"] == "yarn install"
    assert "@vitest/coverage-v8" in setup_dict["packages"]
    assert "vitest" in setup_dict["packages"]
    assert "typescript" not in setup_dict["packages"]
    assert test_dict["test_cmd"] == "yarn vitest run"
    assert test_dict["test_dir"] == "__tests__"


import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

MODULE = "tools.prepare_repo_ts"


class TestDetectTestFrameworkEdgeCases:
    def test_no_package_json_defaults_to_jest(self, tmp_path: Path) -> None:
        assert detect_test_framework(tmp_path) == "jest"

    def test_invalid_package_json_defaults_to_jest(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("not valid json {{")
        assert detect_test_framework(tmp_path) == "jest"

    def test_jest_globals_dep(self, tmp_path: Path) -> None:
        pkg = {"devDependencies": {"@jest/globals": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(tmp_path) == "jest"

    def test_jest_config_file_js(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({}))
        (tmp_path / "jest.config.js").touch()
        assert detect_test_framework(tmp_path) == "jest"

    def test_jest_inline_config(self, tmp_path: Path) -> None:
        pkg = {"jest": {"testMatch": ["**/*.test.ts"]}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(tmp_path) == "jest"

    def test_vitest_in_test_script(self, tmp_path: Path) -> None:
        pkg = {"scripts": {"test": "vitest run"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(tmp_path) == "vitest"

    def test_jest_in_test_script(self, tmp_path: Path) -> None:
        pkg = {"scripts": {"test": "jest --coverage"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(tmp_path) == "jest"

    def test_no_indicators_defaults_jest(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "dependencies": {"lodash": "^4.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        assert detect_test_framework(tmp_path) == "jest"


class TestDetectSpecUrlEdgeCases:
    def test_npm_registry_returns_valid_homepage(self, tmp_path: Path) -> None:
        pkg = {"name": "my-lib", "homepage": "https://github.com/o/r"}
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

    def test_npm_registry_exception_returns_empty(self, tmp_path: Path) -> None:
        # Local homepage is blocked (github.com) and the npm registry probe raises.
        # No Skypack fallback anymore -> '' (caller skips the scrape cleanly).
        pkg = {"name": "my-lib", "homepage": "https://github.com/o/r"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = _detect_spec_url(tmp_path)

        assert result == ""

    def test_no_name_no_homepage_returns_empty(self, tmp_path: Path) -> None:
        pkg = {"version": "1.0.0"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        from tools.prepare_repo_ts import _detect_spec_url

        assert _detect_spec_url(tmp_path) == ""

    def test_npm_registry_blocked_homepage_returns_empty(
        self, tmp_path: Path
    ) -> None:
        # npm registry homepage is itself blocked (github.com); no CDN fallback -> ''.
        pkg = {"name": "my-lib", "homepage": "https://github.com/o/r"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        npm_data = {"homepage": "https://github.com/o/r"}

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(npm_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = _detect_spec_url(tmp_path)

        assert result == ""

    def test_npm_registry_no_homepage_returns_empty(self, tmp_path: Path) -> None:
        # npm registry metadata has no homepage at all; no CDN fallback -> ''.
        pkg = {"name": "my-lib", "homepage": "https://github.com/o/r"}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        npm_data = {"name": "my-lib"}

        from tools.prepare_repo_ts import _detect_spec_url

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(npm_data).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = _detect_spec_url(tmp_path)

        assert result == ""


class TestGenerateSetupDictTsEdgeCases:
    def _write_test_dir(self, tmp_path: Path) -> None:
        tests_dir = tmp_path / "__tests__"
        tests_dir.mkdir()
        (tests_dir / "a.test.ts").write_text("test('x', () => {});")

    def test_package_json_parse_failure_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("invalid json")

        # B5: reverted E3 hard-error — mirror JS (prepare_repo_js.py:711-716)
        # and default test_dir='.' so the framework self-discovers. A truly
        # test-less repo surfaces downstream as 0 collected (infra flag).
        setup_dict, test_dict, test_framework = generate_setup_dict_ts(tmp_path)
        assert test_dict["test_dir"] == "."

    def test_package_json_parse_failure_with_tests_present(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("invalid json")
        self._write_test_dir(tmp_path)

        setup_dict, test_dict, test_framework = generate_setup_dict_ts(tmp_path)

        assert test_framework == "jest"
        assert setup_dict["packages"] == []
        assert test_dict["test_dir"] == "__tests__"
        assert test_dict["test_dir_detected_by"] in ("config", "recursive-scan")

    def test_no_test_dirs_raises_runtime_error(self, tmp_path: Path) -> None:
        # B5: reverted E3 hard-error — defaults test_dir='.' with warning.
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))

        setup_dict, test_dict, test_framework = generate_setup_dict_ts(tmp_path)
        assert test_dict["test_dir"] == "."

    def test_known_test_packages_filtered(self, tmp_path: Path) -> None:
        pkg = {
            "devDependencies": {
                "jest": "^29",
                "@types/jest": "^29",
                "typescript": "^5",
                "eslint": "^8",
                "mocha": "^10",
            }
        }
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        self._write_test_dir(tmp_path)

        setup_dict, _, _ = generate_setup_dict_ts(tmp_path)
        assert "@types/jest" in setup_dict["packages"]
        assert "jest" in setup_dict["packages"]
        assert "mocha" in setup_dict["packages"]
        assert "typescript" not in setup_dict["packages"]
        assert "eslint" not in setup_dict["packages"]


class TestForkRepoTsEdgeCases:
    """``fork_repo_ts`` was removed; forking is delegated to the shared
    ``tools._git_auth.fork_repo`` (re-exported into prepare_repo_ts). These
    exercise its post-fork poll loop against the ``_gh`` helper, with the
    pre-flight scope/access checks stubbed out.
    """

    @staticmethod
    def _enter_preflight(stack) -> None:
        stack.enter_context(patch("tools._git_auth.setup_git_credentials"))
        stack.enter_context(
            patch("tools._git_auth.get_github_token", return_value="ghp_fake")
        )
        stack.enter_context(patch("tools._git_auth.verify_token_scopes"))
        stack.enter_context(patch("tools._git_auth.verify_org_write_access"))
        stack.enter_context(
            patch("tools._git_auth._is_self_account", return_value=False)
        )
        stack.enter_context(patch("time.sleep"))

    def test_fork_poll_becomes_ready_after_lag(self) -> None:
        # Fork missing -> fork ok -> first poll not yet visible -> second poll ready.
        from tools.prepare_repo_ts import fork_repo

        seq = [
            MagicMock(returncode=1, stdout="", stderr="Not Found"),  # existence check
            MagicMock(returncode=0, stdout="", stderr=""),  # repo fork
            MagicMock(returncode=1, stdout="", stderr="Not Found"),  # poll: lagging
            MagicMock(returncode=0, stdout="{}", stderr=""),  # poll: ready
        ]

        with ExitStack() as stack:
            self._enter_preflight(stack)
            stack.enter_context(patch("tools._git_auth._gh", side_effect=seq))
            result = fork_repo("owner/repo", "MyOrg", token="ghp_fake")

        assert result == "MyOrg/repo"

    def test_fork_transient_then_success_retries(self) -> None:
        # Fork missing -> transient fork failure (rate limit) -> retry succeeds -> poll ready.
        from tools.prepare_repo_ts import fork_repo

        seq = [
            MagicMock(returncode=1, stdout="", stderr="Not Found"),  # existence check
            MagicMock(returncode=1, stdout="", stderr="API rate limit exceeded"),  # transient
            MagicMock(returncode=0, stdout="", stderr=""),  # retry fork ok
            MagicMock(returncode=0, stdout="{}", stderr=""),  # poll ready
        ]

        with ExitStack() as stack:
            self._enter_preflight(stack)
            stack.enter_context(patch("tools._git_auth._gh", side_effect=seq))
            result = fork_repo("owner/repo", "MyOrg", token="ghp_fake")

        assert result == "MyOrg/repo"

    def test_fork_never_queryable_raises(self) -> None:
        # Fork created but the poll never sees it before the deadline -> ForkError.
        from tools.prepare_repo_ts import fork_repo
        from tools._git_auth import ForkError

        def side_effect(args, **kwargs):
            joined = " ".join(args)
            if "repos/MyOrg/repo" in joined:
                return MagicMock(returncode=1, stdout="", stderr="Not Found")
            if args[:2] == ["repo", "fork"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="Not Found")

        with ExitStack() as stack:
            self._enter_preflight(stack)
            stack.enter_context(patch("tools._git_auth._gh", side_effect=side_effect))
            # wait_seconds=0 makes the poll deadline expire immediately.
            with pytest.raises(ForkError, match="not queryable"):
                fork_repo("owner/repo", "MyOrg", token="ghp_fake", wait_seconds=0)


class TestCreateTsStubBranchEdgeCases:
    def _setup_repo(self, tmp_path: Path) -> Path:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")
        return tmp_path

    def _git_side_effect(self, repo_dir, *args, **kwargs):
        if args[0] == "status":
            return "M src/main.ts"
        if args[0] == "diff":
            return '+new line\n+  throw new Error("STUB");\n-old line\n'
        return ""

    def test_branch_delete_exception_is_ignored(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        self._setup_repo(tmp_path)

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = self._git_side_effect
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
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            base, ref, _, _ = create_ts_stubbed_branch(
                                tmp_path, "owner/repo", "src"
                            )

        assert ref == "ref123"

    def test_stubbing_with_errors_logs_warning(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        self._setup_repo(tmp_path)

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = self._git_side_effect
                with patch(f"{MODULE}.get_head_sha", side_effect=["ref123", "base456"]):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 5,
                            "files_modified": 3,
                            "functions_stubbed": 10,
                            "functions_preserved": 2,
                            "errors": ["err1", "err2", "err3", "err4"],
                        },
                    ):
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            base, ref, _, _ = create_ts_stubbed_branch(
                                tmp_path, "owner/repo", "src"
                            )

        assert ref == "ref123"

    def test_no_changes_after_stubbing_returns_same_commits(
        self, tmp_path: Path
    ) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        self._setup_repo(tmp_path)

        def git_no_changes(repo_dir, *args, **kwargs):
            if args[0] == "status":
                return ""
            return ""

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = git_no_changes
                with patch(f"{MODULE}.get_head_sha", return_value="ref123"):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 1,
                            "files_modified": 0,
                            "functions_stubbed": 0,
                            "functions_preserved": 0,
                            "errors": [],
                        },
                    ):
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            base, ref, _, _ = create_ts_stubbed_branch(
                                tmp_path, "owner/repo", "src"
                            )

        assert base == "ref123"
        assert ref == "ref123"

    def test_no_ts_stub_markers_raises_runtime_error(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        self._setup_repo(tmp_path)

        def git_no_stub_markers(repo_dir, *args, **kwargs):
            if args[0] == "status":
                return "M src/main.ts"
            if args[0] == "diff":
                return "+new line\n-old line\n+another line\n"
            return ""

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = git_no_stub_markers
                with patch(f"{MODULE}.get_head_sha", return_value="ref123"):
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
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            with pytest.raises(
                                RuntimeError, match="Stubbing verification failed"
                            ):
                                create_ts_stubbed_branch(tmp_path, "owner/repo", "src")

    def test_zero_functions_stubbed_raises_runtime_error(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        self._setup_repo(tmp_path)

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = self._git_side_effect
                with patch(f"{MODULE}.get_head_sha", return_value="ref123"):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 1,
                            "files_modified": 0,
                            "functions_stubbed": 0,
                            "functions_preserved": 0,
                            "errors": [],
                        },
                    ):
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            with pytest.raises(
                                RuntimeError, match="No functions were stubbed"
                            ):
                                create_ts_stubbed_branch(tmp_path, "owner/repo", "src")

    def test_dot_src_dir_uses_repo_dir_directly(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        (tmp_path / "index.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = self._git_side_effect
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
                    ) as mock_stub:
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            create_ts_stubbed_branch(tmp_path, "owner/repo", ".")

        assert mock_stub.call_args[1]["src_dir"] == tmp_path

    def test_extra_scan_dirs_logged(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")
        utils = tmp_path / "utils"
        utils.mkdir()
        (utils / "helper.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = self._git_side_effect
                with patch(f"{MODULE}.get_head_sha", side_effect=["ref123", "base456"]):
                    with patch(
                        f"{MODULE}.run_stub_ts",
                        return_value={
                            "files_processed": 2,
                            "files_modified": 1,
                            "functions_stubbed": 3,
                            "functions_preserved": 1,
                            "errors": [],
                        },
                    ):
                        with patch(
                            f"{MODULE}.subprocess.run",
                            return_value=MagicMock(returncode=0),
                        ):
                            base, ref, _, _ = create_ts_stubbed_branch(
                                tmp_path, "owner/repo", "src"
                            )

        assert ref == "ref123"

    def test_no_package_json_skips_install(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import create_ts_stubbed_branch

        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")

        with patch(f"{MODULE}.get_default_branch", return_value="main"):
            with patch(f"{MODULE}.git") as mock_git:
                mock_git.side_effect = self._git_side_effect
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

        mock_subproc.assert_not_called()


class TestPrepareTsRepoEdgeCases:
    def test_non_dry_run_forks_and_pushes(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}):
            with patch(f"{MODULE}.fork_repo", return_value="Org/repo") as mock_fork:
                with patch(f"{MODULE}.full_clone", return_value=tmp_path):
                    with patch(f"{MODULE}.detect_ts_src_dir", return_value="src"):
                        with patch(
                            f"{MODULE}.generate_setup_dict_ts",
                            return_value=(
                                {
                                    "node_version": "20",
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
                                return_value=("base", "ref", 1, True),
                            ):
                                with patch(f"{MODULE}.git"):
                                    with patch(f"{MODULE}.push_to_fork") as mock_push:
                                        result = prepare_ts_repo(
                                            "owner/repo",
                                            tmp_path,
                                            dry_run=False,
                                        )

        mock_fork.assert_called_once()
        mock_push.assert_called_once()
        assert result is not None

    def test_release_tag_logged(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch(f"{MODULE}.full_clone", return_value=tmp_path) as mock_clone:
            with patch(f"{MODULE}.detect_ts_src_dir", return_value="src"):
                with patch(
                    f"{MODULE}.generate_setup_dict_ts",
                    return_value=(
                        {
                            "node_version": "20",
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
                        return_value=("base", "ref", 1, True),
                    ):
                        result = prepare_ts_repo(
                            "owner/repo",
                            tmp_path,
                            release_tag="v1.0.0",
                            dry_run=True,
                        )

        mock_clone.assert_called_once_with("owner/repo", tmp_path, tag="v1.0.0")
        assert result is not None

    def test_no_github_token_raises(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import prepare_ts_repo

        with patch.dict(os.environ, {}, clear=True):
            with patch("os.environ.get", return_value=None):
                with pytest.raises(EnvironmentError, match="GITHUB_TOKEN"):
                    prepare_ts_repo("owner/repo", tmp_path, dry_run=False)


class TestMainEntryPoint:
    def test_main_successful_with_output(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import main

        output_file = tmp_path / "entries.json"

        test_args = [
            "prepare_repo_ts.py",
            "--repo",
            "owner/my-lib",
            "--dry-run",
            "--clone-dir",
            str(tmp_path),
            "--output",
            str(output_file),
        ]

        with patch("sys.argv", test_args):
            with patch(
                f"{MODULE}.prepare_ts_repo",
                return_value={
                    "instance_id": "commit-0/my-lib",
                    "repo": "Org/my-lib",
                    "original_repo": "owner/my-lib",
                    "base_commit": "abc",
                    "reference_commit": "def",
                    "src_dir": "src",
                    "language": "typescript",
                    "test_framework": "jest",
                    "setup": {},
                    "test": {},
                },
            ):
                main()

        assert output_file.exists()
        entries = json.loads(output_file.read_text())
        assert len(entries) == 1
        assert entries[0]["instance_id"] == "commit-0/my-lib"

    def test_main_successful_prints_to_stdout(self, tmp_path: Path, capsys) -> None:
        from tools.prepare_repo_ts import main

        test_args = [
            "prepare_repo_ts.py",
            "--repo",
            "owner/my-lib",
            "--dry-run",
            "--clone-dir",
            str(tmp_path),
        ]

        with patch("sys.argv", test_args):
            with patch(
                f"{MODULE}.prepare_ts_repo",
                return_value={
                    "instance_id": "commit-0/my-lib",
                    "repo": "Org/my-lib",
                    "original_repo": "owner/my-lib",
                    "base_commit": "abc",
                    "reference_commit": "def",
                    "src_dir": "src",
                    "language": "typescript",
                    "test_framework": "jest",
                    "setup": {},
                    "test": {},
                },
            ):
                main()

        captured = capsys.readouterr()
        assert "commit-0/my-lib" in captured.out

    def test_main_returns_none_exits_1(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import main

        test_args = [
            "prepare_repo_ts.py",
            "--repo",
            "owner/my-lib",
            "--dry-run",
            "--clone-dir",
            str(tmp_path),
        ]

        with patch("sys.argv", test_args):
            with patch(f"{MODULE}.prepare_ts_repo", return_value=None):
                with pytest.raises(SystemExit) as exc_info:
                    main()

        assert exc_info.value.code == 1

    def test_main_with_custom_org_and_tag(self, tmp_path: Path) -> None:
        from tools.prepare_repo_ts import main

        test_args = [
            "prepare_repo_ts.py",
            "--repo",
            "owner/my-lib",
            "--org",
            "CustomOrg",
            "--tag",
            "v2.0.0",
            "--src-dir",
            "lib",
            "--dry-run",
            "--clone-dir",
            str(tmp_path),
        ]

        with patch("sys.argv", test_args):
            with patch(
                f"{MODULE}.prepare_ts_repo",
                return_value={
                    "instance_id": "commit-0/my-lib",
                    "repo": "CustomOrg/my-lib",
                    "original_repo": "owner/my-lib",
                    "base_commit": "abc",
                    "reference_commit": "def",
                    "src_dir": "lib",
                    "language": "typescript",
                    "test_framework": "jest",
                    "setup": {},
                    "test": {},
                },
            ) as mock_prep:
                main()

        mock_prep.assert_called_once_with(
            full_name="owner/my-lib",
            clone_dir=tmp_path,
            org="CustomOrg",
            src_dir_override="lib",
            release_tag="v2.0.0",
            dry_run=True,
            specs_dir="./specs",
        )


class TestMainModuleGuard:
    def test_module_guard(self) -> None:
        with patch(f"{MODULE}.main") as mock_main:
            mock_main()
            mock_main.assert_called_once()


class TestDetectPackageManagerBun:
    def test_bun_lockb(self, tmp_path: Path) -> None:
        (tmp_path / "bun.lockb").write_bytes(b"\x00")
        assert detect_package_manager(tmp_path) == "bun"
