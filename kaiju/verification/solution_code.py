"""Read the FULL solution code for a task at a given commit (+ optional patch),
scoped to the repo's src_dir. Used to give the judge complete cross-file context
(RCA 2: a criterion about the CLI in __main__.py can't be verified from the stubbed
file alone). Shared by the rubric anchors (golden/stub) and the candidate digest so
all three are judged on the SAME scope.
"""
from __future__ import annotations

from pathlib import Path

_CODE_CAP = 30000            # per-solution char budget for the judged code
_MAX_FILES = 40


def read_solution_code(repo_dir, commit: str, src_dir: str = ".",
                       *, patch: str | None = None, cap: int = _CODE_CAP,
                       exts: tuple[str, ...] = (".py",)) -> str:
    """Concatenated source (bounded) of *src_dir* at *commit*, with *patch* applied.
    Returns '' on failure. Uses a throwaway git worktree.

    ``exts`` selects which source files count — callers derive it from the
    task's own stub files (see ``exts_from_stub_files``). The old hardcoded
    ``*.py`` glob made every non-Python golden/stub digest EMPTY, so the
    build-time anchor judge (conservative fail-if-uncertain) failed the GOLDEN
    solution on every anchorable criterion and anchor validation dropped
    nearly the whole rubric on go/rust/etc."""
    from .pytest_runner import checkout_solution, remove_worktree
    wt = checkout_solution(repo_dir, commit, patch=patch)
    if wt is None:
        return ""
    try:
        root = wt / src_dir if src_dir and src_dir != "." else wt
        parts: list[str] = []
        used = 0
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in exts)
        for f in files:
            if any(seg in ("tests", "test", "__pycache__", ".git") for seg in f.parts):
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = f.relative_to(wt)
            chunk = f"# ===== {rel} =====\n{body}\n"
            parts.append(chunk)
            used += len(chunk)
            if used > cap or len(parts) > _MAX_FILES:
                parts.append("# …[solution code truncated]")
                break
        return "\n".join(parts)
    finally:
        remove_worktree(repo_dir, wt)


def exts_from_stub_files(stub_files) -> tuple[str, ...]:
    """Source-file extensions for this task, derived from its own stub files —
    language-agnostic without needing a (frequently empty) language field."""
    exts = tuple(sorted({s for s in (Path(f).suffix for f in (stub_files or [])) if s}))
    return exts or (".py",)
