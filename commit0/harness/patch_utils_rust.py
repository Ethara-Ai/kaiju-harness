"""Rust-specific patch handling utilities.

Wraps the generic patch generation from utils.py with Rust-specific
filtering: excludes ``target/`` build artifacts and ensures ``Cargo.lock``
is included for reproducibility.
"""

import logging
import re

import git

from commit0.harness.utils import generate_patch_between_commits

logger = logging.getLogger(__name__)


class InvalidRustPatchError(Exception):
    """Raised when a generated Rust patch fails post-filter validation.

    Indicates a regression in :func:`_filter_target_dir` or an upstream change
    to git diff output that lets ``target/`` artefacts through. The raw patch
    text is preserved on the exception so callers can persist it for postmortem.
    """

    def __init__(self, patch: str, message: str = "patch contains target/ artefacts") -> None:
        super().__init__(message)
        self.patch = patch


def generate_rust_patch(
    repo_dir: str,
    base_commit: str,
    target_commit: str,
    *,
    strict: bool = False,
) -> str:
    """Generate a patch between two commits with Rust-specific filtering.

    Calls :func:`generate_patch_between_commits` and post-processes the
    result to strip ``target/`` directory changes while preserving
    ``Cargo.lock`` diffs.

    Parameters
    ----------
    repo_dir : str
        Path to the local git repository.
    base_commit : str
        The old commit hash or reference.
    target_commit : str
        The new commit hash or reference.
    strict : bool, keyword-only, default False
        When True, raise :class:`InvalidRustPatchError` if post-filter
        validation detects ``target/`` residue. When False (default,
        preserves legacy behaviour), log a warning and return the patch
        unchanged so callers can decide how to react.

    Returns
    -------
    str
        Filtered patch string.

    Raises
    ------
    InvalidRustPatchError
        When *strict* is True and validation fails. The raw filtered
        patch is preserved on the exception for postmortem inspection.

    """
    repo = git.Repo(repo_dir)
    raw_patch = generate_patch_between_commits(repo, base_commit, target_commit)
    filtered = _filter_target_dir(raw_patch)
    if not validate_rust_patch(filtered):
        msg = (
            f"Rust patch validation failed for repo_dir={repo_dir!r}: "
            "_filter_target_dir let target/ artefacts through. This indicates "
            "a regression in the filter or an upstream change to git diff output."
        )
        if strict:
            raise InvalidRustPatchError(filtered, msg)
        logger.warning("%s (strict=False, returning patch as-is)", msg)
    return filtered


def validate_rust_patch(patch_content: str) -> bool:
    """Validate that a patch does not contain ``target/`` artifacts.

    Checks
    ------
    * No diff headers referencing paths under ``target/``.
    * No binary blob markers from build artifacts inside ``target/``.

    Parameters
    ----------
    patch_content : str
        The unified-diff patch text to validate.

    Returns
    -------
    bool
        ``True`` if the patch is clean, ``False`` otherwise.

    """
    for line in patch_content.splitlines():
        if line.startswith("diff --git") and "/target/" in line:
            return False
        if re.match(r"^(\+\+\+|---)\s+(a|b)/target/", line):
            return False
        if line.startswith("Binary files") and "/target/" in line:
            return False
    return True


def _filter_target_dir(patch: str) -> str:
    """Remove hunks that belong to the ``target/`` directory.

    Splits the patch into per-file sections (delimited by
    ``diff --git ...`` lines) and drops any section whose path falls
    under ``target/``.

    ``Cargo.lock`` is never filtered out.
    """
    if not patch.strip():
        return patch

    sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)

    kept: list[str] = []
    for section in sections:
        if not section.strip():
            continue
        if _section_is_target(section):
            continue
        kept.append(section)

    return "".join(kept) if kept else "\n\n"


def _section_is_target(section: str) -> bool:
    """Return True if *section* modifies a path under ``target/``."""
    first_line = section.split("\n", 1)[0]
    if not first_line.startswith("diff --git"):
        return False
    # "diff --git a/<path> b/<path>" — check both a/ and b/ paths
    return bool(re.search(r"\s[ab]/target/", first_line))
