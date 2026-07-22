"""Git LFS gating for oversized prepared-repo artifacts (e.g. spec.pdf.bz2).

GitHub HARD-rejects a push that contains any single file **larger than 100 MiB**
(``GH001: Large files detected``). The spec PDF is committed into the branch that
prepare pushes to the fork, so one oversized spec fails the push for the WHOLE
repo. We route ONLY such oversized files through git-lfs.

STRICT gate — the whole point of this module:
  A file is LFS-tracked **iff its size exceeds GitHub's hard per-file push limit**
  (``LFS_THRESHOLD_BYTES``). Anything at or under the limit stays a plain git blob
  — LFS is *never* enabled for a file that would push fine. That keeps git-lfs
  (and its storage/bandwidth quota) out of the common path entirely; it engages
  only when a normal push would actually be rejected.

Consumption side: the base images install git-lfs and run ``git lfs install`` so
the setup-script clone materializes an LFS-pointed spec transparently via the
smudge filter — no explicit ``git lfs pull`` is required.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# GitHub blocks a push if any single file is LARGER THAN 100 MiB (strictly). A
# file of exactly 100 MiB pushes fine. So the LFS gate fires only on `size >`
# this value — never for a file that GitHub would accept. Override the threshold
# via KAIJU_LFS_THRESHOLD_BYTES (e.g. to add a safety margin), but it must never
# be set ABOVE the hard limit or a rejectable file would slip through as a blob.
GITHUB_BLOB_HARD_LIMIT_BYTES = 100 * 1024 * 1024  # 100 MiB = 104_857_600


def _resolve_threshold() -> int:
    raw = os.environ.get("KAIJU_LFS_THRESHOLD_BYTES", "").strip()
    if not raw:
        return GITHUB_BLOB_HARD_LIMIT_BYTES
    try:
        val = int(raw)
    except ValueError:
        logger.warning("KAIJU_LFS_THRESHOLD_BYTES=%r not an int; using default", raw)
        return GITHUB_BLOB_HARD_LIMIT_BYTES
    if val <= 0 or val > GITHUB_BLOB_HARD_LIMIT_BYTES:
        # A threshold above GitHub's hard limit would let a rejectable file push
        # as a plain blob (defeating the point). Clamp to the hard limit.
        logger.warning(
            "KAIJU_LFS_THRESHOLD_BYTES=%s outside (0, %d]; clamping to the hard limit",
            val, GITHUB_BLOB_HARD_LIMIT_BYTES,
        )
        return GITHUB_BLOB_HARD_LIMIT_BYTES
    return val


LFS_THRESHOLD_BYTES = _resolve_threshold()


def file_needs_lfs(path: str | Path) -> bool:
    """True iff *path* is strictly larger than the LFS threshold (i.e. a plain
    push would be rejected). Missing/unstat-able files → False."""
    try:
        return os.path.getsize(path) > LFS_THRESHOLD_BYTES
    except OSError:
        return False


_LFS_AVAILABLE: bool | None = None


def git_lfs_available() -> bool:
    """Whether the ``git lfs`` subcommand works on this host (cached)."""
    global _LFS_AVAILABLE
    if _LFS_AVAILABLE is not None:
        return _LFS_AVAILABLE
    ok = False
    if shutil.which("git-lfs") or shutil.which("git"):
        try:
            r = subprocess.run(
                ["git", "lfs", "version"],
                capture_output=True, text=True, timeout=15,
            )
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _LFS_AVAILABLE = ok
    return ok


class GitLfsUnavailableError(RuntimeError):
    """Raised when a file needs LFS but ``git lfs`` isn't installed."""


def ensure_lfs_tracked(repo_dir: str | Path, filename: str) -> bool:
    """Register *filename* for git-lfs in *repo_dir* (install + track).

    Idempotent. Stages the resulting ``.gitattributes`` so it lands in the same
    commit as the file (git-lfs needs the attribute present to smudge on clone).
    Raises :class:`GitLfsUnavailableError` when git-lfs is missing — the caller's
    push would otherwise be rejected for the whole repo, so we fail LOUD rather
    than silently commit a >100 MiB blob that can never be pushed.
    """
    if not git_lfs_available():
        raise GitLfsUnavailableError(
            f"{filename!r} exceeds GitHub's 100 MiB per-file push limit and must be "
            f"stored via git-lfs, but `git lfs` is not installed on this host. "
            f"Install it (apt-get install git-lfs / brew install git-lfs) and retry."
        )
    repo_dir = str(repo_dir)

    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True, text=True, timeout=60, check=True,
        )

    # Register the LFS filters for THIS repo (local, non-global) and track the
    # file. `git lfs track` writes/updates .gitattributes; we stage it so it is
    # committed alongside the file.
    _run("lfs", "install", "--local")
    _run("lfs", "track", filename)
    _run("add", ".gitattributes")
    logger.info(
        "git-lfs: %s (%.1f MiB) exceeds the %.0f MiB push limit — tracking via LFS",
        filename, os.path.getsize(Path(repo_dir) / filename) / (1024 * 1024),
        LFS_THRESHOLD_BYTES / (1024 * 1024),
    )
    return True


def _named_paths(add_args: tuple[str, ...]) -> list[str]:
    """The concrete file paths in a `git add …` arg list (skip flags/pathspecs)."""
    out = []
    for a in add_args:
        if a.startswith("-") or a in (".", "-A", "--all", "*"):
            continue
        out.append(a)
    return out


def lfs_gate_add(repo_dir: str | Path, add_args: tuple[str, ...]) -> None:
    """Before a ``git add``, route any oversized file through git-lfs.

    Handles two shapes:
      * ``git add <file> …``    — checks each named file.
      * ``git add -A`` / ``.``  — scans the working tree for oversized files.
    A file at or under the threshold is untouched (stays a plain blob).
    """
    repo = Path(repo_dir)
    blanket = any(a in ("-A", "--all", ".") for a in add_args)
    candidates: list[str] = []
    if blanket:
        candidates = _scan_worktree_for_large_files(repo)
    else:
        for name in _named_paths(add_args):
            if file_needs_lfs(repo / name):
                candidates.append(name)
    for rel in candidates:
        try:
            ensure_lfs_tracked(repo, rel)
        except GitLfsUnavailableError:
            raise
        except subprocess.SubprocessError as e:
            logger.warning("git-lfs tracking failed for %s: %s", rel, e)


def _scan_worktree_for_large_files(repo: Path, limit: int = 5000) -> list[str]:
    """Best-effort scan for files strictly over the threshold (skip .git and
    already-LFS-tracked paths). Bounded so a giant tree can't stall prepare."""
    found: list[str] = []
    seen = 0
    for root, dirs, files in os.walk(repo):
        if ".git" in dirs:
            dirs.remove(".git")
        for fn in files:
            seen += 1
            if seen > limit:
                return found
            p = Path(root) / fn
            if file_needs_lfs(p):
                found.append(str(p.relative_to(repo)))
    return found
