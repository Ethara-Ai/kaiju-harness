"""Derive the pool of source files aider is allowed to READ (but not edit).

The commit0 agent runners pass a single stub file (``fnames = [target]``) to the
agent, so the model's EDIT scope is one module. But aider needs to READ sibling
source files to understand cross-module signatures, trait bounds, header includes,
type imports, etc.

Historically we set ``GuardedInputOutput(allowed_add_paths=fnames)`` which conflated
edit-scope and read-scope, causing multi-file repos (rust/aarc, java multi-package,
c++ template libraries, etc.) to fail at "Add file to chat?" prompts.

This helper computes the read-scope automatically from ``fnames[0]``:

  1. Walk from ``fnames[0]`` up to the repo root (git worktree root, or the
     highest ancestor containing a src/lib/crates directory).
  2. Recursively find all files matching the language extension(s).
  3. Exclude tests, generated code, and build artifacts via a conservative
     denylist that works across languages.

Runners can override the auto-derivation by passing ``allowed_add_paths_extra``
explicitly to ``agent.run()`` — the auto-derive is a fallback only.
"""
from __future__ import annotations

import os
from pathlib import Path

EXTENSION_MAP: dict[str, tuple[str, ...]] = {
    ".py":   (".py",),
    ".rs":   (".rs",),
    ".go":   (".go",),
    ".java": (".java",),
    ".c":    (".c", ".h"),
    ".h":    (".c", ".h"),
    ".cpp":  (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    ".cc":   (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    ".hpp":  (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"),
    ".js":   (".js", ".mjs", ".cjs"),
    ".mjs":  (".js", ".mjs", ".cjs"),
    ".cjs":  (".js", ".mjs", ".cjs"),
    ".ts":   (".ts", ".tsx"),
    ".tsx":  (".ts", ".tsx"),
}

EXCLUDE_DIR_SEGMENTS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "vendor", "target",
    "build", "dist", "out",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env",
    ".tox", ".nox",
    "coverage", "htmlcov",
    ".idea", ".vscode",
    "generated", "gen",
})

TEST_DIR_SEGMENTS: frozenset[str] = frozenset({
    "tests", "test", "__tests__", "spec", "specs",
})

TEST_FILENAME_PATTERNS: tuple[str, ...] = (
    "_test.",
    ".test.",
    ".spec.",
    "test_",
    "_spec.",
)


def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` to find the git worktree root (or highest sensible
    ancestor). Falls back to ``start.parent`` if no git dir found."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return start.parent if start.is_file() else start


def _is_test_path(path: Path, repo_root: Path) -> bool:
    """True if path lives under a test directory OR matches a test filename pattern."""
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        rel = path
    parts = set(rel.parts)
    if parts & TEST_DIR_SEGMENTS:
        return True
    name = path.name.lower()
    return any(pat in name for pat in TEST_FILENAME_PATTERNS)


def _is_excluded(path: Path, repo_root: Path) -> bool:
    """True if path lives under a known build/vendor/cache directory."""
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        rel = path
    return bool(set(rel.parts) & EXCLUDE_DIR_SEGMENTS)


def derive_source_pool(
    fnames: list[str] | tuple[str, ...],
    include_tests: bool = False,
) -> list[str]:
    """Derive the read-scope pool for ``allowed_add_paths_extra``.

    Args:
        fnames: The agent's edit-scope. Uses ``fnames[0]``'s extension + parent
            tree to find sibling source files.
        include_tests: If True, include test files in the pool. Default False
            because test files are protected separately via ``protected_paths``
            (anti-cheat: model must not read tests to solve the problem).

    Returns:
        Absolute paths to sibling source files the agent MAY add to chat as
        read-only context. Empty list if fnames is empty or extension unknown.
    """
    if not fnames:
        return []

    anchor = Path(fnames[0]).resolve()
    if not anchor.exists() and anchor.parent.exists():
        # anchor is a phantom stub path; use its parent as the search anchor
        anchor = anchor.parent

    extensions = EXTENSION_MAP.get(anchor.suffix.lower())
    if not extensions:
        return []

    repo_root = _find_repo_root(anchor)

    pool: set[str] = set()
    for ext in extensions:
        for candidate in repo_root.rglob(f"*{ext}"):
            if not candidate.is_file():
                continue
            if _is_excluded(candidate, repo_root):
                continue
            if not include_tests and _is_test_path(candidate, repo_root):
                continue
            pool.add(str(candidate))

    return sorted(pool)
