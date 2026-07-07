"""Shared reward-hacking hardening for the per-language eval scripts.

Every language's ``make_eval_script_list`` reconstructs the scored run from the
model's patch: ``git reset --hard base`` -> ``git apply patch`` -> revert the
test/build files -> run the tests. Two cross-cutting hardening steps live here
so every language behaves identically ("same procedure"):

1. **Per-pathspec revert.** ``git checkout <base> -- pathA pathB ...`` is
   ALL-OR-NOTHING per invocation: if *any* pathspec matches zero tracked files
   the whole checkout aborts and reverts NOTHING. A single-command revert
   therefore silently no-ops on any repo missing one listed path (e.g. a repo
   with ``tests/`` but no ``test/``), leaving the model's test edits in place —
   a live reward-hacking hole. We revert each path INDEPENDENTLY so one miss
   can't poison the rest.

2. **Delete model-ADDED hook/config files.** ``git checkout`` only restores
   *tracked* paths; a NEW file the model added (a fresh ``conftest.py``,
   ``build.rs``, babel plugin, ``sitecustomize.py``, ``go.work`` ...) is not at
   base, so it survives the revert and still runs at test time. We delete
   anything newly-added (``git diff --diff-filter=A``) under the given globs.

Callers pass the language-specific ``revert_targets`` (dirs/files/globs) and
``delete_added_globs`` (patterns for files that must not be model-introduced).
"""

from __future__ import annotations

import shlex
from typing import Sequence


def revert_and_clean_lines(
    base_commit: str,
    revert_targets: Sequence[str],
    delete_added_globs: Sequence[str] = (),
    nested: bool = True,
) -> list[str]:
    """Return bash lines that robustly revert test/build files to ``base_commit``
    and delete any newly-added hook/config files.

    ``base_commit`` is interpolated into the script and MUST already be a
    validated bare git SHA (callers use ``_require_commitish``); this function
    does not re-validate.

    With ``nested=True`` each target is reverted at the root AND under ``**/``.
    Pass ``nested=False`` when the caller already supplies both root and nested
    forms (e.g. git ``:(glob)`` magic pathspecs), so magic prefixes aren't
    mangled by a ``**/`` prefix.
    """
    lines: list[str] = []
    # (1) Independent per-pathspec revert, so a pathspec that matches zero
    # tracked files can't abort the reverts for the others (git checkout is
    # all-or-nothing per invocation). Each target is SINGLE-QUOTED so a glob like
    # `*_test.go` reaches git as a pathspec instead of being expanded by the
    # shell — otherwise a model-added sibling (`evil_test.go`) would be included
    # in the expansion and, being absent at base, abort the whole checkout and
    # leave the real test unreverted. TS passes git `:(glob)` magic pathspecs
    # (raw); shlex.quote keeps them intact and we skip the `**/` doubling.
    for tgt in revert_targets:
        lines.append(f"git checkout {base_commit} -- {shlex.quote(tgt)} 2>/dev/null || true")
        if nested and not tgt.startswith(":("):
            lines.append(
                f"git checkout {base_commit} -- {shlex.quote('**/' + tgt)} "
                "2>/dev/null || true"
            )
    # (2) Delete model-ADDED hook/config files. The patch is applied with plain
    # `git apply` (no --index), so added files are UNTRACKED — `git diff` cannot
    # see them, which made the old `git diff --diff-filter=A` a silent no-op.
    # Enumerate untracked adds with `ls-files --others`; also cover the
    # tracked-added case for any flow that stages. `-z` + `read -d ''` is safe
    # for paths containing spaces.
    if delete_added_globs:
        globs = " ".join(shlex.quote(g) for g in delete_added_globs)
        lines.append(
            f"git ls-files --others --exclude-standard -z -- {globs} 2>/dev/null | "
            'while IFS= read -r -d "" _kp; do rm -rf "$_kp" 2>/dev/null || true; done'
        )
        lines.append(
            f"git diff --name-only -z --diff-filter=A {base_commit} -- {globs} "
            '2>/dev/null | while IFS= read -r -d "" _kp; do '
            'rm -rf "$_kp" 2>/dev/null || true; done'
        )
    return lines


__all__ = ["revert_and_clean_lines"]
