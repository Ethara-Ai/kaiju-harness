"""Tests for commit0.harness.lint_ts — ESLint and tsc --noEmit runners."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE = "commit0.harness.lint_ts"


# ---------------------------------------------------------------------------
# run_eslint
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _detect_exec_prefix
# ---------------------------------------------------------------------------


class TestDetectExecPrefix:
    def test_npm_default(self, tmp_path: Path) -> None:
        from commit0.harness.lint_ts import _detect_exec_prefix

        assert _detect_exec_prefix(str(tmp_path)) == "npx"

    def test_pnpm(self, tmp_path: Path) -> None:
        (tmp_path / "pnpm-lock.yaml").write_text("")

        from commit0.harness.lint_ts import _detect_exec_prefix

        assert _detect_exec_prefix(str(tmp_path)) == "pnpm exec"

    def test_yarn(self, tmp_path: Path) -> None:
        (tmp_path / "yarn.lock").write_text("")

        from commit0.harness.lint_ts import _detect_exec_prefix

        assert _detect_exec_prefix(str(tmp_path)) == "yarn"

    def test_bun(self, tmp_path: Path) -> None:
        (tmp_path / "bun.lockb").write_bytes(b"")

        from commit0.harness.lint_ts import _detect_exec_prefix

        assert _detect_exec_prefix(str(tmp_path)) == "bunx"


# ---------------------------------------------------------------------------
# run_eslint — package manager variants
# ---------------------------------------------------------------------------


class TestRunEslintPkgManager:
    def test_pnpm_exec_eslint(self, tmp_path: Path) -> None:
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / "eslint.config.js").write_text("export default [];")

        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_eslint(str(tmp_path))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "pnpm"
        assert cmd[1] == "exec"
        assert "eslint" in cmd

    def test_yarn_eslint(self, tmp_path: Path) -> None:
        (tmp_path / "yarn.lock").write_text("")
        (tmp_path / "eslint.config.js").write_text("export default [];")

        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_eslint(str(tmp_path))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "yarn"
        assert "eslint" in cmd

    def test_bunx_eslint(self, tmp_path: Path) -> None:
        (tmp_path / "bun.lockb").write_bytes(b"")
        (tmp_path / "eslint.config.js").write_text("export default [];")

        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_eslint(str(tmp_path))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "bunx"
        assert "eslint" in cmd


# ---------------------------------------------------------------------------
# run_tsc_noEmit — package manager variants
# ---------------------------------------------------------------------------


class TestRunTscNoEmitPkgManager:
    def test_pnpm_exec_tsc(self, tmp_path: Path) -> None:
        (tmp_path / "pnpm-lock.yaml").write_text("")

        from commit0.harness.lint_ts import run_tsc_noEmit

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_tsc_noEmit(str(tmp_path))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "pnpm"
        assert cmd[1] == "exec"
        assert "tsc" in cmd

    def test_bunx_tsc(self, tmp_path: Path) -> None:
        (tmp_path / "bun.lockb").write_bytes(b"")

        from commit0.harness.lint_ts import run_tsc_noEmit

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_tsc_noEmit(str(tmp_path))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "bunx"
        assert "tsc" in cmd


# ---------------------------------------------------------------------------
# run_eslint
# ---------------------------------------------------------------------------


class TestRunEslint:
    def test_success(self) -> None:
        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "All files pass"
        mock_result.stderr = ""

        with patch(f"{MODULE}._repo_has_eslint_config", return_value=True):
            with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
                rc, output = run_eslint("/repo")

        assert rc == 0
        assert "All files pass" in output
        cmd = mock_run.call_args[0][0]
        assert "npx" in cmd
        assert "eslint" in cmd
        assert "." in cmd

    def test_with_specific_files(self) -> None:
        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=0, stdout="ok", stderr="")

        with patch(f"{MODULE}._repo_has_eslint_config", return_value=True):
            with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
                run_eslint("/repo", files=["src/a.ts", "src/b.ts"])

        cmd = mock_run.call_args[0][0]
        assert "src/a.ts" in cmd
        assert "src/b.ts" in cmd
        assert "." not in cmd

    def test_with_config_path(self) -> None:
        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_eslint("/repo", config_path=".eslintrc.json")

        cmd = mock_run.call_args[0][0]
        assert "--config" in cmd
        assert ".eslintrc.json" in cmd

    def test_failure_returns_nonzero(self) -> None:
        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=1, stdout="errors found", stderr="warning")

        with patch(f"{MODULE}._repo_has_eslint_config", return_value=True):
            with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
                rc, output = run_eslint("/repo")

        assert rc == 1
        assert "errors found" in output
        assert "warning" in output

    def test_cwd_is_repo_dir(self) -> None:
        from commit0.harness.lint_ts import run_eslint

        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch(f"{MODULE}._repo_has_eslint_config", return_value=True):
            with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
                run_eslint("/my/repo")

        assert mock_run.call_args[1]["cwd"] == "/my/repo"


# ---------------------------------------------------------------------------
# run_tsc_noEmit
# ---------------------------------------------------------------------------


class TestRunTscNoEmit:
    def test_success(self) -> None:
        from commit0.harness.lint_ts import run_tsc_noEmit

        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            rc, output = run_tsc_noEmit("/repo")

        assert rc == 0
        cmd = mock_run.call_args[0][0]
        assert "tsc" in cmd
        assert "--noEmit" in cmd

    def test_type_errors(self) -> None:
        from commit0.harness.lint_ts import run_tsc_noEmit

        mock_result = MagicMock(
            returncode=2,
            stdout="src/main.ts(5,3): error TS2339: Property 'x' does not exist",
            stderr="",
        )

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result):
            rc, output = run_tsc_noEmit("/repo")

        assert rc == 2
        assert "TS2339" in output

    def test_cwd_is_repo_dir(self) -> None:
        from commit0.harness.lint_ts import run_tsc_noEmit

        mock_result = MagicMock(returncode=0, stdout="", stderr="")

        with patch(f"{MODULE}.subprocess.run", return_value=mock_result) as mock_run:
            run_tsc_noEmit("/my/repo")

        assert mock_run.call_args[1]["cwd"] == "/my/repo"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestLintTsMain:
    def _make_dataset(self, repo_names: list[str]):
        return [{"repo": f"org/{name}"} for name in repo_names]

    def test_runs_both_linters_exits_with_max(self) -> None:
        from commit0.harness.lint_ts import main

        dataset = self._make_dataset(["my-repo"])

        with patch(f"{MODULE}.load_dataset_from_config", return_value=dataset):
            with patch(f"{MODULE}.os.path.isdir", return_value=True):
                with patch(f"{MODULE}.run_eslint", return_value=(1, "lint errors")):
                    with patch(f"{MODULE}.run_tsc_noEmit", return_value=(0, "")):
                        with pytest.raises(SystemExit) as exc_info:
                            main("dataset", "split", "all", "/base", "my-repo")

        assert exc_info.value.code == 1

    def test_tsc_worse_than_eslint(self) -> None:
        from commit0.harness.lint_ts import main

        dataset = self._make_dataset(["my-repo"])

        with patch(f"{MODULE}.load_dataset_from_config", return_value=dataset):
            with patch(f"{MODULE}.os.path.isdir", return_value=True):
                with patch(f"{MODULE}.run_eslint", return_value=(0, "")):
                    with patch(f"{MODULE}.run_tsc_noEmit", return_value=(2, "errors")):
                        with pytest.raises(SystemExit) as exc_info:
                            main("dataset", "split", "all", "/base", "my-repo")

        assert exc_info.value.code == 2

    def test_both_pass(self) -> None:
        from commit0.harness.lint_ts import main

        dataset = self._make_dataset(["my-repo"])

        with patch(f"{MODULE}.load_dataset_from_config", return_value=dataset):
            with patch(f"{MODULE}.os.path.isdir", return_value=True):
                with patch(f"{MODULE}.run_eslint", return_value=(0, "")):
                    with patch(f"{MODULE}.run_tsc_noEmit", return_value=(0, "")):
                        with pytest.raises(SystemExit) as exc_info:
                            main("dataset", "split", "all", "/base", "my-repo")

        assert exc_info.value.code == 0

    def test_repo_not_found_exits(self) -> None:
        from commit0.harness.lint_ts import main

        dataset = self._make_dataset(["other-repo"])

        with patch(f"{MODULE}.load_dataset_from_config", return_value=dataset):
            with patch(f"{MODULE}.os.path.isdir", return_value=False):
                with pytest.raises(SystemExit) as exc_info:
                    main("dataset", "split", "all", "/base", "nonexistent")

        assert exc_info.value.code == 1

    def test_trailing_slash_stripped(self) -> None:
        from commit0.harness.lint_ts import main

        dataset = self._make_dataset(["my-repo"])

        with patch(f"{MODULE}.load_dataset_from_config", return_value=dataset):
            with patch(f"{MODULE}.os.path.isdir", return_value=True):
                with patch(f"{MODULE}.run_eslint", return_value=(0, "")) as mock_eslint:
                    with patch(f"{MODULE}.run_tsc_noEmit", return_value=(0, "")):
                        with pytest.raises(SystemExit):
                            main("my-repo/", "ds", "sp", "all", "/base")

        repo_dir_arg = mock_eslint.call_args[0][0]
        assert not repo_dir_arg.endswith("/")


# ---------------------------------------------------------------------------
# run_ts_tests._inject_test_ids
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# run_ts_tests.main — orchestration
# ---------------------------------------------------------------------------


class TestRunTsTestsMain:
    RUN_MODULE = "commit0.harness.run_ts_tests"

    def _make_dataset(self):
        return [
            {
                "repo": "org/my-repo",
                "instance_id": "commit-0/my-repo",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
        ]

    @patch("commit0.harness.run_ts_tests.load_dataset_from_config")
    def test_no_spec_raises(self, mock_load) -> None:
        from commit0.harness.run_ts_tests import main as run_main

        mock_load.return_value = self._make_dataset()

        with pytest.raises(ValueError, match="No spec available"):
            run_main(
                dataset_name="test.json",
                dataset_split="test",
                base_dir="/repos",
                repo_or_repo_dir="nonexistent-repo",
                branch="commit0",
                test_ids="",
                backend="local",
                timeout=300,
                num_cpus=1,
                rebuild_image=False,
                verbose=0,
            )

    @patch("commit0.harness.run_ts_tests.load_dataset_from_config")
    def test_trailing_slash_stripped(self, mock_load) -> None:
        from commit0.harness.run_ts_tests import main as run_main

        mock_load.return_value = self._make_dataset()

        with pytest.raises(ValueError, match="No spec available"):
            run_main(
                dataset_name="test.json",
                dataset_split="test",
                base_dir="/repos",
                repo_or_repo_dir="other-repo/",
                branch="commit0",
                test_ids="",
                backend="local",
                timeout=300,
                num_cpus=1,
                rebuild_image=False,
                verbose=0,
            )

    @patch("commit0.harness.run_ts_tests.load_dataset_from_config")
    @patch("commit0.harness.run_ts_tests.make_ts_spec")
    @patch("commit0.harness.run_ts_tests.git.Repo")
    @patch(
        "commit0.harness.run_ts_tests.generate_patch_between_commits", return_value=""
    )
    def test_non_local_backend_raises(
        self, mock_patch, mock_repo_cls, mock_spec, mock_load, tmp_path
    ) -> None:
        from commit0.harness.run_ts_tests import main as run_main

        mock_load.return_value = self._make_dataset()
        mock_spec.return_value = MagicMock()
        mock_spec.return_value.eval_script = "#!/bin/bash\nnpx jest\n"

        mock_repo = MagicMock()
        mock_repo.commit.return_value.hexsha = "abc123"
        mock_repo.branches = ["commit0"]
        mock_repo_cls.return_value = mock_repo

        with patch(
            "commit0.harness.run_ts_tests.setup_logger", return_value=MagicMock()
        ):
            with patch("commit0.harness.run_ts_tests.close_logger"):
                with pytest.raises(ValueError, match="supports LOCAL"):
                    run_main(
                        dataset_name="test.json",
                        dataset_split="test",
                        base_dir="/repos",
                        repo_or_repo_dir="my-repo",
                        branch="commit0",
                        test_ids="",
                        backend="MODAL",
                        timeout=300,
                        num_cpus=1,
                        rebuild_image=False,
                        verbose=0,
                    )


# ---------------------------------------------------------------------------
# run_ts_tests._inject_test_ids
# ---------------------------------------------------------------------------


class TestInjectTestIds:
    def test_appends_to_forceExit_line(self) -> None:
        from commit0.harness.run_ts_tests import _inject_test_ids

        # The injector only rewrites the actual test-invocation line, which is
        # identified by the presence of a ``>`` output redirect (as produced by
        # the real eval-script template). Ids are appended at the end of that
        # line, after the redirect.
        script = (
            "#!/bin/bash\n"
            "npx jest --forceExit --json > test_output.txt 2>&1\n"
            "echo done\n"
        )
        result = _inject_test_ids(script, "test/foo.test.ts")
        assert "> test_output.txt 2>&1 test/foo.test.ts" in result
        assert "echo done" in result

    def test_appends_to_vitest_line(self) -> None:
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "npx vitest run --reporter=json > test_output.txt 2>&1\n"
        result = _inject_test_ids(script, "src/bar.test.ts")
        assert "> test_output.txt 2>&1 src/bar.test.ts" in result

    def test_no_test_ids(self) -> None:
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "npx jest --forceExit\n"
        result = _inject_test_ids(script, "")
        assert result == script

    def test_no_matching_line(self) -> None:
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "echo hello\nexit 0\n"
        result = _inject_test_ids(script, "test.ts")
        assert result == script

    def test_multiple_test_ids(self) -> None:
        from commit0.harness.run_ts_tests import _inject_test_ids

        script = "npx jest --forceExit > test_output.txt 2>&1\n"
        result = _inject_test_ids(script, "a.test.ts b.test.ts")
        assert "a.test.ts b.test.ts" in result


class TestRunEslintTimeout:
    def test_timeout_returns_exit_code_1(self) -> None:
        from commit0.harness.lint_ts import run_eslint

        with patch(f"{MODULE}._repo_has_eslint_config", return_value=True):
            with patch(
                f"{MODULE}.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="eslint", timeout=300),
            ):
                rc, output = run_eslint("/repo")

        assert rc == 1
        assert "timed out" in output
        assert "300" in output


class TestRunTscNoEmitTimeout:
    def test_timeout_returns_exit_code_1(self) -> None:
        from commit0.harness.lint_ts import run_tsc_noEmit

        with patch(
            f"{MODULE}.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tsc", timeout=300),
        ):
            rc, output = run_tsc_noEmit("/repo")

        assert rc == 1
        assert "timed out" in output
        assert "300" in output


class TestLintTsMainRepoDirFallback:
    def _make_dataset(self, repo_names: list[str]):
        return [{"repo": f"org/{name}"} for name in repo_names]

    def test_repo_dir_not_directory_joined_with_base(self) -> None:
        from commit0.harness.lint_ts import main

        dataset = self._make_dataset(["my-repo"])

        isdir_calls = []

        def _isdir_side_effect(path):
            isdir_calls.append(path)
            if path == "my-repo":
                return False
            if path == "/base/my-repo":
                return True
            return False

        with patch(f"{MODULE}.load_dataset_from_config", return_value=dataset):
            with patch(f"{MODULE}.os.path.isdir", side_effect=_isdir_side_effect):
                with patch(f"{MODULE}.run_eslint", return_value=(0, "")) as mock_eslint:
                    with patch(f"{MODULE}.run_tsc_noEmit", return_value=(0, "")):
                        with pytest.raises(SystemExit) as exc_info:
                            main("my-repo", "ds", "sp", "/base")

        assert exc_info.value.code == 0
        assert mock_eslint.call_args[0][0] == "/base/my-repo"
