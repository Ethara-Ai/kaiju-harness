"""Go-specific constants and data models for commit0 Go integration."""

from enum import Enum
from pathlib import Path
from typing import Dict

from commit0.harness.constants import RepoInstance


class Language(str, Enum):
    PYTHON = "python"
    GO = "go"


class GoRepoInstance(RepoInstance):
    src_dir: str = "."
    language: Language = Language.GO


# Curated subsets only. The "all" subset (and dataset aliases like
# ``instance_id`` / ``repo`` / basename) are derived dynamically by
# ``commit0.harness.split_utils.resolve_split``.
GO_SPLIT: Dict[str, list[str]] = {}


# The dynamic alias map (resolve_go_split / resolve_go_split_all) lived here
# previously. It now lives in ``commit0.harness.split_utils.resolve_split``
# and works the same for every language.

GO_VERSION = "1.25.0"
GO_SOURCE_EXT = ".go"
GO_STUB_MARKER = '"STUB: not implemented"'
GO_TEST_FILE_SUFFIX = "_test.go"
GO_SKIP_FILENAMES = ("doc.go",)
RUN_GO_TEST_LOG_DIR = Path("logs/go_test")

SOURCE_EXT_MAP = {Language.PYTHON: ".py", Language.GO: ".go"}
STUB_MARKER_MAP = {Language.PYTHON: "    pass", Language.GO: '"STUB: not implemented"'}


__all__ = [
    "Language",
    "GoRepoInstance",
    "GO_SPLIT",
    "GO_VERSION",
    "GO_SOURCE_EXT",
    "GO_STUB_MARKER",
    "GO_TEST_FILE_SUFFIX",
    "GO_SKIP_FILENAMES",
    "RUN_GO_TEST_LOG_DIR",
    "SOURCE_EXT_MAP",
    "STUB_MARKER_MAP",
]
