"""Tests for commit0.harness.split_utils.

Covers the universal split-resolution helper that replaces every per-language
hardcoded ``*_SPLIT["all"]`` list with dynamic dataset-derived resolution.
"""

from __future__ import annotations

from typing import Any

import pytest

from commit0.harness.split_utils import (
    build_dataset_alias_map,
    derive_all_split,
    resolve_split,
)


@pytest.fixture
def cpp_dataset() -> list[dict[str, Any]]:
    """Mirror the shape of a real ``fmt_cpp_dataset.json`` entry."""
    return [
        {
            "instance_id": "fmt_cpp",
            "repo": "Zahgon/fmt",
            "original_repo": "fmtlib/fmt",
            "base_commit": "abc111",
            "reference_commit": "def111",
            "src_dir": "src",
        },
        {
            "instance_id": "yaml-cpp_cpp",
            "repo": "Zahgon/yaml-cpp",
            "original_repo": "jbeder/yaml-cpp",
            "base_commit": "abc222",
            "reference_commit": "def222",
            "src_dir": "src",
        },
        {
            "instance_id": "CLI11_cpp",
            "repo": "Zahgon/CLI11",
            "original_repo": "CLIUtils/CLI11",
            "base_commit": "abc333",
            "reference_commit": "def333",
            "src_dir": "include",
        },
    ]


@pytest.fixture
def go_dataset() -> list[dict[str, Any]]:
    """Mirror the shape of a real Go dataset entry."""
    return [
        {
            "instance_id": "go-version_go",
            "repo": "Zahgon/go-version",
            "original_repo": "hashicorp/go-version",
            "base_commit": "111",
            "reference_commit": "222",
        },
        {
            "instance_id": "conc_go",
            "repo": "Zahgon/conc",
            "original_repo": "sourcegraph/conc",
            "base_commit": "333",
            "reference_commit": "444",
        },
    ]


class _PydanticLike:
    """Stand-in for ``RepoInstance``-style attribute-access entries."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


# ─── derive_all_split ────────────────────────────────────────────────────────


class TestDeriveAllSplit:
    def test_empty_dataset(self) -> None:
        assert derive_all_split([]) == []

    def test_single_entry(self, cpp_dataset: list[dict]) -> None:
        assert derive_all_split([cpp_dataset[0]]) == ["fmt"]

    def test_multiple_entries_preserve_order(
        self, cpp_dataset: list[dict]
    ) -> None:
        assert derive_all_split(cpp_dataset) == ["fmt", "yaml-cpp", "CLI11"]

    def test_dedup_keeps_first_seen(self) -> None:
        dataset = [
            {"repo": "Zahgon/fmt"},
            {"repo": "OtherOrg/fmt"},
            {"repo": "Zahgon/yaml-cpp"},
        ]
        assert derive_all_split(dataset) == ["fmt", "yaml-cpp"]

    def test_skips_entry_missing_repo_field(self) -> None:
        dataset = [
            {"instance_id": "no_repo"},
            {"repo": ""},
            {"repo": "Zahgon/fmt"},
        ]
        assert derive_all_split(dataset) == ["fmt"]

    def test_attribute_style_entries(self) -> None:
        dataset = [
            _PydanticLike(repo="Zahgon/fmt"),
            _PydanticLike(repo="Zahgon/yaml-cpp"),
        ]
        assert derive_all_split(dataset) == ["fmt", "yaml-cpp"]

    def test_repo_with_no_slash_keeps_whole_string(self) -> None:
        assert derive_all_split([{"repo": "standalone"}]) == ["standalone"]


# ─── build_dataset_alias_map ─────────────────────────────────────────────────


class TestBuildAliasMap:
    def test_all_four_aliases_present(self, go_dataset: list[dict]) -> None:
        aliases = build_dataset_alias_map([go_dataset[0]])
        assert aliases == {
            "go-version_go": ["go-version"],
            "Zahgon/go-version": ["go-version"],
            "hashicorp/go-version": ["go-version"],
            "go-version": ["go-version"],
        }

    def test_missing_original_repo_skipped(self) -> None:
        dataset = [
            {
                "instance_id": "fmt_cpp",
                "repo": "Zahgon/fmt",
            }
        ]
        aliases = build_dataset_alias_map(dataset)
        assert set(aliases.keys()) == {"fmt_cpp", "Zahgon/fmt", "fmt"}
        assert all(v == ["fmt"] for v in aliases.values())

    def test_first_entry_wins_on_collision(self) -> None:
        dataset = [
            {"repo": "OrgA/conc", "instance_id": "conc_go"},
            {"repo": "OrgB/conc", "instance_id": "conc_go_duplicate"},
        ]
        aliases = build_dataset_alias_map(dataset)
        assert aliases["conc"] == ["conc"]
        assert aliases["conc_go"] == ["conc"]

    def test_skips_entries_without_repo(self) -> None:
        dataset = [{"instance_id": "no_repo"}, {"repo": "Zahgon/fmt"}]
        aliases = build_dataset_alias_map(dataset)
        assert aliases == {"Zahgon/fmt": ["fmt"], "fmt": ["fmt"]}


# ─── resolve_split ───────────────────────────────────────────────────────────


class TestResolveSplit:
    def test_all_derives_from_dataset(self, cpp_dataset: list[dict]) -> None:
        assert resolve_split("all", cpp_dataset) == ["fmt", "yaml-cpp", "CLI11"]

    def test_all_with_lang_suffix_also_derives(
        self, cpp_dataset: list[dict]
    ) -> None:
        assert resolve_split("all_cpp", cpp_dataset) == [
            "fmt",
            "yaml-cpp",
            "CLI11",
        ]
        assert resolve_split("all_ts", cpp_dataset) == [
            "fmt",
            "yaml-cpp",
            "CLI11",
        ]

    def test_curated_subset_wins(self, cpp_dataset: list[dict]) -> None:
        curated = {"lite": ["fmt"], "ethara": ["fmt", "yaml-cpp"]}
        assert resolve_split("lite", cpp_dataset, curated=curated) == ["fmt"]
        assert resolve_split("ethara", cpp_dataset, curated=curated) == [
            "fmt",
            "yaml-cpp",
        ]

    def test_curated_overrides_dataset_alias_collision(
        self, cpp_dataset: list[dict]
    ) -> None:
        curated = {"fmt": ["override-result"]}
        assert resolve_split("fmt", cpp_dataset, curated=curated) == [
            "override-result"
        ]

    def test_repo_name_alias_resolves(self, cpp_dataset: list[dict]) -> None:
        assert resolve_split("yaml-cpp", cpp_dataset) == ["yaml-cpp"]

    def test_instance_id_alias_resolves(self, go_dataset: list[dict]) -> None:
        assert resolve_split("go-version_go", go_dataset) == ["go-version"]

    def test_canonical_fork_path_resolves(
        self, go_dataset: list[dict]
    ) -> None:
        assert resolve_split("Zahgon/go-version", go_dataset) == [
            "go-version"
        ]

    def test_original_upstream_path_resolves(
        self, go_dataset: list[dict]
    ) -> None:
        assert resolve_split("hashicorp/go-version", go_dataset) == [
            "go-version"
        ]

    def test_fuzzy_hyphen_underscore_match(
        self, go_dataset: list[dict]
    ) -> None:
        assert resolve_split("go_version", go_dataset) == ["go-version"]

    def test_unknown_split_returns_empty(
        self, cpp_dataset: list[dict]
    ) -> None:
        assert resolve_split("nonexistent_repo", cpp_dataset) == []

    def test_no_curated_arg_is_safe(self, cpp_dataset: list[dict]) -> None:
        assert resolve_split("fmt", cpp_dataset) == ["fmt"]

    def test_empty_curated_dict_is_safe(self, cpp_dataset: list[dict]) -> None:
        assert resolve_split("fmt", cpp_dataset, curated={}) == ["fmt"]

    def test_empty_dataset_with_curated(self) -> None:
        curated = {"lite": ["fmt"]}
        assert resolve_split("lite", [], curated=curated) == ["fmt"]
        assert resolve_split("all", [], curated=curated) == []

    def test_attribute_style_dataset(self) -> None:
        dataset = [
            _PydanticLike(
                instance_id="fmt_cpp",
                repo="Zahgon/fmt",
                original_repo="fmtlib/fmt",
            )
        ]
        assert resolve_split("all", dataset) == ["fmt"]
        assert resolve_split("fmtlib/fmt", dataset) == ["fmt"]


# ─── Integration smoke test ──────────────────────────────────────────────────


class TestIntegrationPattern:
    """Mirror the exact pattern every setup_*.py / build_*.py / evaluate_*.py uses."""

    def test_setup_pipeline_pattern(self, cpp_dataset: list[dict]) -> None:
        repo_split = "all"
        curated_split = {"lite": ["fmt"]}

        allowed = set(
            resolve_split(repo_split, cpp_dataset, curated=curated_split)
        )

        kept: list[str] = []
        for example in cpp_dataset:
            repo_name = example["repo"].split("/")[-1]
            if repo_name not in allowed:
                continue
            kept.append(repo_name)

        assert kept == ["fmt", "yaml-cpp", "CLI11"]

    def test_lite_filter_keeps_only_curated(
        self, cpp_dataset: list[dict]
    ) -> None:
        allowed = set(
            resolve_split("lite", cpp_dataset, curated={"lite": ["fmt"]})
        )
        kept = [
            ex["repo"].split("/")[-1]
            for ex in cpp_dataset
            if ex["repo"].split("/")[-1] in allowed
        ]
        assert kept == ["fmt"]

    def test_single_repo_filter_via_alias(
        self, cpp_dataset: list[dict]
    ) -> None:
        allowed = set(resolve_split("yaml-cpp", cpp_dataset))
        kept = [
            ex["repo"].split("/")[-1]
            for ex in cpp_dataset
            if ex["repo"].split("/")[-1] in allowed
        ]
        assert kept == ["yaml-cpp"]

    def test_unknown_split_filters_nothing_through(
        self, cpp_dataset: list[dict]
    ) -> None:
        """Confirms behavior: unknown split → empty allowed → everything filtered."""
        allowed = set(resolve_split("does-not-exist", cpp_dataset))
        kept = [
            ex["repo"].split("/")[-1]
            for ex in cpp_dataset
            if ex["repo"].split("/")[-1] in allowed
        ]
        assert kept == []
