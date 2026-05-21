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

    Handles both ``import x.y.z`` (top-level: ``x``) and ``from x.y import z``
    (top-level: ``x``). Falls back to regex when AST parsing fails (syntax
    errors are common in stubbed test fixtures, etc.).
    """
    names: set[str] = set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        # Fallback regex: catch common ``import X`` / ``from X import ...`` lines
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
        imports = scan_imports(source)
        hits = imports & SYSTEM_DEP_MODULES
        if hits:
            found.update(hits)
            # Short-circuit: once we've seen all possible matches, stop early
            if found == SYSTEM_DEP_MODULES:
                break

    return sorted(found)
