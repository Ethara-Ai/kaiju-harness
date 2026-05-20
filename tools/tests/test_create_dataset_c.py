"""Pure-logic tests for tools/create_dataset_c.py.

Loaded by file path because ``tools`` is not an importable package.
Covers entry/dataset validation and the HF projection.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "create_dataset_c", _TOOLS / "create_dataset_c.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cdc = _load_module()


def _valid_entry(**overrides) -> dict:
    entry = {
        "instance_id": "cjson-1",
        "repo": "commit0/cjson",
        "original_repo": "DaveGamble/cJSON",
        "base_commit": "abcdef1234567",
        "reference_commit": "1234567abcdef",
        "setup": {"build_system": "cmake", "apt": [], "cmake_flags": ""},
        "test": {"framework": "ctest", "test_cmd": "ctest"},
        "src_dir": ".",
        "language": "c",
    }
    entry.update(overrides)
    return entry


class TestConstants:
    def test_allowed_sets(self) -> None:
        assert cdc.ALLOWED_BUILD_SYSTEMS == frozenset({"cmake"})
        assert cdc.ALLOWED_TEST_FRAMEWORKS == frozenset({"ctest"})
        assert cdc.TEST_REQUIRED_FIELDS == {"framework", "test_cmd"}
        assert "instance_id" in cdc.REQUIRED_FIELDS


class TestValidateEntry:
    def test_valid_entry_has_no_issues(self) -> None:
        assert cdc.validate_entry(_valid_entry(), 0) == []

    def test_missing_field_reported(self) -> None:
        e = _valid_entry()
        del e["repo"]
        issues = cdc.validate_entry(e, 3)
        assert any("Missing field: repo" in i for i in issues)

    def test_wrong_language_reported(self) -> None:
        issues = cdc.validate_entry(_valid_entry(language="cpp"), 0)
        assert any("language must be 'c'" in i for i in issues)

    def test_unsupported_build_system_reported(self) -> None:
        e = _valid_entry()
        e["setup"]["build_system"] = "make"
        issues = cdc.validate_entry(e, 0)
        assert any("build_system" in i for i in issues)

    def test_short_commit_reported(self) -> None:
        issues = cdc.validate_entry(_valid_entry(base_commit="abc"), 0)
        assert any("base_commit too short" in i for i in issues)


class TestValidateDataset:
    def test_splits_valid_from_invalid(self) -> None:
        good = _valid_entry(instance_id="ok")
        bad = _valid_entry(instance_id="bad", language="rust")
        valid, issues = cdc.validate_dataset([good, bad])
        assert [v["instance_id"] for v in valid] == ["ok"]
        assert issues


class TestHfProjection:
    def test_projects_expected_keys(self) -> None:
        out = cdc.create_hf_dataset_dict([_valid_entry()])
        assert len(out) == 1
        assert set(out[0].keys()) == {
            "instance_id",
            "repo",
            "original_repo",
            "base_commit",
            "reference_commit",
            "setup",
            "test",
            "src_dir",
            "language",
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
