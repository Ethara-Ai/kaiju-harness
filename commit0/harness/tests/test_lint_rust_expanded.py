from __future__ import annotations

import json
import logging
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.lint_rust import (
    _collect_rs_files,
    _find_cargo_toml,
    _run_cargo_clippy,
    _run_cargo_fmt,
    main,
)

MODULE = "commit0.harness.lint_rust"


def _clippy_json_line(level, message, spans=None, reason='compiler-message'):
    msg = {"level": level, "message": message, "spans": spans or []}
    return json.dumps({"reason": reason, "message": msg})


def _make_subprocess_result(stdout='', stderr='', returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


# ---------------------------------------------------------------------------
# TestFindCargoToml
# ---------------------------------------------------------------------------


class TestFindCargoToml:

    def test_found_in_current_dir(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        assert _find_cargo_toml(str(tmp_path)) == str(tmp_path.resolve())

    def test_found_in_parent(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        child = tmp_path / "src"
        child.mkdir()
        assert _find_cargo_toml(str(child)) == str(tmp_path.resolve())

    def test_found_in_grandparent(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        assert _find_cargo_toml(str(deep)) == str(tmp_path.resolve())

    def test_not_found_returns_none(self, tmp_path):
        child = tmp_path / "empty_project"
        child.mkdir()
        result = _find_cargo_toml(str(child))
        assert result is None or result == str(child.resolve())

    def test_returns_string(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        result = _find_cargo_toml(str(tmp_path))
        assert isinstance(result, str)

    def test_resolves_path(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        child = tmp_path / "src"
        child.mkdir()
        result = _find_cargo_toml(str(child))
        assert result is not None
        assert ".." not in result

    def test_symlink_to_dir_with_cargo(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "Cargo.toml").write_text("[package]")
        link = tmp_path / "link"
        link.symlink_to(real_dir)
        result = _find_cargo_toml(str(link))
        assert result is not None

    def test_cargo_toml_is_directory_not_file(self, tmp_path):
        (tmp_path / "Cargo.toml").mkdir()
        result = _find_cargo_toml(str(tmp_path))
        assert result is None or result != str(tmp_path.resolve())

    def test_nested_cargo_toml_returns_nearest(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[workspace]")
        sub = tmp_path / "crate_a"
        sub.mkdir()
        (sub / "Cargo.toml").write_text("[package]")
        result = _find_cargo_toml(str(sub))
        assert result == str(sub.resolve())

    def test_deeply_nested_no_cargo(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        result = _find_cargo_toml(str(deep))
        assert result is None or isinstance(result, str)

# ---------------------------------------------------------------------------
# TestRunCargoClippy
# ---------------------------------------------------------------------------


class TestRunCargoClippy:

    # B5 hardening (post-hoc test fix): _run_cargo_clippy now calls
    # _bust_clippy_cache first, which itself invokes subprocess.run for `cargo
    # metadata`. These tests were written before that hardening and mock
    # subprocess.run with a single return value that (a) can't parse as JSON,
    # (b) makes the cache-bust fail, (c) returns returncode=-2 from clippy.
    # Bypass the cache-bust so the tests can focus on _run_cargo_clippy's
    # actual behavior. _bust_clippy_cache has its own dedicated tests.
    @pytest.fixture(autouse=True)
    def _bypass_cache_bust(self):
        with patch(f"{MODULE}._bust_clippy_cache", return_value=True):
            yield

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_success_zero_issues(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 0
        assert result["errors"] == 0
        assert result["messages"] == []
        assert result["returncode"] == 0

    @patch(f"{MODULE}.shutil.which", return_value=None)
    def test_cargo_not_found(self, mock_which):
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 0
        assert result["errors"] == 0
        assert result["messages"] == []
        assert result["raw_stderr"] == "cargo not found"

    @patch(f"{MODULE}.subprocess.run", side_effect=subprocess.TimeoutExpired("cargo", 300))
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_timeout(self, mock_which, mock_run):
        result = _run_cargo_clippy("/project")
        assert result["raw_stderr"] == "timeout"
        assert result["warnings"] == 0
        assert result["errors"] == 0

    @patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError("no cargo"))
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_file_not_found(self, mock_which, mock_run):
        result = _run_cargo_clippy("/project")
        assert "no cargo" in result["raw_stderr"]
        assert result["warnings"] == 0

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_single_warning(self, mock_which, mock_run):
        line = _clippy_json_line("warning", "unused variable")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 1
        assert result["errors"] == 0
        assert len(result["messages"]) == 1
        assert result["messages"][0]["level"] == "warning"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_single_error(self, mock_which, mock_run):
        line = _clippy_json_line("error", "type mismatch")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 0
        assert result["errors"] == 1
        assert result["messages"][0]["level"] == "error"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_mixed_warnings_and_errors(self, mock_which, mock_run):
        lines = "\n".join([
            _clippy_json_line("warning", "w1"),
            _clippy_json_line("error", "e1"),
            _clippy_json_line("warning", "w2"),
        ])
        mock_run.return_value = _make_subprocess_result(stdout=lines)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 2
        assert result["errors"] == 1
        assert len(result["messages"]) == 3

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_note_level_skipped(self, mock_which, mock_run):
        line = _clippy_json_line("note", "some note")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 0
        assert result["errors"] == 0
        assert result["messages"] == []

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_help_level_skipped(self, mock_which, mock_run):
        line = _clippy_json_line("help", "try this")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 0
        assert result["errors"] == 0
        assert result["messages"] == []

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_non_compiler_message_skipped(self, mock_which, mock_run):
        obj = json.dumps({"reason": "compiler-artifact", "target": {}})
        mock_run.return_value = _make_subprocess_result(stdout=obj)
        result = _run_cargo_clippy("/project")
        assert result["messages"] == []

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_malformed_json_skipped(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(stdout="not json at all")
        result = _run_cargo_clippy("/project")
        assert result["messages"] == []
        assert result["warnings"] == 0

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_empty_stdout(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(stdout="")
        result = _run_cargo_clippy("/project")
        assert result["messages"] == []

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_blank_lines_skipped(self, mock_which, mock_run):
        lines = "\n\n  \n"
        mock_run.return_value = _make_subprocess_result(stdout=lines)
        result = _run_cargo_clippy("/project")
        assert result["messages"] == []
    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_span_extraction_single(self, mock_which, mock_run):
        span = {
            "file_name": "src/main.rs",
            "line_start": 10,
            "line_end": 10,
            "column_start": 5,
            "column_end": 15,
            "label": "unused",
        }
        line = _clippy_json_line("warning", "unused var", spans=[span])
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        s = result["messages"][0]["spans"][0]
        assert s["file"] == "src/main.rs"
        assert s["line_start"] == 10
        assert s["line_end"] == 10
        assert s["col_start"] == 5
        assert s["col_end"] == 15
        assert s["label"] == "unused"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_span_extraction_multiple(self, mock_which, mock_run):
        spans = [
            {"file_name": "a.rs", "line_start": 1, "line_end": 2, "column_start": 1, "column_end": 5, "label": "here"},
            {"file_name": "b.rs", "line_start": 3, "line_end": 4, "column_start": 2, "column_end": 6, "label": "there"},
        ]
        line = _clippy_json_line("error", "multi span", spans=spans)
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert len(result["messages"][0]["spans"]) == 2
        assert result["messages"][0]["spans"][1]["file"] == "b.rs"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_span_missing_fields_defaults(self, mock_which, mock_run):
        span = {}
        line = _clippy_json_line("warning", "no fields", spans=[span])
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        s = result["messages"][0]["spans"][0]
        assert s["file"] == ""
        assert s["line_start"] == 0
        assert s["line_end"] == 0
        assert s["col_start"] == 0
        assert s["col_end"] == 0
        assert s["label"] == ""

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_warning_no_spans(self, mock_which, mock_run):
        line = _clippy_json_line("warning", "no spans")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["messages"][0]["spans"] == []

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_returncode_propagated(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(returncode=101)
        result = _run_cargo_clippy("/project")
        assert result["returncode"] == 101

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_raw_stderr_propagated(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(stderr="some error output")
        result = _run_cargo_clippy("/project")
        assert result["raw_stderr"] == "some error output"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_message_text_extracted(self, mock_which, mock_run):
        line = _clippy_json_line("warning", "use of deprecated function")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["messages"][0]["message"] == "use of deprecated function"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_mixed_json_and_malformed(self, mock_which, mock_run):
        lines = "not json\n" + _clippy_json_line("warning", "w1") + "\nalso bad"
        mock_run.return_value = _make_subprocess_result(stdout=lines)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 1
        assert len(result["messages"]) == 1

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_command_args(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_clippy("/project")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/cargo"
        assert "clippy" in cmd
        assert "--all-targets" in cmd
        assert "--message-format=json" in cmd

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_cwd_set(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_clippy("/my/project")
        assert mock_run.call_args[1]["cwd"] == "/my/project"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_timeout_is_300(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_clippy("/project")
        assert mock_run.call_args[1]["timeout"] == 300

    @pytest.mark.parametrize(
        "level,expected_warnings,expected_errors",
        [
            ("warning", 1, 0),
            ("error", 0, 1),
            ("note", 0, 0),
            ("help", 0, 0),
            ("ice", 0, 0),
            ("", 0, 0),
        ],
    )
    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_level_counting_parametrized(self, mock_which, mock_run, level, expected_warnings, expected_errors):
        line = _clippy_json_line(level, "msg")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == expected_warnings
        assert result["errors"] == expected_errors

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_cargo_not_found_logs_error(self, mock_which, mock_run, caplog):
        mock_which.return_value = None
        with caplog.at_level(logging.ERROR):
            _run_cargo_clippy("/project")
        assert "cargo not found" in caplog.text

    @patch(f"{MODULE}.subprocess.run", side_effect=subprocess.TimeoutExpired("cargo", 300))
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_timeout_logs_error(self, mock_which, mock_run, caplog):
        with caplog.at_level(logging.ERROR):
            _run_cargo_clippy("/project")
        assert "timed out" in caplog.text

# ---------------------------------------------------------------------------
# TestRunCargoFmt
# ---------------------------------------------------------------------------


class TestRunCargoFmt:

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_formatted_ok(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(returncode=0)
        result = _run_cargo_fmt("/project")
        assert result["formatted"] is True
        assert result["returncode"] == 0

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_not_formatted(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(returncode=1, stdout="diff output")
        result = _run_cargo_fmt("/project")
        assert result["formatted"] is False
        assert result["diff"] == "diff output"
        assert result["returncode"] == 1

    @patch(f"{MODULE}.shutil.which", return_value=None)
    def test_cargo_not_found(self, mock_which):
        result = _run_cargo_fmt("/project")
        assert result["formatted"] is False
        assert result["returncode"] == -1
        assert result["diff"] == ""

    @patch(f"{MODULE}.subprocess.run", side_effect=subprocess.TimeoutExpired("cargo", 120))
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_timeout(self, mock_which, mock_run):
        result = _run_cargo_fmt("/project")
        assert result["formatted"] is False
        assert result["returncode"] == -1

    @patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError("no binary"))
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_file_not_found(self, mock_which, mock_run):
        result = _run_cargo_fmt("/project")
        assert result["formatted"] is False
        assert result["returncode"] == -1

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_fmt_command_args(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_fmt("/project")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/cargo"
        assert "fmt" in cmd
        assert "--all" in cmd
        assert "--check" in cmd

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_fmt_cwd_set(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_fmt("/my/project")
        assert mock_run.call_args[1]["cwd"] == "/my/project"

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_fmt_timeout_is_120(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_fmt("/project")
        assert mock_run.call_args[1]["timeout"] == 120

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_diff_captured(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result(returncode=1, stdout="-old\n+new")
        result = _run_cargo_fmt("/project")
        assert "-old" in result["diff"]
        assert "+new" in result["diff"]

    @patch(f"{MODULE}.shutil.which", return_value=None)
    def test_cargo_not_found_logs(self, mock_which, caplog):
        with caplog.at_level(logging.ERROR):
            _run_cargo_fmt("/project")
        assert "cargo not found" in caplog.text

    @patch(f"{MODULE}.subprocess.run", side_effect=subprocess.TimeoutExpired("cargo", 120))
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_timeout_logs(self, mock_which, mock_run, caplog):
        with caplog.at_level(logging.ERROR):
            _run_cargo_fmt("/project")
        assert "timed out" in caplog.text

    @pytest.mark.parametrize(
        "returncode,expected_formatted",
        [
            (0, True),
            (1, False),
            (2, False),
            (127, False),
        ],
    )
    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_returncode_to_formatted_parametrized(self, mock_which, mock_run, returncode, expected_formatted):
        mock_run.return_value = _make_subprocess_result(returncode=returncode)
        result = _run_cargo_fmt("/project")
        assert result["formatted"] is expected_formatted

# ---------------------------------------------------------------------------
# TestCollectRsFiles
# ---------------------------------------------------------------------------


class TestCollectRsFiles:

    def test_finds_rs_files(self, tmp_path):
        (tmp_path / "main.rs").write_text("fn main() {}")
        (tmp_path / "lib.rs").write_text("pub fn foo() {}")
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 2

    def test_nested_dirs(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.rs").write_text("")
        (tmp_path / "build.rs").write_text("")
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 2

    def test_no_rs_files(self, tmp_path):
        (tmp_path / "readme.md").write_text("hello")
        (tmp_path / "Cargo.toml").write_text("[package]")
        result = _collect_rs_files(str(tmp_path))
        assert result == []

    def test_mixed_file_types(self, tmp_path):
        (tmp_path / "main.rs").write_text("")
        (tmp_path / "utils.py").write_text("")
        (tmp_path / "config.toml").write_text("")
        (tmp_path / "style.css").write_text("")
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 1
        assert result[0].endswith("main.rs")

    def test_empty_dir(self, tmp_path):
        result = _collect_rs_files(str(tmp_path))
        assert result == []

    def test_sorted_output(self, tmp_path):
        (tmp_path / "z.rs").write_text("")
        (tmp_path / "a.rs").write_text("")
        (tmp_path / "m.rs").write_text("")
        result = _collect_rs_files(str(tmp_path))
        basenames = [os.path.basename(f) for f in result]
        assert basenames == ["a.rs", "m.rs", "z.rs"]

    def test_deeply_nested(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.rs").write_text("")
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 1
        assert "deep.rs" in result[0]

    def test_returns_full_paths(self, tmp_path):
        (tmp_path / "main.rs").write_text("")
        result = _collect_rs_files(str(tmp_path))
        assert os.path.isabs(result[0])

    def test_only_rs_extension(self, tmp_path):
        (tmp_path / "file.rsx").write_text("")
        (tmp_path / "file.r").write_text("")
        (tmp_path / "rs").write_text("")
        (tmp_path / "real.rs").write_text("")
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 1

    def test_empty_subdirs(self, tmp_path):
        (tmp_path / "empty_sub").mkdir()
        (tmp_path / "main.rs").write_text("")
        result = _collect_rs_files(str(tmp_path))
        assert len(result) == 1

# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------


class TestMain:

    def test_nonexistent_dir_raises(self, tmp_path):
        bad = str(tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError, match='does not exist'):
            main(bad)

    def test_no_cargo_toml_raises(self, tmp_path):
        d = tmp_path / "no_cargo"
        d.mkdir()
        with pytest.raises(FileNotFoundError, match='No Cargo.toml'):
            main(str(d))

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_passed_true_clean(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        (tmp_path / "main.rs").write_text("")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert result["passed"] is True

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_passed_false_clippy_warnings(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 2, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert result["passed"] is False

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_passed_false_clippy_errors(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 1, "messages": [],
            "returncode": 1, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert result["passed"] is False

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_passed_false_fmt_issues(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": False, "diff": "some diff", "returncode": 1}
        result = main(str(tmp_path))
        assert result["passed"] is False

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_passed_false_both_issues(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 1, "errors": 1, "messages": [],
            "returncode": 1, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": False, "diff": "diff", "returncode": 1}
        result = main(str(tmp_path))
        assert result["passed"] is False

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_custom_files_list(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path), files=["foo.rs", "bar.rs"])
        assert len(result["files_checked"]) == 2

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_discovered_files(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        (tmp_path / "main.rs").write_text("")
        (tmp_path / "lib.rs").write_text("")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert len(result["files_checked"]) == 2
    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_result_has_clippy_key(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert "clippy" in result

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_result_has_fmt_key(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert "fmt" in result

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_result_has_files_checked_key(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert "files_checked" in result

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_result_has_passed_key(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert "passed" in result

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_clippy_output(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 1, "errors": 0,
            "messages": [{"level": "warning", "message": "unused var", "spans": []}],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "1 warning(s)" in captured.out
        assert "WARNING: unused var" in captured.out

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_clippy_with_span(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 1, "errors": 0,
            "messages": [{
                "level": "warning", "message": "unused",
                "spans": [{"file": "src/main.rs", "line_start": 5, "line_end": 5, "col_start": 1, "col_end": 10, "label": ""}],
            }],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "src/main.rs:5" in captured.out

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_fmt_ok(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "Format: OK" in captured.out

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_fmt_needs_formatting(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": False, "diff": "- old\n+ new", "returncode": 1}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "NEEDS FORMATTING" in captured.out
        assert "- old" in captured.out

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_passed(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "PASSED" in captured.out

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_failed(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 1, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "FAILED" in captured.out
    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_nonexistent_dir_logs_error(self, mock_clippy, mock_fmt, tmp_path, caplog):
        bad = str(tmp_path / "nonexistent")
        with caplog.at_level(logging.ERROR):
            with pytest.raises(FileNotFoundError):
                main(bad)
        assert "does not exist" in caplog.text

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_no_cargo_toml_logs_error(self, mock_clippy, mock_fmt, tmp_path, caplog):
        d = tmp_path / "empty"
        d.mkdir()
        with caplog.at_level(logging.ERROR):
            with pytest.raises(FileNotFoundError):
                main(str(d))
        assert "No Cargo.toml" in caplog.text

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_cargo_dir_passed_to_clippy(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        mock_clippy.assert_called_once_with(str(tmp_path.resolve()))

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_cargo_dir_passed_to_fmt(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        mock_fmt.assert_called_once_with(str(tmp_path.resolve()))

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_files_none_discovers_rs(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        (tmp_path / "a.rs").write_text("")
        (tmp_path / "b.py").write_text("")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path))
        assert len(result["files_checked"]) == 1
        assert result["files_checked"][0].endswith("a.rs")

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_custom_files_absolute_paths(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path), files=["rel/foo.rs"])
        for f in result["files_checked"]:
            assert os.path.isabs(f)

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_empty_files_list_passes(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(tmp_path), files=[])
        assert result["files_checked"] == []
        assert result["passed"] is True

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_fmt_diff_limited_in_print(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        long_diff = '\n'.join([f'line {i}' for i in range(50)])
        mock_fmt.return_value = {"formatted": False, "diff": long_diff, "returncode": 1}
        main(str(tmp_path))
        captured = capsys.readouterr()
        diff_lines = [l for l in captured.out.splitlines() if l.strip().startswith('line ')]
        assert len(diff_lines) <= 20

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_error_messages(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 1,
            "messages": [{
                "level": "error", "message": "cannot find type",
                "spans": [],
            }],
            "returncode": 1, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "ERROR: cannot find type" in captured.out

# ---------------------------------------------------------------------------
# TestEdgeCases - Additional edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.fixture(autouse=True)
    def _bypass_cache_bust(self):
        with patch(f"{MODULE}._bust_clippy_cache", return_value=True):
            yield

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_multiple_json_objects_per_line_only_first(self, mock_which, mock_run):
        line = _clippy_json_line("warning", "w1")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 1

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_unicode_in_message(self, mock_which, mock_run):
        line = _clippy_json_line("warning", "variable \u00e9 unused")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 1

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_empty_message(self, mock_which, mock_run):
        line = _clippy_json_line("warning", "")
        mock_run.return_value = _make_subprocess_result(stdout=line)
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 1
        assert result["messages"][0]["message"] == ""

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_missing_message_key(self, mock_which, mock_run):
        import json as _json
        obj = _json.dumps({"reason": "compiler-message"})
        mock_run.return_value = _make_subprocess_result(stdout=obj)
        result = _run_cargo_clippy("/project")
        assert result["messages"] == []

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_many_warnings(self, mock_which, mock_run):
        lines = []
        for i in range(50):
            lines.append(_clippy_json_line("warning", f"w{i}"))
        mock_run.return_value = _make_subprocess_result(stdout="\n".join(lines))
        result = _run_cargo_clippy("/project")
        assert result["warnings"] == 50
        assert len(result["messages"]) == 50

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_clippy_capture_output_true(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_clippy("/project")
        assert mock_run.call_args[1]["capture_output"] is True
        assert mock_run.call_args[1]["text"] is True

    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.shutil.which", return_value="/usr/bin/cargo")
    def test_fmt_capture_output_true(self, mock_which, mock_run):
        mock_run.return_value = _make_subprocess_result()
        _run_cargo_fmt("/project")
        assert mock_run.call_args[1]["capture_output"] is True
        assert mock_run.call_args[1]["text"] is True

    def test_collect_rs_files_returns_list(self, tmp_path):
        result = _collect_rs_files(str(tmp_path))
        assert isinstance(result, list)

    def test_find_cargo_toml_with_relative_path(self, tmp_path, monkeypatch):
        (tmp_path / "Cargo.toml").write_text("[package]")
        monkeypatch.chdir(tmp_path)
        result = _find_cargo_toml(".")
        assert result is not None

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_main_with_cargo_in_parent(self, mock_clippy, mock_fmt, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.rs").write_text("")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        result = main(str(sub))
        assert result["passed"] is True

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_main_logs_info(self, mock_clippy, mock_fmt, tmp_path, caplog):
        (tmp_path / "Cargo.toml").write_text("[package]")
        (tmp_path / "main.rs").write_text("")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        with caplog.at_level(logging.INFO):
            main(str(tmp_path))
        assert "Rust lint" in caplog.text
        assert "Stage 1" in caplog.text or "clippy" in caplog.text.lower()

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_main_fmt_ok_logs_info(self, mock_clippy, mock_fmt, tmp_path, caplog):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        with caplog.at_level(logging.INFO):
            main(str(tmp_path))
        assert "OK" in caplog.text

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_main_fmt_needs_changes_logs_warning(self, mock_clippy, mock_fmt, tmp_path, caplog):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": False, "diff": "diff", "returncode": 1}
        with caplog.at_level(logging.WARNING):
            main(str(tmp_path))
        assert "needs changes" in caplog.text

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_print_no_messages_no_details(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": True, "diff": "", "returncode": 0}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "WARNING:" not in captured.out
        assert "ERROR:" not in captured.out

    @patch(f"{MODULE}._run_cargo_fmt")
    @patch(f"{MODULE}._run_cargo_clippy")
    def test_fmt_no_diff_no_diff_printed(self, mock_clippy, mock_fmt, tmp_path, capsys):
        (tmp_path / "Cargo.toml").write_text("[package]")
        mock_clippy.return_value = {
            "warnings": 0, "errors": 0, "messages": [],
            "returncode": 0, "raw_stderr": "",
        }
        mock_fmt.return_value = {"formatted": False, "diff": "", "returncode": 1}
        main(str(tmp_path))
        captured = capsys.readouterr()
        assert "NEEDS FORMATTING" in captured.out
