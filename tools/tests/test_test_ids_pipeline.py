"""Unit tests for the new ``--repo-dir`` pipeline behavior.

Covers helpers added in response to ``MISSING_TEST_IDS_BZ2_ISSUE.md`` /
Oracle review:

* ``_discover_breadcrumb`` / ``_resolve_repo_dir_args`` (P0-A)
* ``_ReferenceCommitCheckout`` (P0-B)
* ``_write_status_json`` (P1-B)
* ``UvRuntime`` system-deps pre-flight short-circuit (P1-A)
* ``tools.prepare_repo._write_kaiju_breadcrumb`` (P0-C)

Tests that need ``git`` use real ``git init`` + commits in a tmp_path because
mocking subprocess for the context-manager paths is too brittle.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


from tools.generate_test_ids import (
    _ReferenceCommitCheckout,
    _discover_breadcrumb,
    _resolve_repo_dir_args,
    _write_status_json,
)
from tools.prepare_repo import _write_kaiju_breadcrumb
from tools.python_runtime import (
    TestCollectionResult,
    TestCollectionStatus,
    classify_failure,
    find_docker_image_for_repo,
)


# ---------------------------------------------------------------------------
# Docker image selection: prefer the REPO image over the AGENT image
# ---------------------------------------------------------------------------


class _FakeImage:
    def __init__(self, tags: list[str]) -> None:
        self.tags = tags


def _patch_docker(monkeypatch, tags: list[str]) -> None:
    """Patch ``docker.from_env`` to return a client whose ``images.list()``
    yields one image per tag in ``tags`` (in the given order)."""
    import sys
    import types

    images_ns = types.SimpleNamespace(list=lambda: [_FakeImage([t]) for t in tags])
    client = types.SimpleNamespace(images=images_ns)
    fake_docker = types.SimpleNamespace(from_env=lambda: client)
    monkeypatch.setitem(sys.modules, "docker", fake_docker)


class TestFindDockerImageForRepo:
    def test_prefers_repo_image_over_agent(self, monkeypatch) -> None:
        # Agent image listed FIRST — must still pick the repo image, because the
        # agent image's /opt/kaiju/.venv lacks the repo's deps (essentials) and
        # collects zero tests.
        _patch_docker(
            monkeypatch,
            [
                "commit0.repo.blacksheep.3da1597-agent.cc8664:v0",
                "commit0.repo.blacksheep.3da1597:v0",
            ],
        )
        got = find_docker_image_for_repo("Aman-Yadav-Ethara-AI/BlackSheep")
        assert got == "commit0.repo.blacksheep.3da1597:v0"

    def test_agent_image_only_as_last_resort(self, monkeypatch) -> None:
        _patch_docker(
            monkeypatch, ["commit0.repo.blacksheep.3da1597-agent.cc8664:v0"]
        )
        got = find_docker_image_for_repo("x/BlackSheep")
        assert got == "commit0.repo.blacksheep.3da1597-agent.cc8664:v0"

    def test_no_match_returns_none(self, monkeypatch) -> None:
        _patch_docker(monkeypatch, ["commit0.repo.other.deadbeef:v0"])
        assert find_docker_image_for_repo("x/BlackSheep") is None


# ---------------------------------------------------------------------------
# P0-C: breadcrumb writer
# ---------------------------------------------------------------------------


class TestWriteKaijuBreadcrumb:
    def test_basic_write(self, tmp_path: Path) -> None:
        target = _write_kaiju_breadcrumb(
            repo_dir=tmp_path,
            full_name="acme/widget",
            reference_commit="abc123",
            setup_dict={"python": "3.11", "install": "pip install ."},
            test_dict={"test_dir": "tests", "test_cmd": "pytest"},
        )
        assert target == tmp_path / ".kaiju" / "entries.json"
        assert target.is_file()
        data = json.loads(target.read_text())
        assert data["repo"] == "acme/widget"
        assert data["reference_commit"] == "abc123"
        assert data["setup"]["python"] == "3.11"
        assert data["test"]["test_dir"] == "tests"
        assert data["_schema"] == "kaiju-breadcrumb/1"

    def test_unwritable_dir_returns_none(self, tmp_path: Path) -> None:
        # Make parent a file (not a dir) so .kaiju subdir creation fails
        target_parent = tmp_path / "not_a_dir"
        target_parent.write_text("x")
        result = _write_kaiju_breadcrumb(
            repo_dir=target_parent,
            full_name="x/y",
            reference_commit="aaa",
            setup_dict={},
            test_dict={},
        )
        assert result is None


# ---------------------------------------------------------------------------
# P0-A: breadcrumb discovery + resolution
# ---------------------------------------------------------------------------


class TestDiscoverBreadcrumb:
    def test_primary_breadcrumb_path(self, tmp_path: Path) -> None:
        kaiju = tmp_path / ".kaiju"
        kaiju.mkdir()
        (kaiju / "entries.json").write_text(
            json.dumps({"setup": {"python": "3.10"}, "test": {"test_dir": "src/tests"}})
        )
        data = _discover_breadcrumb(tmp_path, name="x", output_dir=None, explicit=None)
        assert data is not None
        assert data["setup"]["python"] == "3.10"

    def test_explicit_override_wins(self, tmp_path: Path) -> None:
        other = tmp_path / "other.json"
        other.write_text(json.dumps({"setup": {"python": "3.12"}, "test": {}}))
        kaiju = tmp_path / ".kaiju"
        kaiju.mkdir()
        (kaiju / "entries.json").write_text(
            json.dumps({"setup": {"python": "3.9"}, "test": {}})
        )
        data = _discover_breadcrumb(
            tmp_path, name="x", output_dir=None, explicit=other
        )
        assert data["setup"]["python"] == "3.12"  # explicit wins

    def test_output_dir_convention(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        out = tmp_path / "out"
        out.mkdir()
        (out / "myname_entries.json").write_text(
            json.dumps({"setup": {"python": "3.11"}, "test": {"test_dir": "t"}})
        )
        data = _discover_breadcrumb(repo, name="myname", output_dir=out, explicit=None)
        assert data is not None
        assert data["setup"]["python"] == "3.11"

    def test_parent_dir_fallback(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "myname_entries.json").write_text(
            json.dumps({"setup": {"python": "3.11"}, "test": {}})
        )
        data = _discover_breadcrumb(repo, name="myname", output_dir=None, explicit=None)
        assert data is not None

    def test_raw_list_format_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "entries.json")  # parent path
        parent = tmp_path / "parent"
        parent.mkdir()
        repo = parent / "repo"
        repo.mkdir()
        (parent / "entries.json").write_text(
            json.dumps([{"setup": {"python": "3.10"}, "test": {}}])
        )
        data = _discover_breadcrumb(repo, name=None, output_dir=None, explicit=None)
        assert data is not None
        assert data["setup"]["python"] == "3.10"

    def test_invalid_json_skipped(self, tmp_path: Path) -> None:
        kaiju = tmp_path / ".kaiju"
        kaiju.mkdir()
        (kaiju / "entries.json").write_text("not json")
        assert _discover_breadcrumb(tmp_path, name=None, output_dir=None, explicit=None) is None

    def test_no_breadcrumb_returns_none(self, tmp_path: Path) -> None:
        assert _discover_breadcrumb(tmp_path, name=None, output_dir=None, explicit=None) is None


class TestResolveRepoDirArgs:
    def test_cli_overrides_breadcrumb(self, tmp_path: Path) -> None:
        kaiju = tmp_path / ".kaiju"
        kaiju.mkdir()
        (kaiju / "entries.json").write_text(
            json.dumps(
                {
                    "setup": {"python": "3.10"},
                    "test": {"test_dir": "tests"},
                    "reference_commit": "deadbeef",
                }
            )
        )
        py, td, ref, _bc = _resolve_repo_dir_args(
            repo_dir=tmp_path,
            name="x",
            cli_python_version="3.12",
            cli_test_dir="custom_tests",
            cli_reference_commit="cafebabe",
            cli_entries_json=None,
            output_dir=tmp_path / "out",
        )
        assert py == "3.12"
        assert td == "custom_tests"
        assert ref == "cafebabe"

    def test_breadcrumb_fills_in_when_cli_missing(self, tmp_path: Path) -> None:
        kaiju = tmp_path / ".kaiju"
        kaiju.mkdir()
        (kaiju / "entries.json").write_text(
            json.dumps(
                {
                    "setup": {"python": "3.11"},
                    "test": {"test_dir": "src/tests"},
                    "reference_commit": "deadbeef",
                }
            )
        )
        py, td, ref, _bc = _resolve_repo_dir_args(
            repo_dir=tmp_path,
            name="x",
            cli_python_version=None,
            cli_test_dir=None,
            cli_reference_commit=None,
            cli_entries_json=None,
            output_dir=tmp_path / "out",
        )
        assert py == "3.11"
        assert td == "src/tests"
        assert ref == "deadbeef"

    def test_no_breadcrumb_falls_back_to_detect(self, tmp_path: Path) -> None:
        # No breadcrumb, no pyproject.toml → falls back to DEFAULT_PYTHON_VERSION
        py, td, ref, _bc = _resolve_repo_dir_args(
            repo_dir=tmp_path,
            name="x",
            cli_python_version=None,
            cli_test_dir=None,
            cli_reference_commit=None,
            cli_entries_json=None,
            output_dir=tmp_path / "out",
        )
        from commit0.harness.constants import DEFAULT_PYTHON_VERSION

        assert py == DEFAULT_PYTHON_VERSION
        assert td == "tests"
        assert ref is None


# ---------------------------------------------------------------------------
# P0-B: reference_commit checkout context manager
# ---------------------------------------------------------------------------


def _git_init_with_commits(repo: Path) -> tuple[str, str]:
    """Initialize a real git repo with two commits. Return (first_sha, second_sha)."""
    repo.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("first\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=repo, check=True, env=env)
    first = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "a.txt").write_text("second\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=repo, check=True, env=env)
    second = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return first, second


class TestReferenceCommitCheckout:
    def test_no_commit_is_noop(self, tmp_path: Path) -> None:
        first, _ = _git_init_with_commits(tmp_path)
        with _ReferenceCommitCheckout(tmp_path, None):
            after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
            assert after == _  # unchanged in the with-block

    def test_checkout_and_restore(self, tmp_path: Path) -> None:
        first, second = _git_init_with_commits(tmp_path)
        # HEAD is at second. Checkout first inside the context, expect restore on exit.
        with _ReferenceCommitCheckout(tmp_path, first):
            inside = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
            assert inside == first
        after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
        assert after == second

    def test_dirty_tree_stashes_and_pops(self, tmp_path: Path) -> None:
        first, second = _git_init_with_commits(tmp_path)
        # Dirty the worktree
        (tmp_path / "a.txt").write_text("dirty\n")
        with _ReferenceCommitCheckout(tmp_path, first):
            # Checkout succeeded thanks to the stash
            inside = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
            assert inside == first
        # After exit: back at second, with dirty changes restored
        after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
        assert after == second
        assert (tmp_path / "a.txt").read_text() == "dirty\n"

    def test_bad_ref_warns_and_passes_through(self, tmp_path: Path, caplog) -> None:
        first, second = _git_init_with_commits(tmp_path)
        with _ReferenceCommitCheckout(tmp_path, "doesnotexist123"):
            inside = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
            # Checkout failed → HEAD stayed at second
            assert inside == second


# ---------------------------------------------------------------------------
# P1-B: .status.json emission
# ---------------------------------------------------------------------------


class TestWriteStatusJson:
    def test_ok_status_payload(self, tmp_path: Path) -> None:
        result = TestCollectionResult(
            test_ids=["t/a.py::test_x", "t/a.py::test_y"],
            status=TestCollectionStatus.OK,
        )
        path = _write_status_json(
            output_dir=tmp_path,
            name="mylib",
            repo_dir=Path("/repo"),
            result=result,
            python_version="3.11",
            test_dir="tests",
            reference_commit="abc",
            bz2_written=True,
        )
        assert path == tmp_path / "mylib.status.json"
        data = json.loads(path.read_text())
        assert data["status"] == "ok"
        assert data["test_count"] == 2
        assert data["bz2_written"] is True
        assert data["_schema"] == "kaiju-test-id-status/1"

    def test_failure_status_payload(self, tmp_path: Path) -> None:
        result = TestCollectionResult(
            test_ids=[],
            status=TestCollectionStatus.MISSING_SYSTEM_DEPS,
            stderr_snippet="ModuleNotFoundError: qgis",
            failing_module="qgis",
        )
        path = _write_status_json(
            output_dir=tmp_path,
            name="qaequilibrae",
            repo_dir=Path("/repo"),
            result=result,
            python_version="3.12",
            test_dir="tests",
            reference_commit=None,
            bz2_written=False,
            extra={"system_deps_hint": ["qgis", "PyQt5"]},
        )
        data = json.loads(path.read_text())
        assert data["status"] == "missing_system_deps"
        assert data["failing_module"] == "qgis"
        assert data["bz2_written"] is False
        assert data["system_deps_hint"] == ["qgis", "PyQt5"]

    def test_name_normalized_in_filename(self, tmp_path: Path) -> None:
        result = TestCollectionResult(test_ids=[], status=TestCollectionStatus.OK)
        path = _write_status_json(
            output_dir=tmp_path,
            name="Web3.Py",
            repo_dir=Path("/r"),
            result=result,
            python_version="3.11",
            test_dir="tests",
            reference_commit=None,
            bz2_written=False,
        )
        # Lowercase + dot→hyphen (matches save_test_ids convention)
        assert path.name == "web3-py.status.json"


# ---------------------------------------------------------------------------
# P1-A: UvRuntime system-deps pre-flight short-circuit
# ---------------------------------------------------------------------------


class TestUvRuntimeSystemDepsShortCircuit:
    """We can't run real ``uv`` in tests reliably, so we mock the parts we
    need and exercise only the short-circuit logic.
    """

    def test_synthesized_stderr_classified_as_missing_system_deps(self) -> None:
        # The short-circuit emits ModuleNotFoundError stderr that
        # classify_failure must map to MISSING_SYSTEM_DEPS.
        stderr = "ModuleNotFoundError: No module named 'qgis'\n(kaiju-pre-flight: system_deps)\n"
        status, mod = classify_failure(stdout="", stderr=stderr, exit_code=1)
        assert status == TestCollectionStatus.MISSING_SYSTEM_DEPS
        assert mod == "qgis"
