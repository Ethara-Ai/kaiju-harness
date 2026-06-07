"""Tests for the adaptive fallback cascade in batch_prepare.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE = "tools.batch_prepare"
PREPARE_REPO = "tools.prepare_repo"


def _make_prepare_mocks(
    import_results: list[tuple[bool, str]],
) -> dict[str, MagicMock]:
    """Build a dict of mocks wired for prepare_single_repo."""
    call_idx = {"i": 0}

    def _import_check(_repo_dir: Path, _src_dir: str) -> tuple[bool, str]:
        idx = call_idx["i"]
        call_idx["i"] += 1
        if idx < len(import_results):
            return import_results[idx]
        return import_results[-1]

    mocks: dict[str, MagicMock] = {}

    mocks["fork_repo"] = MagicMock(return_value="test-org/test-repo")
    mocks["full_clone"] = MagicMock(return_value=Path("/tmp/test-repo"))
    mocks["create_stubbed_branch"] = MagicMock(return_value=("abc123", "def456"))
    mocks["quick_import_check"] = MagicMock(side_effect=_import_check)
    mocks["get_default_branch"] = MagicMock(return_value="main")
    mocks["get_head_sha"] = MagicMock(return_value="abc123")
    mocks["git"] = MagicMock()
    mocks["push_to_fork"] = MagicMock()
    mocks["generate_setup_dict"] = MagicMock(return_value={})
    mocks["generate_test_dict"] = MagicMock(return_value={})
    mocks["create_dataset_entry"] = MagicMock(
        return_value={
            "instance_id": "test-org__test-repo",
            "repo": "owner/test-repo",
            "base_commit": "abc123",
        }
    )
    mocks["find_src_dir"] = MagicMock(return_value="src/test_repo")
    mocks["find_test_dir"] = MagicMock(return_value="tests")

    return mocks


def _run_prepare(
    mocks: dict[str, MagicMock],
    removal_mode: str = "all",
    fallback: bool = True,
) -> dict | None:
    """Run prepare_single_repo with all external calls mocked."""
    patches = {
        f"{PREPARE_REPO}.fork_repo": mocks["fork_repo"],
        f"{PREPARE_REPO}.full_clone": mocks["full_clone"],
        f"{PREPARE_REPO}.create_stubbed_branch": mocks["create_stubbed_branch"],
        f"{PREPARE_REPO}.quick_import_check": mocks["quick_import_check"],
        f"{PREPARE_REPO}.get_default_branch": mocks["get_default_branch"],
        f"{PREPARE_REPO}.get_head_sha": mocks["get_head_sha"],
        f"{PREPARE_REPO}.git": mocks["git"],
        f"{PREPARE_REPO}.push_to_fork": mocks["push_to_fork"],
        f"{PREPARE_REPO}.generate_setup_dict": mocks["generate_setup_dict"],
        f"{PREPARE_REPO}.generate_test_dict": mocks["generate_test_dict"],
        f"{PREPARE_REPO}.create_dataset_entry": mocks["create_dataset_entry"],
        "tools.validate.find_src_dir": mocks["find_src_dir"],
        "tools.validate.find_test_dir": mocks["find_test_dir"],
        f"{MODULE}._get_latest_tag": MagicMock(return_value=None),
        f"{MODULE}._remove_workflows": MagicMock(return_value=False),
    }

    from tools.batch_prepare import prepare_single_repo

    patches[f"{MODULE}.os.environ.get"] = MagicMock(
        side_effect=lambda k, *a: (
            "fake-token" if k in ("GITHUB_TOKEN", "GH_TOKEN") else None
        )
    )
    stack = [patch(target, mock_obj) for target, mock_obj in patches.items()]
    for p in stack:
        p.start()

    try:
        result = prepare_single_repo(
            full_name="owner/test-repo",
            clone_dir=Path("/tmp/clones"),
            org="test-org",
            removal_mode=removal_mode,
            dry_run=True,
            fallback=fallback,
        )
    finally:
        for p in stack:
            p.stop()

    return result


class TestFallbackCascade:
    def test_import_check_passes_no_fallback_triggered(self) -> None:
        mocks = _make_prepare_mocks([(True, "")])
        result = _run_prepare(mocks)

        assert result is not None
        assert mocks["create_stubbed_branch"].call_count == 1
        assert mocks["quick_import_check"].call_count == 1

    def test_import_fails_fallback_succeeds_on_second_mode(self) -> None:
        mocks = _make_prepare_mocks([(False, "ImportError"), (True, "")])
        result = _run_prepare(mocks, removal_mode="all")

        assert result is not None
        assert mocks["create_stubbed_branch"].call_count == 2
        assert mocks["quick_import_check"].call_count == 2

    def test_all_modes_fail_continues_with_original(self) -> None:
        mocks = _make_prepare_mocks([(False, "error")])
        result = _run_prepare(mocks, removal_mode="all")

        assert result is not None
        assert mocks["create_stubbed_branch"].call_count == 3
        assert mocks["quick_import_check"].call_count == 3

    def test_no_fallback_flag_disables_cascade(self) -> None:
        mocks = _make_prepare_mocks([(False, "ImportError")])
        result = _run_prepare(mocks, fallback=False)

        assert result is not None
        assert mocks["create_stubbed_branch"].call_count == 1
        assert mocks["quick_import_check"].call_count == 0


class TestQuickImportCheck:
    def test_handles_nonexistent_package(self) -> None:
        from tools.prepare_repo import quick_import_check

        ok, err = quick_import_check(Path("/tmp"), "nonexistent_pkg_xyz_12345")
        assert ok is False
        assert "nonexistent_pkg_xyz_12345" in err or "No module named" in err
