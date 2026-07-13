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


# ---------------------------------------------------------------------------
# Layer-2 self-healing guard against harness-induced corruption.
#
# The eval reconstructs the scored run by MUTATING the tree: `git apply` the
# model patch, then a chain of anti-cheat rewrites (per-path reverts, in-src
# test restore, goimports, sed on build files, ...). Any of those steps can, on
# unusual-but-valid code, turn a tree that COMPILES into one that does not —
# scoring a working submission as COMPILE_FAILED (the byteorder macro_rules!
# case). Enumerating every such step is a losing game across a large, diverse
# dataset, so we add ONE general post-condition instead:
#
#   snapshot every changed source file RIGHT AFTER `git apply` (pure model code,
#   known-good), then just before the build re-check each file. If a file was
#   bracket-balanced then and is UNBALANCED now, a harness rewrite corrupted it
#   -> restore the post-apply copy so a valid submission is never failed by the
#   harness. The decision is DELTA-based (balanced-then, unbalanced-now), so an
#   imperfect balance heuristic can't cause a false restore.
#
# Bracket balance is a cheap, dependency-free, language-agnostic proxy for "still
# parses" that catches exactly the corruption these text rewrites produce (an
# orphaned/duplicated brace). It is NOT a parser and is not meant to be.
# ---------------------------------------------------------------------------

_GUARD_PY = r'''
import sys, subprocess, pathlib, shutil
MODE = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else ""
SNAP = pathlib.Path(".kaiju_snap")
EXT = {".c", ".h", ".cc", ".cpp", ".cxx", ".c++", ".hpp", ".hh", ".hxx",
       ".go", ".rs", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".java"}

def balanced(src):
    # Blank comments/strings/char/template/raw-string literals to spaces, then
    # require {} () [] to balance. Handles //, /* */, "...", '...', `...`, and
    # Rust raw/byte strings r#"..."# / b"...". NOT a full parser by design.
    out = []; i = 0; n = len(src)
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n and src[i+1] == '/':
            while i < n and src[i] != '\n': i += 1
            continue
        if c == '/' and i + 1 < n and src[i+1] == '*':
            i += 2
            while i < n and not (src[i] == '*' and i + 1 < n and src[i+1] == '/'): i += 1
            i += 2; continue
        if c in 'rbR':                      # rust raw/byte string prefix
            j = i
            while j < n and src[j] in 'rbR': j += 1
            if j < n and src[j] == '#':
                k = j
                while k < n and src[k] == '#': k += 1
                if k < n and src[k] == '"':
                    close = '"' + src[j:k]
                    end = src.find(close, k + 1)
                    i = n if end < 0 else end + len(close); continue
        if c == '"' or c == "'" or c == '`':
            q = c; i += 1
            while i < n and src[i] != q:
                i += 2 if (src[i] == '\\' and i + 1 < n) else 1
            i += 1; continue
        out.append(c); i += 1
    s = ''.join(out)
    return (s.count('{') == s.count('}')
            and s.count('(') == s.count(')')
            and s.count('[') == s.count(']'))

def changed():
    try:
        o = subprocess.run(["git", "diff", "--name-only", "-z", BASE, "--", "."],
                           capture_output=True, text=True).stdout
    except Exception:
        return []
    return [f for f in o.split("\0") if f]

if MODE == "snapshot":
    try: shutil.rmtree(SNAP, ignore_errors=True); SNAP.mkdir(exist_ok=True)
    except Exception: sys.exit(0)
    man = []
    for f in changed():
        p = pathlib.Path(f)
        if p.suffix not in EXT or not p.is_file(): continue
        try: src = p.read_text(encoding="utf-8", errors="surrogateescape")
        except Exception: continue
        key = str(len(man))
        try: (SNAP / (key + ".body")).write_text(src, encoding="utf-8", errors="surrogateescape")
        except Exception: continue
        man.append(f + "\t" + ("1" if balanced(src) else "0") + "\t" + key)
    try: (SNAP / "manifest.tsv").write_text("\n".join(man), encoding="utf-8")
    except Exception: pass

elif MODE == "heal":
    mf = SNAP / "manifest.tsv"
    if mf.is_file():
        for line in mf.read_text(encoding="utf-8").splitlines():
            try: f, wasbal, key = line.split("\t")
            except ValueError: continue
            if wasbal != "1": continue          # only heal files that STARTED valid
            p = pathlib.Path(f)
            if not p.is_file(): continue
            try: cur = p.read_text(encoding="utf-8", errors="surrogateescape")
            except Exception: continue
            if not balanced(cur):
                try:
                    body = (SNAP / (key + ".body")).read_text(encoding="utf-8", errors="surrogateescape")
                    p.write_text(body, encoding="utf-8", errors="surrogateescape")
                    sys.stderr.write("HARNESS_HEALED " + f + "\n")
                    print("HARNESS_HEALED " + f)
                except Exception: pass
    shutil.rmtree(SNAP, ignore_errors=True)
'''


def guard_snapshot_lines() -> list[str]:
    """Bash lines to emit RIGHT AFTER ``git apply`` (before any anti-cheat
    rewrite): snapshot every changed source file + its balance state. No-op when
    python3 is unavailable (the guard is best-effort; python3 is present in every
    eval image and already required by the Rust in-src restore)."""
    heredoc = "cat > .kaiju_guard.py <<'KAIJU_GUARD_EOF'\n" + _GUARD_PY + "\nKAIJU_GUARD_EOF"
    return [
        "if command -v python3 >/dev/null 2>&1; then",
        heredoc,
        "  python3 .kaiju_guard.py snapshot HEAD 2>/dev/null || true",
        "fi",
    ]


def guard_heal_lines() -> list[str]:
    """Bash lines to emit JUST BEFORE the build/test step: restore any changed
    source file that a harness rewrite turned from balanced -> unbalanced, so a
    valid submission is never failed by the harness's own reconstruction. Prints
    ``HARNESS_HEALED <file>`` for each file it repairs (surfaced in the log)."""
    return [
        "if command -v python3 >/dev/null 2>&1 && [ -f .kaiju_guard.py ]; then",
        "  python3 .kaiju_guard.py heal HEAD 2>/dev/null || true",
        "  rm -f .kaiju_guard.py 2>/dev/null || true",
        "fi",
    ]


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


__all__ = ["revert_and_clean_lines", "guard_snapshot_lines", "guard_heal_lines"]
