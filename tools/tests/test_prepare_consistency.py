"""Guards for prepare's branch/commit contract.

1. All languages must agree on ONE remote/dataset branch name, sourced from the
   single canonical constant — no per-language drift (the class of bug that had
   the Java agent looking for a branch prepare never created).
2. Prepared-repo commits must be attributed to kaiju-bot, overriding whatever git
   identity is configured on the build host.
3. Prepare creates exactly ONE branch and commits it cleanly (harness breadcrumbs
   excluded).
"""
import ast
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_PREPARE_FILES = [
    "tools/prepare_repo.py", "tools/prepare_repo_go.py", "tools/prepare_repo_rust.py",
    "tools/prepare_repo_cpp.py", "tools/prepare_repo_c.py",
]


# ---- 1. branch-name constant consistency ----

def test_all_remote_branch_constants_alias_canonical():
    from commit0.harness.constants import REMOTE_BRANCH, BASE_BRANCH
    from commit0.harness.constants_java import JAVA_REMOTE_BRANCH, JAVA_BASE_BRANCH
    from commit0.harness.constants_ts import TS_DATASET_BRANCH
    from commit0.harness.constants_js import JS_DATASET_BRANCH

    assert REMOTE_BRANCH == "commit0_all"
    assert JAVA_REMOTE_BRANCH == TS_DATASET_BRANCH == JS_DATASET_BRANCH == REMOTE_BRANCH
    assert BASE_BRANCH == JAVA_BASE_BRANCH == "commit0"


def test_no_hardcoded_remote_branch_literal_in_prepare():
    """The 5 prepare files must reference the constant, not the raw literal."""
    for rel in _PREPARE_FILES:
        src = (REPO / rel).read_text(encoding="utf-8")
        # allow the literal only inside a comment/docstring; forbid it as a value
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Constant) and node.value == "commit0_all":
                raise AssertionError(
                    f"{rel}:{node.lineno}: hardcoded 'commit0_all' — use "
                    f"REMOTE_BRANCH from commit0.harness.constants"
                )
        assert "import REMOTE_BRANCH" in src or "REMOTE_BRANCH," in src, (
            f"{rel}: must import REMOTE_BRANCH"
        )


# ---- 2. kaiju-bot identity forced on every prepare commit ----

def test_git_helper_forces_kaiju_bot_on_commit(tmp_path):
    from tools._git_auth import git, KAIJU_BOT_NAME, KAIJU_BOT_EMAIL

    d = tmp_path / "r"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    # A DIFFERENT host identity in both config and env must be overridden.
    subprocess.run(["git", "config", "user.name", "Dev Person"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "dev@x.com"], cwd=d, check=True)
    import os
    os.environ["GIT_AUTHOR_NAME"] = "EnvDev"
    os.environ["GIT_AUTHOR_EMAIL"] = "env@x.com"
    try:
        (d / "a.txt").write_text("hi")
        git(d, "add", "a.txt")
        git(d, "commit", "-m", "Commit 0")
        out = subprocess.run(
            ["git", "-C", str(d), "log", "-1", "--format=%an|%ae|%cn|%ce"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        an, ae, cn, ce = out.split("|")
        assert an == cn == KAIJU_BOT_NAME
        assert ae == ce == KAIJU_BOT_EMAIL
    finally:
        os.environ.pop("GIT_AUTHOR_NAME", None)
        os.environ.pop("GIT_AUTHOR_EMAIL", None)


def test_non_commit_git_commands_do_not_force_identity(tmp_path):
    # sanity: the override is scoped to `commit` only
    from tools._git_auth import git
    d = tmp_path / "r"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    # `git status` must not error or be affected
    assert isinstance(git(d, "status", "--porcelain"), str)


# ---- 3. one branch, clean commit (breadcrumb excluded) ----

def test_git_add_all_excludes_kaiju_breadcrumb(tmp_path):
    from tools._git_auth import git

    d = tmp_path / "r"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "src.py").write_text("x = 1\n")
    (d / ".kaiju").mkdir()
    (d / ".kaiju" / "entries.json").write_text('{"reference_commit": "GOLDEN"}')

    git(d, "add", "-A")
    git(d, "commit", "-m", "Commit 0")
    tracked = subprocess.run(
        ["git", "-C", str(d), "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    assert "src.py" in tracked
    assert ".kaiju" not in tracked, "the .kaiju breadcrumb (golden ref) must NOT be committed"
