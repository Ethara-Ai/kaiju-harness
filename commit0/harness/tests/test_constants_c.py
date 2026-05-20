"""Tests for constants_c — schema, split resolution, apt allowlist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from commit0.harness.constants_c import (
    ALLOWED_APT_PACKAGES,
    CLanguage,
    CRepoInstance,
    C_BASE_BRANCH,
    C_GITIGNORE_ENTRIES,
    C_SOURCE_EXT,
    C_HEADER_EXT,
    C_SKIP_DIRS,
    C_SPLIT,
    C_SPLIT_ALL,
    C_STUB_MARKER,
    C_TEST_FILE_GLOBS,
    resolve_c_split,
    resolve_c_split_all,
)


class TestCLanguageEnum:
    def test_value_is_c(self):
        assert CLanguage.C.value == "c"

    def test_string_enum_compares(self):
        assert CLanguage.C == "c"


class TestCRepoInstance:
    def test_defaults(self):
        inst = CRepoInstance(
            instance_id="x",
            repo="x/y",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={"test_cmd": "ctest"},
            src_dir=".",
        )
        assert inst.language == CLanguage.C
        assert inst.build_system == "cmake"
        assert inst.test_framework == "ctest"
        assert inst.src_dir == "."

    def test_field_access_via_getitem(self):
        inst = CRepoInstance(
            instance_id="x",
            repo="x/y",
            base_commit="a",
            reference_commit="b",
            setup={"apt": []},
            test={"test_cmd": "ctest"},
            src_dir=".",
        )
        assert inst["repo"] == "x/y"
        assert inst["src_dir"] == "."


class TestApptAllowlist:
    def test_libcmocka_allowed(self):
        assert "libcmocka-dev" in ALLOWED_APT_PACKAGES

    def test_libcriterion_allowed(self):
        assert "libcriterion-dev" in ALLOWED_APT_PACKAGES

    def test_arbitrary_package_not_allowed(self):
        assert "evilpkg" not in ALLOWED_APT_PACKAGES

    def test_is_frozenset(self):
        assert isinstance(ALLOWED_APT_PACKAGES, frozenset)


class TestResolveCSplit:
    def test_returns_hardcoded_when_no_dataset(self):
        merged = resolve_c_split("not-a-real-dataset-path.json")
        assert "c_lite" in merged
        assert merged["c_lite"] == ["cJSON"]

    def test_local_json_adds_aliases(self, tmp_path: Path):
        dataset = tmp_path / "dataset.json"
        dataset.write_text(
            json.dumps(
                [
                    {
                        "instance_id": "foolib_c",
                        "repo": "OrgX/foolib",
                        "original_repo": "OriginalOrg/foolib",
                    }
                ]
            )
        )
        merged = resolve_c_split(str(dataset))
        # All four aliases must map to [basename]
        assert merged["foolib_c"] == ["foolib"]
        assert merged["OrgX/foolib"] == ["foolib"]
        assert merged["OriginalOrg/foolib"] == ["foolib"]
        assert merged["foolib"] == ["foolib"]
        # Hardcoded entries still present
        assert merged["c_lite"] == ["cJSON"]

    def test_hardcoded_wins_on_collision(self, tmp_path: Path):
        dataset = tmp_path / "dataset.json"
        dataset.write_text(
            json.dumps([{"repo": "x/c_lite", "instance_id": "c_lite"}])
        )
        merged = resolve_c_split(str(dataset))
        # Hardcoded c_lite -> ["cJSON"] must win over derived ["c_lite"]
        assert merged["c_lite"] == ["cJSON"]

    def test_resolve_c_split_all_dedup(self, tmp_path: Path):
        dataset = tmp_path / "dataset.json"
        dataset.write_text(
            json.dumps([{"repo": "x/cJSON", "instance_id": "cJSON_c"}])
        )
        flat = resolve_c_split_all(str(dataset))
        assert "cJSON" in flat
        # No duplicates
        assert len(flat) == len(set(flat))

    def test_malformed_json_falls_back(self, tmp_path: Path):
        dataset = tmp_path / "dataset.json"
        dataset.write_text("not valid json {{{")
        merged = resolve_c_split(str(dataset))
        # Falls back to hardcoded
        assert "c_lite" in merged


class TestConstants:
    def test_base_branch_matches_go(self):
        assert C_BASE_BRANCH == "commit0"

    def test_source_ext(self):
        assert C_SOURCE_EXT == ".c"
        assert C_HEADER_EXT == ".h"

    def test_stub_marker(self):
        assert C_STUB_MARKER == "STUB_PANIC"

    def test_test_file_globs(self):
        assert "test_*.c" in C_TEST_FILE_GLOBS
        assert "*_test.c" in C_TEST_FILE_GLOBS
        assert "*_tests.c" in C_TEST_FILE_GLOBS
        assert "check_*.c" in C_TEST_FILE_GLOBS

    def test_skip_dirs_include_common(self):
        for d in ("tests", "test", "third_party", "vendor", "deps"):
            assert d in C_SKIP_DIRS

    def test_gitignore_entries(self):
        assert "build/" in C_GITIGNORE_ENTRIES
        assert ".aider*" in C_GITIGNORE_ENTRIES
        assert "logs/" in C_GITIGNORE_ENTRIES

    def test_c_split_all_flat(self):
        assert "cJSON" in C_SPLIT_ALL


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
