from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_ts import (
    Language,
    TsRepoInstance,
    TS_BASE_BRANCH,
    TS_DATASET_BRANCH,
    TS_SOURCE_EXTS,
    TS_STUB_MARKER,
    TS_TEST_FILE_PATTERNS,
    SUPPORTED_NODE_VERSIONS,
    DEFAULT_NODE_VERSION,
    CONTAINER_WORKDIR,
    TS_GITIGNORE_ENTRIES,
    RUN_TS_TEST_LOG_DIR,
)


def _make_ts_instance(**overrides) -> TsRepoInstance:
    defaults = {
        "instance_id": "commit-0/zod",
        "repo": "Zahgon/zod",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node": "20",
            "install": "npm install",
            "packages": [],
            "pre_install": [],
            "specification": "",
        },
        "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
        "src_dir": "src",
    }
    defaults.update(overrides)
    return TsRepoInstance(**defaults)


class TestLanguageEnum:
    def test_python_value(self):
        assert Language.PYTHON == "python"

    def test_typescript_value(self):
        assert Language.TYPESCRIPT == "typescript"

    def test_is_str_subclass(self):
        assert isinstance(Language.TYPESCRIPT, str)

    def test_members_count(self):
        assert len(Language) == 2


class TestTsRepoInstance:
    def test_subclasses_repo_instance(self):
        assert issubclass(TsRepoInstance, RepoInstance)

    def test_default_language(self):
        inst = _make_ts_instance()
        assert inst.language == Language.TYPESCRIPT

    def test_default_test_framework(self):
        inst = _make_ts_instance()
        assert inst.test_framework == "jest"

    def test_override_test_framework(self):
        inst = _make_ts_instance(test_framework="vitest")
        assert inst.test_framework == "vitest"

    def test_override_language(self):
        inst = _make_ts_instance(language=Language.PYTHON)
        assert inst.language == Language.PYTHON

    def test_getitem_repo(self):
        inst = _make_ts_instance()
        assert inst["repo"] == "Zahgon/zod"

    def test_getitem_language(self):
        inst = _make_ts_instance()
        assert inst["language"] == Language.TYPESCRIPT

    def test_getitem_test_framework(self):
        inst = _make_ts_instance()
        assert inst["test_framework"] == "jest"

    def test_getitem_missing_raises_key_error(self):
        inst = _make_ts_instance()
        with pytest.raises(KeyError):
            inst["nonexistent"]

    def test_keys_includes_new_fields(self):
        inst = _make_ts_instance()
        k = inst.keys()
        assert "language" in k
        assert "test_framework" in k
        assert "repo" in k
        assert "instance_id" in k



class TestConstants:
    def test_base_branch_value(self):
        assert TS_BASE_BRANCH == "commit0"

    def test_source_exts_contains_ts(self):
        assert ".ts" in TS_SOURCE_EXTS
        assert ".tsx" in TS_SOURCE_EXTS

    def test_stub_marker(self):
        assert "STUB" in TS_STUB_MARKER
        assert "throw" in TS_STUB_MARKER

    def test_test_file_patterns(self):
        assert "*.test.ts" in TS_TEST_FILE_PATTERNS
        assert "*.spec.ts" in TS_TEST_FILE_PATTERNS

    def test_node_versions(self):
        # Support spans old-to-new libs: Node 18 (older pins) through 24. The
        # default stays on an LTS (20); the set is intentionally wide so batch
        # runs over diverse libraries never fail image selection on version.
        assert "20" in SUPPORTED_NODE_VERSIONS
        assert "22" in SUPPORTED_NODE_VERSIONS
        assert "18" in SUPPORTED_NODE_VERSIONS
        assert "24" in SUPPORTED_NODE_VERSIONS
        assert DEFAULT_NODE_VERSION in SUPPORTED_NODE_VERSIONS

    def test_gitignore_entries(self):
        assert "node_modules/" in TS_GITIGNORE_ENTRIES
        assert "dist/" in TS_GITIGNORE_ENTRIES
        assert ".aider*" in TS_GITIGNORE_ENTRIES
        assert "logs/" in TS_GITIGNORE_ENTRIES

    def test_default_node_version(self):
        assert DEFAULT_NODE_VERSION in SUPPORTED_NODE_VERSIONS

    def test_container_workdir(self):
        assert CONTAINER_WORKDIR == "/testbed"

    def test_dataset_branch(self):
        assert TS_DATASET_BRANCH == "commit0_all"

    def test_log_dir_is_path(self):
        assert isinstance(RUN_TS_TEST_LOG_DIR, Path)
        assert "ts_test" in str(RUN_TS_TEST_LOG_DIR)
