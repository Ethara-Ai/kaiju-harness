"""Unit tests for agent.agent_utils_c (audit finding T-001: C module had no tests).

Characterization tests for the pure helpers: test-file detection, source/test
collection (with skip-dir pruning), stub extraction, and lint-command building.
"""

from __future__ import annotations

from pathlib import Path

from agent.agent_utils_c import (
    _is_test_filename,
    collect_c_files,
    collect_c_test_files,
    extract_c_function_stubs,
    get_c_lint_cmd,
)


class TestIsTestFilename:
    def test_test_prefix(self) -> None:
        assert _is_test_filename("test_foo.c") is True

    def test_suffix_test(self) -> None:
        assert _is_test_filename("foo_test.c") is True

    def test_suffix_tests(self) -> None:
        assert _is_test_filename("foo_tests.c") is True

    def test_check_prefix(self) -> None:
        assert _is_test_filename("check_foo.c") is True

    def test_plain_source_is_not_test(self) -> None:
        assert _is_test_filename("foo.c") is False
        assert _is_test_filename("main.c") is False


class TestCollectCFiles:
    def test_excludes_tests_and_skip_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "foo.c").write_text("int main(){}")
        (tmp_path / "test_foo.c").write_text("// test")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "bar.c").write_text("int bar(){}")
        build = tmp_path / "build"
        build.mkdir()
        (build / "generated.c").write_text("int g(){}")

        result = collect_c_files(str(tmp_path))
        names = {Path(p).name for p in result}
        assert names == {"foo.c", "bar.c"}

    def test_returns_sorted(self, tmp_path: Path) -> None:
        for n in ("c.c", "a.c", "b.c"):
            (tmp_path / n).write_text("")
        result = collect_c_files(str(tmp_path))
        assert result == sorted(result)


class TestCollectCTestFiles:
    def test_collects_only_test_files(self, tmp_path: Path) -> None:
        (tmp_path / "foo.c").write_text("")
        (tmp_path / "test_foo.c").write_text("")
        (tmp_path / "bar_test.c").write_text("")
        result = collect_c_test_files(str(tmp_path))
        names = {Path(p).name for p in result}
        assert names == {"test_foo.c", "bar_test.c"}


class TestExtractCFunctionStubs:
    def test_detects_stub_panic_function(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.c"
        f.write_text('int add(int a, int b) {\n    STUB_PANIC("add");\n}\n')
        stubs = extract_c_function_stubs(str(f))
        assert len(stubs) == 1
        assert stubs[0]["name"] == "add"
        assert stubs[0]["file"] == str(f)

    def test_no_stub_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.c"
        f.write_text("int add(int a, int b) {\n    return a + b;\n}\n")
        assert extract_c_function_stubs(str(f)) == []

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert extract_c_function_stubs(str(tmp_path / "nope.c")) == []


class TestGetCLintCmd:
    def test_includes_repo_and_config(self) -> None:
        cmd = get_c_lint_cmd("myrepo", "/path/cfg.yaml")
        assert "lint" in cmd
        assert "myrepo" in cmd
        assert "/path/cfg.yaml" in cmd
