from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.prepare_repo_cpp import (
    _detect_cmake_library_targets,
    _detect_meson_library_targets,
    _TEST_TARGET_MARKERS,
    _CMAKE_UTILITY_TARGETS,
)


CMAKE_HELP_FMT = """The following are some of the valid targets for this Makefile:
... all (the default if no target is provided)
... clean
... depend
... install
... rebuild_cache
... test
... args-test
... base-test
... c-test
... fmt
... fmt-c
... gtest
... perf-sanity
... src/fmt-c.o
... src/format.o
"""

CMAKE_HELP_BENCHMARK = """The following are some of the valid targets for this Makefile:
... all
... clean
... install
... benchmark
... benchmark_main
... benchmark_test
... check-benchmark
"""

CMAKE_HELP_HEADER_ONLY = """The following are some of the valid targets for this Makefile:
... all
... clean
... install
... rebuild_cache
"""

CMAKE_HELP_PROTOBUF = """The following are some of the valid targets for this Makefile:
... all
... clean
... install
... libprotobuf
... libprotoc
... libprotobuf-lite
... protobuf-test
... protoc
... conformance_test
"""


class TestDetectCmakeLibraryTargets:
    def _make_repo(self, tmp_path: Path, name: str) -> Path:
        repo = tmp_path / name
        (repo / "build").mkdir(parents=True)
        return repo

    def test_fmt_detects_library_targets_only(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "fmt")
        result = MagicMock(returncode=0, stdout=CMAKE_HELP_FMT)
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_cmake_library_targets(repo)
        assert "fmt" in libs
        assert "fmt-c" in libs
        assert "args-test" not in libs
        assert "base-test" not in libs
        assert "gtest" not in libs
        assert "perf-sanity" not in libs
        assert "src/fmt-c.o" not in libs

    def test_benchmark_lib_preserved_by_repo_name_allowance(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "benchmark")
        result = MagicMock(returncode=0, stdout=CMAKE_HELP_BENCHMARK)
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_cmake_library_targets(repo)
        assert "benchmark" in libs
        assert "benchmark_test" not in libs
        assert "check-benchmark" not in libs

    def test_header_only_lib_returns_empty(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "eigen")
        result = MagicMock(returncode=0, stdout=CMAKE_HELP_HEADER_ONLY)
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_cmake_library_targets(repo)
        assert libs == []

    def test_protobuf_style_libs(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "protobuf")
        result = MagicMock(returncode=0, stdout=CMAKE_HELP_PROTOBUF)
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_cmake_library_targets(repo)
        assert "libprotobuf" in libs
        assert "libprotoc" in libs
        assert "libprotobuf-lite" in libs
        assert "protobuf-test" not in libs
        assert "conformance_test" not in libs

    def test_no_build_dir_returns_empty(self, tmp_path: Path) -> None:
        repo = tmp_path / "no_build"
        repo.mkdir()
        assert _detect_cmake_library_targets(repo) == []

    def test_cmake_help_command_failure_returns_empty(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "broken")
        result = MagicMock(returncode=1, stdout="", stderr="oops")
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_cmake_library_targets(repo)
        assert libs == []


MESON_INTROSPECT_FMT = json.dumps([
    {"name": "fmt", "type": "static library"},
    {"name": "fmt-c", "type": "shared library"},
    {"name": "fmt-test", "type": "executable"},
    {"name": "perf-sanity", "type": "executable"},
])

MESON_INTROSPECT_HEADER_ONLY = json.dumps([])


class TestDetectMesonLibraryTargets:
    def _make_repo(self, tmp_path: Path, name: str) -> Path:
        repo = tmp_path / name
        (repo / "builddir").mkdir(parents=True)
        return repo

    def test_fmt_meson_detects_libraries_only(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "fmt")
        result = MagicMock(returncode=0, stdout=MESON_INTROSPECT_FMT)
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_meson_library_targets(repo)
        assert "fmt" in libs
        assert "fmt-c" in libs
        assert "fmt-test" not in libs
        assert "perf-sanity" not in libs

    def test_no_builddir_returns_empty(self, tmp_path: Path) -> None:
        repo = tmp_path / "no_build"
        repo.mkdir()
        assert _detect_meson_library_targets(repo) == []

    def test_empty_targets_list_returns_empty(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "empty")
        result = MagicMock(returncode=0, stdout=MESON_INTROSPECT_HEADER_ONLY)
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_meson_library_targets(repo)
        assert libs == []

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        repo = self._make_repo(tmp_path, "bad")
        result = MagicMock(returncode=0, stdout="not json")
        with patch("tools.prepare_repo_cpp.subprocess.run", return_value=result):
            libs = _detect_meson_library_targets(repo)
        assert libs == []


class TestConstants:
    def test_test_markers_include_common_conventions(self) -> None:
        for marker in ("test", "example", "bench", "benchmark", "fuzz", "demo"):
            assert marker in _TEST_TARGET_MARKERS

    def test_utility_targets_include_cmake_defaults(self) -> None:
        for t in ("all", "clean", "install", "gtest"):
            assert t in _CMAKE_UTILITY_TARGETS
