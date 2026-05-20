"""C-specific constants and data models for commit0 C integration."""

from enum import Enum
from pathlib import Path
from typing import Dict

from commit0.harness.constants import RepoInstance


class CLanguage(str, Enum):
    C = "c"


class CRepoInstance(RepoInstance):
    src_dir: str = "."
    language: CLanguage = CLanguage.C
    build_system: str = "cmake"
    test_framework: str = "ctest"


C_BASE_BRANCH = "commit0"
C_VERSION = "18"
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
        "libcheck-dev",
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

C_SPLIT_ALL: list[str] = [r for repos in C_SPLIT.values() for r in repos]


def resolve_c_split(
    dataset_name: str,
    dataset_split: str = "test",
) -> Dict[str, list[str]]:
    """Union the hardcoded ``C_SPLIT`` with aliases auto-derived from the dataset.

    Mirrors ``resolve_go_split``: every dataset entry contributes up to four
    aliases (``instance_id``, ``repo``, ``original_repo``, basename) all mapping
    to ``[repo_basename]``. Hardcoded ``C_SPLIT`` entries win on key collision.
    """
    import json
    import os
    from pathlib import Path

    del dataset_split

    merged: Dict[str, list[str]] = {}

    is_local = dataset_name.endswith(".json") or (
        os.sep in dataset_name and Path(dataset_name).exists()
    )
    if is_local:
        try:
            with open(Path(dataset_name).resolve(), "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None

        if isinstance(data, dict) and "data" in data:
            entries = data["data"]
        elif isinstance(data, list):
            entries = data
        else:
            entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            repo = entry.get("repo") or ""
            if not repo:
                continue
            basename = repo.split("/")[-1]
            aliases = {
                entry.get("instance_id") or "",
                repo,
                entry.get("original_repo") or "",
                basename,
            }
            aliases.discard("")
            for alias in aliases:
                merged.setdefault(alias, [basename])

    for key, value in C_SPLIT.items():
        merged[key] = value

    return merged


def resolve_c_split_all(
    dataset_name: str,
    dataset_split: str = "test",
) -> list[str]:
    """Flat list of repo basenames corresponding to ``resolve_c_split``."""
    merged = resolve_c_split(dataset_name, dataset_split)
    seen: set[str] = set()
    flat: list[str] = []
    for repos in merged.values():
        for repo in repos:
            if repo not in seen:
                seen.add(repo)
                flat.append(repo)
    return flat


__all__ = [
    "CLanguage",
    "CRepoInstance",
    "C_SPLIT",
    "C_SPLIT_ALL",
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
    "resolve_c_split",
    "resolve_c_split_all",
]
