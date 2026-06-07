"""Unit tests for ``tools._repo_naming`` + ``tools.dedup_dataset``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools._repo_naming import (
    canonical_repo_name,
    normalized_for_dedup,
    split_org_repo,
)
from tools.dedup_dataset import (
    apply_plan,
    build_dedup_plan,
    find_duplicate_groups,
    pick_winner,
)


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------


class TestSplitOrgRepo:
    @pytest.mark.parametrize(
        "input,expected",
        [
            ("pallets/flask", ("pallets", "flask")),
            ("dsdanielpark_bard-api", ("dsdanielpark", "bard-api")),
            ("dsdanielpark_bard_api", ("dsdanielpark", "bard_api")),  # first _ wins
            ("just-no-separator", ("just-no-separator", "")),
        ],
    )
    def test_split(self, input: str, expected: tuple[str, str]) -> None:
        assert split_org_repo(input) == expected


class TestCanonicalRepoName:
    @pytest.mark.parametrize(
        "input,expected",
        [
            ("Pallets-eco/Flask-SQLAlchemy", "pallets-eco_flask-sqlalchemy"),
            ("dsdanielpark_bard_api", "dsdanielpark_bard_api"),  # preserves underscores
            ("dsdanielpark/bard-api", "dsdanielpark_bard-api"),
            ("just-no-org", "just-no-org"),  # no separator → just lowercase
            ("UPPER", "upper"),
        ],
    )
    def test_canonical(self, input: str, expected: str) -> None:
        assert canonical_repo_name(input) == expected


class TestNormalizedForDedup:
    @pytest.mark.parametrize(
        "a,b",
        [
            # These pairs from MISSING_TEST_IDS_BZ2_ISSUE.md Section 5 must
            # collapse to the same key.
            ("dsdanielpark_bard-api", "dsdanielpark_bard_api"),
            ("jtesta_ssh-audit", "jtesta_ssh_audit"),
            ("vitalik_django-ninja", "vitalik_django_ninja"),
            ("pallets-eco_flask-sqlalchemy", "pallets-eco_flask_sqlalchemy"),
        ],
    )
    def test_known_dupes_collapse(self, a: str, b: str) -> None:
        assert normalized_for_dedup(a) == normalized_for_dedup(b)

    def test_distinct_repos_distinct(self) -> None:
        # Genuinely different repos must NOT collide
        assert normalized_for_dedup("acme_widget") != normalized_for_dedup("acme_gadget")
        assert normalized_for_dedup("a_b") != normalized_for_dedup("aa_b")

    def test_strips_trailing_separators(self) -> None:
        assert normalized_for_dedup("-foo-") == "foo"

    def test_handles_slash_form(self) -> None:
        assert normalized_for_dedup("ds/bard-api") == normalized_for_dedup("ds_bard_api")


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _make_folder_with_files(root: Path, name: str, n_files: int = 3) -> Path:
    folder = root / name
    folder.mkdir()
    for i in range(n_files):
        (folder / f"file{i}.txt").write_text(f"contents-{i}")
    return folder


class TestFindDuplicateGroups:
    def test_no_duplicates(self, tmp_path: Path) -> None:
        _make_folder_with_files(tmp_path, "acme_widget")
        _make_folder_with_files(tmp_path, "beta_gadget")
        assert find_duplicate_groups(tmp_path) == []

    def test_hyphen_underscore_pair_detected(self, tmp_path: Path) -> None:
        _make_folder_with_files(tmp_path, "ds_bard-api")
        _make_folder_with_files(tmp_path, "ds_bard_api")
        groups = find_duplicate_groups(tmp_path)
        assert len(groups) == 1
        assert len(groups[0].folders) == 2

    def test_triple_variant(self, tmp_path: Path) -> None:
        _make_folder_with_files(tmp_path, "a_b-c")
        _make_folder_with_files(tmp_path, "a_b_c")
        _make_folder_with_files(tmp_path, "a-b-c")  # canonical
        groups = find_duplicate_groups(tmp_path)
        assert len(groups) == 1
        assert len(groups[0].folders) == 3


class TestPickWinner:
    def test_most_files_wins(self, tmp_path: Path) -> None:
        a = _make_folder_with_files(tmp_path, "a_b-c", n_files=10)
        b = _make_folder_with_files(tmp_path, "a_b_c", n_files=2)
        winner, losers = pick_winner((a, b))
        assert winner == a
        assert losers == (b,)

    def test_hyphen_wins_on_tie(self, tmp_path: Path) -> None:
        a = _make_folder_with_files(tmp_path, "a_b-c", n_files=5)  # hyphen
        b = _make_folder_with_files(tmp_path, "a_b_c", n_files=5)  # underscore
        winner, losers = pick_winner((a, b))
        assert winner == a  # hyphen variant preferred

    def test_deterministic_secondary_tiebreak(self, tmp_path: Path) -> None:
        # Two underscore variants with the same file count — fallback to name
        a = _make_folder_with_files(tmp_path, "a_b_c", n_files=3)
        b = _make_folder_with_files(tmp_path, "a_b_d", n_files=3)
        # Even though they share count + no hyphen, the result is deterministic
        winner, losers = pick_winner((a, b))
        assert winner in {a, b}
        # Re-call: same result
        winner2, _ = pick_winner((a, b))
        assert winner == winner2


# ---------------------------------------------------------------------------
# Plan construction + apply
# ---------------------------------------------------------------------------


class TestBuildAndApplyPlan:
    def test_full_plan(self, tmp_path: Path) -> None:
        _make_folder_with_files(tmp_path, "ds_bard-api", n_files=6)  # winner
        _make_folder_with_files(tmp_path, "ds_bard_api", n_files=2)  # loser
        _make_folder_with_files(tmp_path, "unrelated_repo", n_files=4)

        plan = build_dedup_plan(tmp_path)
        assert len(plan) == 1
        decision = plan[0]
        assert decision.winner.name == "ds_bard-api"
        assert decision.losers[0].name == "ds_bard_api"
        assert decision.winner_file_count == 6
        assert decision.loser_file_counts == (2,)

    def test_dry_run_does_not_delete(self, tmp_path: Path) -> None:
        _make_folder_with_files(tmp_path, "ds_bard-api")
        _make_folder_with_files(tmp_path, "ds_bard_api")
        plan = build_dedup_plan(tmp_path)
        apply_plan(plan, dry_run=True)
        assert (tmp_path / "ds_bard-api").is_dir()
        assert (tmp_path / "ds_bard_api").is_dir()

    def test_apply_deletes_losers(self, tmp_path: Path) -> None:
        _make_folder_with_files(tmp_path, "ds_bard-api", n_files=6)
        _make_folder_with_files(tmp_path, "ds_bard_api", n_files=2)
        plan = build_dedup_plan(tmp_path)
        deleted = apply_plan(plan)
        assert deleted == 1
        assert (tmp_path / "ds_bard-api").is_dir()  # winner kept
        assert not (tmp_path / "ds_bard_api").exists()  # loser gone

    def test_empty_dataset(self, tmp_path: Path) -> None:
        assert build_dedup_plan(tmp_path) == []
