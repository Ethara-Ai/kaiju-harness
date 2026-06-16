from pathlib import Path
import os
from typing import Dict, List

from pydantic import Field

from commit0.harness.constants import (
    DOCKERFILES_DIR,
    RepoInstance,
    TestStatus,
)

__all__ = [
    "RustRepoInstance",
    "RUST_VERSION",
    "RUST_STUB_MARKER",
    "RUST_SPLIT",
    "RUST_BASE_BRANCH",
    "RUST_GITIGNORE_ENTRIES",
    "CARGO_NEXTEST_VERSION",
    "RUN_RUST_TESTS_LOG_DIR",
    "RUST_TEST_IDS_DIR",
    "DOCKERFILES_RUST_DIR",
    "DOCKERFILES_DIR",
    "TestStatus",
]

# Rust toolchain version. Pinned for reproducibility across runs.
# Override via the RUST_VERSION environment variable when needed (e.g. for a
# bisect, MSRV check, or nightly-only feature). The default below should match
# the version installed in the Rust base Dockerfile to avoid silent drift.
RUST_VERSION = os.environ.get("RUST_VERSION", "1.84.0")

# Marker used to identify stub functions in Rust source
RUST_STUB_MARKER = 'panic!("STUB: not implemented")'

# Base branch name for Rust repos (mirrors TS_BASE_BRANCH)
RUST_BASE_BRANCH = "commit0"

# Entries to add to .gitignore for Rust repos
RUST_GITIGNORE_ENTRIES = ["target/", ".aider*", "logs/"]

# Curated Rust repo splits.
#
# This dict is populated at runtime by dataset loaders (see commit0/cli_rust.py
# and tools/prepare_repo_rust.py) rather than at import time, so it is empty
# here by design. To discover the active splits, inspect the dataset JSON's
# ``repo_split`` field or call ``commit0.harness.split_utils.resolve_split``.
# The literal "all" split is also derived dynamically from the loaded dataset.
RUST_SPLIT: Dict[str, list[str]] = {}

# cargo-nextest version for test execution. Override via the
# CARGO_NEXTEST_VERSION env var to pick up format or behaviour changes in newer
# nextest releases (the JSON event schema may change between minor versions).
CARGO_NEXTEST_VERSION = os.environ.get("CARGO_NEXTEST_VERSION", "0.9.96")

# Log directory for Rust test runs
RUN_RUST_TESTS_LOG_DIR = Path("logs/rust_tests")

# Directory containing per-repo Rust test IDs
RUST_TEST_IDS_DIR = Path(__file__).parent.parent / "data" / "rust_test_ids"

# Directory containing Rust Dockerfile templates
DOCKERFILES_RUST_DIR = Path(__file__).parent / "dockerfiles"


class RustRepoInstance(RepoInstance):
    """Repo instance with Rust-specific metadata."""

    edition: str = "2021"
    features: List[str] = Field(default_factory=list)
    workspace: bool = False
