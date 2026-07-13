from pathlib import Path

import pytest
from pydantic import ValidationError

from commit0.harness.constants import (
    BASE_BRANCH,
    BASE_IMAGE_BUILD_DIR,
    COMMANDS,
    EVAL_BACKENDS,
    INSTALL_FAIL,
    INSTALL_PASS,
    INSTALL_TIMEOUT,
    NON_TEST_EXTS,
    OCI_IMAGE_DIR,
    REPO_IMAGE_BUILD_DIR,
    RESET_FAILED,
    RUN_AGENT_LOG_DIR,
    RUN_PYTEST_LOG_DIR,
    SPLIT,
    SUPPORTED_PYTHON_VERSIONS,
    TESTS_ERROR,
    TESTS_FAILED,
    TESTS_PASSED,
    TESTS_TIMEOUT,
    Files,
    RepoInstance,
    ResolvedStatus,
    SimpleInstance,
    TestStatus,
)


class TestRepoInstance:
    def _make_instance(self, **overrides):
        defaults = {
            "instance_id": "test/repo",
            "repo": "test-repo",
            "base_commit": "abc123",
            "reference_commit": "def456",
            "setup": {"python": "3.12"},
            "test": {"test_cmd": "pytest"},
            "src_dir": "src",
        }
        defaults.update(overrides)
        return RepoInstance(**defaults)

    def test_creation_valid(self):
        inst = self._make_instance()
        assert inst.instance_id == "test/repo"
        assert inst.repo == "test-repo"
        assert inst.base_commit == "abc123"
        assert inst.reference_commit == "def456"
        assert inst.setup == {"python": "3.12"}
        assert inst.test == {"test_cmd": "pytest"}
        assert inst.src_dir == "src"

    def test_getitem_access(self):
        inst = self._make_instance()
        assert inst["repo"] == "test-repo"
        assert inst["instance_id"] == "test/repo"
        assert inst["src_dir"] == "src"

    def test_keys_returns_field_names(self):
        inst = self._make_instance()
        expected = {
            "instance_id",
            "repo",
            "base_commit",
            "reference_commit",
            "setup",
            "test",
            "src_dir",
        }
        assert set(inst.keys()) == expected

    def test_missing_required_field_raises(self):
        with pytest.raises(ValidationError):
            RepoInstance(
                instance_id="test/repo",
                base_commit="abc123",
                reference_commit="def456",
                setup={"python": "3.12"},
                test={"test_cmd": "pytest"},
                src_dir="src",
            )

    def test_getitem_nonexistent_raises(self):
        inst = self._make_instance()
        with pytest.raises(KeyError):
            inst["nonexistent_field"]


class TestSimpleInstance:
    def _make_instance(self, **overrides):
        defaults = {
            "instance_id": "simple/1",
            "prompt": "Write a function",
            "canonical_solution": "def f(): pass",
            "test": "assert f() is None",
        }
        defaults.update(overrides)
        return SimpleInstance(**defaults)

    def test_creation_valid(self):
        inst = self._make_instance()
        assert inst.instance_id == "simple/1"
        assert inst.prompt == "Write a function"
        assert inst.canonical_solution == "def f(): pass"
        assert inst.test == "assert f() is None"

    def test_getitem_access(self):
        inst = self._make_instance()
        assert inst["prompt"] == "Write a function"

    def test_keys_returns_field_names(self):
        inst = self._make_instance()
        expected = {"instance_id", "prompt", "canonical_solution", "test"}
        assert set(inst.keys()) == expected
        assert len(set(inst.keys())) == 4

    def test_missing_field_raises(self):
        with pytest.raises(ValidationError):
            SimpleInstance(
                instance_id="simple/1",
                prompt="Write a function",
                test="assert True",
            )


class TestFiles:
    def test_creation_valid(self):
        f = Files(
            eval_script={"a": Path("/tmp/a")},
            patch={"b": Path("/tmp/b")},
        )
        assert f.eval_script == {"a": Path("/tmp/a")}
        assert f.patch == {"b": Path("/tmp/b")}

    def test_getitem_access(self):
        f = Files(eval_script={"a": Path("/tmp/a")}, patch={"b": Path("/tmp/b")})
        assert f["eval_script"] == {"a": Path("/tmp/a")}

    def test_items_returns_dict_items(self):
        f = Files(eval_script={"a": Path("/tmp/a")}, patch={"b": Path("/tmp/b")})
        d = dict(f.items())
        assert "eval_script" in d
        assert "patch" in d

    def test_empty_dicts(self):
        f = Files(eval_script={}, patch={})
        assert f.eval_script == {}
        assert f.patch == {}


class TestResolvedStatus:
    def test_values(self):
        assert ResolvedStatus.NO.value == "RESOLVED_NO"
        assert ResolvedStatus.PARTIAL.value == "RESOLVED_PARTIAL"
        assert ResolvedStatus.FULL.value == "RESOLVED_FULL"

    def test_all_members(self):
        assert len(ResolvedStatus) == 3


class TestTestStatus:
    def test_values(self):
        assert TestStatus.FAILED.value == "FAILED"
        assert TestStatus.PASSED.value == "PASSED"
        assert TestStatus.SKIPPED.value == "SKIPPED"
        assert TestStatus.ERROR.value == "ERROR"
        assert TestStatus.XFAIL.value == "XFAIL"

    def test_all_members(self):
        assert len(TestStatus) == 5


class TestConstants:
    def test_split_dict_has_required_keys(self):
        for key in ("lite", "ethara", "ethara-lite"):
            assert key in SPLIT, f"Missing key {key!r} in SPLIT"

    def test_eval_backends(self):
        assert EVAL_BACKENDS == ["local", "local_inplace", "modal", "e2b"]

    def test_base_branch(self):
        assert BASE_BRANCH == "commit0"

    def test_supported_python_versions(self):
        assert SUPPORTED_PYTHON_VERSIONS == {"3.9", "3.10", "3.11", "3.12", "3.13"}

    def test_non_test_exts_is_list_of_strings(self):
        assert isinstance(NON_TEST_EXTS, list)
        assert all(isinstance(ext, str) for ext in NON_TEST_EXTS)
        assert ".json" in NON_TEST_EXTS

    def test_commands_list(self):
        assert isinstance(COMMANDS, list)
        for cmd in ("clone", "build", "test", "evaluate", "lint", "save"):
            assert cmd in COMMANDS, f"Missing command {cmd!r}"

    def test_log_constants(self):
        assert INSTALL_FAIL == ">>>>> Init Failed"
        assert INSTALL_PASS == ">>>>> Init Succeeded"
        assert INSTALL_TIMEOUT == ">>>>> Init Timed Out"
        assert RESET_FAILED == ">>>>> Reset Failed"
        assert TESTS_ERROR == ">>>>> Tests Errored"
        assert TESTS_FAILED == ">>>>> Some Tests Failed"
        assert TESTS_PASSED == ">>>>> All Tests Passed"
        assert TESTS_TIMEOUT == ">>>>> Tests Timed Out"

    def test_path_constants(self):
        assert isinstance(BASE_IMAGE_BUILD_DIR, Path)
        assert isinstance(REPO_IMAGE_BUILD_DIR, Path)
        assert isinstance(OCI_IMAGE_DIR, Path)
        assert isinstance(RUN_PYTEST_LOG_DIR, Path)
        assert isinstance(RUN_AGENT_LOG_DIR, Path)
