"""Git LFS gating for oversized prepared-repo artifacts (spec.pdf.bz2).

Contract:
  * STRICT threshold — a file is LFS-tracked iff it is *strictly larger* than
    GitHub's 100 MiB hard per-file push limit; a file at or under the limit stays
    a plain git blob (LFS never engages unnecessarily).
  * Routed through the single git() choke point, so all 8 languages / all
    spec-commit sites are covered automatically.
  * Fail-loud when git-lfs is missing for a file that needs it.
"""
import importlib
import os
import subprocess

import pytest

import tools._git_lfs as L

_HAVE_LFS = L.git_lfs_available()


def _reload_with_threshold(monkeypatch, n):
    monkeypatch.setenv("KAIJU_LFS_THRESHOLD_BYTES", str(n))
    importlib.reload(L)
    return L


def test_default_threshold_is_github_hard_limit():
    importlib.reload(L)
    assert L.LFS_THRESHOLD_BYTES == 100 * 1024 * 1024  # 100 MiB


def test_threshold_never_exceeds_hard_limit(monkeypatch):
    # a threshold ABOVE the hard limit would let a rejectable file push as a blob
    m = _reload_with_threshold(monkeypatch, 10 * 1024 ** 3)  # 10 GiB
    assert m.LFS_THRESHOLD_BYTES == m.GITHUB_BLOB_HARD_LIMIT_BYTES


def test_strict_gate_boundary(tmp_path, monkeypatch):
    m = _reload_with_threshold(monkeypatch, 1000)
    at = tmp_path / "at"; at.write_bytes(b"x" * 1000)     # == limit
    over = tmp_path / "over"; over.write_bytes(b"x" * 1001)  # > limit
    under = tmp_path / "under"; under.write_bytes(b"x" * 999)
    assert m.file_needs_lfs(under) is False
    assert m.file_needs_lfs(at) is False        # exactly at limit → plain blob
    assert m.file_needs_lfs(over) is True       # strictly over → LFS
    assert m.file_needs_lfs(tmp_path / "missing") is False


@pytest.mark.skipif(not _HAVE_LFS, reason="git-lfs not installed")
def test_git_add_large_file_becomes_lfs_pointer(tmp_path, monkeypatch):
    _reload_with_threshold(monkeypatch, 2048)
    from tools._git_auth import git

    d = tmp_path / "r"; d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)

    # small → plain blob, no .gitattributes
    (d / "small.bz2").write_bytes(b"x" * 100)
    git(d, "add", "small.bz2"); git(d, "commit", "-m", "small")
    tracked = subprocess.run(["git", "-C", str(d), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    assert ".gitattributes" not in tracked

    # large → LFS pointer + .gitattributes
    (d / "spec.pdf.bz2").write_bytes(b"x" * 5000)
    git(d, "add", "spec.pdf.bz2"); git(d, "commit", "-m", "Add spec PDF")
    tracked = subprocess.run(["git", "-C", str(d), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    assert ".gitattributes" in tracked and "spec.pdf.bz2" in tracked
    blob = subprocess.run(["git", "-C", str(d), "show", "HEAD:spec.pdf.bz2"],
                          capture_output=True, text=True).stdout
    assert blob.startswith("version https://git-lfs"), "large file must be an LFS pointer"
    lsf = subprocess.run(["git", "-C", str(d), "lfs", "ls-files"],
                         capture_output=True, text=True).stdout
    assert "spec.pdf.bz2" in lsf


def test_ensure_lfs_tracked_fails_loud_without_git_lfs(tmp_path, monkeypatch):
    # simulate git-lfs missing → must raise, not silently commit an unpushable blob
    monkeypatch.setattr(L, "git_lfs_available", lambda: False)
    d = tmp_path / "r"; d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "spec.pdf.bz2").write_bytes(b"x" * 10)
    with pytest.raises(L.GitLfsUnavailableError):
        L.ensure_lfs_tracked(d, "spec.pdf.bz2")


def test_small_add_untouched_when_lfs_missing(tmp_path, monkeypatch):
    # a normal small add must NOT be affected by lfs logic even if git-lfs is absent
    monkeypatch.setattr(L, "git_lfs_available", lambda: False)
    _reload_with_threshold(monkeypatch, 100 * 1024 * 1024)
    from tools._git_auth import git
    d = tmp_path / "r"; d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "src.py").write_text("x = 1\n")
    git(d, "add", "-A"); git(d, "commit", "-m", "Commit 0")  # must not raise
    tracked = subprocess.run(["git", "-C", str(d), "ls-files"],
                             capture_output=True, text=True).stdout
    assert "src.py" in tracked
