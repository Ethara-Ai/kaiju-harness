"""libclang-based function-body stubber for C source files.

For each ``.c`` file in a repo, replaces every qualifying function body with a
``STUB_PANIC("<func_name>")`` call. Headers (``.h``) are never modified.

Key correctness invariants:
* ``compile_commands.json`` is parsed for per-translation-unit ``-I`` / ``-D``
  flags. Without these libclang silently emits an empty AST whenever the
  source references headers outside the current directory.
* ``cursor.extent`` is verified to point at the current source file before any
  rewrite. This guards against macro-expanded bodies (libclang gives expansion
  location, not spelling location, so a naive rewrite can corrupt the source).
* The rewrite targets the function's ``COMPOUND_STMT`` child — the ``{...}``
  block only — not the full ``cursor.extent``. This correctly handles K&R
  (old-style) function definitions whose parameter type declarations appear
  between the declarator and the body.
* Idempotent: running twice yields identical output (no duplicate
  ``#include "commit0_stub.h"`` and no re-stubbing of ``STUB_PANIC`` bodies).

Output: ``stub_report.json`` next to the directory root, with counts and
per-skip reasons.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:
    from tree_sitter_language_pack import get_parser as _ts_get_parser

    _TS_C_AVAILABLE = True
except Exception:
    _TS_C_AVAILABLE = False

TS_FALLBACK_THRESHOLD = 0.30

STUB_HEADER_FILENAME = "commit0_stub.h"
STUB_INCLUDE_LINE = f'#include "{STUB_HEADER_FILENAME}"'
STUB_MARKER = "STUB_PANIC"

STUB_HEADER_CONTENT = """\
#ifndef COMMIT0_STUB_H
#define COMMIT0_STUB_H
#include <stdio.h>
#include <stdlib.h>
#define STUB_PANIC(name) do { \\
    fprintf(stderr, "STUB: %s called in %s:%d\\n", (name), __FILE__, __LINE__); \\
    abort(); \\
} while (0)
#endif
"""

DEFAULT_SKIP_DIR_RE = re.compile(
    r"(^|/)(tests?|examples?|demos?|benchmarks?|third_party|vendor|deps)(/|$)"
)

DEFAULT_FALLBACK_ARGS: Tuple[str, ...] = (
    "-I.",
    "-Iinclude",
    "-Isrc",
    "-std=c11",
    "-D__linux__",
    "-D__x86_64__",
)


@dataclass
class StubReport:
    files_processed: int = 0
    files_modified: int = 0
    functions_stubbed: int = 0
    functions_skipped: dict = field(default_factory=dict)
    compile_commands_loaded: bool = False
    used_fallback_args: bool = False
    function_decl_count: int = 0
    # functions_stubbed is the TOTAL (libclang + tree-sitter recovery).
    libclang_functions_stubbed: int = 0
    treesitter_functions_stubbed: int = 0
    treesitter_fallback_used: bool = False

    def skip(self, reason: str, name: str) -> None:
        self.functions_skipped.setdefault(reason, []).append(name)

    def to_dict(self) -> dict:
        return {
            "files_processed": self.files_processed,
            "files_modified": self.files_modified,
            "functions_stubbed": self.functions_stubbed,
            "libclang_functions_stubbed": self.libclang_functions_stubbed,
            "treesitter_functions_stubbed": self.treesitter_functions_stubbed,
            "treesitter_fallback_used": self.treesitter_fallback_used,
            "function_decl_count": self.function_decl_count,
            "functions_skipped": self.functions_skipped,
            "compile_commands_loaded": self.compile_commands_loaded,
            "used_fallback_args": self.used_fallback_args,
        }


# ---------------------------------------------------------------------------
# compile_commands.json parsing
# ---------------------------------------------------------------------------

_FLAG_KEEP_PREFIXES = ("-I", "-isystem", "-D", "-U", "-std=", "-include")
_FLAG_KEEP_EXACT = {"-nostdinc", "-nostdinc++"}


def _tokenize_command(command: str) -> List[str]:
    import shlex

    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _extract_include_flags(args: Sequence[str]) -> List[str]:
    kept: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _FLAG_KEEP_EXACT:
            kept.append(a)
        elif a.startswith(_FLAG_KEEP_PREFIXES):
            kept.append(a)
            # Standalone `-I path` and `-isystem path` and `-include path`.
            if a in ("-I", "-isystem", "-include") and i + 1 < len(args):
                kept.append(args[i + 1])
                i += 1
        i += 1
    return kept


def load_compile_commands(
    compile_db_path: Path,
) -> dict[str, List[str]]:
    """Return ``{absolute_source_path: [include flags]}``."""
    if not compile_db_path.exists():
        return {}
    try:
        with open(compile_db_path) as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return {}

    out: dict[str, List[str]] = {}
    for entry in entries:
        directory = entry.get("directory", "")
        if "arguments" in entry:
            args = entry["arguments"]
        elif "command" in entry:
            args = _tokenize_command(entry["command"])
        else:
            continue
        src = entry.get("file")
        if not src:
            continue
        # Resolve source path relative to its directory.
        if not os.path.isabs(src):
            src = os.path.normpath(os.path.join(directory, src))
        flags = _extract_include_flags(args)
        # Re-anchor relative -I paths against the build directory.
        resolved: List[str] = []
        for f in flags:
            if (
                f.startswith("-I")
                and len(f) > 2
                and not os.path.isabs(f[2:])
            ):
                resolved.append("-I" + os.path.normpath(os.path.join(directory, f[2:])))
            else:
                resolved.append(f)
        out[os.path.realpath(src)] = resolved
    return out


# ---------------------------------------------------------------------------
# Source-text utilities
# ---------------------------------------------------------------------------


def _line_col_to_offset(text: str, line: int, column: int) -> int:
    """Convert 1-based (line, column) to a byte offset into ``text``."""
    pos = 0
    for _ in range(line - 1):
        nl = text.find("\n", pos)
        if nl < 0:
            return len(text)
        pos = nl + 1
    return pos + max(column - 1, 0)


def _find_compound_stmt_offsets(
    text: str, start_offset: int
) -> Optional[Tuple[int, int]]:
    """Return ``(open_brace_offset, close_brace_offset)`` for the function body.

    Walks forward from ``start_offset``, skipping string literals, char
    literals, and comments, until balanced braces are found.
    """
    i = start_offset
    n = len(text)
    # Locate opening brace.
    while i < n and text[i] != "{":
        ch = text[i]
        if ch in "\"'":
            i = _skip_string_or_char(text, i)
        elif ch == "/" and i + 1 < n and text[i + 1] in "/*":
            i = _skip_comment(text, i)
        else:
            i += 1
    if i >= n:
        return None
    open_brace = i
    depth = 0
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_brace, i
            i += 1
        elif ch in "\"'":
            i = _skip_string_or_char(text, i)
        elif ch == "/" and i + 1 < n and text[i + 1] in "/*":
            i = _skip_comment(text, i)
        else:
            i += 1
    return None


def _skip_string_or_char(text: str, i: int) -> int:
    quote = text[i]
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    return n


def _skip_comment(text: str, i: int) -> int:
    n = len(text)
    if i + 1 >= n:
        return i + 1
    if text[i + 1] == "/":
        nl = text.find("\n", i)
        return n if nl < 0 else nl + 1
    if text[i + 1] == "*":
        close = text.find("*/", i + 2)
        return n if close < 0 else close + 2
    return i + 1


def _is_already_stubbed(body_text: str) -> bool:
    """Cheap heuristic for idempotency: body already contains STUB_PANIC."""
    return STUB_MARKER in body_text


def _normalize_indent(open_brace_offset: int, text: str) -> str:
    """Indentation of the line that contains the open brace (best-effort)."""
    line_start = text.rfind("\n", 0, open_brace_offset) + 1
    prefix = text[line_start:open_brace_offset]
    indent = re.match(r"[ \t]*", prefix)
    return indent.group(0) if indent else ""


def _build_stub_body(function_name: str, indent: str) -> str:
    inner = indent + "    "
    return (
        "{\n"
        f"{inner}/* STUB: generated by cstubber */\n"
        f'{inner}STUB_PANIC("{function_name}");\n'
        f"{indent}}}"
    )


def _ensure_stub_include(text: str) -> Tuple[str, bool]:
    """Prepend ``#include "commit0_stub.h"`` if not already present."""
    if STUB_INCLUDE_LINE in text:
        return text, False
    # Insert after the leading comment block / first blank line if any.
    return STUB_INCLUDE_LINE + "\n" + text, True


# ---------------------------------------------------------------------------
# libclang AST walker
# ---------------------------------------------------------------------------


def _import_clang():
    """Import libclang Python bindings. Defers errors until actually invoked.

    The Docker base image installs ``python3-clang-18`` (apt). Locally, users
    can ``pip install clang==18.1.8`` — but we never error at import time so
    importers of this module (e.g. test fixtures) don't have to have clang
    available just to read the module's helper utilities.
    """
    try:
        import clang.cindex as cindex  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "libclang Python bindings not found. Install via "
            "'apt install python3-clang-18' or 'pip install clang==18.1.8'."
        ) from exc
    # On macOS the python bindings don't ship libclang.dylib and the default
    # loader search misses the Command Line Tools / Xcode copy — point cindex at
    # it explicitly (honoring CLANG_LIBRARY_FILE if the operator set one). Linux
    # (Docker python3-clang-18) finds its .so on the default path, so only do
    # this when a real file is located and cindex isn't already configured.
    import os as _os, sys as _sys, subprocess as _sp
    if _sys.platform == "darwin":
        _cands = []
        _envf = _os.environ.get("CLANG_LIBRARY_FILE")
        if _envf:
            _cands.append(_envf)
        try:
            _dev = _sp.check_output(["xcode-select", "-p"], text=True).strip()
            _cands.append(_os.path.join(_dev, "usr", "lib", "libclang.dylib"))
            _cands.append(_os.path.join(_dev, "Toolchains", "XcodeDefault.xctoolchain",
                                        "usr", "lib", "libclang.dylib"))
        except Exception:  # noqa: BLE001
            pass
        _cands += [
            "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
            "/Applications/Xcode.app/Contents/Developer/Toolchains/"
            "XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
        ]
        for _lib in _cands:
            if _lib and _os.path.isfile(_lib):
                try:
                    cindex.Config.set_library_file(_lib)
                except Exception:  # noqa: BLE001 - already-initialized is fine
                    pass
                break
    return cindex


def _function_should_skip(
    cursor: Any,
    cindex: Any,
    source_path: str,
    keep_re: Optional[re.Pattern],
    skip_dir_re: re.Pattern,
    report: StubReport,
) -> Optional[str]:
    """Return a skip-reason or None to indicate the function should be stubbed."""
    name = cursor.spelling or "<anonymous>"
    if not cursor.is_definition():
        return "declaration_only"

    if source_path.endswith(".h"):
        return "header_file"

    # __attribute__((constructor)) / ((destructor)) — never stub, would break init.
    for child in cursor.get_children():
        kind_name = child.kind.name
        if kind_name in ("CONSTRUCTOR_ATTR", "DESTRUCTOR_ATTR"):
            return "init_hook"

    file_path = source_path
    if name == "main" and skip_dir_re.search(file_path.replace(os.sep, "/")):
        return "main_in_skip_dir"
    if skip_dir_re.search(file_path.replace(os.sep, "/")):
        return "in_skip_dir"

    if keep_re and keep_re.search(name):
        return "kept_by_regex"

    # Verify cursor extent points at this source file (defend against macro expansion).
    extent = cursor.extent
    if extent.start.file is None or extent.end.file is None:
        return "macro_expanded"
    if (
        os.path.realpath(extent.start.file.name) != os.path.realpath(source_path)
        or os.path.realpath(extent.end.file.name) != os.path.realpath(source_path)
    ):
        return "macro_expanded"

    return None


def _find_compound_stmt(cursor: Any, cindex: Any) -> Optional[Any]:
    for child in cursor.get_children():
        if child.kind == cindex.CursorKind.COMPOUND_STMT:
            return child
    return None


def stub_file(
    source_path: Path,
    cc_db: dict[str, List[str]],
    fallback_args: Sequence[str],
    keep_re: Optional[re.Pattern],
    skip_dir_re: re.Pattern,
    report: StubReport,
    inject_include: bool = True,
) -> bool:
    """Stub one ``.c`` file in place. Returns True if modified."""
    cindex = _import_clang()

    src_abs = os.path.realpath(str(source_path))
    args = cc_db.get(src_abs)
    if args is None:
        args = list(fallback_args)
        report.used_fallback_args = True
    else:
        report.compile_commands_loaded = True

    text = source_path.read_text(encoding="utf-8", errors="replace")

    index = cindex.Index.create()
    try:
        tu = index.parse(
            str(source_path),
            args=list(args),
            options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
    except cindex.TranslationUnitLoadError as exc:  # pragma: no cover
        report.skip("parse_failed", f"{source_path}: {exc}")
        return False

    # Collect targets sorted by offset DESCENDING so rewrites don't shift later ones.
    targets: List[Tuple[int, int, str]] = []  # (open, close, func_name)
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind != cindex.CursorKind.FUNCTION_DECL:
            continue
        # Only count function decls that physically live in this file.
        file_attr = cursor.location.file
        if file_attr is None or os.path.realpath(file_attr.name) != src_abs:
            continue
        report.function_decl_count += 1

        skip_reason = _function_should_skip(
            cursor, cindex, str(source_path), keep_re, skip_dir_re, report
        )
        if skip_reason is not None:
            report.skip(skip_reason, cursor.spelling or "<anonymous>")
            continue

        body = _find_compound_stmt(cursor, cindex)
        if body is None:
            report.skip("no_compound_stmt", cursor.spelling or "<anonymous>")
            continue

        body_extent = body.extent
        if (
            body_extent.start.file is None
            or os.path.realpath(body_extent.start.file.name) != src_abs
        ):
            report.skip("body_in_different_file", cursor.spelling or "<anonymous>")
            continue

        body_start = _line_col_to_offset(
            text, body_extent.start.line, body_extent.start.column
        )
        offsets = _find_compound_stmt_offsets(text, body_start)
        if offsets is None:
            report.skip("unbalanced_braces", cursor.spelling or "<anonymous>")
            continue
        open_brace, close_brace = offsets
        body_text = text[open_brace : close_brace + 1]
        if _is_already_stubbed(body_text):
            report.skip("already_stubbed", cursor.spelling)
            continue
        targets.append((open_brace, close_brace, cursor.spelling or "<anonymous>"))

    if not targets:
        return False

    # Apply rewrites from highest offset to lowest.
    targets.sort(key=lambda t: t[0], reverse=True)
    new_text = text
    stubbed_here = 0
    for open_brace, close_brace, func_name in targets:
        indent = _normalize_indent(open_brace, new_text)
        replacement = _build_stub_body(func_name, indent)
        new_text = new_text[:open_brace] + replacement + new_text[close_brace + 1 :]
        stubbed_here += 1

    if inject_include:
        new_text, _ = _ensure_stub_include(new_text)

    source_path.write_text(new_text, encoding="utf-8")
    report.functions_stubbed += stubbed_here
    return True


# ---------------------------------------------------------------------------
# tree-sitter recovery engine
#
# tree-sitter parses source text with no preprocessor, so a macro-supplied
# body produces no function_definition node and is left untouched — the same
# safe outcome libclang reaches via its macro_expanded guard.
# ---------------------------------------------------------------------------


def _ts_function_name(defn_node: Any) -> str:
    decl = defn_node.child_by_field_name("declarator")
    while decl is not None and decl.type != "function_declarator":
        nxt = decl.child_by_field_name("declarator")
        if nxt is None:
            nxt = next(
                (c for c in decl.children if c.type == "function_declarator"),
                None,
            )
        decl = nxt
    if decl is None:
        return "<anonymous>"
    ident = decl.child_by_field_name("declarator")
    while ident is not None and ident.type not in ("identifier", "field_identifier"):
        nxt = ident.child_by_field_name("declarator")
        if nxt is None:
            nxt = next(
                (c for c in ident.children if c.type.endswith("identifier")),
                None,
            )
        ident = nxt
    if ident is None:
        return "<anonymous>"
    return ident.text.decode("utf-8", errors="replace")


def _ts_indent(source: bytes, open_byte: int) -> str:
    line_start = source.rfind(b"\n", 0, open_byte) + 1
    prefix = source[line_start:open_byte].decode("utf-8", errors="replace")
    m = re.match(r"[ \t]*", prefix)
    return m.group(0) if m else ""


def _stub_file_treesitter(
    source_path: Path,
    keep_re: Optional[re.Pattern],
    report: StubReport,
    inject_include: bool = True,
) -> int:
    """Stub remaining un-stubbed bodies in one .c file via tree-sitter.

    Honours the same skip rules as the libclang path (init hooks, keep
    regex, idempotency) and never touches bodies already carrying
    STUB_PANIC, so it composes additively with a prior libclang pass and
    is itself idempotent.
    """
    if not _TS_C_AVAILABLE:
        return 0
    source = source_path.read_bytes()
    parser = _ts_get_parser("c")
    tree = parser.parse(source)

    targets: List[Tuple[int, int, str]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "function_definition":
            body = node.child_by_field_name("body")
            if body is None:
                body = next(
                    (c for c in node.children if c.type == "compound_statement"),
                    None,
                )
            if body is None:
                continue
            pre = source[node.start_byte : body.start_byte]
            if b"__attribute__" in pre and (
                b"constructor" in pre or b"destructor" in pre
            ):
                report.skip("init_hook", _ts_function_name(node))
                continue
            name = _ts_function_name(node)
            if keep_re and keep_re.search(name):
                report.skip("kept_by_regex", name)
                continue
            body_bytes = source[body.start_byte : body.end_byte]
            if STUB_MARKER.encode() in body_bytes:
                report.skip("already_stubbed", name)
                continue
            targets.append((body.start_byte, body.end_byte, name))
            continue
        stack.extend(node.children)

    if not targets:
        return 0

    targets.sort(key=lambda t: t[0], reverse=True)
    result = bytearray(source)
    for open_byte, close_byte, name in targets:
        indent = _ts_indent(bytes(result), open_byte)
        replacement = _build_stub_body(name, indent).encode("utf-8")
        result[open_byte:close_byte] = replacement

    new_bytes = bytes(result)
    if inject_include and STUB_INCLUDE_LINE.encode() not in new_bytes:
        new_bytes = (STUB_INCLUDE_LINE + "\n").encode() + new_bytes

    source_path.write_bytes(new_bytes)
    return len(targets)


# ---------------------------------------------------------------------------
# Directory walk
# ---------------------------------------------------------------------------


def _iter_c_files(root: Path, skip_dir_re: re.Pattern) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_for_match = "" if rel == "." else rel
        if rel_for_match and skip_dir_re.search("/" + rel_for_match + "/"):
            dirnames[:] = []
            continue
        # Prune common non-source dirs.
        dirnames[:] = [d for d in dirnames if d not in (".git", "build", "_build")]
        for name in filenames:
            if name.endswith(".c"):
                yield Path(dirpath) / name


def write_stub_header(root: Path) -> Path:
    """Write ``commit0_stub.h`` at repo root. Idempotent."""
    target = root / STUB_HEADER_FILENAME
    if target.exists() and target.read_text() == STUB_HEADER_CONTENT:
        return target
    target.write_text(STUB_HEADER_CONTENT, encoding="utf-8")
    return target


def stub_directory(
    root: Path,
    compile_db_path: Optional[Path] = None,
    keep_regex: Optional[str] = None,
    skip_dir_regex: str = DEFAULT_SKIP_DIR_RE.pattern,
    fallback_args: Sequence[str] = DEFAULT_FALLBACK_ARGS,
    write_header: bool = True,
) -> StubReport:
    """Stub every ``.c`` file under ``root``. Returns a StubReport."""
    skip_dir_re = re.compile(skip_dir_regex)
    keep_re = re.compile(keep_regex) if keep_regex else None
    report = StubReport()

    if compile_db_path is None:
        compile_db_path = root / "build" / "compile_commands.json"
    if not compile_db_path.exists():
        # Try repo root.
        alt = root / "compile_commands.json"
        if alt.exists():
            compile_db_path = alt
    cc_db = load_compile_commands(compile_db_path)

    if write_header:
        write_stub_header(root)

    for c_file in _iter_c_files(root, skip_dir_re):
        report.files_processed += 1
        modified = stub_file(
            c_file,
            cc_db,
            fallback_args,
            keep_re,
            skip_dir_re,
            report,
            inject_include=write_header,
        )
        if modified:
            report.files_modified += 1

    report.libclang_functions_stubbed = report.functions_stubbed

    if (
        _TS_C_AVAILABLE
        and report.function_decl_count > 0
        and report.functions_stubbed / report.function_decl_count
        < TS_FALLBACK_THRESHOLD
    ):
        report.treesitter_fallback_used = True
        logger.warning(
            "libclang stubbed %d/%d functions (<%.0f%%); "
            "re-running with tree-sitter recovery",
            report.libclang_functions_stubbed,
            report.function_decl_count,
            TS_FALLBACK_THRESHOLD * 100,
        )
        for c_file in _iter_c_files(root, skip_dir_re):
            recovered = _stub_file_treesitter(
                c_file, keep_re, report, inject_include=write_header
            )
            if recovered:
                report.treesitter_functions_stubbed += recovered
                report.functions_stubbed += recovered

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("path", type=Path, help="Repository root or single .c file")
    p.add_argument(
        "--compile-commands",
        type=Path,
        default=None,
        help="Path to compile_commands.json (default: <root>/build/compile_commands.json)",
    )
    p.add_argument(
        "--keep",
        type=str,
        default=None,
        help="Regex; functions whose names match are NOT stubbed",
    )
    p.add_argument(
        "--skip-dir-regex",
        type=str,
        default=DEFAULT_SKIP_DIR_RE.pattern,
        help="Regex matched against POSIX-style relative directory paths",
    )
    p.add_argument(
        "--no-header",
        action="store_true",
        help="Do NOT write commit0_stub.h or inject its #include",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Where to write stub_report.json (default: <root>/stub_report.json)",
    )
    p.add_argument(
        "--fallback-arg",
        action="append",
        default=None,
        help="Extra clang flag when compile_commands.json is absent (repeatable)",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    root: Path = args.path.resolve()
    if not root.exists():
        print(f"path does not exist: {root}", file=sys.stderr)
        return 2

    fallback = (
        tuple(args.fallback_arg) if args.fallback_arg else DEFAULT_FALLBACK_ARGS
    )

    if root.is_file() and root.suffix == ".c":
        report = StubReport()
        skip_dir_re = re.compile(args.skip_dir_regex)
        keep_re = re.compile(args.keep) if args.keep else None
        compile_db_path = args.compile_commands or (
            root.parent / "build" / "compile_commands.json"
        )
        cc_db = load_compile_commands(compile_db_path)
        if not args.no_header:
            write_stub_header(root.parent)
        modified = stub_file(
            root,
            cc_db,
            fallback,
            keep_re,
            skip_dir_re,
            report,
            inject_include=not args.no_header,
        )
        report.files_processed = 1
        report.files_modified = int(modified)
    else:
        report = stub_directory(
            root,
            compile_db_path=args.compile_commands,
            keep_regex=args.keep,
            skip_dir_regex=args.skip_dir_regex,
            fallback_args=fallback,
            write_header=not args.no_header,
        )

    report_path = args.report or (
        (root if root.is_dir() else root.parent) / "stub_report.json"
    )
    report_path.write_text(json.dumps(report.to_dict(), indent=2))
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
