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
                       *, patch: str | None = None, cap: int = _CODE_CAP) -> str:
    """Concatenated source (bounded) of *src_dir* at *commit*, with *patch* applied.
    Returns '' on failure. Uses a throwaway git worktree."""
    from .pytest_runner import checkout_solution, remove_worktree
    wt = checkout_solution(repo_dir, commit, patch=patch)
    if wt is None:
        return ""
    try:
        root = wt / src_dir if src_dir and src_dir != "." else wt
        parts: list[str] = []
        used = 0
        for f in sorted(root.rglob("*.py")):
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
