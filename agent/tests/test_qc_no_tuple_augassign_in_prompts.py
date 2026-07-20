"""Guard against the `str += (tuple)` bug that crashed EVERY C worker.

Root cause (RCA): agent/agents_c.py built the inject_test_files_readonly=False
system-prompt suffix as a parenthesized block, but several lines ended with a
trailing comma — so the RHS parsed as a 5-element TUPLE, not one concatenated
string. `coder.gpt_prompts.main_system += (tuple)` then raised

    TypeError: can only concatenate str (not "tuple") to str

before any model call, so aider produced zero exchange and the module went to
`.needs_retry` limbo (rc=1 on every auto-resume).

Why it surfaced only now: the QC inject-default flip (True -> False) made the
`else` branch the DEFAULT path for every language. Those else-branches had never
executed before, so the latent tuple bug was invisible. This static guard walks
every agent runner and fails if ANY `x += (...)` right-hand side is a tuple — the
cheap, language-agnostic way to keep an unexercised prompt branch from shipping
a crash. It fires on the exact AST shape regardless of which branch runs at
runtime, so no per-language inject fixture is needed.
"""
import ast
from pathlib import Path

import pytest

AGENT_DIR = Path(__file__).resolve().parents[1]
AGENT_FILES = sorted(AGENT_DIR.glob("agents*.py"))


def _tuple_augassigns(path: Path):
    """Return [(lineno, n_elements)] for every `X += (a, b, ...)` in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.op, ast.Add)
            and isinstance(node.value, ast.Tuple)
        ):
            hits.append((node.lineno, len(node.value.elts)))
    return hits


def test_agent_files_present():
    assert AGENT_FILES, f"no agents*.py found under {AGENT_DIR} (glob drift?)"


@pytest.mark.parametrize("path", AGENT_FILES, ids=lambda p: p.name)
def test_no_tuple_rhs_augassign(path: Path):
    hits = _tuple_augassigns(path)
    assert not hits, (
        f"{path.name}: `+= (tuple)` at line(s) {hits} — a trailing comma turned a "
        f"parenthesized string-concat into a tuple. `str += tuple` raises TypeError "
        f"at runtime (crashed every worker once inject_test_files_readonly=False "
        f"made this branch the default). Remove the stray comma(s)."
    )
