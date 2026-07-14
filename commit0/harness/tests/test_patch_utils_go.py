"""Unit tests for commit0.harness.patch_utils_go.

Locks the .go/go.mod/go.sum filtering behaviour. Includes XFAIL anchors for
M4 (InvalidGoPatchError / validate_go_patch) so a future implementation lands
against a spec.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

MODULE = "commit0.harness.patch_utils_go"


from commit0.harness.patch_utils_go import GO_PATCH_EXTENSIONS, generate_go_patch


class TestGoPatchExtensions:
    def test_is_tuple(self) -> None:
        """Test that GO_PATCH_EXTENSIONS is a tuple (immutable)."""
        assert isinstance(GO_PATCH_EXTENSIONS, tuple)

    def test_has_expected_entries(self) -> None:
        """Test that GO_PATCH_EXTENSIONS covers Go source + module files."""
        assert ".go" in GO_PATCH_EXTENSIONS
        assert "go.mod" in GO_PATCH_EXTENSIONS
        assert "go.sum" in GO_PATCH_EXTENSIONS

    def test_no_extra_entries(self) -> None:
        """Test that GO_PATCH_EXTENSIONS is exactly three entries (no drift)."""
        assert len(GO_PATCH_EXTENSIONS) == 3


class TestGenerateGoPatch:
    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_calls_upstream_with_repo_object(self, mock_gen, mock_repo) -> None:
        """Test that generate_go_patch passes a git.Repo to the upstream helper."""
        mock_gen.return_value = "diff --git a/main.go b/main.go\n+x\n"
        generate_go_patch("/repo", "abc123", "def456")
        mock_repo.assert_called_once_with("/repo")
        mock_gen.assert_called_once_with(mock_repo.return_value, "abc123", "def456")

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_keeps_go_source(self, mock_gen, mock_repo) -> None:
        """Test that .go source files survive the filter."""
        mock_gen.return_value = (
            "diff --git a/pkg/foo.go b/pkg/foo.go\n"
            "--- a/pkg/foo.go\n+++ b/pkg/foo.go\n+line\n"
        )
        result = generate_go_patch("/r", "a", "b")
        assert "pkg/foo.go" in result

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_keeps_go_mod(self, mock_gen, mock_repo) -> None:
        """Test that go.mod is retained."""
        mock_gen.return_value = (
            "diff --git a/go.mod b/go.mod\n+require x v1\n"
        )
        result = generate_go_patch("/r", "a", "b")
        assert "go.mod" in result

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_keeps_go_sum(self, mock_gen, mock_repo) -> None:
        """Test that go.sum is retained."""
        mock_gen.return_value = (
            "diff --git a/go.sum b/go.sum\n+hash\n"
        )
        result = generate_go_patch("/r", "a", "b")
        assert "go.sum" in result

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_drops_non_go(self, mock_gen, mock_repo) -> None:
        """Test that non-Go files (README.md, Makefile, .py) are dropped."""
        mock_gen.return_value = (
            "diff --git a/main.go b/main.go\n+x\n"
            "diff --git a/README.md b/README.md\n+y\n"
            "diff --git a/Makefile b/Makefile\n+z\n"
            "diff --git a/tool.py b/tool.py\n+q\n"
        )
        result = generate_go_patch("/r", "a", "b")
        assert "main.go" in result
        assert "README.md" not in result
        assert "Makefile" not in result
        assert "tool.py" not in result

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_empty_patch_returned_as_is(self, mock_gen, mock_repo) -> None:
        """Test that an empty upstream patch is returned unchanged."""
        mock_gen.return_value = ""
        assert generate_go_patch("/r", "a", "b") == ""

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_whitespace_only_patch_returned_as_is(self, mock_gen, mock_repo) -> None:
        """Test that a whitespace-only patch is returned unchanged (no filtering)."""
        mock_gen.return_value = "   \n   \n"
        result = generate_go_patch("/r", "a", "b")
        assert result == "   \n   \n"

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_only_non_go_returns_empty(self, mock_gen, mock_repo) -> None:
        """Test that a patch containing only non-Go files is filtered to ''."""
        mock_gen.return_value = (
            "diff --git a/README.md b/README.md\n+y\n"
        )
        result = generate_go_patch("/r", "a", "b")
        assert result == ""

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_malformed_diff_header_raises_strict(self, mock_gen, mock_repo) -> None:
        """Test N6: malformed 'diff --git' header raises InvalidGoPatchError in strict mode (was silent hunk drop)."""
        mock_gen.return_value = (
            "diff --git broken\n+line-inside-broken-section\n"
            "diff --git a/main.go b/main.go\n+good\n"
        )
        with pytest.raises(InvalidGoPatchError, match="malformed 'diff --git' header"):
            generate_go_patch("/r", "a", "b")

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_malformed_diff_header_lenient_drops_and_warns(self, mock_gen, mock_repo, caplog) -> None:
        """Test N6: malformed header with strict=False drops the hunk and warns (legacy behaviour preserved)."""
        import logging
        mock_gen.return_value = (
            "diff --git broken\n+line-inside-broken-section\n"
            "diff --git a/main.go b/main.go\n+good\n"
        )
        with caplog.at_level(logging.WARNING, logger=MODULE):
            result = generate_go_patch("/r", "a", "b", strict=False)
        assert "main.go" in result
        assert "line-inside-broken-section" not in result
        assert any("malformed" in r.message for r in caplog.records)

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_multiple_go_sections_preserved(self, mock_gen, mock_repo) -> None:
        """Test that every .go section is retained across a multi-file patch."""
        mock_gen.return_value = (
            "diff --git a/a.go b/a.go\n+a\n"
            "diff --git a/b.go b/b.go\n+b\n"
            "diff --git a/c.go b/c.go\n+c\n"
        )
        result = generate_go_patch("/r", "a", "b")
        assert "a.go" in result
        assert "b.go" in result
        assert "c.go" in result

    @patch(f"{MODULE}.gitpython.Repo", side_effect=Exception("not a repo"))
    def test_invalid_repo_propagates(self, mock_repo) -> None:
        """Test that a bad repo path propagates the underlying exception."""
        with pytest.raises(Exception, match="not a repo"):
            generate_go_patch("/bad", "a", "b")


class TestModuleExports:
    def test_all_exports_generate_go_patch(self) -> None:
        """Test that generate_go_patch is publicly exported via __all__."""
        import commit0.harness.patch_utils_go as mod
        assert "generate_go_patch" in mod.__all__
        assert "validate_go_patch" in mod.__all__
        assert "InvalidGoPatchError" in mod.__all__
        assert "GO_PATCH_EXTENSIONS" in mod.__all__


# ===== M4: validate_go_patch =====
from commit0.harness.patch_utils_go import validate_go_patch, InvalidGoPatchError


class TestValidateGoPatch:
    def test_empty_patch_is_valid(self) -> None:
        """Test that an empty patch validates as clean (no headers to check)."""
        assert validate_go_patch("") is True

    def test_pure_go_patch_is_valid(self) -> None:
        """Test that a patch containing only .go files validates clean."""
        patch = (
            "diff --git a/main.go b/main.go\n"
            "--- a/main.go\n+++ b/main.go\n+line\n"
        )
        assert validate_go_patch(patch) is True

    def test_go_mod_and_go_sum_are_valid(self) -> None:
        """Test that go.mod and go.sum are permitted paths."""
        patch = (
            "diff --git a/go.mod b/go.mod\n+require x v1\n"
            "diff --git a/go.sum b/go.sum\n+hash\n"
        )
        assert validate_go_patch(patch) is True

    def test_non_go_file_rejected(self) -> None:
        """Test that a diff --git for a non-Go file (README.md) fails validation."""
        patch = (
            "diff --git a/main.go b/main.go\n+x\n"
            "diff --git a/README.md b/README.md\n+y\n"
        )
        assert validate_go_patch(patch) is False

    def test_python_file_rejected(self) -> None:
        """Test that a .py path is caught by validate_go_patch."""
        patch = "diff --git a/tool.py b/tool.py\n+x\n"
        assert validate_go_patch(patch) is False

    def test_makefile_rejected(self) -> None:
        """Test that Makefile (no extension match) is rejected."""
        patch = "diff --git a/Makefile b/Makefile\n+all:\n"
        assert validate_go_patch(patch) is False

    def test_dev_null_side_is_ok_when_other_is_go(self) -> None:
        """Test that a create/delete (/dev/null on one side) validates if the other side is Go."""
        patch = "diff --git a/new.go b/new.go\n--- /dev/null\n+++ b/new.go\n+package main\n"
        assert validate_go_patch(patch) is True

    def test_malformed_diff_header_rejected(self) -> None:
        """Test that a diff --git header not matching the expected shape is rejected."""
        # Missing 'b/' path
        patch = "diff --git broken\n+line\n"
        assert validate_go_patch(patch) is False

    def test_content_line_mentioning_py_is_ok(self) -> None:
        """Test that content lines (not headers) mentioning non-Go paths are ignored."""
        # A Go source file that has a string "tool.py" inside — should pass.
        patch = (
            "diff --git a/main.go b/main.go\n"
            "+    exec(\"tool.py\")\n"
        )
        assert validate_go_patch(patch) is True


class TestGenerateGoPatchStrictMode:
    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_strict_true_default_raises_on_polluted_input(
        self, mock_gen, mock_repo
    ) -> None:
        """Test that a filter regression (non-Go path in the filtered output) raises
        InvalidGoPatchError when strict=True is the default.
        """
        # Craft an upstream response the filter WOULD strip cleanly — to force the
        # validator to see something bad, we short-circuit by patching
        # validate_go_patch to return False (simulating a filter regression
        # without needing to invent an upstream shape the current filter misses).
        with patch(f"{MODULE}.validate_go_patch", return_value=False):
            mock_gen.return_value = "diff --git a/main.go b/main.go\n+x\n"
            with pytest.raises(InvalidGoPatchError):
                generate_go_patch("/r", "a", "b")

    @patch(f"{MODULE}.gitpython.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_strict_false_returns_polluted_patch_with_warning(
        self, mock_gen, mock_repo, caplog
    ) -> None:
        """Test that strict=False logs but returns the (polluted) patch, matching Rust."""
        import logging
        with patch(f"{MODULE}.validate_go_patch", return_value=False):
            mock_gen.return_value = "diff --git a/main.go b/main.go\n+x\n"
            with caplog.at_level(logging.WARNING, logger=MODULE):
                result = generate_go_patch("/r", "a", "b", strict=False)
        assert "main.go" in result
        assert any("validation failed" in r.message for r in caplog.records)

    def test_invalid_go_patch_error_preserves_patch(self) -> None:
        """Test that InvalidGoPatchError.patch attribute preserves the raw text."""
        err = InvalidGoPatchError("diff --git a/x.md b/x.md\n+y\n", "bad")
        assert err.patch == "diff --git a/x.md b/x.md\n+y\n"
        assert str(err) == "bad"

    def test_invalid_go_patch_error_default_message(self) -> None:
        """Test that InvalidGoPatchError has a sensible default message."""
        err = InvalidGoPatchError("patch text")
        assert "non-Go" in str(err)
