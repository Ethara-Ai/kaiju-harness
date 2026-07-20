"""Guard against the stale-image `base_commit` crash (Java run, all langs).

RCA: a Java run produced 0/0 / $0 at every stage. The agent crashed BEFORE the
first LLM call with GitPython's

    ValueError: SHA 199dcfd2... could not be resolved, git returned: ... missing

Two compounding faults:
  1. run_agent_java looked for the local branch ``commit0_java`` (JAVA_BASE_BRANCH)
     which nothing ever creates (prepare pushes ``commit0_all``), so it always
     fell through to the recorded ``base_commit`` SHA.
  2. Every prepare re-commits stubs+spec, minting a FRESH base_commit. A cached
     agent image (no --rebuild-agent) bakes an OLDER commit0_all tip, so the
     dataset's recorded base_commit is legitimately ABSENT from that image ->
     unconditional deref crashed -> upstream isolated it into a silent green
     0/0 run (the worst outcome).

Fixes under test:
  * shared ``create_branch`` degrades to current HEAD (the baked stub tip) when
    ``from_commit`` is unresolvable, instead of raising (protects python/cpp/rust
    which pass base_commit straight in);
  * run_agent_java resolves the stub base from a robust candidate chain that
    prefers the branch that actually exists (commit0_all) over a fragile SHA.
"""
import subprocess

import git
import pytest

from agent.agent_utils import create_branch


def _new_repo(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    def g(*a):
        subprocess.run(["git", *a], cwd=d, check=True, capture_output=True, text=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (d / "a.txt").write_text("hi")
    g("add", "-A")
    g("commit", "-qm", "c0")
    return git.Repo(d)


def test_create_branch_falls_back_to_head_on_missing_start(tmp_path):
    repo = _new_repo(tmp_path)
    head = repo.head.commit.hexsha
    # a bogus 40-hex SHA that does not exist in the repo (the stale-image case)
    create_branch(repo, "agent-branch", "dead" + "beef" * 9)
    assert repo.active_branch.name == "agent-branch"
    assert repo.head.commit.hexsha == head, "must branch from HEAD when start missing"


def test_create_branch_happy_path_valid_sha(tmp_path):
    repo = _new_repo(tmp_path)
    head = repo.head.commit.hexsha
    create_branch(repo, "b1", head)
    assert repo.active_branch.name == "b1"
    # switching to an already-created branch still works
    create_branch(repo, "b1", "dead" + "beef" * 9)
    assert repo.active_branch.name == "b1"


def test_create_branch_empty_start_does_not_crash(tmp_path):
    # cpp passes example.get("base_commit", "") — an empty start must not crash
    repo = _new_repo(tmp_path)
    create_branch(repo, "b-empty", "")
    assert repo.active_branch.name == "b-empty"


def test_java_resolution_prefers_commit0_all_over_missing_sha(tmp_path):
    """Mirror run_agent_java's candidate chain: commit0_all resolves even when
    commit0_java is absent and the recorded base_commit SHA is missing."""
    from commit0.harness.constants_java import JAVA_BASE_BRANCH, JAVA_REMOTE_BRANCH

    repo = _new_repo(tmp_path)
    repo.git.branch(JAVA_REMOTE_BRANCH)  # create commit0_all at HEAD
    instance = {"base_commit": "dead" + "beef" * 9}  # bogus / stale

    # replicate the exact resolution order used in run_agent_java.run_agent_for_repo
    candidates = [
        (JAVA_BASE_BRANCH, JAVA_BASE_BRANCH in repo.heads),
        (JAVA_REMOTE_BRANCH, True),
        (f"origin/{JAVA_REMOTE_BRANCH}", True),
        (instance.get("base_commit"), True),
        ("HEAD", True),
    ]
    stub_base, resolved_from = None, None
    for cand, eligible in candidates:
        if not cand or not eligible:
            continue
        try:
            stub_base = repo.commit(cand).hexsha
            resolved_from = cand
            break
        except Exception:
            continue
    assert stub_base == repo.head.commit.hexsha
    assert resolved_from == JAVA_REMOTE_BRANCH, resolved_from
