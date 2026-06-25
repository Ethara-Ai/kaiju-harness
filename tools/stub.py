"""AST-based Python code stubbing tool.

Replaces function/method bodies with `pass` statements while preserving:
- All imports and module-level code
- Class definitions and class-level variables
- Function signatures, decorators, type annotations
- Docstrings (configurable)
- Abstract methods, overloads, protocol stubs (already stubs)
- Python dunder methods (__init__, __str__, __repr__, etc.)

Supports three removal modes (matching the official commit0 paper methodology):
- "all":       Replace ALL function bodies with pass (keep docstrings)
- "docstring": Only stub functions that HAVE docstrings; leave others unchanged
- "combined":  Stub functions with docstrings + REMOVE functions without docstrings entirely

This is the missing tool from commit0 (arXiv:2412.01769, Section 3.2).

Usage:
    python -m tools.stub /path/to/repo /path/to/output [--removal-mode combined] [--keep-docstrings] [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import ast
import io
import tokenize
import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# Directories to skip
SKIP_DIRS: set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "env",
    ".env",
    "node_modules",
    ".tox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    ".nox",
}


def is_test_file(path: Path) -> bool:
    """Check if a file is a test file (should be skipped)."""
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or "/tests/" in str(path)
        or "/test/" in str(path)
    )


# Files that should never be stubbed — they define package structure or entry points
SKIP_FILENAMES: set[str] = {"__init__.py", "__main__.py", "conftest.py"}


def should_skip_file(path: Path) -> bool:
    """Skip __init__.py, __main__.py, conftest.py, and test files from stubbing."""
    return path.name in SKIP_FILENAMES or is_test_file(path)


def is_dunder_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function is a Python dunder/magic method (e.g., __init__, __str__)."""
    return node.name.startswith("__") and node.name.endswith("__")


def _extract_call_names(node: ast.AST) -> set[str]:
    """Extract all function/method names being called in an AST node.

    Handles:
    - Simple calls: foo() -> {"foo"}
    - Attribute calls: module.foo() -> {"foo"}
    - Nested calls: foo(bar()) -> {"foo", "bar"}
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _is_type_checking_guard(node: ast.If) -> bool:
    """Return True if this ``if`` node is a ``if TYPE_CHECKING:`` guard."""
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _scan_dir_for_import_time_names(
    scan_dir: Path, names: set[str], all_trees: list[ast.Module]
) -> None:
    """Scan a directory for import-time call names, collecting AST trees.

    Applies the same 6-pattern logic and SKIP_DIRS filter used by
    ``collect_import_time_names``.  Results are accumulated into *names*
    and parsed trees are appended to *all_trees*.
    """
    for py_file in sorted(scan_dir.rglob("*.py")):
        rel_parts = py_file.relative_to(scan_dir).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, str(py_file))
        except (SyntaxError, UnicodeDecodeError) as e:
            logger.debug("Skipping %s: %s", py_file, e)
            continue

        all_trees.append(tree)

        for node in tree.body:
            # 1. Module-level expressions (bare calls, assignments with calls)
            if isinstance(node, ast.Expr):
                names.update(_extract_call_names(node))

            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = getattr(node, "value", None)
                if value is not None:
                    names.update(_extract_call_names(value))

            # 2. Function/method decorators (decorator factories)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        names.update(_extract_call_names(dec))
                    # Also: @name where name is a variable holding a callable
                    # e.g., inline_args = v_args(inline=True); @inline_args
                    # We already catch v_args from the assignment above

            # 3. Class definitions
            elif isinstance(node, ast.ClassDef):
                # Metaclass keyword calls
                for kw in node.keywords:
                    if isinstance(kw.value, ast.Call):
                        names.update(_extract_call_names(kw.value))

                # Decorators on the class itself
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        names.update(_extract_call_names(dec))

                # Class body: assignments with calls, nested decorators
                for item in node.body:
                    if isinstance(item, (ast.Assign, ast.AnnAssign)):
                        value = getattr(item, "value", None)
                        if value is not None:
                            names.update(_extract_call_names(value))

                    elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for dec in item.decorator_list:
                            if isinstance(dec, ast.Call):
                                names.update(_extract_call_names(dec))
                        # __init_subclass__ runs at class definition time
                        # for every subclass — preserve functions it calls
                        if item.name == "__init_subclass__":
                            names.update(_extract_call_names(item))

                    elif isinstance(item, ast.Expr):
                        names.update(_extract_call_names(item))

            # 4. Module-level if/try blocks (including TryStar for Python 3.11+)
            elif (
                isinstance(node, ast.If)
                or isinstance(node, ast.Try)
                or (
                    hasattr(ast, "TryStar")
                    and isinstance(node, getattr(ast, "TryStar"))
                )
            ):
                # Skip `if TYPE_CHECKING:` blocks — those are not import-time
                if isinstance(node, ast.If) and _is_type_checking_guard(node):
                    continue
                # Calls in the test condition
                if isinstance(node, ast.If) and node.test:
                    names.update(_extract_call_names(node.test))
                # Walk body for assignments/calls at module scope
                body_nodes: list[ast.stmt] = list(getattr(node, "body", [])) + list(
                    getattr(node, "orelse", [])
                )
                if isinstance(node, ast.Try) or (
                    hasattr(ast, "TryStar")
                    and isinstance(node, getattr(ast, "TryStar"))
                ):
                    # Unwrap ExceptHandler bodies (handlers are not stmts)
                    for handler in getattr(node, "handlers", []):
                        body_nodes.extend(getattr(handler, "body", []))
                    body_nodes.extend(getattr(node, "finalbody", []))
                for sub in body_nodes:
                    if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                        value = getattr(sub, "value", None)
                        if value is not None:
                            names.update(_extract_call_names(value))
                    elif isinstance(sub, ast.Expr):
                        names.update(_extract_call_names(sub))

            # 5. Module-level from-imports make names import-time references
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname if alias.asname else alias.name
                    if name != "*":
                        names.add(name)


def collect_import_time_names(
    source_dir: Path, *, extra_scan_dirs: list[Path] | None = None
) -> set[str]:
    """Scan all Python files to find function names called at import time.

    Analyzes module-level code, class bodies, and decorators to identify
    functions that MUST remain implemented for imports to succeed.

    Patterns detected:
    1. Module-level assignments: `foo = some_func()` -> "some_func"
    2. Module-level bare calls: `register()` -> "register"
    3. Decorator factories: `@decorator_factory(args)` -> "decorator_factory"
    4. Class body assignments: `class C: x = func()` -> "func"
    5. Metaclass keyword calls: `class C(metaclass=Meta())` -> "Meta"
    6. Module-level conditionals with calls: `if cond(): ...` -> "cond"

    If *extra_scan_dirs* is given (e.g. sibling packages or test dirs),
    those directories are scanned with the same patterns.  A fixed-point
    iteration then resolves **transitive** calls — functions called inside
    import-time functions are also preserved when they are defined in any
    of the scanned directories.
    """
    names: set[str] = set()
    all_trees: list[ast.Module] = []

    # --- Scan primary source dir ---
    _scan_dir_for_import_time_names(source_dir, names, all_trees)

    # --- Scan extra dirs (sibling packages, test dirs) ---
    if extra_scan_dirs:
        for extra_dir in extra_scan_dirs:
            _scan_dir_for_import_time_names(extra_dir, names, all_trees)

    # --- Build maps for transitive resolution ---
    body_calls_map: dict[str, set[str]] = {}
    defined_funcs: set[str] = set()

    for tree in all_trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined_funcs.add(node.name)
                calls = _extract_call_names(node)
                if node.name in body_calls_map:
                    body_calls_map[node.name] |= calls
                else:
                    body_calls_map[node.name] = calls

    # Filter out common builtins/stdlib that are never user-defined stubs
    builtins_to_ignore = {
        "print",
        "len",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "tuple",
        "set",
        "type",
        "super",
        "isinstance",
        "issubclass",
        "getattr",
        "setattr",
        "hasattr",
        "property",
        "classmethod",
        "staticmethod",
        "abstractmethod",
        "overload",
        "dataclass",
        "range",
        "enumerate",
        "zip",
        "map",
        "filter",
        "sorted",
        "reversed",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "vars",
        "dir",
        "id",
        "hash",
        "repr",
        "format",
        "open",
        "object",
        "Exception",
    }
    names -= builtins_to_ignore

    # --- Fixed-point iteration: resolve transitive calls ---
    for iteration in range(10):  # safety limit
        before = len(names)
        new_names: set[str] = set()
        for name in list(names):
            if name in body_calls_map:
                for called in body_calls_map[name]:
                    if called in defined_funcs and called not in names:
                        new_names.add(called)
        names |= new_names
        names -= builtins_to_ignore
        if new_names:
            logger.debug(
                "Transitive iteration %d: added %d names: %s",
                iteration,
                len(new_names),
                sorted(new_names)[:10],
            )
        if len(names) == before:
            break

    return names


def is_docstring(node: ast.stmt) -> bool:
    """Check if an AST statement is a docstring (string expression)."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def is_pure_assignment_init(func_node: ast.FunctionDef) -> bool:
    """Check if an __init__ method only does self.x = ... assignments.

    These are kept because they define instance attributes needed for class structure.
    """
    if func_node.name != "__init__":
        return False

    for stmt in func_node.body:
        # Skip docstrings
        if is_docstring(stmt):
            continue
        # Allow: self.x = expr
        if isinstance(stmt, ast.Assign):
            if all(
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
                for t in stmt.targets
            ):
                continue
        # Allow: self.x: type = expr (annotated assignment)
        if isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
        # Allow: super().__init__(...) calls
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "__init__":
                if isinstance(call.func.value, ast.Call):
                    func = call.func.value
                    if isinstance(func.func, ast.Name) and func.func.id == "super":
                        continue
        # Any other statement means this isn't pure assignment
        return False

    return True


def has_abstractmethod(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has @abstractmethod decorator."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "abstractmethod":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod":
            return True
    return False


def has_overload(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function has @overload decorator."""
    for dec in func_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "overload":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "overload":
            return True
    return False


def is_already_stub(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if function body is already a stub (pass, ..., or raise NotImplementedError)."""
    body = func_node.body

    # Skip docstring if present
    start = 1 if body and is_docstring(body[0]) else 0
    remaining = body[start:]

    if not remaining:
        return True

    if len(remaining) == 1:
        stmt = remaining[0]
        # pass
        if isinstance(stmt, ast.Pass):
            return True
        # ... (Ellipsis)
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            if stmt.value.value is ...:
                return True
        # raise NotImplementedError
        if isinstance(stmt, ast.Raise):
            return True

    return False


class StubTransformer:
    """Transforms Python source by replacing function bodies with pass.

    Uses a line-based approach to preserve comments and formatting:
    1. Parse AST to identify function body ranges
    2. Collect replacement ranges (body start line, body end line)
    3. Reconstruct source with bodies replaced

    Supports three removal modes (matching the official commit0 paper):
    - "all":       Replace ALL function bodies with pass
    - "docstring": Only stub functions that HAVE docstrings
    - "combined":  Stub functions with docstrings + REMOVE functions without
    """

    VALID_MODES = ("all", "docstring", "combined")

    def __init__(
        self,
        *,
        keep_docstrings: bool = False,
        removal_mode: str = "all",
        import_time_names: set[str] | None = None,
    ) -> None:
        if removal_mode not in self.VALID_MODES:
            raise ValueError(
                f"Invalid removal_mode: {removal_mode!r}. "
                f"Must be one of {self.VALID_MODES}"
            )
        self.keep_docstrings = keep_docstrings
        self.removal_mode = removal_mode
        self.import_time_names = import_time_names or set()
        self.stub_count = 0
        self.removed_count = 0
        self.preserved_count = 0

    def transform_source(self, source: str, filename: str = "<unknown>") -> str | None:
        """Transform a Python source string, returning stubbed version.

        Returns None if the file has no functions to stub.
        """
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as e:
            logger.warning("Syntax error in %s: %s — copying as-is", filename, e)
            return None

        lines = source.splitlines(keepends=True)
        if not lines:
            return source

        replacements = self._collect_replacements(tree, lines)
        removals = self._collect_removals(tree, lines)

        # When keep_docstrings is False, also delete module-level and class-level
        # docstrings. Function/method docstrings are already removed via body
        # replacement in _collect_replacements. Docstrings can leak implementation
        # intent ("This module uses lazy splitting") just like body comments, so
        # we strip them when the caller opts into stripping at all.
        doc_deletes: list[tuple[int, int, str | None]] = []
        if not self.keep_docstrings:
            for n in ast.walk(tree):
                if isinstance(n, (ast.Module, ast.ClassDef)) and n.body and is_docstring(n.body[0]):
                    stmt = n.body[0]
                    start_0 = stmt.lineno - 1
                    end_0 = self._get_end_lineno(stmt, lines) - 1
                    # A docstring that is the SOLE statement of a class body
                    # must become `pass`, not vanish — otherwise the class body
                    # goes empty and the file fails to parse (IndentationError).
                    if isinstance(n, ast.ClassDef) and len(n.body) == 1:
                        indent = self._get_indent(lines, n.lineno - 1) + "    "
                        doc_deletes.append((start_0, end_0, indent))
                    else:
                        doc_deletes.append((start_0, end_0, None))

        if not replacements and not removals and not doc_deletes:
            return source

        all_ops: list[tuple[int, int, str | None]] = []
        for body_start, body_end, indent_str in replacements:
            all_ops.append((body_start, body_end, indent_str))
        for func_start, func_end in removals:
            all_ops.append((func_start, func_end, None))
        all_ops.extend(doc_deletes)

        all_ops = self._remove_nested_ops(all_ops)
        all_ops.sort(key=lambda r: r[0], reverse=True)

        for start, end, indent_str in all_ops:
            if indent_str is None:
                lines[start : end + 1] = []
                self.removed_count += 1
            else:
                lines[start : end + 1] = [f"{indent_str}pass\n"]
                self.stub_count += 1

        result = "".join(lines)
        result = self._strip_standalone_comments(result)

        if self.removal_mode == "combined" and removals:
            result = self._fix_empty_classes(result, filename)

        return result

    def _collect_replacements(
        self,
        tree: ast.Module,
        lines: list[str],
    ) -> list[tuple[int, int, str | None]]:
        """Collect (body_start_0idx, body_end_0idx, indent) for each function to stub."""
        replacements: list[tuple[int, int, str | None]] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            if has_abstractmethod(node):
                continue
            if has_overload(node):
                continue
            if is_already_stub(node):
                continue
            if is_dunder_method(node):
                continue
            if isinstance(node, ast.FunctionDef) and is_pure_assignment_init(node):
                continue
            if node.name in self.import_time_names:
                self.preserved_count += 1
                continue

            has_doc = bool(node.body and is_docstring(node.body[0]))

            if self.removal_mode == "docstring" and not has_doc:
                continue
            if self.removal_mode == "combined" and not has_doc:
                continue

            body = node.body
            if not body:
                continue

            body_end_1 = self._get_end_lineno(body[-1], lines)
            body_end_0 = body_end_1 - 1

            # Find where the function body truly begins — the line immediately
            # after the def/async def header's closing colon. Using body[0].lineno
            # would miss comment lines between the colon and the first real statement
            # (comments are not AST nodes and would survive stubbing as hint leaks).
            real_start_0 = self._find_body_real_start(node, lines)
            body_start_0 = real_start_0

            # Use body[0].lineno for indent detection (reliable; the def colon line
            # or a comment line could have unexpected leading whitespace).
            indent_str = self._get_indent(lines, node.body[0].lineno - 1)

            if self.keep_docstrings and body and is_docstring(body[0]):
                doc_node = body[0]
                doc_start_0 = doc_node.lineno - 1
                doc_end_1 = self._get_end_lineno(doc_node, lines)
                doc_end_0 = doc_end_1 - 1

                # Delete any comment/blank lines between the def colon and the
                # docstring — they are invisible to AST but can leak hints.
                if real_start_0 < doc_start_0:
                    replacements.append((real_start_0, doc_start_0 - 1, None))

                if len(body) > 1:
                    body_start_0 = doc_end_0 + 1
                else:
                    continue

            replacements.append((body_start_0, body_end_0, indent_str))

        return replacements

    def _collect_removals(
        self,
        tree: ast.Module,
        lines: list[str],
    ) -> list[tuple[int, int]]:
        """Collect (func_start_0idx, func_end_0idx) for functions to remove entirely.

        Only applies in 'combined' mode: functions WITHOUT docstrings are removed.
        """
        if self.removal_mode != "combined":
            return []

        removals: list[tuple[int, int]] = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            if has_abstractmethod(node):
                continue
            if has_overload(node):
                continue
            if is_already_stub(node):
                continue
            if is_dunder_method(node):
                continue
            if isinstance(node, ast.FunctionDef) and is_pure_assignment_init(node):
                continue
            if node.name in self.import_time_names:
                continue

            has_doc = bool(node.body and is_docstring(node.body[0]))

            if has_doc:
                continue

            func_start_0 = node.lineno - 1
            for dec in node.decorator_list:
                func_start_0 = min(func_start_0, dec.lineno - 1)
            func_end_0 = self._get_end_lineno(node, lines) - 1

            removals.append((func_start_0, func_end_0))

        return removals

    def _fix_empty_classes(self, source: str, filename: str) -> str:
        """After removing functions, replace empty class bodies with `pass`."""
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as e:
            logger.debug("SyntaxError in _minify_ast for %s: %s", filename, e)
            return source

        lines = source.splitlines(keepends=True)
        fixes: list[tuple[int, str]] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            non_pass = [s for s in node.body if not isinstance(s, ast.Pass)]
            if non_pass:
                continue
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                continue
            if not node.body:
                body_line = node.end_lineno - 1 if node.end_lineno else node.lineno - 1
                indent = self._get_indent(lines, node.lineno - 1) + "    "
                fixes.append((body_line, f"{indent}pass\n"))

        for line_idx, replacement in sorted(fixes, reverse=True):
            lines[line_idx : line_idx + 1] = [replacement]

        return "".join(lines)

    @staticmethod
    def _remove_nested(
        replacements: list[tuple[int, int, str]],
    ) -> list[tuple[int, int, str]]:
        """Filter out replacements whose range is entirely inside another's range."""
        sorted_by_range = sorted(replacements, key=lambda r: (r[0], -r[1]))
        result: list[tuple[int, int, str]] = []

        for start, end, indent in sorted_by_range:
            is_nested = any(ps <= start and end <= pe for ps, pe, _ in result)
            if not is_nested:
                result.append((start, end, indent))

        return result

    @staticmethod
    def _remove_nested_ops(
        ops: list[tuple[int, int, str | None]],
    ) -> list[tuple[int, int, str | None]]:
        """Filter out operations whose range is entirely inside another's range."""
        sorted_by_range = sorted(ops, key=lambda r: (r[0], -r[1]))
        result: list[tuple[int, int, str | None]] = []

        for start, end, data in sorted_by_range:
            is_nested = any(ps <= start and end <= pe for ps, pe, _ in result)
            if not is_nested:
                result.append((start, end, data))

        return result

    @staticmethod
    def _get_end_lineno(node: ast.AST, lines: list[str]) -> int:
        """Get the end line number of an AST node (1-indexed).

        Falls back to scanning for the last non-empty line if end_lineno is not available.
        """
        end = getattr(node, "end_lineno", None)
        if end is not None:
            return end

        # Fallback: use node's line number (imprecise but safe)
        return getattr(node, "lineno", len(lines))

    @staticmethod
    def _get_indent(lines: list[str], line_idx: int) -> str:
        """Extract the leading whitespace from a line."""
        if line_idx < len(lines):
            line = lines[line_idx]
            stripped = line.lstrip()
            return line[: len(line) - len(stripped)]
        return "    "  # Default to 4 spaces

    @staticmethod
    def _find_body_real_start(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        lines: list[str],
    ) -> int:
        """Return the 0-indexed line immediately after the function header's closing colon.

        Scans forward from the ``def``/``async def`` line to find the first line whose
        trailing content ends with ``:``.  This covers both single-line signatures
        (``def foo():``) and multi-line signatures (``def foo(\n    x,\n):``).
        Including pre-body comment lines in the replacement range prevents them from
        leaking as implementation hints in the stubbed output.

        Heuristic limitation: a multi-line parameter default whose continuation line
        ends with ``:`` (e.g. ``x: dict = {"a":\n    1}`` split across lines) can
        produce a false-positive, pointing ``real_start_0`` into the middle of the
        signature.  The post-stub ``ast.parse()`` call in ``stub_file`` is the safety
        net: any such corruption raises ``SyntaxError`` and the file is copied as-is.
        """
        first_stmt_0 = node.body[0].lineno - 1  # 0-indexed
        def_start_0 = node.lineno - 1  # 0-indexed

        for scan_0 in range(def_start_0, first_stmt_0):
            if lines[scan_0].rstrip().endswith(":"):
                return scan_0 + 1  # line immediately after the closing colon

        # Fallback: start at body[0].lineno (original behaviour)
        return first_stmt_0

    @staticmethod
    def _strip_standalone_comments(source: str) -> str:
        """Remove standalone comment lines from the stubbed output.

        Uses Python’s tokenizer so that ``#``-prefixed lines *inside*
        docstrings and multi-line strings are never touched.  Only lines
        where the comment is the sole non-whitespace content are removed;
        inline comments on code lines (e.g. ``x = 1  # noqa``) survive.

        Preserved standalone ``#`` lines:
        - Shebang lines (``#!``)
        - Encoding declarations (``# -*- coding:`` / ``# coding=``)
        """
        comment_lines: set[int] = set()
        try:
            toks = tokenize.generate_tokens(io.StringIO(source).readline)
            for tok_type, tok_string, tok_start, _end, tok_line in toks:
                if tok_type != tokenize.COMMENT:
                    continue
                # Standalone only: the comment must be the first non-whitespace
                if not tok_line.lstrip().startswith("#"):
                    continue
                # Preserve shebang and encoding declarations
                if tok_string.startswith("#!") or "coding" in tok_string[:30]:
                    continue
                comment_lines.add(tok_start[0])  # tok_start is (row, col), row is 1-indexed
        except tokenize.TokenError:
            return source  # fallback: don’t strip if tokenisation fails

        if not comment_lines:
            return source

        out_lines: list[str] = []
        for lineno, line in enumerate(source.splitlines(keepends=True), start=1):
            if lineno not in comment_lines:
                out_lines.append(line)
        return "".join(out_lines)


def stub_file(
    source_path: Path,
    output_path: Path,
    *,
    keep_docstrings: bool = False,
    removal_mode: str = "all",
    dry_run: bool = False,
    import_time_names: set[str] | None = None,
) -> tuple[bool, int, int, int]:
    """Stub a single Python file.

    Returns (was_modified, stub_count, removed_count, preserved_count).
    """
    try:
        source = source_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError) as e:
        logger.warning("Cannot read %s: %s — skipping", source_path, e)
        return False, 0, 0, 0

    transformer = StubTransformer(
        keep_docstrings=keep_docstrings,
        removal_mode=removal_mode,
        import_time_names=import_time_names,
    )
    result = transformer.transform_source(source, str(source_path))

    if result is None:
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, output_path)
        return False, 0, 0, 0

    if result == source:
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(result, encoding="utf-8")
        return False, 0, 0, transformer.preserved_count

    try:
        ast.parse(result, str(output_path))
    except SyntaxError as e:
        logger.error(
            "Stubbing produced invalid Python for %s: %s — copying original",
            source_path,
            e,
        )
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, output_path)
        return False, 0, 0, 0

    if not dry_run:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result, encoding="utf-8")

    return (
        True,
        transformer.stub_count,
        transformer.removed_count,
        transformer.preserved_count,
    )


def stub_directory(
    source_dir: Path,
    output_dir: Path,
    *,
    keep_docstrings: bool = False,
    removal_mode: str = "all",
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Stub all Python files in a directory tree.

    Returns summary stats.
    """
    import_time_names = collect_import_time_names(source_dir)
    if import_time_names and verbose:
        logger.info(
            "  [SAFE] Preserving %d import-time functions: %s",
            len(import_time_names),
            ", ".join(sorted(import_time_names)[:20]),
        )

    stats = {
        "files_processed": 0,
        "files_modified": 0,
        "files_skipped": 0,
        "files_copied": 0,
        "total_stubs": 0,
        "total_removed": 0,
        "total_preserved": 0,
        "test_files_skipped": 0,
        "errors": 0,
    }

    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()

    for py_file in sorted(source_dir.rglob("*.py")):
        # Skip directories we don't want
        rel_parts = py_file.relative_to(source_dir).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue

        # Skip .pyi files
        if py_file.suffix == ".pyi":
            continue

        rel_path = py_file.relative_to(source_dir)

        if should_skip_file(py_file):
            stats["test_files_skipped"] += 1
            if not dry_run:
                out = output_dir / rel_path
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(py_file, out)
            if verbose:
                logger.info("  [SKIP] %s — copied as-is", rel_path)
            continue

        stats["files_processed"] += 1

        out_path = output_dir / rel_path
        modified, count, removed, preserved = stub_file(
            py_file,
            out_path,
            keep_docstrings=keep_docstrings,
            removal_mode=removal_mode,
            dry_run=dry_run,
            import_time_names=import_time_names,
        )

        if modified:
            stats["files_modified"] += 1
            stats["total_stubs"] += count
            stats["total_removed"] += removed
            stats["total_preserved"] += preserved
            if verbose:
                msg = f"  [STUB] {rel_path} — {count} stubbed"
                if removed:
                    msg += f", {removed} removed"
                logger.info(msg)
        else:
            stats["files_copied"] += 1
            stats["total_preserved"] += preserved
            if verbose:
                logger.info("  [COPY] %s — no functions to stub", rel_path)

    # Copy non-Python files needed for the project
    _copy_non_python_files(source_dir, output_dir, dry_run=dry_run)

    return stats


def _copy_non_python_files(
    source_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Copy essential non-Python files (configs, data, etc.)."""
    # Essential config files to copy
    config_patterns = [
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "MANIFEST.in",
        "requirements*.txt",
        "tox.ini",
        "pytest.ini",
        "conftest.py",
        ".coveragerc",
        "Makefile",
        "LICENSE*",
        "README*",
    ]

    for pattern in config_patterns:
        for f in source_dir.glob(pattern):
            if f.is_file():
                rel = f.relative_to(source_dir)
                if not dry_run:
                    out = output_dir / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, out)


def print_summary(stats: dict, output_dir: Path) -> None:
    """Print a human-readable summary."""
    print(f"\n{'=' * 60}")
    print("STUBBING SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Output directory:     {output_dir}")
    print(f"  Files processed:      {stats['files_processed']}")
    print(f"  Files modified:       {stats['files_modified']}")
    print(f"  Files copied (no fn): {stats['files_copied']}")
    print(f"  Test files skipped:   {stats['test_files_skipped']}")
    print(f"  Total stubs created:  {stats['total_stubs']}")
    if stats.get("total_preserved"):
        print(f"  Import-safe preserved:{stats['total_preserved']}")
    if stats.get("total_removed"):
        print(f"  Functions removed:    {stats['total_removed']}")
    if stats["errors"]:
        print(f"  Errors:               {stats['errors']}")
    print(f"{'=' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stub Python function bodies with pass statements"
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Source directory (repo root)",
    )
    parser.add_argument(
        "output",
        type=Path,
        help="Output directory for stubbed files",
    )
    parser.add_argument(
        "--removal-mode",
        choices=("all", "docstring", "combined"),
        default="all",
        help=(
            "How to handle functions: "
            "'all' = stub everything, "
            "'docstring' = only stub functions with docstrings, "
            "'combined' = stub docstring functions + remove non-docstring functions "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--keep-docstrings",
        action="store_true",
        help="Keep docstrings in stubbed functions (default: strip them, since docstrings can leak implementation intent for benchmark stubbing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-file details",
    )

    args = parser.parse_args()

    if not args.source.is_dir():
        logger.error("Source directory does not exist: %s", args.source)
        sys.exit(1)

    if args.output.exists() and not args.dry_run:
        logger.warning(
            "Output directory exists: %s — files may be overwritten", args.output
        )

    logger.info(
        "Stubbing %s → %s (mode=%s)", args.source, args.output, args.removal_mode
    )

    stats = stub_directory(
        args.source,
        args.output,
        keep_docstrings=args.keep_docstrings,
        removal_mode=args.removal_mode,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    print_summary(stats, args.output)


if __name__ == "__main__":
    main()
