"""#5 — sandbox hardening helpers (config, not verification checks).

Two anti-retrieval defenses the harness applies at prepare/eval time:
  * NETWORK_OFF_ARGS — deny egress on the agent+eval containers so the fix can't be
    fetched (research: the GitHub-API retrieval channel). Add to the `docker run`.
  * git_strip_history — reduce the repo's git so the golden solution isn't reachable
    from the container (SWE-bench Pro leaked future commits/tags/reflogs). Removes all
    refs except the working branch, tags, remotes and stash, expires the reflog, and
    gc-prunes unreachable objects — so the reference_commit (not an ancestor of the
    working HEAD) is pruned and `git cat-file` can no longer surface it.

The hash-tripwire part of Tier 5 is delivered by `SIDE_CHANNEL_CLEAN`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# Add to `docker run` for the agent + eval containers (loopback only). Dependency
# installs must be vendored or use a registry-only proxy when this is on.
NETWORK_OFF_ARGS = ["--network=none"]


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=120, check=check)


def _current_branch(repo: Path) -> str | None:
    r = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return r.stdout.strip() or None


def git_strip_history(repo_dir: str | Path) -> bool:
    """Strip everything that could surface the golden from the container's git.
    Returns True on success. Best-effort and idempotent."""
    repo = Path(repo_dir)
    if not (repo / ".git").exists():
        return False
    keep = _current_branch(repo)
    # 1) delete every ref except the working branch (other branches, tags, remotes, stash)
    r = _git(repo, "for-each-ref", "--format=%(refname)")
    for ref in r.stdout.splitlines():
        ref = ref.strip()
        if not ref:
            continue
        if keep and ref == f"refs/heads/{keep}":
            continue
        _git(repo, "update-ref", "-d", ref)
    _git(repo, "stash", "clear")
    # 2) drop packed refs remnants, expire reflog, prune unreachable objects
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "--prune=now")
    return True


def is_object_reachable(repo_dir: str | Path, sha: str) -> bool:
    """True iff *sha* still exists as a reachable object (used to assert a strip)."""
    r = _git(Path(repo_dir), "cat-file", "-e", f"{sha}^{{commit}}")
    return r.returncode == 0
