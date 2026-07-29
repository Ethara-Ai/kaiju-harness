"""C++ code stubbing tool — wraps cppstubber (Clang LibTooling) with tree-sitter fallback.

Replaces function bodies with stub markers:
- Regular functions: std::abort(); /* STUB: not implemented */
- constexpr functions: return {};
- noexcept functions: std::abort();

Preserves:
- All #include directives, macros, and using declarations
- Type definitions, constants, enums, variables
- Class/struct declarations (non-inline member functions are stubbed)
- Test files (files under test/ or tests/ directories)
- Function signatures, templates, and comments

Usage:
    python -m tools.stub_cpp /path/to/src [--compile-commands /path/to/compile_commands.json]
    python -m tools.stub_cpp --file /path/to/file.cpp

Requires (for Clang mode):
    cppstubber binary built from tools/cppstubber/
    Build: cd tools/cppstubber && mkdir -p build && cd build && cmake .. && make
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from commit0.harness.constants_cpp import (
    CPP_STUB_MARKER,
    CPP_STUB_MARKER_CONSTEXPR,
    CPP_STUB_MARKER_NOEXCEPT,
)

CPPSTUBBER_BINARY = Path(__file__).parent / "cppstubber" / "build" / "cppstubber"

# Includes the bare ".h" and inline-template extensions so the tree-sitter
# FALLBACK still scans header-only libraries (most keep everything in ".h").
# cppstubber (the primary, AST-based path) is what produces correct template
# stubs; this list only governs which files the fallback even looks at.
CPP_EXTENSIONS = {".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx", ".c++", ".h++",
                  ".h", ".ipp", ".tpp", ".inl"}

SKIP_DIRS = {".git", "build", "cmake-build-debug", "cmake-build-release",
             "builddir", "third_party", "3rdparty", "vendor", "extern",
             "bundled", "node_modules", ".cache", "test", "tests"}

try:
    import tree_sitter

    # Prefer tree_sitter_language_pack: it ships the C++ grammar and is ALREADY a
    # harness dependency (the cpp base-compiles gate in prepare_repo_cpp.py uses it),
    # so the fallback works out-of-the-box without a separate tree-sitter-cpp install.
    # Fall back to a standalone tree_sitter_cpp only if the pack is unavailable.
    try:
        from tree_sitter_language_pack import get_language as _ts_get_language

        _CPP_LANGUAGE = _ts_get_language("cpp")
    except Exception:  # noqa: BLE001
        import tree_sitter_cpp as tscpp

        _CPP_LANGUAGE = tree_sitter.Language(tscpp.language())
    _TS_AVAILABLE = True
    _TS_IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001 — missing dep or ABI mismatch
    _TS_AVAILABLE = False
    _TS_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    logger.warning(
        "tree-sitter C++ fallback unavailable (%s). "
        "Install with: pip install tree-sitter tree-sitter-language-pack",
        _TS_IMPORT_ERROR,
    )


def _is_cpp_file(path: Path) -> bool:
    return path.suffix.lower() in CPP_EXTENSIONS


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


_DECL_STRIP_KW = re.compile(r"\b(constexpr|consteval|explicit|inline|static|virtual|friend|template\s*<[^>]*>)\b")


def _is_void_or_constructor(decl_text: str) -> bool:
    """Return True if decl_text represents a constructor or a void-returning function."""
    stripped = _DECL_STRIP_KW.sub(" ", decl_text)
    match = re.search(r"([A-Za-z_][\w:]*)(?:\s*<[^>]*>)?\s*\(", stripped)
    if not match:
        return False
    before = stripped[: match.start()].strip()
    if not before:
        return True
    name = match.group(1)
    if "::" in name:
        parts = name.split("::")
        if len(parts) >= 2 and parts[-1] == parts[-2]:
            return True
    tokens = re.split(r"[\s*&]+", before.rstrip("*&"))
    return bool(tokens) and tokens[-1] == "void"


def _make_stub_body(decl_text: str) -> str:
    """Choose the appropriate stub body based on function qualifiers."""
    is_constexpr = "constexpr" in decl_text or "consteval" in decl_text
    void_or_ctor = _is_void_or_constructor(decl_text)
    if is_constexpr and void_or_ctor:
        return "{ }"
    if is_constexpr:
        return "{ " + CPP_STUB_MARKER_CONSTEXPR + "; }"
    if "noexcept" in decl_text:
        return "{ " + CPP_STUB_MARKER_NOEXCEPT + "; }"
    return '{ ' + CPP_STUB_MARKER + '; }'


# ─── Clang LibTooling Mode ──────────────────────────────────────────────────


def _stub_with_clang(
    src_dir: Path,
    compile_commands: Path,
    in_place: bool = True,
) -> int:
    """Invoke the cppstubber binary (Clang LibTooling) on files under ``src_dir``.

    ``compile_commands`` may be either the ``compile_commands.json`` file itself
    or the directory that contains it. cppstubber uses ``-p <dir>`` to find the
    database, so we normalize to the directory here.

    Files are enumerated with the same skip-dir rules as the tree-sitter fallback
    (``SKIP_DIRS``) and passed one at a time so a per-file parse error only
    disqualifies that file, not the entire run. On macOS the Xcode SDK sysroot
    and the Homebrew LLVM resource-dir are appended via ``--extra-arg`` so the
    stubber can resolve ``stdbool.h`` / ``stdarg.h`` regardless of which compiler
    generated the compile database. Mirrors ``tools/prepare_repo_cpp.py:
    stub_source_dir`` so the module CLI and pipeline share behavior."""
    cc_dir = compile_commands if compile_commands.is_dir() else compile_commands.parent

    files: list[str] = []
    for path in src_dir.rglob("*"):
        if not path.is_file() or not _is_cpp_file(path):
            continue
        rel_parts = path.relative_to(src_dir).parts[:-1]
        if any(_should_skip_dir(p) for p in rel_parts):
            continue
        files.append(str(path.resolve()))
    if not files:
        logger.warning("No C++ files found under %s", src_dir)
        return 0

    extra_args = ["--extra-arg=-Wno-error=unused-command-line-argument"]
    if sys.platform == "darwin":
        try:
            sdk = subprocess.run(
                ["xcrun", "--show-sdk-path"], capture_output=True, text=True,
                check=True, timeout=10,
            ).stdout.strip()
            if sdk:
                extra_args.append(f"--extra-arg=-isysroot{sdk}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
        for candidate in sorted(
            Path("/opt/homebrew/opt/llvm/lib/clang").glob("*/include/stdarg.h"),
            reverse=True,
        ):
            extra_args.append(f"--extra-arg=-resource-dir={candidate.parent.parent}")
            break
    elif sys.platform.startswith("linux"):
        extra_args.append("--extra-arg=--gcc-toolchain=/usr")

    count = 0
    crashed = 0
    for f in files:
        cmd = [str(CPPSTUBBER_BINARY), "-p", str(cc_dir), *extra_args]
        if in_place:
            cmd.append("--in-place")
        cmd.append(f)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            logger.warning("cppstubber timed out on %s", f)
            continue
        if r.returncode < 0:
            crashed += 1
            continue
        for line in r.stderr.splitlines() + r.stdout.splitlines():
            m = (re.search(r"(\d+)\s+functions?\s+stubbed", line)
                 or re.search(r"[Ff]unctions?\s+stubbed:\s*(\d+)", line))
            if m:
                count += int(m.group(1))

    if crashed:
        logger.warning("cppstubber crashed on %d file(s) — partial coverage", crashed)

    return count


# ─── Tree-sitter Fallback ────────────────────────────────────────────────────


_MACRO_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

# Known problematic macros that tree-sitter's C++ grammar happily parses as
# function_definition (`identifier(params) { body }` matches its grammar). The
# _MACRO_NAME_RE regex catches most of these by convention; this frozenset is a
# belt-and-braces backup for any name that slips past the regex. Extend if you
# observe a real repo where a legitimate C++ function is incorrectly skipped by
# the all-caps check — in practice modern C++ style avoids all-caps identifiers
# for functions and reserves them for macros, so false positives are rare.
_KNOWN_MACRO_NAMES: frozenset[str] = frozenset({
    "FMT_TRY", "FMT_CATCH",
    "TRY", "CATCH",
    "PROTECTED_TRY", "PROTECTED_CATCH",
    "BOOST_TRY", "BOOST_CATCH",
    "SPDLOG_TRY", "SPDLOG_CATCH",
    "CPPTRACE_TRY", "CPPTRACE_CATCH",
})


def _looks_like_macro_name(name: str) -> bool:
    """Return True when ``name`` follows the ALL_CAPS macro naming convention.

    Real C++ functions almost never use all-caps identifiers; the convention
    reserves that style for macros. When tree-sitter mis-parses ``FMT_CATCH(...) {}``
    as a ``function_definition``, the name it extracts is all-caps — this check
    catches that case before we blindly stub a macro invocation and change its
    semantics."""
    if not name:
        return False
    return name in _KNOWN_MACRO_NAMES or bool(_MACRO_NAME_RE.match(name))


def _get_function_name(node: "tree_sitter.Node") -> str:
    """Extract the declared function name from a ``function_definition`` node.

    Walks into the ``function_declarator`` child and returns the deepest
    ``identifier`` encountered — this handles both flat names (``foo``) and
    qualified names (``Namespace::Class::method`` → ``method``). Returns an
    empty string when no identifier can be located."""
    declarator = None
    stack: list["tree_sitter.Node"] = list(node.children)
    while stack:
        c = stack.pop(0)
        if c.type == "function_declarator":
            declarator = c
            break
        # A function returning a pointer or reference wraps the declarator; the
        # actual function_declarator is nested inside. Descend into these.
        if c.type in ("pointer_declarator", "reference_declarator"):
            stack = list(c.children) + stack
    if declarator is None:
        return ""

    result = ""

    def _walk(n: "tree_sitter.Node") -> None:
        nonlocal result
        # Never look inside the parameter list — parameter identifiers
        # (`int x` -> `x`) are not the function's name.
        if n.type == "parameter_list":
            return
        if n.type in ("identifier", "field_identifier") and n.text is not None:
            result = n.text.decode("utf-8", errors="replace")
        for c in n.children:
            _walk(c)

    _walk(declarator)
    return result


def _find_matching_brace(source: bytes, start: int) -> int:
    """Return the byte offset of the ``}`` that matches the ``{`` at ``start``.

    Returns ``-1`` when there is no match or when ``source[start]`` is not ``{``.
    Correctly skips content inside C++ tokens that may contain unbalanced braces:
    line and block comments, regular string literals (with backslash escapes),
    character literals (with a heuristic for the C++14 digit separator ``1'000``),
    and raw string literals ``R"delim(...)delim"`` (with L / u / u8 / U prefixes).

    Preprocessor directives are NOT specially handled; the stubber runs against
    source expected to compile with a real preprocessor, so any file with truly
    unbalanced braces inside ``#if``/``#endif`` blocks would break the compiler
    too and belong in the revert path.

    Used as the safety fallback when tree-sitter's ``compound_statement.end_byte``
    cannot be trusted (unknown macros such as FMT_TRY inside the body cause the
    grammar to close the function early at an inner ``}`` rather than the real
    outer one)."""
    n = len(source)
    if start < 0 or start >= n or source[start:start + 1] != b"{":
        return -1

    depth = 0
    i = start
    while i < n:
        c = source[i:i + 1]

        if c == b"/" and source[i + 1:i + 2] == b"/":
            nl = source.find(b"\n", i)
            if nl == -1:
                return -1
            i = nl + 1
            continue
        if c == b"/" and source[i + 1:i + 2] == b"*":
            end = source.find(b"*/", i + 2)
            if end == -1:
                return -1
            i = end + 2
            continue
        if c == b'"' and i > 0 and source[i - 1:i] == b"R":
            paren = source.find(b"(", i + 1)
            if paren != -1 and paren - i <= 17:
                delim = source[i + 1:paren]
                if not any(bad in delim for bad in (b" ", b"\t", b"\n", b"\r", b"(", b")", b"\\")):
                    terminator = b")" + delim + b'"'
                    end = source.find(terminator, paren + 1)
                    if end == -1:
                        return -1
                    i = end + len(terminator)
                    continue
        if c == b'"':
            i += 1
            while i < n:
                ch = source[i:i + 1]
                if ch == b"\\":
                    i += 2
                    continue
                if ch == b'"':
                    i += 1
                    break
                i += 1
            continue
        if c == b"'":
            if i > 0 and source[i - 1:i].isalnum():
                i += 1
                continue
            i += 1
            while i < n:
                ch = source[i:i + 1]
                if ch == b"\\":
                    i += 2
                    continue
                if ch == b"'":
                    i += 1
                    break
                i += 1
            continue
        if c == b"{":
            depth += 1
        elif c == b"}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1



def _stub_file_treesitter(filepath: Path) -> int:
    """Stub a single C++ file using tree-sitter. Returns number of stubbed functions.

    Robustness layers (top to bottom):
    1. Skip functions whose declared name matches the ALL_CAPS macro convention
       (e.g. ``FMT_CATCH``). Tree-sitter mis-parses macro invocations as
       function_definitions; stubbing them would silently rewrite macro bodies.
    2. When tree-sitter reports a parse error on the function or its body
       (``has_error`` on either node), fall back to manual brace counting from
       the compound_statement's opening ``{`` to find the true matching ``}``.
       Tree-sitter's ``end_byte`` on error-recovered nodes points to the wrong
       ``}`` (inner macro block instead of the real function end).
    3. After all replacements are applied, re-parse the modified buffer and
       compare ERROR + MISSING node counts against the original tree. If
       stubbing introduced new errors, revert the file rather than write
       corruption downstream.
    """
    if not _TS_AVAILABLE:
        logger.error("tree-sitter or tree-sitter-cpp not available")
        return 0

    source = filepath.read_bytes()
    parser = tree_sitter.Parser(_CPP_LANGUAGE)
    tree = parser.parse(source)

    replacements: list[tuple[int, int, bytes]] = []
    skipped_macro = 0
    skipped_no_brace = 0
    stubbed_brace_count = 0

    def visit(node: tree_sitter.Node) -> None:
        nonlocal skipped_macro, skipped_no_brace, stubbed_brace_count
        if node.type == "function_definition":
            # Layer 1: reject macro-invocation lookalikes such as FMT_CATCH(...).
            name = _get_function_name(node)
            if _looks_like_macro_name(name):
                skipped_macro += 1
                return

            body = None
            for child in node.children:
                if child.type == "compound_statement":
                    body = child
                    break
            if body is None:
                return

            # Layer 2: pick the true body end. When tree-sitter's parse of this
            # function contains errors (unknown macros inside the body), its
            # reported end_byte cannot be trusted — walk the source manually.
            if node.has_error or body.has_error:
                end_pos = _find_matching_brace(source, body.start_byte)
                if end_pos < 0:
                    skipped_no_brace += 1
                    return
                body_end = end_pos + 1
                stubbed_brace_count += 1
            else:
                body_end = body.end_byte

            decl_bytes = source[node.start_byte : body.start_byte]
            decl_text = decl_bytes.decode("utf-8", errors="replace")
            stub = _make_stub_body(decl_text)
            replacements.append((body.start_byte, body_end, stub.encode()))
            return

        for child in node.children:
            visit(child)

    visit(tree.root_node)

    if not replacements:
        return 0

    replacements.sort(key=lambda r: r[0], reverse=True)
    result = bytearray(source)
    for start, end, replacement in replacements:
        result[start:end] = replacement

    # Layer 3: post-write revert if we made the file measurably worse. Compare
    # ERROR + MISSING node counts on old vs new parse trees. A NET INCREASE
    # means at least one replacement corrupted the file — discard the write.
    def _count_error_nodes(n: "tree_sitter.Node") -> int:
        acc = 1 if (n.type == "ERROR" or n.is_missing) else 0
        for c in n.children:
            acc += _count_error_nodes(c)
        return acc

    new_tree = parser.parse(bytes(result))
    old_err = _count_error_nodes(tree.root_node)
    new_err = _count_error_nodes(new_tree.root_node)
    if new_err > old_err:
        logger.warning(
            "  %s: stubbing introduced parse errors (%d -> %d); reverting file",
            filepath, old_err, new_err,
        )
        return 0

    filepath.write_bytes(bytes(result))
    if skipped_macro or skipped_no_brace or stubbed_brace_count:
        logger.debug(
            "  %s: %d stub(s) applied (%d via brace-count fallback), "
            "%d macro-name skip(s), %d unmatched-brace skip(s)",
            filepath, len(replacements), stubbed_brace_count,
            skipped_macro, skipped_no_brace,
        )
    return len(replacements)


# ─── Public API ──────────────────────────────────────────────────────────────


def stub_cpp_directory(
    src_dir: str | Path,
    in_place: bool = True,
    compile_commands: str | Path | None = None,
) -> int:
    """Stub all C++ files in a directory.

    If CPPSTUBBER_BINARY exists and compile_commands is provided, uses
    Clang LibTooling. Otherwise falls back to tree-sitter-based stubbing.

    Returns count of stubbed functions.
    """
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        logger.error("Source directory not found: %s", src_dir)
        return 0

    cc_path = Path(compile_commands) if compile_commands else None

    if CPPSTUBBER_BINARY.exists() and cc_path and cc_path.exists():
        logger.info("Using Clang LibTooling stubber")
        return _stub_with_clang(src_dir, cc_path, in_place)

    if not _TS_AVAILABLE:
        logger.error(
            "Neither cppstubber binary nor tree-sitter available. "
            "Install tree-sitter + tree-sitter-language-pack (or tree-sitter-cpp), "
            "or build cppstubber."
        )
        return 0

    logger.info("Using tree-sitter fallback stubber on %s", src_dir)
    total = 0
    for path in src_dir.rglob("*"):
        if not path.is_file():
            continue
        if any(_should_skip_dir(p) for p in path.relative_to(src_dir).parts[:-1]):
            continue
        if not _is_cpp_file(path):
            continue

        count = _stub_file_treesitter(path)
        if count > 0:
            logger.debug("  Stubbed %d functions in %s", count, path.relative_to(src_dir))
            total += count

    logger.info("Stubbed %d functions total in %s", total, src_dir)
    return total


def stub_cpp_file(filepath: str | Path) -> int:
    """Stub a single C++ file using tree-sitter. Returns count of stubbed functions."""
    filepath = Path(filepath)
    if not filepath.is_file():
        logger.error("File not found: %s", filepath)
        return 0

    if not _TS_AVAILABLE:
        logger.error("tree-sitter or tree-sitter-cpp not available")
        return 0

    return _stub_file_treesitter(filepath)


def count_stubs(directory: str | Path) -> int:
    """Count files in directory that contain the stub marker."""
    directory = Path(directory)
    count = 0
    for path in directory.rglob("*"):
        if not path.is_file() or not _is_cpp_file(path):
            continue
        try:
            content = path.read_text(errors="replace")
            if CPP_STUB_MARKER in content:
                count += 1
        except OSError:
            continue
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Stub C++ source files for commit0")
    parser.add_argument("src_dir", nargs="?", type=Path, help="Source directory to stub")
    parser.add_argument("--file", type=Path, help="Stub a single file")
    parser.add_argument(
        "--compile-commands", type=Path,
        help="Path to compile_commands.json (enables Clang mode)",
    )
    parser.add_argument("--count", action="store_true", help="Count files with stub markers")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.count:
        if not args.src_dir:
            parser.error("--count requires src_dir")
        n = count_stubs(args.src_dir)
        print(f"{n} files contain stub markers")
        return

    if args.file:
        n = stub_cpp_file(args.file)
        print(f"Stubbed {n} functions in {args.file}")
        return

    if not args.src_dir:
        parser.error("Provide src_dir or --file")

    n = stub_cpp_directory(args.src_dir, compile_commands=args.compile_commands)
    print(f"Stubbed {n} functions in {args.src_dir}")


if __name__ == "__main__":
    main()
