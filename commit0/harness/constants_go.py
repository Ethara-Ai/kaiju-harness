"""Go-specific constants and data models for commit0 Go integration."""

import os
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

# Go toolchain version. Override via the GO_VERSION env var for a bisect / MSRV
# check (mirrors RUST_VERSION in constants_rust.py). NOTE: unlike RUST_VERSION —
# which is substituted into the base image tag and is therefore the single
# source of truth — the Go base image tag is hardcoded in
# ``dockerfiles/Dockerfile.go`` (``FROM golang:1.25-bookworm``). This constant is
# used only for the (currently non-blocking) health-check version assertion, so
# it MUST be kept in sync with the Dockerfile manually. The env override lets an
# operator point the version check at a different toolchain without editing this
# file.
GO_VERSION = os.environ.get("GO_VERSION", "1.25.0")

# Immutable base image tag. Includes GO_VERSION so a version bump forces a
# fresh base rebuild instead of reusing a stale `:latest` cache (M7 fix).
# Mirrors RUST_BASE_IMAGE_TAG in constants_rust.py.
GO_BASE_IMAGE_TAG = f"commit0.base.go:{GO_VERSION}"
GO_SOURCE_EXT = ".go"
GO_STUB_MARKER = '"STUB: not implemented"'
GO_TEST_FILE_SUFFIX = "_test.go"
GO_SKIP_FILENAMES = ("doc.go",)
RUN_GO_TEST_LOG_DIR = Path("logs/go_test")

# Mirrors RUST_GITIGNORE_ENTRIES in constants_rust.py. Kept as a top-level
# constant so setup_go.py doesn't inline the list, and future changes have a
# single source of truth.
GO_GITIGNORE_ENTRIES = [".aider*", "logs/", "vendor/"]

SOURCE_EXT_MAP = {Language.PYTHON: ".py", Language.GO: ".go"}
STUB_MARKER_MAP = {Language.PYTHON: "    pass", Language.GO: '"STUB: not implemented"'}


__all__ = [
    "Language",
    "GoRepoInstance",
    "GO_SPLIT",
    "GO_VERSION",
    "GO_BASE_IMAGE_TAG",
    "GO_SOURCE_EXT",
    "GO_STUB_MARKER",
    "GO_TEST_FILE_SUFFIX",
    "GO_SKIP_FILENAMES",
    "RUN_GO_TEST_LOG_DIR",
    "GO_GITIGNORE_ENTRIES",
    "SOURCE_EXT_MAP",
    "STUB_MARKER_MAP",
]
