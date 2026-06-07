"""Detect system-level (non-pip) Python dependencies by scanning imports.

Some repos depend on Python bindings to system libraries that aren't
installable via pip alone — QGIS (qgis.core), GDAL (osgeo), GTK (gi),
Qt (PyQt5/PyQt6/PySide), OpenCV (cv2), ROS (rospy), etc. These require apt
packages (or platform-specific installers) before the test suite can
even *collect* without `ModuleNotFoundError`.

This scanner walks a repo's test directory, parses each .py file's AST,
extracts top-level import names, and returns the subset that match known
system-dep modules. The list is written into dataset entries as
``setup["system_deps_hint"]`` so downstream tooling can:

  * Choose a Docker image with the needed apt packages preinstalled.
  * Mark the entry as "needs system deps" without ever running collection.
  * Pick a different runtime tier (skip uv, use Docker).

Heuristics-only — false negatives are fine (downstream collection will
catch them). The goal is to surface obvious cases before pipeline failure.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "SYSTEM_DEP_MODULES",
    "scan_imports",
    "scan_repo_for_system_deps",
]


# Top-level import names that require apt/system installation.
# Keep this list in sync with ``tools/python_runtime._SYSTEM_DEP_MODULES``
# (used by the failure classifier) — different layer, same vocabulary.
SYSTEM_DEP_MODULES: frozenset[str] = frozenset(
    {
        "qgis",
        "cv2",
        "osgeo",
        "gdal",
        "rasterio",
        "gi",  # pygobject (GTK)
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "rospy",
        "tkinter",
        "_tkinter",
        "cairo",
        "gobject",
        "vtk",
        "ROOT",  # CERN/PyROOT
    }
)


def scan_imports(source: str) -> set[str]:
    """Extract top-level import module names from a Python source string.

    Includes BOTH unconditional and conditional imports (those wrapped in
    ``try/except ImportError``). For the pre-flight gate use
    :func:`scan_required_imports`, which excludes the conditional ones.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        import re

        for m in re.finditer(
            r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
            source,
            re.MULTILINE,
        ):
            mod = m.group(1) or m.group(2)
            if mod:
                names.add(mod.split(".")[0])
        return names

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


# Exception classes treated as "optional-import signaling" — catching any of
# these around an import means the code is prepared for it to be absent.
_OPTIONAL_IMPORT_EXCEPTIONS = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


def _handler_catches_optional_import(handler: ast.ExceptHandler) -> bool:
    """True if this ``except`` clause would swallow an ImportError."""
    if handler.type is None:  # bare ``except:``
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in _OPTIONAL_IMPORT_EXCEPTIONS
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(e, ast.Name) and e.id in _OPTIONAL_IMPORT_EXCEPTIONS
            for e in handler.type.elts
        )
    if isinstance(handler.type, ast.Attribute):
        # e.g. ``except builtins.ImportError`` (rare but legal)
        return handler.type.attr in _OPTIONAL_IMPORT_EXCEPTIONS
    return False


class _RequiredImportCollector(ast.NodeVisitor):
    """Collect top-level imports NOT wrapped in ``try/except ImportError``."""

    def __init__(self) -> None:
        self.required: set[str] = set()
        self._optional_depth = 0

    def visit_Try(self, node: ast.Try) -> None:
        catches_optional = any(
            _handler_catches_optional_import(h) for h in node.handlers
        )
        if catches_optional:
            self._optional_depth += 1
            for stmt in node.body:
                self.visit(stmt)
            self._optional_depth -= 1
        else:
            for stmt in node.body:
                self.visit(stmt)
        for handler in node.handlers:
            self.visit(handler)
        for stmt in node.orelse:
            self.visit(stmt)
        for stmt in node.finalbody:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        if self._optional_depth > 0:
            return
        for alias in node.names:
            self.required.add(alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._optional_depth > 0:
            return
        if node.module and node.level == 0:
            self.required.add(node.module.split(".")[0])


def scan_required_imports(source: str) -> set[str]:
    """Like :func:`scan_imports` but excludes imports inside ``try/except``
    blocks that catch ``ImportError`` (or one of its supertypes).

    Why: these are *optional* imports — the surrounding code already handles
    their absence (e.g. ``try: import ROOT; except ImportError: pytest.skip()``).
    A pre-flight gate that short-circuits on them produces false positives.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Regex fallback can't see try-blocks — caller will treat all as required.
        return scan_imports(source)
    collector = _RequiredImportCollector()
    collector.visit(tree)
    return collector.required


def scan_repo_for_system_deps(
    repo_dir: Path,
    test_dirs: list[str] | None = None,
    *,
    max_files: int = 500,
) -> list[str]:
    """Walk a repo's test directories and return any system-dep modules found.

    Parameters
    ----------
    repo_dir
        Repository root.
    test_dirs
        Subdirectory names to scan. Defaults to ``["tests", "test"]`` plus
        any toplevel ``conftest.py``.
    max_files
        Safety cap — stop scanning after this many .py files. Repos with
        huge test directories don't need exhaustive coverage; one positive
        hit per module is enough.

    Returns
    -------
    Sorted list of top-level module names from :data:`SYSTEM_DEP_MODULES`
    found anywhere in the scanned files. Empty if none detected.

    """
    if test_dirs is None:
        test_dirs = ["tests", "test"]

    found: set[str] = set()
    scanned = 0

    candidates: list[Path] = []
    for d in test_dirs:
        td = repo_dir / d
        if td.is_dir():
            candidates.extend(sorted(td.rglob("*.py")))
    # Always scan toplevel conftest.py — often imports fixtures with system deps
    top_conftest = repo_dir / "conftest.py"
    if top_conftest.is_file():
        candidates.append(top_conftest)

    for py_file in candidates:
        if scanned >= max_files:
            logger.debug("scan_repo_for_system_deps hit max_files=%d", max_files)
            break
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        imports = scan_required_imports(source)
        hits = imports & SYSTEM_DEP_MODULES
        if hits:
            found.update(hits)
            # Short-circuit: once we've seen all possible matches, stop early
            if found == SYSTEM_DEP_MODULES:
                break

    return sorted(found)
