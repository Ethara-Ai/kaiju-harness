from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness import lint_js
from commit0.harness.lint_js import (
    ESLINT_TIMEOUT_EXIT_CODE,
    ESLINT_TIMEOUT_MARKER,
    LINT_NO_CONFIG_MARKER,
    JsLintResult,
    _detect_exec_prefix,
    _enumerate_source_files,
    _has_eslint_config,
    run_eslint,
    run_node_check,
)


class TestDetectExecPrefix:
    @pytest.mark.parametrize(
        ("lockfile", "expected"),
        [
            ("pnpm-lock.yaml", ["pnpm", "exec"]),
            ("yarn.lock", ["yarn"]),
            ("bun.lockb", ["bunx"]),
            ("package-lock.json", ["npx"]),
        ],
    )
    def test_per_lockfile(
        self, tmp_path: Path, lockfile: str, expected: list[str]
    ) -> None:
        (tmp_path / lockfile).write_text("", encoding="utf-8")
        assert _detect_exec_prefix(str(tmp_path)) == expected

    def test_no_lockfile_defaults_to_npx(self, tmp_path: Path) -> None:
        assert _detect_exec_prefix(str(tmp_path)) == ["npx"]


class TestYarnEslintInvocation:
    def test_yarn_prefix_produces_yarn_eslint_command(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
        (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
        captured: dict[str, list[str]] = {}

        def _fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("commit0.harness.lint_js.subprocess.run", _fake_run):
            run_eslint(str(tmp_path))
        assert captured["cmd"][:2] == ["yarn", "eslint"]
        assert "--no-error-on-unmatched-pattern" in captured["cmd"]

    @pytest.mark.parametrize(
        ("lockfile", "expected_head"),
        [
            ("pnpm-lock.yaml", ["pnpm", "exec", "eslint"]),
            ("yarn.lock", ["yarn", "eslint"]),
            ("bun.lockb", ["bunx", "eslint"]),
            ("package-lock.json", ["npx", "eslint"]),
        ],
    )
    def test_each_package_manager_produces_correct_head(
        self,
        tmp_path: Path,
        lockfile: str,
        expected_head: list[str],
    ) -> None:
        (tmp_path / lockfile).write_text("", encoding="utf-8")
        (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
        captured: dict[str, list[str]] = {}

        def _fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("commit0.harness.lint_js.subprocess.run", _fake_run):
            run_eslint(str(tmp_path))
        assert captured["cmd"][: len(expected_head)] == expected_head


class TestRunEslintNoConfig:
    def test_no_config_emits_marker_and_skips(self, tmp_path: Path) -> None:
        rc, output, skipped = run_eslint(str(tmp_path))
        assert rc == 0
        assert skipped is True
        assert output == LINT_NO_CONFIG_MARKER

    def test_eslintconfig_key_in_package_json_detected(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "package.json").write_text(
            '{"name":"x","eslintConfig":{"extends":["eslint:recommended"]}}',
            encoding="utf-8",
        )
        assert _has_eslint_config(str(tmp_path)) is True

    def test_corrupted_package_json_treated_as_no_config(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "package.json").write_text(
            "{not json", encoding="utf-8"
        )
        assert _has_eslint_config(str(tmp_path)) is False


class TestRunEslintTimeout:
    def test_timeout_returns_distinct_exit_code_with_marker(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
        import subprocess

        def _fake_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd=["eslint"], timeout=300)

        with patch("commit0.harness.lint_js.subprocess.run", _fake_run):
            rc, output, skipped = run_eslint(str(tmp_path))
        assert rc == ESLINT_TIMEOUT_EXIT_CODE
        assert ESLINT_TIMEOUT_MARKER in output
        assert "timed out" in output
        assert skipped is False

    def test_timeout_exit_code_distinct_from_lint_failure(self) -> None:
        assert ESLINT_TIMEOUT_EXIT_CODE != 1
        assert ESLINT_TIMEOUT_EXIT_CODE != 0


class TestF025StatusJsonMarker:
    def test_status_json_includes_timeout_marker_on_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        logs_dir = tmp_path / "logs"

        monkeypatch.setattr(lint_js, "RUN_JS_TEST_LOG_DIR", logs_dir)
        monkeypatch.setattr(
            lint_js, "_locate_repo_dir", lambda *_a, **_kw: str(repo_dir)
        )
        monkeypatch.setattr(
            lint_js, "_enumerate_source_files", lambda *_a, **_kw: []
        )
        monkeypatch.setattr(
            lint_js,
            "run_eslint",
            MagicMock(
                return_value=(
                    ESLINT_TIMEOUT_EXIT_CODE,
                    f"{ESLINT_TIMEOUT_MARKER} ESLint timed out after 300 seconds",
                    False,
                )
            ),
        )
        monkeypatch.setattr(
            lint_js, "run_node_check", MagicMock(return_value=(0, ""))
        )
        with pytest.raises(SystemExit):
            lint_js.main(
                repo_or_repo_dir=str(repo_dir),
                dataset_name="x",
                dataset_split="test",
                base_dir=str(tmp_path),
                files=None,
                verbose=0,
            )
        import json as _json

        status_path = logs_dir / "lint" / f"{repo_dir.name}.json"
        data = _json.loads(status_path.read_text())
        assert data["eslint_status_marker"] == ESLINT_TIMEOUT_MARKER
        assert data["eslint_exit_code"] == ESLINT_TIMEOUT_EXIT_CODE

    def test_status_json_marker_none_on_normal_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        logs_dir = tmp_path / "logs"

        monkeypatch.setattr(lint_js, "RUN_JS_TEST_LOG_DIR", logs_dir)
        monkeypatch.setattr(
            lint_js, "_locate_repo_dir", lambda *_a, **_kw: str(repo_dir)
        )
        monkeypatch.setattr(
            lint_js, "_enumerate_source_files", lambda *_a, **_kw: []
        )
        monkeypatch.setattr(
            lint_js,
            "run_eslint",
            MagicMock(return_value=(1, "5 errors found", False)),
        )
        monkeypatch.setattr(
            lint_js, "run_node_check", MagicMock(return_value=(0, ""))
        )
        with pytest.raises(SystemExit):
            lint_js.main(
                repo_or_repo_dir=str(repo_dir),
                dataset_name="x",
                dataset_split="test",
                base_dir=str(tmp_path),
                files=None,
                verbose=0,
            )
        import json as _json

        status_path = logs_dir / "lint" / f"{repo_dir.name}.json"
        data = _json.loads(status_path.read_text())
        assert data["eslint_status_marker"] is None


class TestEnumerateSourceFilesPathTraversal:
    def test_user_supplied_files_traversal_rejected(
        self, tmp_path: Path
    ) -> None:
        traversal_files = [
            "../etc/passwd",
            "../../../../proc/self/environ",
            "/etc/shadow",
            "src/../../outside.js",
        ]
        result = _enumerate_source_files(str(tmp_path), traversal_files)
        assert result == []

    def test_empty_strings_filtered(self, tmp_path: Path) -> None:
        result = _enumerate_source_files(str(tmp_path), ["", "a.js", ""])
        assert result == ["a.js"]

    def test_no_files_walks_directory_excluding_heavy_dirs(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "evil.js").write_text("", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "good.js").write_text("", encoding="utf-8")
        out = _enumerate_source_files(str(tmp_path), None)
        assert "src/good.js" in out
        assert not any("node_modules" in p for p in out)


class TestRunNodeCheckSkipsNonexistentFiles:
    def test_nonexistent_file_skipped(self, tmp_path: Path) -> None:
        worst, output = run_node_check(str(tmp_path), ["nonexistent.js"])
        assert worst == 0
        assert output == ""

    def test_empty_files_returns_zero(self, tmp_path: Path) -> None:
        worst, output = run_node_check(str(tmp_path), [])
        assert worst == 0
        assert output == ""


class TestJsLintResultExitCode:
    def test_max_of_eslint_and_node(self) -> None:
        r = JsLintResult(
            repo_dir="/x",
            eslint_exit_code=1,
            node_check_exit_code=0,
        )
        assert r.final_exit_code == 1

        r = JsLintResult(
            repo_dir="/x",
            eslint_exit_code=0,
            node_check_exit_code=2,
        )
        assert r.final_exit_code == 2

        r = JsLintResult(
            repo_dir="/x",
            eslint_exit_code=0,
            node_check_exit_code=0,
        )
        assert r.final_exit_code == 0


class TestCliFlagPathTraversal:
    def test_main_passes_user_files_directly_to_enumerate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        traversal = ["../../etc/passwd", "/etc/shadow"]
        captured: dict[str, list[str] | None] = {}

        def _capture(repo_dir, files):
            captured["files"] = files
            return list(files or [])

        monkeypatch.setattr(lint_js, "_enumerate_source_files", _capture)
        monkeypatch.setattr(
            lint_js,
            "run_eslint",
            MagicMock(return_value=(0, "", True)),
        )
        monkeypatch.setattr(
            lint_js,
            "run_node_check",
            MagicMock(return_value=(0, "")),
        )
        monkeypatch.setattr(
            lint_js,
            "_locate_repo_dir",
            lambda *_a, **_kw: str(tmp_path),
        )
        monkeypatch.setattr(
            lint_js,
            "RUN_JS_TEST_LOG_DIR",
            tmp_path / "logs",
        )
        with pytest.raises(SystemExit):
            lint_js.main(
                repo_or_repo_dir=str(tmp_path),
                dataset_name="x",
                dataset_split="test",
                base_dir=str(tmp_path),
                files=traversal,
                verbose=0,
            )
        assert captured["files"] == traversal
