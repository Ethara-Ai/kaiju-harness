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
    # all-or-nothing per invocation).
    for tgt in revert_targets:
        lines.append(f"git checkout {base_commit} -- {tgt} 2>/dev/null || true")
        if nested and not tgt.startswith(":("):
            lines.append(f"git checkout {base_commit} -- '**/{tgt}' 2>/dev/null || true")
    # (2) Delete model-ADDED files under the delete globs (checkout can't remove
    # a path that didn't exist at base).
    if delete_added_globs:
        globs = " ".join("'" + g + "'" for g in delete_added_globs)
        lines.append(
            f"for _kp in $(git diff --name-only --diff-filter=A {base_commit} -- "
            f"{globs} 2>/dev/null); do rm -rf \"$_kp\" 2>/dev/null || true; done"
        )
    return lines


def cheat_guard_line(base_commit: str, verify_globs: Sequence[str],
                     output_file: str = "test_output.txt") -> str:
    """A belt-and-suspenders check: if any protected path STILL differs from base
    after the revert, append a CHEAT_DETECTED sentinel to ``output_file`` (which
    the language's evaluator greps). Prevention (revert+clean) is primary; this
    only catches a revert that somehow failed.
    """
    globs = " ".join(verify_globs)
    return (
        f"if ! git diff --quiet {base_commit} -- {globs} 2>/dev/null; then "
        f"echo 'CHEAT_DETECTED: protected test/build paths still differ from base "
        f"after revert' >> {output_file}; fi"
    )


__all__ = ["revert_and_clean_lines", "cheat_guard_line"]
