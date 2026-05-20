from pathlib import Path
from typing import Dict, List

from pydantic import Field

from commit0.harness.constants import (
    DOCKERFILES_DIR,
    RepoInstance,
    TestStatus,
)

__all__ = [
    "CppRepoInstance",
    "CPP_STUB_MARKER",
    "CPP_STUB_MARKER_CONSTEXPR",
    "CPP_STUB_MARKER_NOEXCEPT",
    "CPP_SPLIT",
    "CPP_BASE_BRANCH",
    "CPP_GITIGNORE_ENTRIES",
    "CPP_BUILD_SYSTEMS",
    "CPP_TEST_FRAMEWORKS",
    "RUN_CPP_TESTS_LOG_DIR",
    "CPP_TEST_IDS_DIR",
    "DOCKERFILES_CPP_DIR",
    "DOCKERFILES_DIR",
    "TestStatus",
]

CPP_STUB_MARKER = 'throw std::runtime_error("STUB: not implemented")'
CPP_STUB_MARKER_CONSTEXPR = "return {}"
CPP_STUB_MARKER_NOEXCEPT = "std::abort()"

CPP_BASE_BRANCH = "commit0"

CPP_GITIGNORE_ENTRIES = [
    "build/",
    "cmake-build-*/",
    "builddir/",
    ".cache/",
    "compile_commands.json",
    ".aider*",
    "logs/",
]

CPP_BUILD_SYSTEMS = ["cmake", "meson", "autotools", "make"]

CPP_TEST_FRAMEWORKS = ["gtest", "catch2", "doctest", "boost_test", "ctest"]

# Curated subsets only — the "all" subset is derived dynamically from the
# loaded dataset by ``commit0.harness.split_utils.resolve_split``.
CPP_SPLIT: Dict[str, list[str]] = {}

RUN_CPP_TESTS_LOG_DIR = Path("logs/cpp_tests")

CPP_TEST_IDS_DIR = Path(__file__).parent.parent / "data" / "cpp_test_ids"

DOCKERFILES_CPP_DIR = Path(__file__).parent / "dockerfiles"


class CppRepoInstance(RepoInstance):
    """Repo instance with C++-specific metadata."""

    build_system: str = "cmake"
    cpp_standard: str = "17"
    test_framework: str = "gtest"
    cmake_options: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    compiler: str = "gcc"
    submodules: bool = False
