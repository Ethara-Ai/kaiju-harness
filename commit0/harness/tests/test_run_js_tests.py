from __future__ import annotations

import shlex
from unittest.mock import MagicMock, patch

import pytest

import git.exc

from commit0.harness.constants_js import SUPPORTED_TEST_FRAMEWORKS
from commit0.harness.run_js_tests import (
    _FETCH_TIMEOUT_SECONDS,
    _FRAMEWORK_INJECTION_MARKERS,
    _inject_into_test_line,
    _inject_test_ids,
    _read_exit_code,
    _resolve_branch_commit,
    _strip_log_unsafe_chars,
    main as run_main,
)


_SAMPLE_SCRIPT = (
    "set -uo pipefail\n"
    "cd /testbed\n"
    "npm ci --no-audit --no-fund --ignore-scripts\n"
    "npx jest --json --outputFile=/tmp/test_results.json --reporters=default\n"
    "echo $? > /tmp/test_exit_code.txt\n"
)


class TestInjectTestIdsShortCircuit:
    def test_empty_string(self) -> None:
        assert _inject_test_ids(_SAMPLE_SCRIPT, "", "jest") == _SAMPLE_SCRIPT

    def test_whitespace_only(self) -> None:
        assert _inject_test_ids(_SAMPLE_SCRIPT, "   \t  ", "jest") == _SAMPLE_SCRIPT

    def test_only_strippable_chars(self) -> None:
        assert _inject_test_ids(_SAMPLE_SCRIPT, "\n\r\x00", "jest") == _SAMPLE_SCRIPT


class TestInjectTestIdsShellInjection:
    @pytest.mark.parametrize(
        "payload",
        [
            "$(rm -rf /)",
            "`rm -rf /`",
            ";rm -rf /",
            "&& evil",
            "|| evil",
            "|sh",
            "\x1bm[31mfake",
            "‮Reverse",
            "a\nb",
            "a\rb",
            "a\x00b",
            "'; rm -rf /; '",
            '"; rm -rf /; "',
            "\\; rm -rf /",
            "\\$IFS$evil",
            "${IFS}evil",
            "tests/foo.test.js ; rm -rf /",
            "tests/foo.test.js | nc evil 1234",
        ],
    )
    def test_payload_quoted_not_executed(self, payload: str) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, payload, "jest")
        sanitized = payload.replace("\n", " ").replace("\r", " ").replace("\x00", "")
        sanitized = _strip_log_unsafe_chars(sanitized)
        tokens = [t for t in sanitized.split() if t]
        if not tokens:
            assert result == _SAMPLE_SCRIPT
            return
        for tok in tokens:
            assert shlex.quote(tok) in result

    def test_dollar_paren_quoted_literal(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "$(rm -rf /)", "jest")
        injected_line = [
            ln for ln in result.split("\n") if "npx jest" in ln
        ][0]
        assert "'$(rm'" in injected_line
        assert "'/)'" in injected_line

    def test_backticks_quoted_literal(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "`evil`", "jest")
        injected_line = [
            ln for ln in result.split("\n") if "npx jest" in ln
        ][0]
        assert "'`evil`'" in injected_line

    def test_semicolon_does_not_split_command(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, ";rm -rf /", "jest")
        injected_line = [
            ln for ln in result.split("\n") if "npx jest" in ln
        ][0]
        assert "';rm'" in injected_line
        assert injected_line.count("npx jest") == 1

    def test_newline_collapses_to_space(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "a\nb", "jest")
        injected_line = [
            ln for ln in result.split("\n") if "npx jest" in ln
        ][0]
        assert " a b" in injected_line
        assert "\na" not in injected_line

    def test_null_byte_stripped(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "a\x00b", "jest")
        injected_line = [
            ln for ln in result.split("\n") if "npx jest" in ln
        ][0]
        assert "\x00" not in injected_line
        assert "ab" in injected_line


class TestResolveBranchCommit:
    def test_local_branch_short_circuits_no_fetch(self) -> None:
        repo = MagicMock()
        repo.branches = ["feature/x"]
        repo.commit.return_value.hexsha = "a" * 40
        repo.remotes = []
        repo.git = MagicMock()

        result = _resolve_branch_commit(repo, "feature/x")

        assert result == "a" * 40
        repo.git.fetch.assert_not_called()

    def test_remote_fetch_uses_depth_1_and_timeout(self) -> None:
        repo = MagicMock()
        repo.branches = []
        remote = MagicMock()
        remote.name = "origin"
        repo.remotes = [remote]
        repo.commit.return_value.hexsha = "b" * 40
        repo.git = MagicMock()

        result = _resolve_branch_commit(repo, "main")

        assert result == "b" * 40
        repo.git.fetch.assert_called_once_with(
            "origin",
            "main",
            depth=1,
            kill_after_timeout=_FETCH_TIMEOUT_SECONDS,
        )
        repo.commit.assert_called_with("FETCH_HEAD")

    def test_first_remote_failure_continues_to_next(self) -> None:
        repo = MagicMock()
        repo.branches = []
        bad = MagicMock()
        bad.name = "fork"
        good = MagicMock()
        good.name = "origin"
        repo.remotes = [bad, good]
        repo.commit.return_value.hexsha = "c" * 40

        repo.git = MagicMock()
        repo.git.fetch.side_effect = [
            git.exc.GitCommandError("git fetch fork main", 128),
            None,
        ]

        result = _resolve_branch_commit(repo, "main")

        assert result == "c" * 40
        assert repo.git.fetch.call_count == 2
        assert repo.git.fetch.call_args_list[0].args == ("fork", "main")
        assert repo.git.fetch.call_args_list[1].args == ("origin", "main")

    def test_all_remotes_fail_raises(self) -> None:
        repo = MagicMock()
        repo.branches = []
        bad = MagicMock()
        bad.name = "origin"
        repo.remotes = [bad]
        repo.git = MagicMock()
        repo.git.fetch.side_effect = git.exc.GitCommandError("fetch", 128)

        with pytest.raises(Exception, match="does not exist"):
            _resolve_branch_commit(repo, "nope")

    def test_does_not_iterate_remote_refs(self) -> None:
        repo = MagicMock()
        repo.branches = []
        remote = MagicMock()
        remote.name = "origin"
        remote.refs = MagicMock(side_effect=AssertionError("must not enumerate refs"))
        repo.remotes = [remote]
        repo.commit.return_value.hexsha = "d" * 40
        repo.git = MagicMock()

        result = _resolve_branch_commit(repo, "main")

        assert result == "d" * 40


class TestInjectIntoTestLine:
    def test_or_short_circuit_preserved(self) -> None:
        line = "npx jest --json || echo fail"
        result = _inject_into_test_line(line, "'foo'")
        assert "'foo' ||" in result

    def test_appends_when_no_or(self) -> None:
        line = "npx jest --json"
        result = _inject_into_test_line(line, "'foo'")
        assert result.endswith(" 'foo'")


class TestInjectTestIdsLineContinuation:
    def test_backslash_continuation_injects_into_last_physical_line(self) -> None:
        script = (
            "set -uo pipefail\n"
            "npx jest \\\n"
            "  --json \\\n"
            "  --outputFile=/tmp/test_results.json\n"
            "echo done\n"
        )
        result = _inject_test_ids(script, "tests/foo.test.js", "jest")
        out_lines = result.split("\n")
        assert "tests/foo.test.js" in out_lines[3]
        assert "tests/foo.test.js" not in out_lines[1]
        assert "tests/foo.test.js" not in out_lines[2]
        assert out_lines[1].rstrip().endswith("\\")
        assert out_lines[2].rstrip().endswith("\\")

    def test_continuation_then_or_combines_correctly(self) -> None:
        script = (
            "set -uo pipefail\n"
            "npx jest \\\n"
            "  --json || echo fail\n"
        )
        result = _inject_test_ids(script, "tests/foo.test.js", "jest")
        out_lines = result.split("\n")
        assert "tests/foo.test.js" in out_lines[2]
        assert "||" in out_lines[2]
        assert "tests/foo.test.js ||" in out_lines[2]


class TestInjectTestIdsMultiOccurrence:
    def test_every_marker_line_gets_injection(self) -> None:
        script = (
            "set -uo pipefail\n"
            "npx jest tests/a\n"
            "echo between\n"
            "npx jest tests/b\n"
        )
        result = _inject_test_ids(script, "tests/foo.test.js", "jest")
        lines = result.split("\n")
        jest_lines = [ln for ln in lines if " jest " in ln or ln.endswith(" jest")
                      or "npx jest" in ln]
        assert len(jest_lines) == 2
        for ln in jest_lines:
            assert "tests/foo.test.js" in ln

    def test_multi_occurrence_count_matches_input(self) -> None:
        script = (
            "set -uo pipefail\n"
            "npx jest tests/a\n"
            "npx jest tests/b\n"
            "npx jest tests/c\n"
        )
        result = _inject_test_ids(script, "myid", "jest")
        assert result.count("myid") == 3


class TestInjectTestIdsControlChars:
    def test_ansi_escape_stripped(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "\x1b[31mfoo", "jest")
        assert "\x1b" not in result
        injected_line = [ln for ln in result.split("\n") if "npx jest" in ln][0]
        assert "[31mfoo" in injected_line or "'[31mfoo'" in injected_line

    def test_rtl_override_stripped(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "\u202eevil", "jest")
        assert "\u202e" not in result
        injected_line = [ln for ln in result.split("\n") if "npx jest" in ln][0]
        assert "evil" in injected_line

    def test_ltr_override_stripped(self) -> None:
        result = _inject_test_ids(_SAMPLE_SCRIPT, "a\u202dbad", "jest")
        assert "\u202d" not in result
        assert "abad" in result


class TestExitCodeContract:
    @pytest.fixture
    def mock_runtime(self, tmp_path, monkeypatch):
        log_root = tmp_path / "logs"
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.RUN_JS_TEST_LOG_DIR", log_root
        )

        def _fake_loader(*_args, **_kwargs):
            return iter(
                [
                    {
                        "instance_id": "commit-0/foo",
                        "repo": "owner/foo",
                        "base_commit": "a" * 40,
                        "reference_commit": "b" * 40,
                        "test": {"test_dir": "tests", "test_cmd": "npx jest"},
                        "setup": {
                            "node_version": 20,
                            "install": "npm ci",
                            "packages": [],
                            "pre_install": [],
                            "specification": "",
                        },
                        "src_dir": "src",
                        "language": "javascript",
                    }
                ]
            )

        monkeypatch.setattr(
            "commit0.harness.run_js_tests.load_dataset_from_config", _fake_loader
        )

        fake_repo = MagicMock()
        fake_repo.branches = []
        fake_repo.remotes = []
        fake_repo.close = MagicMock()
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.git.Repo",
            MagicMock(return_value=fake_repo),
        )
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.generate_patch_between_commits",
            MagicMock(return_value=""),
        )

        docker_cm = MagicMock()
        docker_cm.__enter__ = MagicMock(return_value=docker_cm)
        docker_cm.__exit__ = MagicMock(return_value=False)
        docker_cm.exec_run_with_timeout = MagicMock(return_value=("", False, 0.0))
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.Docker",
            MagicMock(return_value=docker_cm),
        )
        return log_root

    def _make_log_dir(self, log_root, install_rc, syntax_rc, test_rc):
        log_dir = log_root / "foo" / "reference"
        log_dir.mkdir(parents=True, exist_ok=True)
        from commit0.harness.utils import get_hash_string

        hashed = get_hash_string("")
        log_dir = log_dir / hashed
        log_dir.mkdir(parents=True, exist_ok=True)
        if install_rc is not None:
            (log_dir / "install_exit_code.txt").write_text(str(install_rc))
        if syntax_rc is not None:
            (log_dir / "syntax_exit_code.txt").write_text(str(syntax_rc))
        if test_rc is not None:
            (log_dir / "test_exit_code.txt").write_text(str(test_rc))
        return log_dir

    def test_install_failure_exits_2(self, mock_runtime):
        log_root = mock_runtime
        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": 0,
                "install_exit_code.txt": 1,
                "syntax_exit_code.txt": 0,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(log_root),
                    repo_or_repo_dir="foo",
                    branch="reference",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
            assert excinfo.value.code == 2

    def test_symlinked_testbed_path_falls_back_to_single_entry(self, mock_runtime):
        # local_inplace resolves the repo dir through a symlink to /testbed, whose
        # basename ('testbed') doesn't match the dataset repo ('foo'). Stage 3's
        # test_cmd must STILL resolve (single-entry fallback) instead of raising
        # "No matching JS spec" and silently zero-working stage 3.
        log_root = mock_runtime
        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": 0,
                "install_exit_code.txt": 0,
                "syntax_exit_code.txt": 0,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(log_root),
                    repo_or_repo_dir="/testbed",  # symlink-resolved, != 'foo'
                    branch="reference",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
            # Reached the exit-code contract (rc=0) rather than raising ValueError.
            assert excinfo.value.code == 0

    def test_syntax_failure_exits_2(self, mock_runtime):
        log_root = mock_runtime
        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": 0,
                "install_exit_code.txt": 0,
                "syntax_exit_code.txt": 1,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(log_root),
                    repo_or_repo_dir="foo",
                    branch="reference",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
            assert excinfo.value.code == 2

    def test_test_failure_exits_1(self, mock_runtime):
        log_root = mock_runtime
        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": 1,
                "install_exit_code.txt": 0,
                "syntax_exit_code.txt": 0,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(log_root),
                    repo_or_repo_dir="foo",
                    branch="reference",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
            assert excinfo.value.code == 1

    def test_test_success_exits_0(self, mock_runtime):
        log_root = mock_runtime
        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": 0,
                "install_exit_code.txt": 0,
                "syntax_exit_code.txt": 0,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(log_root),
                    repo_or_repo_dir="foo",
                    branch="reference",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
            assert excinfo.value.code == 0


class TestFrameworkInjectionMarkersCoverage:
    def test_every_injection_framework_has_non_empty_markers(self) -> None:
        for framework in ("jest", "vitest", "mocha"):
            markers = _FRAMEWORK_INJECTION_MARKERS.get(framework, ())
            assert markers, (
                f"framework {framework!r} has no marker in "
                f"_FRAMEWORK_INJECTION_MARKERS; test-id injection will silently "
                f"drop for this framework"
            )

    # node:test and ava select tests by pattern, not positional args (positionals
    # are file paths/globs), so per-test-ID injection is unsupported. Rather than
    # RAISE (which crashed the per-file agent-feedback subprocess for these repos),
    # _inject_test_ids now runs the WHOLE suite and logs a warning — correct (the
    # canonical inventory supplies the denominator) and not silent.
    @pytest.mark.parametrize("framework", ["node_test", "ava"])
    def test_pattern_frameworks_run_whole_suite_not_silent(
        self, framework: str, caplog
    ) -> None:
        import logging

        script = "set -uo pipefail\nnode --test\n"
        with caplog.at_level(logging.WARNING):
            out = _inject_test_ids(script, "tests/foo.test.js", framework)
        # whole suite (script unchanged) + a warning => not a silent drop
        assert out == script
        assert any(
            "full suite" in rec.message.lower() or "unsupported" in rec.message.lower()
            for rec in caplog.records
        ), f"expected a warning for {framework}; got {[r.message for r in caplog.records]}"

    def test_supported_frameworks_either_have_marker_or_handled(self) -> None:
        # Pattern-based frameworks (node:test, ava) intentionally have no positional
        # injection marker — they run the whole suite (tested above). Every OTHER
        # supported framework must have a marker so injection never silently drops.
        for framework in SUPPORTED_TEST_FRAMEWORKS:
            if framework in ("node_test", "ava"):
                continue
            assert framework in _FRAMEWORK_INJECTION_MARKERS, (
                f"framework {framework!r} is in SUPPORTED_TEST_FRAMEWORKS but "
                f"absent from _FRAMEWORK_INJECTION_MARKERS — injection would "
                f"silently drop"
            )

    @pytest.mark.parametrize(
        "framework,marker",
        [
            (fw, m)
            for fw, markers in _FRAMEWORK_INJECTION_MARKERS.items()
            for m in markers
        ],
    )
    def test_marker_triggers_injection(self, framework: str, marker: str) -> None:
        script = f"set -uo pipefail\n{marker} --json\n"
        result = _inject_test_ids(script, "tests/foo.test.js", framework)
        assert "tests/foo.test.js" in result

    def test_unmatched_script_silently_drops_test_ids(self) -> None:
        script = "set -uo pipefail\nbun test --reporter=json\n"
        result = _inject_test_ids(script, "tests/foo.test.js", "jest")
        assert "tests/foo.test.js" not in result
        assert result.strip().endswith("bun test --reporter=json")


class TestReadExitCodePartialWrite:
    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert _read_exit_code(tmp_path / "absent.txt") is None

    def test_empty_file_returns_none(self, tmp_path) -> None:
        p = tmp_path / "empty.txt"
        p.write_text("")
        assert _read_exit_code(p) is None

    def test_garbage_returns_none(self, tmp_path) -> None:
        p = tmp_path / "garbage.txt"
        p.write_text("not a number")
        assert _read_exit_code(p) is None

    def test_whitespace_only_returns_none(self, tmp_path) -> None:
        p = tmp_path / "ws.txt"
        p.write_text("   \n\t   ")
        assert _read_exit_code(p) is None

    def test_partial_binary_returns_none(self, tmp_path) -> None:
        p = tmp_path / "bin.txt"
        p.write_bytes(b"\x00\x01\x02")
        assert _read_exit_code(p) is None

    def test_valid_zero_returns_int(self, tmp_path) -> None:
        p = tmp_path / "ok.txt"
        p.write_text("0\n")
        assert _read_exit_code(p) == 0

    def test_valid_nonzero_returns_int(self, tmp_path) -> None:
        p = tmp_path / "fail.txt"
        p.write_text("137\n")
        assert _read_exit_code(p) == 137

    def test_trailing_whitespace_tolerated(self, tmp_path) -> None:
        p = tmp_path / "ws.txt"
        p.write_text("  42  \n\n")
        assert _read_exit_code(p) == 42


class TestF010MissingExitCodeIsInfraFailure:
    def test_no_test_exit_code_file_exits_3_not_1(
        self, tmp_path, monkeypatch
    ) -> None:
        log_root = tmp_path / "logs"
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.RUN_JS_TEST_LOG_DIR", log_root
        )

        def _fake_loader(*_args, **_kwargs):
            return iter(
                [
                    {
                        "instance_id": "commit-0/foo",
                        "repo": "owner/foo",
                        "base_commit": "a" * 40,
                        "reference_commit": "b" * 40,
                        "test": {"test_dir": "tests", "test_cmd": "npx jest"},
                        "setup": {
                            "node_version": 20,
                            "install": "npm ci",
                            "packages": [],
                            "pre_install": [],
                            "specification": "",
                        },
                        "src_dir": "src",
                        "language": "javascript",
                    }
                ]
            )

        monkeypatch.setattr(
            "commit0.harness.run_js_tests.load_dataset_from_config", _fake_loader
        )
        fake_repo = MagicMock()
        fake_repo.branches = []
        fake_repo.remotes = []
        fake_repo.close = MagicMock()
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.git.Repo",
            MagicMock(return_value=fake_repo),
        )
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.generate_patch_between_commits",
            MagicMock(return_value=""),
        )
        docker_cm = MagicMock()
        docker_cm.__enter__ = MagicMock(return_value=docker_cm)
        docker_cm.__exit__ = MagicMock(return_value=False)
        docker_cm.exec_run_with_timeout = MagicMock(return_value=("", False, 0.0))
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.Docker",
            MagicMock(return_value=docker_cm),
        )

        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": None,
                "install_exit_code.txt": 0,
                "syntax_exit_code.txt": 0,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(log_root),
                    repo_or_repo_dir="foo",
                    branch="reference",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
        assert excinfo.value.code == 3, (
            "F-010: missing test_exit_code.txt must exit 3 (infra) so "
            "evaluate_js does not conflate it with a real test failure (exit 1)"
        )


class TestF005ExactRepoNameMatch:
    def _fake_dataset(self, repos: list[str]):
        def _loader(*_a, **_kw):
            return iter(
                [
                    {
                        "instance_id": f"commit-0/{name.split('/')[-1]}",
                        "repo": name,
                        "base_commit": "a" * 40,
                        "reference_commit": "b" * 40,
                        "test": {"test_dir": "tests", "test_cmd": "npx jest"},
                        "setup": {
                            "node_version": 20,
                            "install": "npm ci",
                            "packages": [],
                            "pre_install": [],
                            "specification": "",
                        },
                        "src_dir": "src",
                        "language": "javascript",
                    }
                    for name in repos
                ]
            )

        return _loader

    def test_substring_prefix_no_longer_collides(
        self, tmp_path, monkeypatch
    ) -> None:
        log_root = tmp_path / "logs"
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.RUN_JS_TEST_LOG_DIR", log_root
        )
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.load_dataset_from_config",
            self._fake_dataset(["facebook/react", "remix-run/react-router"]),
        )
        with pytest.raises(ValueError, match="No matching JS spec"):
            run_main(
                dataset_name="x",
                dataset_split="test",
                base_dir=str(tmp_path),
                repo_or_repo_dir="/tmp/repos_js/react-router-dom",
                branch="main",
                test_ids="",
                backend="local",
                timeout=60,
                num_cpus=1,
                rebuild_image=False,
                verbose=0,
            )

    def test_exact_name_match_succeeds(
        self, tmp_path, monkeypatch
    ) -> None:
        log_root = tmp_path / "logs"
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.RUN_JS_TEST_LOG_DIR", log_root
        )
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.load_dataset_from_config",
            self._fake_dataset(["facebook/react"]),
        )
        fake_repo = MagicMock()
        fake_repo.branches = []
        fake_repo.close = MagicMock()
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.git.Repo",
            MagicMock(return_value=fake_repo),
        )
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.generate_patch_between_commits",
            MagicMock(return_value=""),
        )
        monkeypatch.setattr(
            "commit0.harness.run_js_tests._resolve_branch_commit",
            MagicMock(return_value="c" * 40),
        )
        docker_cm = MagicMock()
        docker_cm.__enter__ = MagicMock(return_value=docker_cm)
        docker_cm.__exit__ = MagicMock(return_value=False)
        docker_cm.exec_run_with_timeout = MagicMock(return_value=("", False, 0.0))
        monkeypatch.setattr(
            "commit0.harness.run_js_tests.Docker",
            MagicMock(return_value=docker_cm),
        )
        with patch(
            "commit0.harness.run_js_tests._read_exit_code",
            side_effect=lambda p: {
                "test_exit_code.txt": 0,
                "install_exit_code.txt": 0,
                "syntax_exit_code.txt": 0,
            }.get(p.name),
        ):
            with pytest.raises(SystemExit) as excinfo:
                run_main(
                    dataset_name="x",
                    dataset_split="test",
                    base_dir=str(tmp_path),
                    repo_or_repo_dir="/tmp/repos_js/react",
                    branch="main",
                    test_ids="",
                    backend="local",
                    timeout=60,
                    num_cpus=1,
                    rebuild_image=False,
                    verbose=0,
                )
        assert excinfo.value.code == 0
