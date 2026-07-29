"""C++ prepare: header-only / single-header / C-only repo support.

Pins the harness fixes that make cppstubber + prepare_repo_cpp handle the repo
classes the CPP_1 failure report flagged (rapidjson, spdlog, thread-pool, …):

1. LLVM PORTABILITY — stubber.cpp guards isPureVirtual()/isPure() by
   LLVM_VERSION_MAJOR so the stubber builds on every toolchain (14→latest);
   _discover_llvm_cmake_dir lets the harness self-build it where LLVM isn't on
   cmake's default path.
2. HEADER-ONLY STUBBING — the stubber parses standalone headers as C++
   (-x c++), with a language standard, include roots, and the platform sysroot,
   so template/inline code in .h/.hpp yields real stubs instead of 0. The
   tree-sitter FALLBACK also scans .h now.
3. isInTestFile is PATH-SEGMENT anchored, not a bare "test" substring (an
   ancestor dir like ho_test/latest no longer skips the whole repo).
4. BUILD-SYSTEM 'none' — a header-only repo with no CMake/Make is not fatal;
   detect_build_system returns "none", the compile-commands + compile gate are
   skipped, and correctness rides on the stubbed-base-parses gate.

The C++-toolchain tests (stubber build + stub) are gated on cppstubber being
buildable/available on the host; the Python-logic tests always run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools import prepare_repo_cpp as P
from tools.stub_cpp import CPP_EXTENSIONS

REPO = Path(__file__).resolve().parents[1]
STUBBER = REPO / "tools" / "cppstubber" / "build" / "cppstubber"


# ---------------------------------------------------------------------------
# Python-logic fixes (always run)
# ---------------------------------------------------------------------------
class TestHeaderOnlyDetection:
    def _mk(self, tmp_path, headers, impls=()):
        for h in headers:
            p = tmp_path / h
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("inline int f(){return 0;}")
        for c in impls:
            p = tmp_path / c
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("int g(){return 1;}")
        return tmp_path

    def test_header_only_true_for_headers_no_impl(self, tmp_path):
        self._mk(tmp_path, ["include/lib/a.hpp", "include/lib/b.h"])
        assert P.is_header_only_repo(tmp_path) is True

    def test_header_only_false_with_impl(self, tmp_path):
        self._mk(tmp_path, ["include/lib/a.hpp"], ["src/a.cpp"])
        assert P.is_header_only_repo(tmp_path) is False

    def test_test_and_vendor_impls_do_not_count(self, tmp_path):
        # a .cpp only under tests/ or third_party/ must not defeat header-only
        self._mk(tmp_path, ["include/lib/a.hpp"],
                 ["tests/a_test.cpp", "third_party/dep/x.cpp"])
        assert P.is_header_only_repo(tmp_path) is True

    def test_detect_build_system_returns_none_for_header_only(self, tmp_path):
        self._mk(tmp_path, ["include/lib/a.hpp"])
        # no CMakeLists/Makefile anywhere
        assert P.detect_build_system(tmp_path) == "none"

    def test_detect_build_system_still_finds_cmake(self, tmp_path):
        self._mk(tmp_path, ["include/lib/a.hpp"])
        (tmp_path / "CMakeLists.txt").write_text("project(x)")
        assert P.detect_build_system(tmp_path) == "cmake"

    def test_none_build_system_skips_compile_commands(self, tmp_path, caplog):
        assert P.generate_compile_commands(tmp_path, "none") is False

    def test_none_build_system_verify_compiles_true(self, tmp_path):
        assert P.verify_compiles(tmp_path, "none") is True

    def test_none_default_test_cmd(self):
        assert P._default_test_cmd("none") == "true"


class TestFallbackAndDiscovery:
    def test_tree_sitter_fallback_scans_h_and_inline_exts(self):
        for ext in (".h", ".hpp", ".ipp", ".tpp", ".inl"):
            assert ext in CPP_EXTENSIONS, ext

    def test_llvm_cmake_discovery_returns_valid_dir_or_none(self):
        d = P._discover_llvm_cmake_dir()
        assert d is None or (Path(d) / "LLVMConfig.cmake").exists()

    def test_build_system_cli_offers_none(self):
        src = (REPO / "tools" / "prepare_repo_cpp.py").read_text()
        assert '"make", "none"' in src


# ---------------------------------------------------------------------------
# C++ toolchain fixes (gated on a buildable/available stubber)
# ---------------------------------------------------------------------------
def _stubber_available() -> bool:
    if STUBBER.exists():
        return True
    try:
        P._ensure_cppstubber_fresh()
    except Exception:
        return False
    return STUBBER.exists()


pytestmark_toolchain = pytest.mark.skipif(
    not _stubber_available(), reason="cppstubber not buildable/available on host")


@pytestmark_toolchain
class TestStubberHeaderOnly:
    def test_version_guard_source_present(self):
        src = (REPO / "tools" / "cppstubber" / "src" / "stubber.cpp").read_text()
        assert "LLVM_VERSION_MAJOR >= 18" in src
        assert "isPureVirtual" in src and "isPure()" in src

    def test_stubs_a_header_only_library(self, tmp_path):
        # a self-contained header (no system includes) with inline + template
        # + member functions, at a clean (test-free) path
        lib = tmp_path / "myorg__mylib" / "include" / "mylib"
        lib.mkdir(parents=True)
        (lib / "core.hpp").write_text(
            "#pragma once\n"
            "namespace mylib {\n"
            "inline int add(int a, int b) { return a + b; }\n"
            "struct W { int c(int x) const { return x*2; } };\n"
            "template<typename T> T mx(T a, T b){ return a<b?b:a; }\n"
            "}\n")
        r = subprocess.run(
            [str(STUBBER), "--input-dir", str(tmp_path / "myorg__mylib"),
             "--in-place", "--quiet"],
            capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        body = (lib / "core.hpp").read_text()
        # all three function kinds stubbed
        assert body.count("__builtin_trap") >= 3, body

    def test_test_path_substring_does_not_skip_repo(self, tmp_path):
        # repo under a dir whose NAME contains "test" (e.g. ho_test) must still
        # stub — the old bare has("test") skipped everything here.
        lib = tmp_path / "ho_test" / "proj" / "include"
        lib.mkdir(parents=True)
        (lib / "a.hpp").write_text(
            "#pragma once\ninline int f(int x){ return x+1; }\n")
        r = subprocess.run(
            [str(STUBBER), "--input-dir", str(tmp_path / "ho_test" / "proj"),
             "--in-place", "--quiet"],
            capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert "__builtin_trap" in (lib / "a.hpp").read_text()

    def test_real_test_dir_still_skipped(self, tmp_path):
        # a genuine tests/ dir must still be left alone
        proj = tmp_path / "proj"
        (proj / "include").mkdir(parents=True)
        (proj / "tests").mkdir(parents=True)
        (proj / "include" / "a.hpp").write_text(
            "#pragma once\ninline int f(int x){ return x+1; }\n")
        (proj / "tests" / "a_test.cpp").write_text(
            "int run_case(){ return 7; }\n")
        subprocess.run(
            [str(STUBBER), "--input-dir", str(proj), "--in-place", "--quiet"],
            capture_output=True, text=True, timeout=120)
        assert "__builtin_trap" in (proj / "include" / "a.hpp").read_text()
        assert "__builtin_trap" not in (proj / "tests" / "a_test.cpp").read_text()
