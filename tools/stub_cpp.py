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
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from commit0.harness.constants_cpp import (
    CPP_STUB_MARKER,
    CPP_STUB_MARKER_CONSTEXPR,
    CPP_STUB_MARKER_NOEXCEPT,
)

CPPSTUBBER_BINARY = Path(__file__).parent / "cppstubber" / "build" / "cppstubber"

CPP_EXTENSIONS = {".cpp", ".hpp", ".cc", ".hh", ".cxx", ".hxx", ".c++", ".h++"}

SKIP_DIRS = {".git", "build", "cmake-build-debug", "cmake-build-release",
             "builddir", "third_party", "3rdparty", "vendor", "extern",
             "node_modules", ".cache", "test", "tests"}

try:
    import tree_sitter
    import tree_sitter_cpp as tscpp

    _CPP_LANGUAGE = tree_sitter.Language(tscpp.language())
    _TS_AVAILABLE = True
except (ImportError, Exception):
    _TS_AVAILABLE = False


def _is_cpp_file(path: Path) -> bool:
    return path.suffix.lower() in CPP_EXTENSIONS


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _make_stub_body(decl_text: str) -> str:
    """Choose the appropriate stub body based on function qualifiers."""
    if "constexpr" in decl_text or "consteval" in decl_text:
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
    """Invoke cppstubber binary (Clang LibTooling)."""
    cmd = [str(CPPSTUBBER_BINARY), str(src_dir)]
    if in_place:
        cmd.append("--in-place")
    if compile_commands:
        cmd.extend(["--compile-commands", str(compile_commands)])

    logger.info("Running cppstubber: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.error("cppstubber timed out on %s", src_dir)
        return 0

    count = 0
    for line in result.stderr.splitlines() + result.stdout.splitlines():
        m = re.search(r"(\d+)\s+functions?\s+stubbed", line)
        if m:
            count = int(m.group(1))

    if result.returncode != 0:
        logger.warning("cppstubber exited %d: %s", result.returncode, result.stderr.strip()[:500])

    return count


# ─── Tree-sitter Fallback ────────────────────────────────────────────────────


def _stub_file_treesitter(filepath: Path) -> int:
    """Stub a single C++ file using tree-sitter. Returns number of stubbed functions."""
    if not _TS_AVAILABLE:
        logger.error("tree-sitter or tree-sitter-cpp not available")
        return 0

    source = filepath.read_bytes()
    parser = tree_sitter.Parser(_CPP_LANGUAGE)
    tree = parser.parse(source)

    replacements: list[tuple[int, int, bytes]] = []

    def visit(node: tree_sitter.Node) -> None:
        if node.type == "function_definition":
            body = None
            for child in node.children:
                if child.type == "compound_statement":
                    body = child
                    break
            if body is None:
                return

            decl_bytes = source[node.start_byte : body.start_byte]
            decl_text = decl_bytes.decode("utf-8", errors="replace")
            stub = _make_stub_body(decl_text)
            replacements.append((body.start_byte, body.end_byte, stub.encode()))
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

    filepath.write_bytes(bytes(result))
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
            "Install tree-sitter + tree-sitter-cpp or build cppstubber."
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
