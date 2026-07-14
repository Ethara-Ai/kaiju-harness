"""Regression tests for commit0.harness.spec_cpp.

Locks in the eval-script invariants so we never re-ship the class of bug where
the eval script tries to build without first configuring - which produces a
silent 0/N score on the local_inplace backend (fresh worktree, no build/ dir).

Fixture that triggered the fix: outputs/fb704030-.../stage2_eval_artifacts/
fmt/aider-cpp-gpt-5.5-dataset/cdb4ee2aea69cc6a83331b/test_output.txt showed:

    + cmake --build build --parallel 8
    Error: /tmp/commit0-inplace-b7_1ue09/tree/build is not a directory
    + ctest --test-dir build --output-on-failure .
    Failed to change working directory ...
"""
from __future__ import annotations

import pytest

from commit0.harness.spec_cpp import (
    BUILD_CMD_MAP,
    CONFIGURE_CMD_MAP,
    CppSpec,
    make_cpp_spec,
)


def _dict_instance(build_system: str = "cmake") -> dict:
    return {
        "instance_id": "example__demo",
        "repo": "example/demo",
        "original_repo": "example/demo",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "reference_commit": "abcdef0123456789abcdef0123456789abcdef01",
        "build_system": build_system,
        "setup": {
            "install": "",
            "packages": "",
            "pip_packages": "",
            "pre_install": "",
            "python": "3.11",
            "specification": "",
            "cmake_flags": "",
        },
        "test": {"test_cmd": "ctest --test-dir build --verbose", "test_dir": "tests"},
        "src_dir": "src",
    }


@pytest.fixture
def cmake_spec() -> CppSpec:
    return make_cpp_spec(_dict_instance("cmake"), absolute=True)


@pytest.fixture
def meson_spec() -> CppSpec:
    return make_cpp_spec(_dict_instance("meson"), absolute=True)


@pytest.fixture
def make_spec() -> CppSpec:
    return make_cpp_spec(_dict_instance("make"), absolute=True)


class TestBuildCmdMap:
    def test_cmake_uses_cmake_build(self) -> None:
        assert "cmake --build build" in BUILD_CMD_MAP["cmake"]

    def test_meson_uses_ninja(self) -> None:
        assert "ninja" in BUILD_CMD_MAP["meson"]

    def test_all_supported_systems_have_build_cmd(self) -> None:
        for system in ("cmake", "meson", "autotools", "make"):
            assert BUILD_CMD_MAP.get(system), f"{system} missing build cmd"


class TestConfigureCmdMap:
    def test_cmake_configure_creates_build_dir(self) -> None:
        assert "-B build" in CONFIGURE_CMD_MAP["cmake"]

    def test_cmake_configure_exports_compile_commands(self) -> None:
        assert "CMAKE_EXPORT_COMPILE_COMMANDS=ON" in CONFIGURE_CMD_MAP["cmake"]

    def test_meson_configure_creates_builddir(self) -> None:
        assert "builddir" in CONFIGURE_CMD_MAP["meson"]


class TestEvalScriptConfigureStep:
    """The load-bearing invariant: eval script MUST run configure before build.

    The local_inplace backend runs eval in a fresh git worktree that has NO
    `build/` dir. Without a configure step, `cmake --build build` fails with
    'is not a directory' and no tests run - silently scored as 0/N.
    """

    def test_cmake_eval_runs_configure_before_build(self, cmake_spec: CppSpec) -> None:
        script = cmake_spec.make_eval_script_list()
        joined = "\n".join(script)
        configure_idx = joined.find("cmake -S . -B build")
        build_idx = joined.find("cmake --build build")
        assert configure_idx != -1, "cmake configure line missing from eval script"
        assert build_idx != -1, "cmake build line missing from eval script"
        assert configure_idx < build_idx, (
            "cmake configure MUST run before cmake --build (else the build "
            "directory doesn't exist on local_inplace)"
        )

    def test_cmake_configure_disables_warning_as_error(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "CMAKE_COMPILE_WARNING_AS_ERROR=OFF" in joined, (
            "stubbed functions have unused parameters; -Werror would turn that "
            "into a hard build error and produce a false COMPILE_FAILED 0/N"
        )

    def test_cmake_configure_writes_stdout_file(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "configure_stdout.txt" in joined, (
            "configure stdout must be captured for the BUILD_CONFIGURE_FAILED"
            " sentinel to include diagnostics"
        )

    def test_cmake_build_captures_compile_errors(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "compile_errors.txt" in joined, (
            "build stderr must be captured so COMPILE_FAILED sentinel has context"
        )

    def test_meson_eval_runs_setup_before_ninja(self, meson_spec: CppSpec) -> None:
        joined = "\n".join(meson_spec.make_eval_script_list())
        setup_idx = joined.find("meson setup builddir")
        ninja_idx = joined.find("ninja -C builddir")
        assert setup_idx != -1
        assert ninja_idx != -1
        assert setup_idx < ninja_idx


class TestEvalScriptFailureSentinels:
    """Configure and build failures must emit distinct sentinels so
    evaluate_cpp can exclude them from scoring instead of reading them as a
    legitimate 0/N."""

    def test_configure_failure_emits_sentinel(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "BUILD_CONFIGURE_FAILED" in joined, (
            "configure failure must write a BUILD_CONFIGURE_FAILED sentinel"
        )
        assert "CONFIGURE_RC" in joined

    def test_build_failure_emits_compile_failed_sentinel(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "COMPILE_FAILED" in joined
        assert "BUILD_RC" in joined

    def test_patch_apply_failure_still_bails(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "PATCH_APPLY_FAILED" in joined


class TestEvalScriptExitCode:
    def test_test_step_writes_exit_code(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "test_exit_code.txt" in joined

    def test_test_output_captured(self, cmake_spec: CppSpec) -> None:
        joined = "\n".join(cmake_spec.make_eval_script_list())
        assert "test_output.txt" in joined
