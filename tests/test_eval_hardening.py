"""Cross-language reward-hacking hardening (commit0.harness.eval_hardening).

The load-bearing test is the all-or-nothing revert fix: a single
`git checkout base -- pathA pathB ...` aborts entirely if ANY path matches
nothing, silently leaving model test edits in place. The per-pathspec revert
must survive missing paths and still restore the tampered test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from commit0.harness.eval_hardening import revert_and_clean_lines


def _run(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def _git_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "t@t.t"], repo)
    _run(["git", "config", "user.name", "t"], repo)
    return repo


def test_helper_emits_independent_reverts_and_cleanup():
    lines = revert_and_clean_lines(
        "abc123", revert_targets=["tests/", "conftest.py"],
        delete_added_globs=["conftest.py", "**/conftest.py"])
    # One independent `git checkout ... || true` per target (root + nested).
    checkouts = [ln for ln in lines if ln.startswith("git checkout")]
    assert len(checkouts) == 4  # 2 targets x (root + nested)
    assert all(ln.endswith("|| true") for ln in checkouts)
    # A delete-added loop guarded by diff-filter=A.
    assert any("diff-filter=A" in ln and "rm -rf" in ln for ln in lines)


def test_nested_false_skips_double_and_magic_pathspecs():
    lines = revert_and_clean_lines(
        "abc123", revert_targets=[":(glob)**/*.test.ts", "test/"], nested=False)
    checkouts = [ln for ln in lines if ln.startswith("git checkout")]
    assert len(checkouts) == 2  # no '**/' doubling when nested=False


def test_all_or_nothing_revert_is_fixed(tmp_path):
    """The critical fix: a revert list containing paths that match nothing must
    still revert the tampered test (the old single-command would abort)."""
    repo = _git_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "src").mkdir()
    (repo / "tests" / "t.txt").write_text("REAL_TEST\n")
    (repo / "src" / "lib.c").write_text("stub\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "base"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    # Model tampers the test (and the repo has NO test/, conftest.py, etc.).
    (repo / "tests" / "t.txt").write_text("CHEATED\n")
    (repo / "src" / "lib.c").write_text("real impl\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "model"], repo)
    head = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    patch = _run(["git", "diff", base, head], repo).stdout
    (repo / "p.diff").write_text(patch)

    # Reproduce the eval: reset base -> apply patch -> revert. The revert list
    # deliberately includes paths that DON'T exist in this repo.
    revert = revert_and_clean_lines(
        base,
        revert_targets=["tests/", "test/", "does_not_exist/", "conftest.py",
                        "pytest.ini", "setup.py"],
        delete_added_globs=["conftest.py", "**/conftest.py"])
    script = "\n".join([
        "set -u",
        f"git reset --hard {base} >/dev/null",
        "git apply --allow-empty p.diff",
        *revert,
    ])
    _run(["bash", "-c", script], repo)

    # tests/ was reverted despite the missing paths; impl kept.
    assert (repo / "tests" / "t.txt").read_text() == "REAL_TEST\n"
    assert (repo / "src" / "lib.c").read_text() == "real impl\n"


def test_newly_added_hook_file_is_deleted(tmp_path):
    """A conftest.py the model ADDS is removed — tested via the REAL eval flow
    where the patch is applied with `git apply` (so the add is UNTRACKED, which
    the old `git diff --diff-filter=A` cleanup silently missed)."""
    repo = _git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "lib.py").write_text("x = 1\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "base"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    # Model branch adds conftest.py + a nested one, then we take the PATCH.
    _run(["git", "checkout", "-q", "-b", "model"], repo)
    (repo / "conftest.py").write_text("def pytest_collection_modifyitems(items): items.clear()\n")
    (repo / "src" / "conftest.py").write_text("x = 1\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "add conftest"], repo)
    head = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    patch = _run(["git", "diff", base, head], repo).stdout
    (repo / "p.diff").write_text(patch)

    revert = revert_and_clean_lines(
        base, revert_targets=["conftest.py"],
        delete_added_globs=["conftest.py", "**/conftest.py"])
    # Reproduce the eval: reset base -> apply patch (untracked adds) -> clean.
    script = "\n".join([f"git reset --hard {base} >/dev/null",
                        "git apply --allow-empty p.diff", *revert])
    _run(["bash", "-c", script], repo)
    assert not (repo / "conftest.py").exists(), "root conftest.py survived"
    assert not (repo / "src" / "conftest.py").exists(), "nested conftest.py survived"


def test_glob_revert_survives_model_added_sibling(tmp_path):
    """A model that tampers foo_test.go AND adds evil_test.go must not defeat the
    revert of foo_test.go. The unquoted `*_test.go` form would shell-expand to
    include evil_test.go and abort the whole checkout."""
    repo = _git_repo(tmp_path)
    (repo / "foo_test.go").write_text("REAL\n")
    (repo / "src").mkdir()
    (repo / "src" / "lib.go").write_text("stub\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "base"], repo)
    base = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()

    _run(["git", "checkout", "-q", "-b", "model"], repo)
    (repo / "foo_test.go").write_text("CHEATED\n")           # tamper existing test
    (repo / "evil_test.go").write_text("fn(){}\n")           # add sibling matching *_test.go
    (repo / "src" / "lib.go").write_text("real impl\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "model"], repo)
    head = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    patch = _run(["git", "diff", base, head], repo).stdout
    (repo / "p.diff").write_text(patch)

    revert = revert_and_clean_lines(
        base, revert_targets=["*_test.go"],
        delete_added_globs=["*_test.go", "**/*_test.go"])
    script = "\n".join([f"git reset --hard {base} >/dev/null",
                        "git apply --allow-empty p.diff", *revert])
    _run(["bash", "-c", script], repo)
    assert (repo / "foo_test.go").read_text() == "REAL\n"    # reverted despite sibling
    assert not (repo / "evil_test.go").exists()              # added sibling deleted
    assert (repo / "src" / "lib.go").read_text() == "real impl\n"  # impl kept
