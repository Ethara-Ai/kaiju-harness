"""Tests for the LOCAL_INPLACE execution backend (git-worktree eval).

These validate the load-bearing guarantees of fully-containerized inference:

1. Reward-hacking is still neutralized — a model that tampers with test/manifest
   files has those reverted before scoring, exactly as the Docker backend does,
   because LocalInplace runs the *same* eval.sh (reset -> apply patch -> revert
   tests -> cheat-guard) in an isolated worktree.
2. The agent's LIVE checkout is never disturbed — the worktree is separate, so
   `git reset --hard base` during eval cannot clobber the model's branch/working
   tree.
3. Result artifacts (exit code, test output) are collected into log_dir.
4. Worktrees are cleaned up (no leaks in the repo's worktree registry).

The tests build a real git repo and a real RustSpec, but substitute a trivial
shell `test_cmd` for `cargo test` so they run without a Rust toolchain — the
reconstruction logic under test is identical regardless of the test command.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import git

from commit0.harness.spec_rust import RustSpec
from commit0.harness.execution_context import LocalInplace
from commit0.harness.constants import Files
from commit0.harness.utils import generate_patch_between_commits


logger = logging.getLogger("test_local_inplace")


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "t@t.t"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)


def _commit_all(repo: Path, msg: str) -> str:
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", msg], repo)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _build_spec(repo: Path, base_commit: str,
                test_cmd: str = "bash tests/check.sh") -> RustSpec:
    """A RustSpec pointed at the real temp repo, with a shell test_cmd.

    `test_cmd` runs the repo's own `tests/check.sh`, which is the REAL test. A
    reward-hacking model edits check.sh to always pass; the eval's test-revert
    must restore the real check.sh before running it.
    """
    instance = {
        "instance_id": "acme/widget",
        "repo": "acme/widget",
        "base_commit": base_commit,
        "reference_commit": base_commit,
        "test": {"test_cmd": test_cmd, "test_dir": "tests"},
    }
    # repo_directory points at the real temp repo (not /testbed) so the worktree
    # is created from it; absolute=True keeps the /patch.diff path that
    # LocalInplace rewrites to a scratch file.
    return RustSpec(
        repo="acme/widget",
        repo_directory=str(repo),
        instance=instance,  # type: ignore[arg-type]
        absolute=True,
    )


def _prepare_files(tmp: Path, repo: Path, spec: RustSpec, base: str, head: str) -> Files:
    local_repo = git.Repo(str(repo))
    patch = generate_patch_between_commits(local_repo, base, head)
    local_repo.close()
    patch_file = tmp / "patch.diff"
    patch_file.write_text(patch, encoding="utf-8", errors="surrogateescape")
    eval_file = tmp / "eval.sh"
    eval_file.write_text(spec.eval_script.replace("__TEST_IDS__", ""), encoding="utf-8")
    return Files(
        eval_script={"src": eval_file, "dest": Path("/eval.sh")},
        patch={"src": patch_file, "dest": Path("/patch.diff")},
    )


def _exit_code(log_dir: Path) -> int:
    return int((log_dir / "cargo_test_exit_code.txt").read_text().strip())


def test_reward_hack_test_tamper_is_reverted(tmp_path):
    """Model leaves src broken but rewrites the test to pass -> eval reverts the
    test and the run FAILS (cheat neutralized)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "lib.rs").write_text("FAIL\n")  # stubbed/broken impl
    (repo / "tests" / "check.sh").write_text("#!/bin/bash\ngrep -q PASS src/lib.rs\n")
    base = _commit_all(repo, "base")

    # Cheat branch: DON'T fix src; instead make the test always pass.
    _run(["git", "checkout", "-q", "-b", "model"], repo)
    (repo / "tests" / "check.sh").write_text("#!/bin/bash\nexit 0\n")
    head = _commit_all(repo, "cheat")

    spec = _build_spec(repo, base)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, head)

    with LocalInplace(spec, logger, 60, 1, log_dir, files,
                      ["cargo_test_exit_code.txt", "test_output.txt"]) as ctx:
        ctx.exec_run_with_timeout("/bin/bash /eval.sh")

    # Test was reverted to the real check -> src still FAIL -> nonzero exit.
    assert _exit_code(log_dir) != 0


def test_legit_fix_passes_and_live_repo_untouched(tmp_path):
    """Model fixes src, doesn't touch tests -> eval PASSES, and the live repo is
    left exactly as the agent had it (worktree isolation)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "lib.rs").write_text("FAIL\n")
    (repo / "tests" / "check.sh").write_text("#!/bin/bash\ngrep -q PASS src/lib.rs\n")
    base = _commit_all(repo, "base")

    _run(["git", "checkout", "-q", "-b", "model"], repo)
    (repo / "src" / "lib.rs").write_text("PASS\n")  # the real fix
    # Also leave an uncommitted scratch edit to prove the worktree doesn't touch it.
    (repo / "src" / "scratch.rs").write_text("wip\n")
    head = _commit_all(repo, "fix")
    (repo / "src" / "scratch.rs").write_text("wip-uncommitted\n")

    spec = _build_spec(repo, base)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, head)

    with LocalInplace(spec, logger, 60, 1, log_dir, files,
                      ["cargo_test_exit_code.txt", "test_output.txt"]) as ctx:
        ctx.exec_run_with_timeout("/bin/bash /eval.sh")

    assert _exit_code(log_dir) == 0

    # Live repo untouched: still on branch model, src fixed, uncommitted scratch intact.
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "model"
    assert (repo / "src" / "lib.rs").read_text() == "PASS\n"
    assert (repo / "src" / "scratch.rs").read_text() == "wip-uncommitted\n"


def test_worktree_is_cleaned_up(tmp_path):
    """After the context exits, no worktree entry leaks in the repo registry and
    the scratch root is gone."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "lib.rs").write_text("PASS\n")
    (repo / "tests" / "check.sh").write_text("#!/bin/bash\ngrep -q PASS src/lib.rs\n")
    base = _commit_all(repo, "base")
    _run(["git", "checkout", "-q", "-b", "model"], repo)
    head = _commit_all(repo, "noop") if False else base  # no-op; reuse base

    spec = _build_spec(repo, base)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, head)

    work_root = None
    with LocalInplace(spec, logger, 60, 1, log_dir, files,
                      ["cargo_test_exit_code.txt"]) as ctx:
        work_root = ctx.work_root
        ctx.exec_run_with_timeout("/bin/bash /eval.sh")

    assert not Path(work_root).exists()
    wt = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                        capture_output=True, text=True, check=True).stdout
    # Only the main worktree should remain.
    assert wt.strip().count("\n") == 0


def test_timeout_is_reported(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "lib.rs").write_text("PASS\n")
    (repo / "tests" / "check.sh").write_text("#!/bin/bash\nsleep 30\n")
    base = _commit_all(repo, "base")

    spec = _build_spec(repo, base)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, base)

    with LocalInplace(spec, logger, 1, 1, log_dir, files, []) as ctx:
        _out, timed_out, _rt = ctx.exec_run_with_timeout("/bin/bash /eval.sh")

    assert timed_out is True


# --------------------------------------------------------------------------
# Reward-hacking hardening: in-src #[cfg(test)] restore + build.rs deletion.
# The test_cmd extracts the expected value from the (post-eval) in-src test and
# the actual from the impl and compares — simulating `cargo test` without cargo.
# --------------------------------------------------------------------------

_INSRC_CMP = (
    "bash -c '"
    "exp=$(grep -oE \"f\\(\\), [0-9]+\" src/lib.rs | grep -oE \"[0-9]+\" | head -1); "  # from the in-src test
    "act=$(grep -oE \"return [0-9]+\" src/lib.rs | grep -oE \"[0-9]+\" | head -1); "     # from the impl
    "[ \"$exp\" = \"$act\" ]'"
)


def _lib_with_test(impl_val: int, assert_val: int) -> str:
    return (
        f"pub fn f() -> i32 {{ return {impl_val}; }}\n\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "    #[test]\n"
        f"    fn t() {{ assert_eq!(f(), {assert_val}); }}\n"
        "}\n"
    )


def test_insrc_test_tamper_is_restored(tmp_path):
    """Model leaves impl wrong (returns 0) but weakens the IN-SRC test to expect 0.
    The eval must restore the test module to base (expects 42) -> mismatch -> FAIL."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()  # present so the tests/ revert pathspec matches
    (repo / "tests" / ".keep").write_text("")
    (repo / "src" / "lib.rs").write_text(_lib_with_test(impl_val=0, assert_val=42))
    base = _commit_all(repo, "base")

    _run(["git", "checkout", "-q", "-b", "model"], repo)
    # Cheat: don't fix f (still 0), weaken the in-src test to assert 0.
    (repo / "src" / "lib.rs").write_text(_lib_with_test(impl_val=0, assert_val=0))
    head = _commit_all(repo, "cheat")

    spec = _build_spec(repo, base, test_cmd=_INSRC_CMP)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, head)
    with LocalInplace(spec, logger, 60, 1, log_dir, files,
                      ["cargo_test_exit_code.txt", "test_output.txt"]) as ctx:
        ctx.exec_run_with_timeout("/bin/bash /eval.sh")
    # Restored test asserts 42 vs impl 0 -> nonzero exit (cheat neutralized).
    assert _exit_code(log_dir) != 0


def test_insrc_legit_fix_passes(tmp_path):
    """Model really fixes f to 42, doesn't touch the test -> PASS after restore."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / ".keep").write_text("")
    (repo / "src" / "lib.rs").write_text(_lib_with_test(impl_val=0, assert_val=42))
    base = _commit_all(repo, "base")

    _run(["git", "checkout", "-q", "-b", "model"], repo)
    (repo / "src" / "lib.rs").write_text(_lib_with_test(impl_val=42, assert_val=42))
    head = _commit_all(repo, "fix")

    spec = _build_spec(repo, base, test_cmd=_INSRC_CMP)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, head)
    with LocalInplace(spec, logger, 60, 1, log_dir, files,
                      ["cargo_test_exit_code.txt", "test_output.txt"]) as ctx:
        ctx.exec_run_with_timeout("/bin/bash /eval.sh")
    assert _exit_code(log_dir) == 0


def test_model_added_build_rs_is_removed(tmp_path):
    """A build.rs the model ADDS (didn't exist at base) must be deleted before tests."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / ".keep").write_text("")
    (repo / "src" / "lib.rs").write_text("pub fn f() -> i32 { return 42; }\n")
    base = _commit_all(repo, "base")

    _run(["git", "checkout", "-q", "-b", "model"], repo)
    (repo / "build.rs").write_text('fn main() { println!("evil build script"); }\n')
    head = _commit_all(repo, "add-build-rs")

    # test_cmd asserts build.rs is ABSENT (proves it was removed by the eval).
    spec = _build_spec(repo, base, test_cmd="bash -c '! test -e build.rs'")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    files = _prepare_files(tmp_path, repo, spec, base, head)
    with LocalInplace(spec, logger, 60, 1, log_dir, files,
                      ["cargo_test_exit_code.txt"]) as ctx:
        ctx.exec_run_with_timeout("/bin/bash /eval.sh")
    assert _exit_code(log_dir) == 0  # build.rs gone -> `! test -e build.rs` succeeds
