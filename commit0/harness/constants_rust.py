from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field

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

# Rust toolchain version
RUST_VERSION = "stable"

# Marker used to identify stub functions in Rust source
RUST_STUB_MARKER = 'panic!("STUB: not implemented")'

# Base branch name for Rust repos (mirrors TS_BASE_BRANCH)
RUST_BASE_BRANCH = "commit0"

# Entries to add to .gitignore for Rust repos
RUST_GITIGNORE_ENTRIES = ["target/", ".aider*", "logs/"]

# Repo split mapping for Rust repos. Curated subsets only — the "all" subset is
# derived dynamically from the loaded dataset by
# ``commit0.harness.split_utils.resolve_split``.
RUST_SPLIT: Dict[str, list[str]] = {}

# cargo-nextest version for test execution
CARGO_NEXTEST_VERSION = "0.9.96"

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
