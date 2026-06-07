"""Exhaustive unit tests for commit0.harness.patch_utils_rust."""

import pytest
from unittest.mock import patch

MODULE = "commit0.harness.patch_utils_rust"


# ===== _section_is_target =====
from commit0.harness.patch_utils_rust import _section_is_target


class TestSectionIsTarget:
    def test_target_a_path(self):
        line = "diff --git a/target/debug/build b/target/debug/build"
        assert _section_is_target(line) is True

    def test_target_b_path(self):
        line = "diff --git a/src/main.rs b/target/release/out"
        assert _section_is_target(line) is True

    def test_non_target_path(self):
        line = "diff --git a/src/lib.rs b/src/lib.rs"
        assert _section_is_target(line) is False

    def test_not_diff_line(self):
        line = "+++ b/target/foo"
        assert _section_is_target(line) is False

    def test_empty_string(self):
        assert _section_is_target("") is False

    def test_target_in_name_but_not_dir(self):
        # "target_utils.rs" should not match
        line = "diff --git a/src/target_utils.rs b/src/target_utils.rs"
        assert _section_is_target(line) is False

    def test_nested_target_dir(self):
        line = "diff --git a/target/release/deps/foo b/target/release/deps/foo"
        assert _section_is_target(line) is True

    def test_multiline_section_only_checks_first(self):
        section = "diff --git a/src/lib.rs b/src/lib.rs\n+++ b/target/foo"
        assert _section_is_target(section) is False

    def test_cargo_lock_not_target(self):
        line = "diff --git a/Cargo.lock b/Cargo.lock"
        assert _section_is_target(line) is False

    def test_target_at_root(self):
        line = "diff --git a/target/file.o b/target/file.o"
        assert _section_is_target(line) is True


# ===== _filter_target_dir =====
from commit0.harness.patch_utils_rust import _filter_target_dir


class TestFilterTargetDir:
    def test_empty_patch(self):
        assert _filter_target_dir("") == ""

    def test_whitespace_only(self):
        assert _filter_target_dir("   \n  ") == "   \n  "

    def test_no_target_sections(self):
        patch = "diff --git a/src/lib.rs b/src/lib.rs\n+line\n"
        assert _filter_target_dir(patch) == patch

    def test_only_target_sections(self):
        patch = "diff --git a/target/debug/foo b/target/debug/foo\n+line\n"
        result = _filter_target_dir(patch)
        assert result == "\n\n"

    def test_mixed_keeps_non_target(self):
        patch = (
            "diff --git a/src/lib.rs b/src/lib.rs\n+good\n"
            "diff --git a/target/debug/x b/target/debug/x\n+bad\n"
        )
        result = _filter_target_dir(patch)
        assert "src/lib.rs" in result
        assert "target/debug" not in result

    def test_preserves_cargo_lock(self):
        patch = (
            "diff --git a/Cargo.lock b/Cargo.lock\n+dep\n"
            "diff --git a/target/x b/target/x\n+bad\n"
        )
        result = _filter_target_dir(patch)
        assert "Cargo.lock" in result
        assert "target/x" not in result

    def test_multiple_non_target_sections(self):
        patch = (
            "diff --git a/src/a.rs b/src/a.rs\n+a\n"
            "diff --git a/src/b.rs b/src/b.rs\n+b\n"
            "diff --git a/src/c.rs b/src/c.rs\n+c\n"
        )
        result = _filter_target_dir(patch)
        assert "a.rs" in result
        assert "b.rs" in result
        assert "c.rs" in result

    def test_multiple_target_sections_all_removed(self):
        patch = (
            "diff --git a/target/a b/target/a\n+x\n"
            "diff --git a/target/b b/target/b\n+y\n"
        )
        result = _filter_target_dir(patch)
        assert result == "\n\n"

    def test_leading_text_before_first_diff(self):
        patch = "some header\ndiff --git a/src/lib.rs b/src/lib.rs\n+line\n"
        result = _filter_target_dir(patch)
        assert "src/lib.rs" in result

    def test_returns_string(self):
        assert isinstance(_filter_target_dir(""), str)


# ===== validate_rust_patch =====
from commit0.harness.patch_utils_rust import validate_rust_patch


class TestValidateRustPatch:
    def test_clean_patch(self):
        patch = "diff --git a/src/lib.rs b/src/lib.rs\n+line\n"
        assert validate_rust_patch(patch) is True

    def test_empty_patch(self):
        assert validate_rust_patch("") is True

    def test_target_in_diff_header(self):
        patch = "diff --git a/target/debug/foo b/target/debug/foo\n+x\n"
        assert validate_rust_patch(patch) is False

    def test_target_in_plus_header(self):
        patch = "+++ b/target/release/out\n"
        assert validate_rust_patch(patch) is False

    def test_target_in_minus_header(self):
        patch = "--- a/target/debug/build\n"
        assert validate_rust_patch(patch) is False

    def test_target_in_binary_files(self):
        patch = "Binary files a/target/foo and b/target/foo differ\n"
        assert validate_rust_patch(patch) is False

    def test_target_in_content_line_ok(self):
        # target/ in a regular content line (not header) is fine
        patch = "+    let p = Path::new(\"target/debug\");\n"
        assert validate_rust_patch(patch) is True

    def test_cargo_lock_is_valid(self):
        patch = "diff --git a/Cargo.lock b/Cargo.lock\n+dep\n"
        assert validate_rust_patch(patch) is True

    def test_mixed_clean_and_dirty(self):
        patch = (
            "diff --git a/src/lib.rs b/src/lib.rs\n+good\n"
            "diff --git a/target/x b/target/x\n+bad\n"
        )
        assert validate_rust_patch(patch) is False

    def test_nested_target(self):
        patch = "diff --git a/target/release/deps/x b/target/release/deps/x\n"
        assert validate_rust_patch(patch) is False

    @pytest.mark.parametrize("prefix", ["+++ ", "--- "])
    @pytest.mark.parametrize("side", ["a", "b"])
    def test_header_variants(self, prefix, side):
        patch = f"{prefix}{side}/target/foo\n"
        assert validate_rust_patch(patch) is False

    def test_target_as_filename_not_dir(self):
        # /target without trailing slash in diff header
        patch = "diff --git a/target_file b/target_file\n"
        assert validate_rust_patch(patch) is True

    def test_binary_without_target_ok(self):
        patch = "Binary files a/src/data.bin and b/src/data.bin differ\n"
        assert validate_rust_patch(patch) is True

    def test_multiline_patch_one_bad(self):
        lines = [
            "diff --git a/src/lib.rs b/src/lib.rs",
            "--- a/src/lib.rs",
            "+++ b/src/lib.rs",
            "+good line",
            "diff --git a/target/x b/target/x",
            "--- a/target/x",
            "+++ b/target/x",
            "+bad line",
        ]
        assert validate_rust_patch("\n".join(lines)) is False


# ===== generate_rust_patch =====
from commit0.harness.patch_utils_rust import generate_rust_patch


class TestGenerateRustPatch:
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_calls_generate_patch(self, mock_gen, mock_repo):
        mock_gen.return_value = "diff --git a/src/lib.rs b/src/lib.rs\n+x\n"
        result = generate_rust_patch("/repo", "abc", "def")
        mock_repo.assert_called_once_with("/repo")
        mock_gen.assert_called_once_with(mock_repo.return_value, "abc", "def")
        assert "src/lib.rs" in result

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_filters_target_dir(self, mock_gen, mock_repo):
        mock_gen.return_value = (
            "diff --git a/src/lib.rs b/src/lib.rs\n+good\n"
            "diff --git a/target/x b/target/x\n+bad\n"
        )
        result = generate_rust_patch("/repo", "a", "b")
        assert "src/lib.rs" in result
        assert "target/x" not in result

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_empty_patch(self, mock_gen, mock_repo):
        mock_gen.return_value = ""
        result = generate_rust_patch("/repo", "a", "b")
        assert result == ""

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_all_target_returns_newlines(self, mock_gen, mock_repo):
        mock_gen.return_value = "diff --git a/target/x b/target/x\n+bad\n"
        result = generate_rust_patch("/repo", "a", "b")
        assert result == "\n\n"

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.generate_patch_between_commits")
    def test_preserves_cargo_lock(self, mock_gen, mock_repo):
        mock_gen.return_value = (
            "diff --git a/Cargo.lock b/Cargo.lock\n+dep\n"
            "diff --git a/target/x b/target/x\n+bad\n"
        )
        result = generate_rust_patch("/repo", "a", "b")
        assert "Cargo.lock" in result

    @patch(f"{MODULE}.git.Repo", side_effect=Exception("not a repo"))
    def test_invalid_repo_raises(self, mock_repo):
        with pytest.raises(Exception, match="not a repo"):
            generate_rust_patch("/bad", "a", "b")


# ===== Edge cases / Integration =====
class TestPatchEdgeCases:
    def test_filter_and_validate_consistency(self):
        """After filtering, the result should validate clean."""
        dirty_patch = (
            "diff --git a/src/lib.rs b/src/lib.rs\n+good\n"
            "diff --git a/target/x b/target/x\n+bad\n"
        )
        filtered = _filter_target_dir(dirty_patch)
        assert validate_rust_patch(filtered) is True

    def test_clean_patch_filter_is_idempotent(self):
        clean = "diff --git a/src/lib.rs b/src/lib.rs\n+line\n"
        assert _filter_target_dir(clean) == clean

    def test_double_filter_same_result(self):
        dirty = (
            "diff --git a/src/a.rs b/src/a.rs\n+a\n"
            "diff --git a/target/x b/target/x\n+bad\n"
        )
        once = _filter_target_dir(dirty)
        twice = _filter_target_dir(once)
        assert once == twice

    @pytest.mark.parametrize("target_path", [
        "target/debug/build",
        "target/release/out",
        "target/rls/x",
        "target/doc/index.html",
        "target/.rustc_info.json",
    ])
    def test_various_target_subdirs_filtered(self, target_path):
        patch = f"diff --git a/{target_path} b/{target_path}\n+x\n"
        filtered = _filter_target_dir(patch)
        assert target_path not in filtered

    @pytest.mark.parametrize("safe_path", [
        "src/target_utils.rs",
        "tests/target_test.rs",
        "benches/target_bench.rs",
    ])
    def test_target_in_filename_not_filtered(self, safe_path):
        patch = f"diff --git a/{safe_path} b/{safe_path}\n+x\n"
        filtered = _filter_target_dir(patch)
        assert safe_path in filtered

    def test_large_patch_performance(self):
        """Ensure filter handles large patches without issues."""
        sections = []
        for i in range(100):
            sections.append(f"diff --git a/src/mod{i}.rs b/src/mod{i}.rs\n+line{i}\n")
        for i in range(50):
            sections.append(f"diff --git a/target/debug/dep{i} b/target/debug/dep{i}\n+x\n")
        patch = "".join(sections)
        filtered = _filter_target_dir(patch)
        assert validate_rust_patch(filtered) is True
        for i in range(100):
            assert f"mod{i}.rs" in filtered
        for i in range(50):
            assert f"dep{i}" not in filtered
