"""Unit tests for agent.guarded_io.GuardedInputOutput.

Closes Issue 4 (V17 Aider auto-add bypass) Phase 1E acceptance.

Each test exercises a single decision branch of GuardedInputOutput.confirm_ask
against the three Aider file-add prompts:

  1. "Add file to the chat?"                                    (file-mention path)
  2. "Allow edits to file that has not been added to the chat?" (SEARCH/REPLACE path, EMPIRICAL EXPLOIT)
  3. "Create new file?"                                         (new-file path)

The tests do NOT spin up Aider. They construct GuardedInputOutput directly with
allowed_add_paths / protected_paths and assert confirm_ask returns False (refused)
or delegates to super (which under yes=True would return "y").
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make `agent` importable when running pytest from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Stub `aider.io.InputOutput` when aider is not installed (unit-test-only env).
# Production env imports the real Aider; this stub only kicks in for CI unit tests.
try:
    from aider.io import InputOutput  # noqa: F401
except ImportError:
    import types as _types

    _aider_mod = _types.ModuleType("aider")
    _aider_io_mod = _types.ModuleType("aider.io")

    class InputOutput:  # minimal stub matching Aider 0.86.3.dev surface
        def __init__(self, *args, **kwargs):
            self.yes = kwargs.get("yes", False)

        def confirm_ask(
            self,
            question,
            default="y",
            subject=None,
            explicit_yes_required=False,
            group=None,
            allow_never=False,
        ):
            return "y" if self.yes else "n"

    _aider_io_mod.InputOutput = InputOutput
    _aider_mod.io = _aider_io_mod
    sys.modules["aider"] = _aider_mod
    sys.modules["aider.io"] = _aider_io_mod

from agent.guarded_io import GuardedInputOutput  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Construct a tiny fake repo layout with src + tests dirs."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    src_file = tmp_path / "src" / "module.py"
    src_file.write_text("x = 1\n")
    test_file = tmp_path / "tests" / "test_module.py"
    test_file.write_text("def test_x(): assert True\n")
    return tmp_path


@pytest.fixture()
def gio(repo: Path, tmp_path: Path) -> GuardedInputOutput:
    """GuardedInputOutput with src/module.py allowed, tests/test_module.py protected."""
    src_file = repo / "src" / "module.py"
    test_file = repo / "tests" / "test_module.py"
    # Use yes=True to match production agents.py configuration.
    return GuardedInputOutput(
        yes=True,
        allowed_add_paths=[str(src_file)],
        protected_paths=[str(test_file)],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProtectedPathRefused:
    """All 3 file-add prompts must refuse a path explicitly in protected_paths."""

    def test_prompt_1_add_file_refused(
        self, gio: GuardedInputOutput, repo: Path
    ) -> None:
        """Prompt 1: 'Add file to the chat?' (base_coder.py:1854)."""
        test_file = repo / "tests" / "test_module.py"
        result = gio.confirm_ask("Add file to the chat?", subject=str(test_file))
        assert result is False, "Protected test file must be refused on prompt 1"

    def test_prompt_2_allow_edits_refused(
        self, gio: GuardedInputOutput, repo: Path
    ) -> None:
        """Prompt 2: 'Allow edits to file that has not been added to the chat?'
        (base_coder.py:2360-2363) — THE EMPIRICAL EXPLOIT PATH from flake8/kimik25.
        """
        test_file = repo / "tests" / "test_module.py"
        result = gio.confirm_ask(
            "Allow edits to file that has not been added to the chat?",
            subject=str(test_file),
        )
        assert result is False, (
            "Protected test file must be refused on prompt 2 (V17 exploit)"
        )

    def test_prompt_3_create_new_file_refused(
        self, gio: GuardedInputOutput, repo: Path
    ) -> None:
        """Prompt 3: 'Create new file?' (base_coder.py:2341)."""
        # Hypothetical new test file the agent tries to create.
        new_test = repo / "tests" / "test_sneaky_new.py"
        # Add it to protected_paths so the canonical path matches.
        gio._protected_paths.add(new_test.resolve())
        result = gio.confirm_ask("Create new file?", subject=str(new_test))
        assert result is False, "Newly-created test file must be refused on prompt 3"


class TestOutsideAllowlistRefused:
    """When allowed_add_paths is non-empty, paths NOT in the allowlist are refused."""

    def test_unknown_path_refused(self, gio: GuardedInputOutput, repo: Path) -> None:
        """File neither in allowed_add_paths nor protected_paths is still refused
        because allowed_add_paths is non-empty and acts as a strict allowlist."""
        unknown = repo / "src" / "untracked.py"
        unknown.write_text("# new file model tried to add\n")
        result = gio.confirm_ask("Add file to the chat?", subject=str(unknown))
        assert result is False, "Unknown path must be refused under strict allowlist"


class TestAllowedPathDelegates:
    """A path explicitly in allowed_add_paths AND not in protected_paths
    must delegate to super().confirm_ask (which under yes=True returns 'y')."""

    def test_allowed_path_delegates_to_super(
        self, gio: GuardedInputOutput, repo: Path
    ) -> None:
        src_file = repo / "src" / "module.py"
        # Patch parent class confirm_ask to record delegation.
        with patch.object(
            GuardedInputOutput.__mro__[1],  # InputOutput parent
            "confirm_ask",
            return_value="y",
        ) as mock_super:
            result = gio.confirm_ask("Add file to the chat?", subject=str(src_file))
        assert result == "y", (
            "Allowed path must delegate (super returns 'y' under yes=True)"
        )
        mock_super.assert_called_once()


class TestSymlinkResolution:
    """Symlinks pointing INTO protected paths must be refused via canonicalization."""

    def test_symlink_to_test_file_refused(
        self, gio: GuardedInputOutput, repo: Path, tmp_path: Path
    ) -> None:
        # Create a symlink elsewhere pointing at the protected test file.
        target = repo / "tests" / "test_module.py"
        link = tmp_path / "sneaky_link.py"
        try:
            os.symlink(target, link)
        except OSError:
            pytest.skip("Filesystem does not support symlinks")
        # The model tries to add the symlink path; resolution must lead back to the protected target.
        result = gio.confirm_ask("Add file to the chat?", subject=str(link))
        assert result is False, (
            "Symlink to protected path must be refused after resolve()"
        )


class TestFailClosedOnNoneSubject:
    """confirm_ask called with subject=None on a file-add prompt must FAIL CLOSED."""

    def test_subject_none_refuses(self, gio: GuardedInputOutput) -> None:
        result = gio.confirm_ask("Add file to the chat?", subject=None)
        assert result is False, "Fail-CLOSED when subject is None on file-add prompt"

    def test_empty_subject_refuses(self, gio: GuardedInputOutput) -> None:
        result = gio.confirm_ask("Add file to the chat?", subject="")
        assert result is False, "Fail-CLOSED when subject is empty on file-add prompt"


class TestNonAddPromptPassesThrough:
    """confirm_ask for non-file-add prompts must delegate to super (no guardrail)."""

    def test_unrelated_prompt_delegates(self, gio: GuardedInputOutput) -> None:
        with patch.object(
            GuardedInputOutput.__mro__[1],
            "confirm_ask",
            return_value="y",
        ) as mock_super:
            result = gio.confirm_ask("Confirm overwrite?", subject="/some/file")
        assert result == "y", "Non-add prompts must pass through to super"
        mock_super.assert_called_once()

    def test_unrelated_prompt_with_none_subject_delegates(
        self, gio: GuardedInputOutput
    ) -> None:
        """subject=None is only fail-CLOSED on add prompts; other prompts pass through."""
        with patch.object(
            GuardedInputOutput.__mro__[1],
            "confirm_ask",
            return_value="n",
        ) as mock_super:
            result = gio.confirm_ask("Continue?", subject=None)
        assert result == "n", "Non-add prompts with subject=None must delegate"
        mock_super.assert_called_once()


class TestEmptyAllowlist:
    """When allowed_add_paths is empty (None), only protected_paths is consulted.
    Any path not in protected_paths is allowed (delegated to super)."""

    def test_empty_allowlist_allows_unknown_path(self, repo: Path) -> None:
        test_file = repo / "tests" / "test_module.py"
        gio = GuardedInputOutput(
            yes=True,
            allowed_add_paths=None,
            protected_paths=[str(test_file)],
        )
        # Unknown path with no allowlist: delegates to super.
        unknown = repo / "src" / "module.py"
        with patch.object(
            GuardedInputOutput.__mro__[1],
            "confirm_ask",
            return_value="y",
        ) as mock_super:
            result = gio.confirm_ask("Add file to the chat?", subject=str(unknown))
        assert result == "y", "Empty allowlist must allow non-protected paths"
        mock_super.assert_called_once()

    def test_empty_allowlist_still_refuses_protected(self, repo: Path) -> None:
        test_file = repo / "tests" / "test_module.py"
        gio = GuardedInputOutput(
            yes=True,
            allowed_add_paths=None,
            protected_paths=[str(test_file)],
        )
        result = gio.confirm_ask("Add file to the chat?", subject=str(test_file))
        assert result is False, (
            "Protected path must be refused even with empty allowlist"
        )
