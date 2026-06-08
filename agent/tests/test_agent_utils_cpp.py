"""Unit tests for agent.agent_utils_cpp (audit finding T-001: C++ module had no tests).

Characterization tests for C++ source collection, stub-target detection, and
test-output parsing.
"""

from __future__ import annotations

from pathlib import Path

from agent.agent_utils_cpp import (
    find_cpp_files_to_edit,
    get_target_edit_files_cpp,
    _parse_cpp_test_output,
)

CPP_STUB = 'throw std::runtime_error("STUB: not implemented")'


class TestFindCppFilesToEdit:
    def test_collects_cpp_and_headers_excludes_cmake_and_build(self, tmp_path: Path) -> None:
        (tmp_path / "a.cpp").write_text("int a();")
        (tmp_path / "b.hpp").write_text("int b();")
        (tmp_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.0)")
        build = tmp_path / "build"
        build.mkdir()
        (build / "gen.cpp").write_text("int g();")

        result = find_cpp_files_to_edit(str(tmp_path))
        names = {Path(p).name for p in result}
        assert names == {"a.cpp", "b.hpp"}

    def test_returns_sorted(self, tmp_path: Path) -> None:
        for n in ("c.cpp", "a.cpp", "b.cpp"):
            (tmp_path / n).write_text("")
        result = find_cpp_files_to_edit(str(tmp_path))
        assert result == sorted(result)


class TestGetTargetEditFilesCpp:
    def test_only_files_with_stub_marker(self, tmp_path: Path) -> None:
        stubbed = tmp_path / "stub.cpp"
        stubbed.write_text(f"int f() {{ {CPP_STUB}; }}")
        plain = tmp_path / "done.cpp"
        plain.write_text("int g() { return 1; }")

        result = get_target_edit_files_cpp(str(tmp_path))
        names = {Path(p).name for p in result}
        assert names == {"stub.cpp"}


class TestParseCppTestOutput:
    def test_surfaces_failed_lines(self) -> None:
        raw = (
            "[==========] Running 2 tests\n"
            "[  FAILED  ] MathTest.AddsCorrectly\n"
            "[       OK ] MathTest.Subtracts\n"
        )
        out = _parse_cpp_test_output(raw)
        assert "FAILED" in out
        assert "MathTest.AddsCorrectly" in out

    def test_surfaces_all_tests_passed(self) -> None:
        raw = "All tests passed (3 assertions in 1 test case)"
        out = _parse_cpp_test_output(raw)
        assert "All tests passed" in out

    def test_returns_string(self) -> None:
        assert isinstance(_parse_cpp_test_output("random unstructured output"), str)
