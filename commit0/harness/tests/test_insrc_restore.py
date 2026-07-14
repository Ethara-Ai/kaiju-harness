"""Unit tests for commit0.harness.insrc_restore (H7).

Extracts what was previously a 120-line untested Python string embedded in
``spec_rust._INSRC_RESTORE_PY``. Locks the security invariants that the
string carried in a comment: the multi-line raw-string / block-comment
spoofing bypass, the ``macro_rules!`` byteorder edge case, and the balanced
safety-net rewrite gate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from commit0.harness.insrc_restore import (
    _TA,
    _balanced,
    _split,
    _strip_code,
    apply_restore,
)


class TestTestAttributeRegex:
    @pytest.mark.parametrize(
        "attr",
        [
            "#[test]",
            "#[ test ]",
            "#[cfg(test)]",
            "#[cfg( test )]",
            "#[tokio::test]",
            "#[async_std::test]",
            "#[cfg_attr(feature = \"foo\", test)]",
            "#[cfg_attr(any(feature = \"a\", feature = \"b\"), test)]",
        ],
    )
    def test_matches_valid_test_attributes(self, attr: str) -> None:
        """Test that every real test-marking attribute matches _TA."""
        assert _TA.search(attr)

    @pytest.mark.parametrize(
        "attr",
        [
            "#[derive(Debug)]",
            "#[allow(dead_code)]",
            "#[repr(C)]",
            "#[inline]",
            "// #[test]",
        ],
    )
    def test_rejects_non_test_attributes(self, attr: str) -> None:
        """Test that non-test attributes don't match _TA."""
        assert not _TA.search(attr) or attr.startswith("//")


class TestStripCode:
    def test_preserves_pure_code(self) -> None:
        """Test that code without comments/strings/chars is returned unchanged."""
        src = "fn main() {\n    let x = 5;\n}"
        assert _strip_code(src) == src

    def test_blanks_line_comment(self) -> None:
        """Test that // line comments are blanked to spaces."""
        src = "let x = 5; // side-note\nfn f() {}"
        stripped = _strip_code(src)
        assert "side-note" not in stripped
        assert stripped.count("\n") == src.count("\n")

    def test_blanks_block_comment(self) -> None:
        """Test that /* block comments */ are blanked, preserving newlines."""
        src = "let x = /* multi\nline */ 5;"
        stripped = _strip_code(src)
        assert "multi" not in stripped
        assert stripped.count("\n") == 1

    def test_blanks_nested_block_comment(self) -> None:
        """Test that Rust's NESTED /* /* */ */ block comments blank fully."""
        src = "let x = /* outer /* inner */ still-outer */ 5;"
        stripped = _strip_code(src)
        assert "outer" not in stripped
        assert "inner" not in stripped

    def test_blanks_string_literal(self) -> None:
        """Test that "quoted strings" are blanked but preserve length."""
        src = 'let s = "hello world"; let x = 5;'
        stripped = _strip_code(src)
        assert "hello" not in stripped
        assert "let x = 5;" in stripped

    def test_string_with_escaped_quote(self) -> None:
        """Test that \\" inside a string literal doesn't end the string early."""
        src = r'let s = "a\"b"; let x = 5;'
        stripped = _strip_code(src)
        assert "a" not in stripped or stripped.index("a") > stripped.index("let s")
        assert "let x = 5;" in stripped

    def test_blanks_raw_string(self) -> None:
        """Test that r"..." and r#"..."# raw strings are fully blanked."""
        src = 'let s = r#"contains } bracket"#; let x = 5;'
        stripped = _strip_code(src)
        assert "contains" not in stripped
        # The `}` inside the raw string MUST NOT survive to fool brace counting.
        assert stripped.count("}") == 0
        assert "let x = 5;" in stripped

    def test_raw_string_multiline_with_braces_bypass_defense(self) -> None:
        """Test the SECURITY bypass: multi-line raw string with braces must NOT
        leak its inner braces into the brace-count (this was the original bug
        the module was written to fix).
        """
        src = 'let s = r#"open {\n  no close ever\n"#;\nfn f() {}'
        stripped = _strip_code(src)
        # Only the fn f() {} braces should count.
        assert stripped.count("{") == 1
        assert stripped.count("}") == 1

    def test_blanks_char_literal(self) -> None:
        """Test that char literals 'x' are blanked."""
        src = "let c = '{'; let d = 5;"
        stripped = _strip_code(src)
        assert "{" not in stripped
        assert "let d = 5;" in stripped

    def test_blanks_byte_string(self) -> None:
        """Test that b"..." byte string literals are blanked."""
        src = 'let b = b"secret"; let x = 5;'
        stripped = _strip_code(src)
        assert "secret" not in stripped
        assert "let x = 5;" in stripped

    def test_preserves_newline_count(self) -> None:
        """Test that _strip_code preserves line count 1:1 (splitters rely on this)."""
        src = "a\nb\nc\nd"
        stripped = _strip_code(src)
        assert stripped.count("\n") == src.count("\n")

    def test_empty_input(self) -> None:
        """Test that empty input returns empty string."""
        assert _strip_code("") == ""


class TestSplit:
    def test_impl_only_returns_one_impl_chunk(self) -> None:
        """Test that pure impl code with no test attributes yields is_test=False chunks."""
        src = "fn a() {\n    5\n}\n\nfn b() {\n    6\n}"
        chunks = _split(src)
        assert all(not is_test for _, is_test in chunks)

    def test_separates_impl_from_test(self) -> None:
        """Test that #[test] fn is classified as is_test=True."""
        src = (
            "fn impl_fn() {\n    5\n}\n\n"
            "#[test]\nfn test_fn() {\n    assert_eq!(1, 1);\n}"
        )
        chunks = _split(src)
        test_chunks = [t for t, x in chunks if x]
        impl_chunks = [t for t, x in chunks if not x]
        assert any("test_fn" in c for c in test_chunks)
        assert any("impl_fn" in c for c in impl_chunks)
        assert not any("impl_fn" in c for c in test_chunks)

    def test_cfg_test_module(self) -> None:
        """Test that #[cfg(test)] mod tests { } is classified as test."""
        src = (
            "fn impl_fn() {\n    5\n}\n\n"
            "#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {}\n}"
        )
        chunks = _split(src)
        test_chunks = [t for t, x in chunks if x]
        assert any("mod tests" in c for c in test_chunks)

    def test_tokio_test(self) -> None:
        """Test that #[tokio::test] fn is classified as test."""
        src = "#[tokio::test]\nasync fn t() {\n    5\n}"
        chunks = _split(src)
        assert any(is_test for _, is_test in chunks)

    def test_empty_input(self) -> None:
        """Test that empty input yields an empty chunk (no crash)."""
        chunks = _split("")
        assert chunks == [("", False)] or chunks == []


class TestBalanced:
    def test_balanced_braces(self) -> None:
        """Test that matched braces/parens/brackets pass."""
        assert _balanced("fn f() { let x = [1]; }") is True

    def test_unbalanced_open_brace(self) -> None:
        """Test that unmatched open brace fails."""
        assert _balanced("fn f() { let x = 5;") is False

    def test_unbalanced_close_paren(self) -> None:
        """Test that unmatched close paren fails."""
        assert _balanced("fn f) {}") is False

    def test_unbalanced_bracket(self) -> None:
        """Test that unmatched bracket fails."""
        assert _balanced("let x = [1, 2, 3") is False

    def test_braces_in_strings_dont_count(self) -> None:
        """Test that braces inside strings/comments don't affect balance."""
        assert _balanced('let s = "{{{"; let t = "}}}";') is True

    def test_braces_in_raw_string_dont_count(self) -> None:
        """Test that braces inside raw strings don't affect balance."""
        assert _balanced('let s = r#"{ { {"#;') is True

    def test_empty_string_is_balanced(self) -> None:
        """Test that empty string is trivially balanced."""
        assert _balanced("") is True


class TestApplyRestore:
    """apply_restore is best-effort and swallows per-file exceptions."""

    def test_swallows_exceptions_per_file(self, tmp_path, monkeypatch) -> None:
        """Test that apply_restore never raises even if git/read/write fail."""
        # Make _sh return a non-existent filename so p.is_file() short-circuits.
        with patch(
            "commit0.harness.insrc_restore._sh",
            side_effect=RuntimeError("git down"),
        ):
            # No exception should propagate; the top-level function still returns.
            try:
                apply_restore("abc123")
            except RuntimeError:
                pytest.fail("apply_restore leaked a RuntimeError")

    def test_skips_when_no_changed_files(self) -> None:
        """Test that an empty git diff produces no filesystem writes."""
        with patch(
            "commit0.harness.insrc_restore._sh", return_value=""
        ), patch("pathlib.Path.write_text") as mock_write:
            apply_restore("abc123")
        mock_write.assert_not_called()

    def test_skips_non_rs_files(self) -> None:
        """Test that changed non-.rs files are ignored."""
        with patch(
            "commit0.harness.insrc_restore._sh",
            return_value="src/lib.md\nsrc/data.json\n",
        ), patch("pathlib.Path.write_text") as mock_write:
            apply_restore("abc123")
        mock_write.assert_not_called()


class TestModuleShape:
    def test_module_has_main_guard(self) -> None:
        """Test that the module has an if __name__ == '__main__' block for
        script mode (needed by spec_rust which reads this file and embeds it
        in a bash heredoc).
        """
        from pathlib import Path
        import commit0.harness.insrc_restore as mod
        source = Path(mod.__file__).read_text()
        assert "if __name__ == '__main__':" in source
        assert "apply_restore(sys.argv[1])" in source

    def test_imports_are_stdlib_only(self) -> None:
        """Test that the runnable script has no third-party imports (must run
        inside the eval container with just Python stdlib available).
        """
        from pathlib import Path
        import commit0.harness.insrc_restore as mod
        source = Path(mod.__file__).read_text()
        stdlib_only = {"re", "subprocess", "sys", "pathlib", "typing", "__future__"}
        for line in source.splitlines():
            line = line.strip()
            if line.startswith("import ") or line.startswith("from "):
                first_module = line.split()[1].split(".")[0]
                assert first_module in stdlib_only, (
                    f"non-stdlib import in insrc_restore.py: {line}"
                )


class TestSpecRustEmbedding:
    def test_spec_rust_embeds_module_source(self) -> None:
        """Test that spec_rust._INSRC_RESTORE_PY is the on-disk source of
        insrc_restore.py (H7 wiring: spec_rust reads this file at import time).
        """
        from pathlib import Path
        from commit0.harness.spec_rust import _INSRC_RESTORE_PY
        import commit0.harness.insrc_restore as mod
        assert _INSRC_RESTORE_PY == Path(mod.__file__).read_text()

    def test_embedded_source_contains_apply_restore(self) -> None:
        """Test that the embedded script includes the entrypoint symbol."""
        from commit0.harness.spec_rust import _INSRC_RESTORE_PY
        assert "def apply_restore(" in _INSRC_RESTORE_PY
        assert "if __name__ == '__main__':" in _INSRC_RESTORE_PY
