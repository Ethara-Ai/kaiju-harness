from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Install proper stubs for uninstalled optional [agent] deps (aider, import_deps)
# BEFORE any test module is collected, so tests that import agent.* modules behave
# deterministically regardless of collection order. Shared with agent/tests so both
# directories install the exact same valid module objects. See the helper's docstring.
from commit0.harness._optional_dep_stubs import install_missing_optional_dep_stubs

install_missing_optional_dep_stubs()


from commit0.harness.constants import RepoInstance, SimpleInstance  # noqa: E402


@pytest.fixture
def sample_repo_instance() -> RepoInstance:
    return RepoInstance(
        instance_id="test/repo",
        repo="test-repo",
        # 40-char valid-hex SHAs (eval_hardening now requires a bare hex SHA);
        # prefixes kept so existing "abc123"/"def456" substring asserts still hold.
        base_commit="abc123" + "0" * 34,
        reference_commit="def456" + "0" * 34,
        setup={
            "python": "3.12",
            "packages": "requirements.txt",
            "install": "pip install -e .",
        },
        test={"test_cmd": "pytest", "test_file.py": "def test_example(): pass"},
        src_dir="src",
    )


@pytest.fixture
def sample_simple_instance() -> SimpleInstance:
    return SimpleInstance(
        instance_id="simple/1",
        prompt="Write hello",
        canonical_solution="print('hello')",
        test="assert True",
    )


@pytest.fixture
def mock_logger() -> MagicMock:
    logger = MagicMock()
    logger.log_file = Path("/tmp/test.log")
    return logger
