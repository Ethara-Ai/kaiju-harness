"""Exhaustive unit tests for commit0.harness.constants_rust."""

import pytest
from pathlib import Path
from pydantic import ValidationError


# ---------------------------------------------------------------------------
# Module-level imports
# ---------------------------------------------------------------------------
from commit0.harness.constants_rust import (
    RUST_VERSION,
    RUST_STUB_MARKER,
    RUST_BASE_BRANCH,
    RUST_GITIGNORE_ENTRIES,
    RUST_SPLIT,
    CARGO_NEXTEST_VERSION,
    RUN_RUST_TESTS_LOG_DIR,
    RUST_TEST_IDS_DIR,
    DOCKERFILES_RUST_DIR,
    DOCKERFILES_DIR,
    RustRepoInstance,
    TestStatus,
)
from commit0.harness.constants import RepoInstance


# ===== RUST_VERSION =====
class TestRustVersion:
    def test_is_string(self):
        assert isinstance(RUST_VERSION, str)

    def test_value(self):
        assert RUST_VERSION == "stable"

    def test_not_empty(self):
        assert len(RUST_VERSION) > 0


# ===== RUST_STUB_MARKER =====
class TestRustStubMarker:
    def test_is_string(self):
        assert isinstance(RUST_STUB_MARKER, str)

    def test_contains_panic(self):
        assert "panic!" in RUST_STUB_MARKER

    def test_contains_stub(self):
        assert "STUB" in RUST_STUB_MARKER

    def test_exact_value(self):
        assert RUST_STUB_MARKER == 'panic!("STUB: not implemented")'

    def test_is_valid_rust_macro_call(self):
        assert RUST_STUB_MARKER.startswith("panic!(")
        assert RUST_STUB_MARKER.endswith(")")


# ===== RUST_BASE_BRANCH =====
class TestRustBaseBranch:
    def test_value(self):
        assert RUST_BASE_BRANCH == "commit0"

    def test_type(self):
        assert isinstance(RUST_BASE_BRANCH, str)

    def test_no_spaces(self):
        assert " " not in RUST_BASE_BRANCH

    def test_valid_git_branch_name(self):
        # Git branch names cannot contain: space, ~, ^, :, \, ?, *, [
        invalid_chars = set(" ~^:\\?*[")
        assert not any(c in invalid_chars for c in RUST_BASE_BRANCH)


# ===== RUST_GITIGNORE_ENTRIES =====
class TestRustGitignoreEntries:
    def test_is_list(self):
        assert isinstance(RUST_GITIGNORE_ENTRIES, list)

    def test_contains_target(self):
        assert "target/" in RUST_GITIGNORE_ENTRIES

    def test_contains_aider(self):
        assert ".aider*" in RUST_GITIGNORE_ENTRIES

    def test_contains_logs(self):
        assert "logs/" in RUST_GITIGNORE_ENTRIES

    def test_length(self):
        assert len(RUST_GITIGNORE_ENTRIES) == 3

    def test_all_strings(self):
        assert all(isinstance(e, str) for e in RUST_GITIGNORE_ENTRIES)

    def test_no_empty_entries(self):
        assert all(len(e) > 0 for e in RUST_GITIGNORE_ENTRIES)

    def test_no_duplicates(self):
        assert len(RUST_GITIGNORE_ENTRIES) == len(set(RUST_GITIGNORE_ENTRIES))



# ===== CARGO_NEXTEST_VERSION =====
class TestCargoNextestVersion:
    def test_is_string(self):
        assert isinstance(CARGO_NEXTEST_VERSION, str)

    def test_semver_format(self):
        parts = CARGO_NEXTEST_VERSION.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_value(self):
        assert CARGO_NEXTEST_VERSION == "0.9.96"

    def test_major_version(self):
        assert CARGO_NEXTEST_VERSION.split(".")[0] == "0"


# ===== Path Constants =====
class TestPathConstants:
    def test_run_rust_tests_log_dir_type(self):
        assert isinstance(RUN_RUST_TESTS_LOG_DIR, Path)

    def test_run_rust_tests_log_dir_value(self):
        assert RUN_RUST_TESTS_LOG_DIR == Path("logs/rust_tests")

    def test_rust_test_ids_dir_type(self):
        assert isinstance(RUST_TEST_IDS_DIR, Path)

    def test_rust_test_ids_dir_contains_data(self):
        assert "data" in str(RUST_TEST_IDS_DIR)

    def test_rust_test_ids_dir_contains_rust_test_ids(self):
        assert "rust_test_ids" in str(RUST_TEST_IDS_DIR)

    def test_dockerfiles_rust_dir_type(self):
        assert isinstance(DOCKERFILES_RUST_DIR, Path)

    def test_dockerfiles_rust_dir_contains_dockerfiles(self):
        assert "dockerfiles" in str(DOCKERFILES_RUST_DIR)

    def test_dockerfiles_dir_type(self):
        assert isinstance(DOCKERFILES_DIR, Path)

    def test_dockerfiles_dir_imported_correctly(self):
        assert "dockerfiles" in str(DOCKERFILES_DIR)

    def test_log_dir_is_relative(self):
        assert not RUN_RUST_TESTS_LOG_DIR.is_absolute()

    def test_test_ids_dir_is_absolute(self):
        assert RUST_TEST_IDS_DIR.is_absolute()


# ===== RustRepoInstance =====
class TestRustRepoInstance:
    @pytest.fixture
    def sample_instance(self):
        return RustRepoInstance(
            instance_id="test-1",
            repo="Rust-commit0/taffy",
            base_commit="abc123",
            reference_commit="def456",
            setup={"install": "cargo build"},
            test={"test_cmd": "cargo test"},
            src_dir="src",
        )

    def test_inherits_repo_instance(self):
        assert issubclass(RustRepoInstance, RepoInstance)

    def test_create_minimal(self, sample_instance):
        assert sample_instance.instance_id == "test-1"

    def test_edition_default(self, sample_instance):
        assert sample_instance.edition == "2021"

    def test_features_default(self, sample_instance):
        assert sample_instance.features == []

    def test_workspace_default(self, sample_instance):
        assert sample_instance.workspace is False

    def test_custom_edition(self):
        inst = RustRepoInstance(
            instance_id="t",
            repo="r",
            base_commit="b",
            reference_commit="r",
            setup={},
            test={},
            src_dir="s",
            edition="2018",
        )
        assert inst.edition == "2018"

    def test_custom_features(self):
        inst = RustRepoInstance(
            instance_id="t",
            repo="r",
            base_commit="b",
            reference_commit="r",
            setup={},
            test={},
            src_dir="s",
            features=["serde", "tokio"],
        )
        assert inst.features == ["serde", "tokio"]

    def test_workspace_true(self):
        inst = RustRepoInstance(
            instance_id="t",
            repo="r",
            base_commit="b",
            reference_commit="r",
            setup={},
            test={},
            src_dir="s",
            workspace=True,
        )
        assert inst.workspace is True

    def test_getitem_repo(self, sample_instance):
        assert sample_instance["repo"] == "Rust-commit0/taffy"

    def test_getitem_edition(self, sample_instance):
        assert sample_instance["edition"] == "2021"

    def test_getitem_missing_raises(self, sample_instance):
        with pytest.raises(KeyError):
            sample_instance["nonexistent"]

    def test_keys_contains_all_fields(self, sample_instance):
        keys = set(sample_instance.keys())
        assert "instance_id" in keys
        assert "repo" in keys
        assert "edition" in keys
        assert "features" in keys
        assert "workspace" in keys

    def test_model_dump(self, sample_instance):
        d = sample_instance.model_dump()
        assert d["edition"] == "2021"
        assert d["features"] == []
        assert d["workspace"] is False

    def test_features_is_mutable_list(self):
        inst = RustRepoInstance(
            instance_id="t",
            repo="r",
            base_commit="b",
            reference_commit="r",
            setup={},
            test={},
            src_dir="s",
        )
        inst.features.append("feature1")
        assert "feature1" in inst.features

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            RustRepoInstance(instance_id="t")

    def test_equality(self):
        kwargs = dict(
            instance_id="t",
            repo="r",
            base_commit="b",
            reference_commit="r",
            setup={},
            test={},
            src_dir="s",
        )
        a = RustRepoInstance(**kwargs)
        b = RustRepoInstance(**kwargs)
        assert a == b

    def test_inequality_different_edition(self):
        kwargs = dict(
            instance_id="t",
            repo="r",
            base_commit="b",
            reference_commit="r",
            setup={},
            test={},
            src_dir="s",
        )
        a = RustRepoInstance(**kwargs, edition="2018")
        b = RustRepoInstance(**kwargs, edition="2021")
        assert a != b

    def test_json_roundtrip(self, sample_instance):
        json_str = sample_instance.model_dump_json()
        restored = RustRepoInstance.model_validate_json(json_str)
        assert restored == sample_instance


# ===== TestStatus imported correctly =====
class TestTestStatusImport:
    def test_has_passed(self):
        assert TestStatus.PASSED.value == "PASSED"

    def test_has_failed(self):
        assert TestStatus.FAILED.value == "FAILED"

    def test_has_skipped(self):
        assert TestStatus.SKIPPED.value == "SKIPPED"

    def test_has_error(self):
        assert TestStatus.ERROR.value == "ERROR"

    def test_has_xfail(self):
        assert TestStatus.XFAIL.value == "XFAIL"


# ===== __all__ exports =====
class TestModuleExports:
    def test_all_exports(self):
        import commit0.harness.constants_rust as mod

        expected = {
            "RustRepoInstance",
            "RUST_VERSION",
            "RUST_STUB_MARKER",
            "RUST_SPLIT",
            "RUST_BASE_BRANCH",
            "RUST_GITIGNORE_ENTRIES",
            "CARGO_NEXTEST_VERSION",
            "RUN_RUST_TESTS_LOG_DIR",
            "RUST_TEST_IDS_DIR",
            "DOCKERFILES_RUST_DIR",
            "DOCKERFILES_DIR",
            "TestStatus",
        }
        assert expected == set(mod.__all__)

    def test_all_exports_are_accessible(self):
        import commit0.harness.constants_rust as mod

        for name in mod.__all__:
            assert hasattr(mod, name), f"{name} in __all__ but not accessible"


class TestRustRepoInstanceEdge:
    def test_default_edition(self):
        inst = RustRepoInstance(
            repo="org/r",
            instance_id="r-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
        )
        assert inst.edition == "2021"

    def test_custom_edition(self):
        inst = RustRepoInstance(
            repo="org/r",
            instance_id="r-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
            edition="2024",
        )
        assert inst.edition == "2024"

    def test_default_features_empty(self):
        inst = RustRepoInstance(
            repo="org/r",
            instance_id="r-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
        )
        assert inst.features == []

    def test_custom_features(self):
        inst = RustRepoInstance(
            repo="org/r",
            instance_id="r-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
            features=["serde", "async"],
        )
        assert inst.features == ["serde", "async"]

    def test_default_workspace_false(self):
        inst = RustRepoInstance(
            repo="org/r",
            instance_id="r-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
        )
        assert inst.workspace is False

    def test_workspace_true(self):
        inst = RustRepoInstance(
            repo="org/r",
            instance_id="r-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
            workspace=True,
        )
        assert inst.workspace is True

    def test_inherits_from_repo_instance(self):
        assert issubclass(RustRepoInstance, RepoInstance)

    def test_features_independent_across_instances(self):
        a = RustRepoInstance(
            repo="org/a",
            instance_id="a-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
        )
        b = RustRepoInstance(
            repo="org/b",
            instance_id="b-1",
            base_commit="a",
            reference_commit="b",
            setup={},
            test={},
            src_dir="src",
        )
        a.features.append("serde")
        assert b.features == []


class TestRustSplitEdge:
    def test_split_is_dict(self):
        assert isinstance(RUST_SPLIT, dict)

    def test_each_split_value_is_list(self):
        for key, val in RUST_SPLIT.items():
            assert isinstance(val, list)


class TestPathConstants:
    def test_run_log_dir_is_path(self):
        assert isinstance(RUN_RUST_TESTS_LOG_DIR, Path)

    def test_test_ids_dir_is_path(self):
        assert isinstance(RUST_TEST_IDS_DIR, Path)

    def test_dockerfiles_rust_dir_is_path(self):
        assert isinstance(DOCKERFILES_RUST_DIR, Path)

    def test_dockerfiles_dir_is_path(self):
        assert isinstance(DOCKERFILES_DIR, Path)

    def test_run_log_dir_relative(self):
        assert not RUN_RUST_TESTS_LOG_DIR.is_absolute()

    def test_test_ids_dir_contains_rust(self):
        assert "rust" in str(RUST_TEST_IDS_DIR)

    def test_dockerfiles_rust_dir_contains_dockerfiles(self):
        assert "dockerfiles" in str(DOCKERFILES_RUST_DIR)


class TestGitignoreEntries:
    def test_target_in_entries(self):
        assert "target/" in RUST_GITIGNORE_ENTRIES

    def test_all_entries_are_strings(self):
        for entry in RUST_GITIGNORE_ENTRIES:
            assert isinstance(entry, str)

    def test_no_empty_entries(self):
        for entry in RUST_GITIGNORE_ENTRIES:
            assert len(entry) > 0

    def test_entries_is_list(self):
        assert isinstance(RUST_GITIGNORE_ENTRIES, list)

    def test_entries_not_empty(self):
        assert len(RUST_GITIGNORE_ENTRIES) > 0
