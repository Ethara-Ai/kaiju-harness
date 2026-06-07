"""Unit tests for ``tools.cpp_version`` (C and C++ standard detection)."""

from __future__ import annotations

from pathlib import Path


from tools.cpp_version import (
    C_STANDARDS,
    CPP_STANDARDS,
    detect_c,
    detect_cpp,
)


class TestCStandard:
    def test_no_signals_returns_fallback(self, tmp_path: Path) -> None:
        r = detect_c(tmp_path)
        assert r.version == "11"
        assert r.source == "default"

    def test_cmake_c_standard_pinned(self, tmp_path: Path) -> None:
        (tmp_path / "CMakeLists.txt").write_text("set(CMAKE_C_STANDARD 17)\n")
        r = detect_c(tmp_path)
        assert r.version == "17"

    def test_cmake_compile_features(self, tmp_path: Path) -> None:
        (tmp_path / "CMakeLists.txt").write_text(
            "target_compile_features(mylib PUBLIC c_std_11)\n"
        )
        r = detect_c(tmp_path)
        assert r.version == "11"

    def test_makefile_cflags(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("CFLAGS = -std=c99\n")
        r = detect_c(tmp_path)
        assert r.version == "99"

    def test_meson(self, tmp_path: Path) -> None:
        (tmp_path / "meson.build").write_text(
            "project('x', 'c', default_options: ['c_std=c11'])\n"
        )
        r = detect_c(tmp_path)
        assert r.version == "11"


class TestCppStandard:
    def test_no_signals_returns_fallback(self, tmp_path: Path) -> None:
        r = detect_cpp(tmp_path)
        assert r.version == "17"
        assert r.source == "default"

    def test_cmake_cxx_standard(self, tmp_path: Path) -> None:
        (tmp_path / "CMakeLists.txt").write_text("set(CMAKE_CXX_STANDARD 20)\n")
        r = detect_cpp(tmp_path)
        assert r.version == "20"

    def test_cmake_compile_features_cxx(self, tmp_path: Path) -> None:
        (tmp_path / "CMakeLists.txt").write_text(
            "target_compile_features(mylib PUBLIC cxx_std_23)\n"
        )
        r = detect_cpp(tmp_path)
        assert r.version == "23"

    def test_makefile_cxxflags(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("CXXFLAGS = -std=c++20\n")
        r = detect_cpp(tmp_path)
        assert r.version == "20"

    def test_meson(self, tmp_path: Path) -> None:
        (tmp_path / "meson.build").write_text(
            "project('x', 'cpp', default_options: ['cpp_std=c++20'])\n"
        )
        r = detect_cpp(tmp_path)
        assert r.version == "20"

    def test_priority_cmake_over_makefile(self, tmp_path: Path) -> None:
        (tmp_path / "CMakeLists.txt").write_text("set(CMAKE_CXX_STANDARD 20)\n")
        (tmp_path / "Makefile").write_text("CXXFLAGS = -std=c++17\n")
        r = detect_cpp(tmp_path)
        assert r.version == "20"
        # Conflict reported but not raised
        assert any("Makefile" in c for c in r.conflicts)


class TestNormalization:
    def test_c_standards_canonical(self) -> None:
        assert C_STANDARDS == ("89", "99", "11", "17", "23")

    def test_cpp_standards_canonical(self) -> None:
        assert CPP_STANDARDS == ("98", "11", "14", "17", "20", "23", "26")
