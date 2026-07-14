"""In-src Rust test restore script (H7: extracted from spec_rust._INSRC_RESTORE_PY).

Runs INSIDE the eval container. Its purpose is to reconstruct each changed
``src/**.rs`` file as:

    MODEL's impl (every ``#[test]`` / ``#[cfg(test)]`` / ``#[tokio::test]`` item
    stripped) + BASE's test items

so that ANY model edit to in-src tests (a variable rename, a reformat, or a
real weakening) is neutralized \u2014 regardless of whether tests live in a
``#[cfg(test)] mod`` block or as top-level ``#[test]`` fns, and even when
interspersed with impl.

This module is BOTH:
- Importable (for unit tests \u2014 ``commit0/harness/tests/test_insrc_restore.py``).
  Import-time side effects are ZERO; ``sys.argv`` is not read at module load.
- Runnable as a standalone script inside the container. ``spec_rust.py`` reads
  this file's source and embeds it in a bash heredoc, then invokes
  ``python3 - <base_commit> << 'EOF' ... EOF`` \u2014 the ``__main__`` block runs.

Best-effort per-file: any per-file exception is swallowed (the count-based
guard elsewhere in the eval script backstops any restore that gets skipped).
"""

from __future__ import annotations

import re
import subprocess
import sys
import pathlib
from typing import List, Tuple


_TA = re.compile(
    r'#\[\s*(?:cfg\(\s*test\s*\)|test|tokio::test|async_std::test|'
    r'cfg_attr\([^\]]*\btest\b[^\]]*\))\s*\]'
)
_RAW = re.compile(r'b?r(#*)"')
_STR = re.compile(r'b?"')
_CHR = re.compile(r"b?'(?:\\.[^']*|[^'\\])'")


def _strip_code(src: str) -> str:
    """Blank every comment/string/char literal (multi-line aware) to spaces.

    Preserves newlines 1:1 so brace counting sees ONLY real code braces. A
    per-line regex could not do this: a multi-line raw string ``r#"...{..."#``
    or block comment ``/* ...} */`` would leak its inner braces and make the
    splitter mis-classify a following ``#[test]`` as impl \u2192 the base test
    would never be restored \u2192 a model could weaken it undetected. This is
    the fix.
    """
    out: List[str] = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n and src[i+1] == '/':
            while i < n and src[i] != '\n':
                out.append(' '); i += 1
            continue
        if c == '/' and i + 1 < n and src[i+1] == '*':
            depth = 1; out.append('  '); i += 2
            while i < n and depth > 0:
                if src[i] == '/' and i + 1 < n and src[i+1] == '*':
                    depth += 1; out.append('  '); i += 2; continue
                if src[i] == '*' and i + 1 < n and src[i+1] == '/':
                    depth -= 1; out.append('  '); i += 2; continue
                out.append('\n' if src[i] == '\n' else ' '); i += 1
            continue
        m = _RAW.match(src, i)
        if m:
            close = '"' + m.group(1)
            end = src.find(close, m.end())
            end = n if end == -1 else end + len(close)
            for j in range(i, end):
                out.append('\n' if src[j] == '\n' else ' ')
            i = end; continue
        m = _STR.match(src, i)
        if m:
            start = i; i = m.end()
            while i < n and src[i] != '"':
                i += 2 if (src[i] == '\\' and i + 1 < n) else 1
            i += 1 if i < n else 0
            for j in range(start, min(i, n)):
                out.append('\n' if src[j] == '\n' else ' ')
            continue
        m = _CHR.match(src, i)
        if m:
            for _ in range(m.end() - i):
                out.append(' ')
            i = m.end(); continue
        out.append(c); i += 1
    return ''.join(out)


def _split(src: str) -> List[Tuple[str, bool]]:
    """Segment source into (text, is_test) chunks.

    Uses brace-depth on the code-blanked source (from :func:`_strip_code`) so
    multi-line strings/comments cannot spoof braces. Each chunk starts with
    its attribute cluster; ``is_test`` is True when at least one attribute in
    the cluster matches :data:`_TA`.
    """
    lines = src.split('\n'); n = len(lines); i = 0
    out: List[Tuple[str, bool]] = []
    clean = _strip_code(src).split('\n')
    if len(clean) < n:
        clean = clean + [''] * (n - len(clean))
    elif len(clean) > n:
        clean = clean[:n]
    while i < n:
        start = i; is_test = False
        while i < n and (lines[i].lstrip().startswith('#[')
                         or lines[i].lstrip().startswith('//')
                         or lines[i].lstrip().startswith('#!')):
            if _TA.search(lines[i]): is_test = True
            i += 1
        if i >= n:
            out.append(('\n'.join(lines[start:i]), is_test)); break
        depth = 0; opened = False
        while i < n:
            depth += clean[i].count('{') - clean[i].count('}')
            if depth > 0: opened = True
            prev = clean[i].rstrip(); i += 1
            if opened:
                if depth <= 0: break
            elif prev.endswith(';') or prev.endswith('}') or prev == '':
                break
        out.append(('\n'.join(lines[start:i]), is_test))
    return out


def _sh(*a: str) -> str:
    try:
        return subprocess.run(a, capture_output=True, text=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _balanced(s: str) -> bool:
    """Return True if the source has balanced braces/parens/brackets.

    Cheap, dependency-free proxy for "still parses": with comments/strings
    blanked, every bracket kind must balance. The brace-based :func:`_split`
    can mis-segment files whose in-src tests are EMITTED by ``macro_rules!``
    (e.g. byteorder's ``mod $name { ... }`` templates): the impl/test cut
    then lands inside a macro body and the reassembled file is unbalanced.
    Building on that would fail a VALID submission (compiles + all tests
    pass) with a bogus COMPILE_FAILED. Balance is exactly the property that
    breaks here \u2014 use it as a safety net so we only commit a rewrite when
    it stays balanced AND the original was balanced.
    """
    c = _strip_code(s)
    return (c.count('{') == c.count('}')
            and c.count('(') == c.count(')')
            and c.count('[') == c.count(']'))


def apply_restore(base_commit: str) -> None:
    """Walk every src/**.rs changed vs base_commit; rewrite impl + base tests.

    Best-effort: per-file failures are swallowed so a single pathological
    file cannot abort the whole restore pass (the count-based cheat guard
    elsewhere in the eval script backstops any file we skip). Only commits
    the rewrite when BOTH the model file and the rewritten result are
    balanced \u2014 losing the in-src cheat guard on one file is strictly
    better than a false COMPILE_FAILED on code that actually compiles.
    """
    try:
        changed = _sh('git', 'diff', '--name-only', base_commit, '--', 'src').split()
    except Exception:
        return
    for f in changed:
        try:
            if not f.endswith('.rs'):
                continue
            p = pathlib.Path(f)
            if not p.is_file():
                continue
            base_src = _sh('git', 'show', base_commit + ':' + f)
            if not _TA.search(base_src):
                continue
            model_src = p.read_text()
            impl = '\n'.join(t for t, x in _split(model_src) if not x)
            tests = '\n'.join(t for t, x in _split(base_src) if x)
            rewritten = impl.rstrip() + '\n\n' + tests.strip() + '\n'
            if _balanced(model_src) and _balanced(rewritten):
                p.write_text(rewritten)
        except Exception:
            pass


if __name__ == '__main__':
    apply_restore(sys.argv[1])
