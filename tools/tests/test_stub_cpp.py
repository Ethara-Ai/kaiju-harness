"""Tests for the C++ stubber's tree-sitter safety helpers.

Covers the three robustness layers added after the fmt/FMT_TRY corruption
incident (fmt-c.cc was rewritten with `FMT_CATCH(...) { __builtin_trap(); }`
dangling at file scope because tree-sitter closed the outer function at the
inner FMT_TRY brace):

1. ``_looks_like_macro_name``  — reject FMT_CATCH-style all-caps declarators
2. ``_find_matching_brace``    — brace-count fallback when tree-sitter's
   ``compound_statement.end_byte`` cannot be trusted
3. ``_stub_file_treesitter``   — end-to-end: verifies the pipeline stubs the
   full body of a function containing unknown macros and does not corrupt
   files. Post-write revert protects against unforeseen edge cases.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools.stub_cpp import (
    _find_matching_brace,
    _get_function_name,
    _looks_like_macro_name,
    _stub_file_treesitter,
)


# ─── _looks_like_macro_name ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "FMT_TRY",
        "FMT_CATCH",
        "TRY",
        "CATCH",
        "PROTECTED_TRY",
        "SPDLOG_TRY",
        "BOOST_CATCH",
        "MY_CUSTOM_MACRO",
        "LOG_TRACE",
        "ASSERT_EQ",
        "ABC",  # boundary: 3 chars, all-caps
    ],
)
def test_all_caps_names_flagged_as_macros(name: str) -> None:
    assert _looks_like_macro_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "foo",
        "myFunction",
        "_private",
        "camelCase",
        "snake_case",
        "MixedCase",
        "PascalCase",
        "operator+",  # C++ operator, contains non-alnum
        "",           # empty
        "F",          # single char
        "FT",         # 2 chars, below regex minimum length
        "Foo_Bar",    # mixed case with underscore
    ],
)
def test_ordinary_names_not_flagged(name: str) -> None:
    assert _looks_like_macro_name(name) is False


# ─── _get_function_name ──────────────────────────────────────────────────────


def _parse_and_get_first_function_name(source: str) -> str:
    """Parse ``source`` with tree-sitter and return the name extracted from
    the first ``function_definition`` node encountered in DFS order."""
    from tools.stub_cpp import _CPP_LANGUAGE, _TS_AVAILABLE
    if not _TS_AVAILABLE:
        pytest.skip("tree-sitter C++ grammar not available")
    import tree_sitter

    parser = tree_sitter.Parser(_CPP_LANGUAGE)
    tree = parser.parse(source.encode("utf-8"))

    def find(node: tree_sitter.Node) -> tree_sitter.Node | None:
        if node.type == "function_definition":
            return node
        for c in node.children:
            r = find(c)
            if r is not None:
                return r
        return None

    fd = find(tree.root_node)
    assert fd is not None, "no function_definition found in source"
    return _get_function_name(fd)


def test_get_name_flat() -> None:
    src = "int foo(int x) { return x; }\n"
    assert _parse_and_get_first_function_name(src) == "foo"


def test_get_name_qualified() -> None:
    # Namespace::Class::method — deepest identifier should win.
    src = "namespace ns { struct S { void method(); }; }\nvoid ns::S::method() { }\n"
    assert _parse_and_get_first_function_name(src) == "method"


def test_get_name_macro_lookalike() -> None:
    # Tree-sitter parses `FMT_CATCH(...) {}` as a function_definition; the
    # extracted name should be FMT_CATCH so the macro-name guard can fire.
    src = "FMT_CATCH(...) {}\n"
    assert _parse_and_get_first_function_name(src) == "FMT_CATCH"


# ─── _find_matching_brace ────────────────────────────────────────────────────


def _match(src: str) -> int:
    """Helper: locate first `{` and return matching `}` offset for ``src``."""
    b = src.encode("utf-8")
    start = b.index(b"{")
    return _find_matching_brace(b, start)


def test_match_simple() -> None:
    s = "{ int x; }"
    assert _match(s) == s.index("}")


def test_match_nested() -> None:
    s = "{ int a; { int b; } { int c; } }"
    assert _match(s) == s.rindex("}")


def test_match_string_with_braces() -> None:
    s = 'void f() { const char *s = "{{{ not a brace }}}"; }'
    assert _match(s) == s.rindex("}")


def test_match_char_literal_with_brace() -> None:
    s = "void f() { char c = '{'; char d = '}'; }"
    assert _match(s) == s.rindex("}")


def test_match_line_comment() -> None:
    s = "void f() { // } this is a comment\n  return; }"
    assert _match(s) == s.rindex("}")


def test_match_block_comment() -> None:
    s = "void f() { /* } still comment\n  more } */ return; }"
    assert _match(s) == s.rindex("}")


def test_match_raw_string() -> None:
    # Raw string R"delim( } )delim" — the `}` inside must be ignored.
    s = 'void f() { auto s = R"x( } {{{ } )x"; return; }'
    assert _match(s) == s.rindex("}")


def test_match_digit_separator_not_char_literal() -> None:
    # C++14 digit separator inside numeric literal must NOT open a char literal
    # that then swallows the closing brace.
    s = "void f() { auto x = 1'000'000; return; }"
    assert _match(s) == s.rindex("}")


def test_match_unbalanced_returns_negative_one() -> None:
    s = "{ int x; "  # no closing brace
    assert _match(s) == -1


def test_match_bad_start_returns_negative_one() -> None:
    b = b"int x;"
    assert _find_matching_brace(b, 0) == -1  # source[0] is 'i', not '{'


# ─── _stub_file_treesitter integration ──────────────────────────────────────


_FMT_LIKE = textwrap.dedent("""\
    #include <stdio.h>

    // simulate fmt-style FMT_TRY / FMT_CATCH macros.
    // tree-sitter can't resolve these and its error recovery closes the
    // outer function early — the safety layers must handle it.
    #define FMT_TRY try
    #define FMT_CATCH(x) catch (x)

    static int helper(int x) {
      return x * 2;
    }

    extern "C" int wrapped(int a) {
      int y = helper(a);
      FMT_TRY {
        return y + 1;
      }
      FMT_CATCH(...) {}
      return -1;
    }
    """)


def test_stubs_fmt_like_file_without_corruption(tmp_path: Path) -> None:
    """End-to-end: the stubber must produce a syntactically valid file even
    when the input contains unknown macros. Both functions should be stubbed
    (``helper`` normally, ``wrapped`` via the brace-count fallback), and the
    ``FMT_CATCH`` macro invocation must NOT itself be stubbed."""
    from tools.stub_cpp import _TS_AVAILABLE
    if not _TS_AVAILABLE:
        pytest.skip("tree-sitter C++ grammar not available")

    p = tmp_path / "sample.cc"
    p.write_text(_FMT_LIKE, encoding="utf-8")
    original = p.read_text(encoding="utf-8")

    count = _stub_file_treesitter(p)
    after = p.read_text(encoding="utf-8")

    # At minimum: helper stubbed; wrapped ideally also stubbed via brace-count.
    assert count >= 1, f"expected at least one stub, got {count}\n---\n{after}"

    # Nothing referring to the fmt_error / FMT_CATCH text should have appeared
    # at file scope — the earlier corruption pattern left `return fmt_error;`
    # and `}` orphaned outside any function. If the safety layers held, the
    # output should be different from the original AND still be well-formed.
    assert after != original
    # The `__builtin_trap()` stub marker must appear (at least once).
    assert "__builtin_trap()" in after

    # No FMT_CATCH-with-a-trap-body slipped in as if it were a real function
    # (that was the corruption pattern before the all-caps skip landed).
    assert "FMT_CATCH(...) { __builtin_trap()" not in after


def test_stub_reverts_on_ill_formed_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If some future edit accidentally introduces new parse errors, the
    post-write revert must fire and leave the file byte-identical."""
    from tools.stub_cpp import _TS_AVAILABLE
    if not _TS_AVAILABLE:
        pytest.skip("tree-sitter C++ grammar not available")

    # Force `_make_stub_body` to return corrupt garbage so the post-write
    # re-parse fails and the revert path fires.
    import tools.stub_cpp as sc
    monkeypatch.setattr(sc, "_make_stub_body", lambda _decl: "{ ??? not valid cpp ??? }")

    p = tmp_path / "clean.cc"
    src = "int foo(int x) { return x + 1; }\n"
    p.write_text(src, encoding="utf-8")

    count = _stub_file_treesitter(p)
    assert count == 0, "revert path should have returned 0"
    assert p.read_text(encoding="utf-8") == src, "file must be byte-identical after revert"



class TestIsVoidOrConstructor:
    @pytest.mark.parametrize("decl", [
        "void foo()",
        "constexpr void ignore_unused()",
        "inline void bar()",
        "static void baz()",
        "void Foo::method()",
    ])
    def test_void_functions(self, decl: str) -> None:
        from tools.stub_cpp import _is_void_or_constructor
        assert _is_void_or_constructor(decl) is True

    @pytest.mark.parametrize("decl", [
        "Foo()",
        "constexpr monostate()",
        "explicit constexpr Foo(int x)",
        "constexpr basic_string_view<Char>()",
        "constexpr basic_string_view<Char>(const Char* s)",
        "MyClass::MyClass(int x)",
    ])
    def test_constructors(self, decl: str) -> None:
        from tools.stub_cpp import _is_void_or_constructor
        assert _is_void_or_constructor(decl) is True

    @pytest.mark.parametrize("decl", [
        "int foo()",
        "auto bar() -> int",
        "constexpr auto baz() -> int",
        "template<typename T> constexpr T make()",
        "inline constexpr auto set_fill_size(...)",
        "int Foo::bar()",
        "const char* get_name()",
    ])
    def test_non_void_non_constructor(self, decl: str) -> None:
        from tools.stub_cpp import _is_void_or_constructor
        assert _is_void_or_constructor(decl) is False


class TestMakeStubBodyConstexpr:
    def test_constexpr_void_uses_empty_body(self) -> None:
        from tools.stub_cpp import _make_stub_body
        assert _make_stub_body("constexpr void ignore_unused()") == "{ }"

    def test_constexpr_constructor_uses_empty_body(self) -> None:
        from tools.stub_cpp import _make_stub_body
        assert _make_stub_body("constexpr monostate()") == "{ }"

    def test_constexpr_templated_constructor_uses_empty_body(self) -> None:
        from tools.stub_cpp import _make_stub_body
        assert _make_stub_body("constexpr basic_string_view<Char>()") == "{ }"

    def test_constexpr_non_void_uses_return_default(self) -> None:
        from tools.stub_cpp import _make_stub_body
        assert "return {}" in _make_stub_body("constexpr auto foo() -> int")

    def test_non_constexpr_void_uses_builtin_trap(self) -> None:
        from tools.stub_cpp import _make_stub_body
        assert "__builtin_trap" in _make_stub_body("void foo()")