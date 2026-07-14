"""C-specific constants and data models for commit0 C integration."""

from enum import Enum
from pathlib import Path
from typing import Dict

from commit0.harness.constants import RepoInstance
import os


class CLanguage(str, Enum):
    C = "c"


class CRepoInstance(RepoInstance):
    src_dir: str = "."
    language: CLanguage = CLanguage.C
    build_system: str = "cmake"
    test_framework: str = "ctest"


C_BASE_BRANCH = "commit0"
C_VERSION = "18"

# GCC toolchain version for the C base image tag. Selected to match Docker Hub
# gcc images (gcc:<N>-bookworm). Override via C_GCC_VERSION env var for
# repos requiring a specific GCC (e.g. legacy code stuck on gcc 9/10, or
# testing against GCC 14 preview). Default GCC 13 supports C89..C18.
C_GCC_VERSION = os.environ.get("C_GCC_VERSION", "13")

C_SOURCE_EXT = ".c"
C_HEADER_EXT = ".h"
C_STUB_MARKER = "STUB_PANIC"
C_TEST_FILE_GLOBS: list[str] = ["test_*.c", "*_test.c", "*_tests.c", "check_*.c"]
C_SKIP_DIRS = (
    "tests",
    "test",
    "examples",
    "demo",
    "benchmark",
    "third_party",
    "vendor",
    "deps",
)
RUN_C_TEST_LOG_DIR = Path("logs/c_test")

C_GITIGNORE_ENTRIES = [
    "build/",
    "cmake-build-*/",
    "compile_commands.json",
    ".cache/",
    ".aider*",
    "logs/",
]

_DEFAULT_APT_PACKAGES: frozenset[str] = frozenset(
    {
        "libcmocka-dev",
        "libcmocka0",
        "libcriterion-dev",
        "check",
        "libsubunit-dev",
        "libyaml-dev",
        "zlib1g-dev",
        "libbz2-dev",
        "libzstd-dev",
    }
)


def _load_apt_allowlist() -> frozenset[str]:
    """Load apt allowlist from JSON config, falling back to hardcoded default."""
    config_path = Path(__file__).parent / "apt_allowlist_c.json"
    if config_path.exists():
        import json

        with open(config_path) as f:
            data = json.load(f)
        return frozenset(data) | _DEFAULT_APT_PACKAGES
    return _DEFAULT_APT_PACKAGES


ALLOWED_APT_PACKAGES: frozenset[str] = _load_apt_allowlist()


C_SPLIT: Dict[str, list[str]] = {
    "c_lite": ["cJSON"],
}



# The dynamic alias map (resolve_c_split / resolve_c_split_all) lived here
# previously. It now lives in ``commit0.harness.split_utils.resolve_split``
# and works the same for every language.


__all__ = [
    "CLanguage",
    "CRepoInstance",
    "C_SPLIT",
    "C_BASE_BRANCH",
    "C_VERSION",
    "C_SOURCE_EXT",
    "C_HEADER_EXT",
    "C_STUB_MARKER",
    "C_TEST_FILE_GLOBS",
    "C_SKIP_DIRS",
    "C_GITIGNORE_ENTRIES",
    "ALLOWED_APT_PACKAGES",
    "RUN_C_TEST_LOG_DIR",
]
