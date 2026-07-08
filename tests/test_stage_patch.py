"""Tests for agent.stage_patch.write_stage_patch — the stage-wise patch.diff."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from git import Repo

from agent.stage_patch import write_stage_patch

logger = logging.getLogger("test_stage_patch")


def _repo(tmp_path) -> Repo:
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    r = Repo(str(d))
    r.git.config("user.email", "t@t.t")
    r.git.config("user.name", "t")
    return r


def test_writes_cumulative_diff(tmp_path):
    r = _repo(tmp_path)
    root = Path(r.working_tree_dir)
    (root / "a.txt").write_text("base\n")
    r.git.add(A=True)
    r.index.commit("base")
    base = r.head.commit.hexsha
    # Two commits on the branch (the stage's progressive work).
    (root / "a.txt").write_text("changed\n")
    (root / "b.txt").write_text("new\n")
    r.git.add(A=True)
    r.index.commit("s1")

    out = tmp_path / "logs"
    out.mkdir()
    write_stage_patch(r, base, out, logger)

    patch = (out / "patch.diff").read_text()
    assert patch.startswith("diff --git")
    assert "changed" in patch and "b.txt" in patch  # cumulative base..HEAD


def test_empty_when_no_changes(tmp_path):
    r = _repo(tmp_path)
    root = Path(r.working_tree_dir)
    (root / "a.txt").write_text("x\n")
    r.git.add(A=True)
    r.index.commit("base")
    base = r.head.commit.hexsha  # HEAD == base -> empty diff

    out = tmp_path / "logs"
    out.mkdir()
    write_stage_patch(r, base, out, logger)
    assert (out / "patch.diff").read_text() == ""


def test_filter_fn_applied(tmp_path):
    r = _repo(tmp_path)
    root = Path(r.working_tree_dir)
    (root / "a.txt").write_text("base\n")
    r.git.add(A=True)
    r.index.commit("base")
    base = r.head.commit.hexsha
    (root / "a.txt").write_text("changed\n")
    r.git.add(A=True)
    r.index.commit("s1")

    out = tmp_path / "logs"
    out.mkdir()
    write_stage_patch(r, base, out, logger, filter_fn=lambda s: "FILTERED")
    # Helper appends a single trailing newline for patch hygiene.
    assert (out / "patch.diff").read_text() == "FILTERED\n"


def test_never_raises_on_bad_repo(tmp_path):
    """Best-effort: a bad base ref logs a warning, doesn't raise, writes nothing."""
    r = _repo(tmp_path)
    root = Path(r.working_tree_dir)
    (root / "a.txt").write_text("x\n")
    r.git.add(A=True)
    r.index.commit("base")
    out = tmp_path / "logs"
    out.mkdir()
    write_stage_patch(r, "deadbeef" * 5, out, logger)  # nonexistent commit
    assert not (out / "patch.diff").exists()  # write skipped, no crash


def test_module_file_patch_scopes_to_own_file(tmp_path):
    """_module_file_patch must contain ONLY the module's own file, even when
    aider bundles several files' edits into one commit window."""
    from agent.run_rust_agent import _module_file_patch
    import subprocess
    d = tmp_path / "repo"; d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    r = Repo(str(d)); r.git.config("user.email", "t@t.t"); r.git.config("user.name", "t")
    (d / "src").mkdir()
    (d / "src/a.rs").write_text("fn a(){ unimplemented!() }\n")
    (d / "src/b.rs").write_text("fn b(){ unimplemented!() }\n")
    r.git.add(A=True); r.index.commit("base"); base = r.head.commit.hexsha
    # one commit touches BOTH files (aider cadence)
    (d / "src/a.rs").write_text("fn a(){ 1 }\n")
    (d / "src/b.rs").write_text("fn b(){ 2 }\n")
    r.git.add(A=True); r.index.commit("bundled"); head = r.head.commit.hexsha

    pa = _module_file_patch(r, base, head, "src/a.rs")
    pb = _module_file_patch(r, base, head, "src/b.rs")
    assert "src/a.rs" in pa and "src/b.rs" not in pa
    assert "src/b.rs" in pb and "src/a.rs" not in pb
    # empty when the file is unchanged from base
    (d / "src/c.rs").write_text("x\n")
    assert _module_file_patch(r, base, head, "src/c.rs") == ""
