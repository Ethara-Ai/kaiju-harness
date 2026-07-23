"""Differential oracle tests (EvalPlus/ALGO style): the LLM generates INPUT calls
(no expected values); the GOLDEN solution is executed to produce the expected
output; the test asserts the candidate reproduces it. Such a test CANNOT carry a
wrong oracle — the golden defines truth — so it passes the golden by construction
and fails any solution that diverges. It only needs a decent input distribution.
"""
from __future__ import annotations

import json
import re
import subprocess

from .model_client import ModelClient

_SYSTEM = (
    "Generate diverse INPUT calls (NOT expected outputs) that exercise a solution's public "
    "API + edge cases, for differential testing against a reference implementation. Each call "
    "is a Python expression string that calls the API. Cover normal, boundary, unicode, empty, "
    "and option-combination cases.\n"
    'Return ONLY JSON: {"import": "from <module> import <names>", "calls": ["fn(args)", ...]}.'
)


def generate_input_calls(truth_md: str, import_hint: str, client: ModelClient, *,
                         n: int = 14, max_tokens: int = 3072) -> tuple[str, list[str]]:
    user = (f"# TRUTH.md\n{truth_md}\n\nSuggested import: {import_hint}\n"
            f"Produce ~{n} input calls. Return the JSON.")
    try:
        text = client.complete(_SYSTEM, user, max_tokens=max_tokens).text
        d = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
        calls = [c for c in (d.get("calls") or []) if isinstance(c, str) and c.strip()]
        return str(d.get("import") or import_hint), calls[:40]
    except Exception:
        return import_hint, []


# Driver run inside the golden worktree: eval each call, print repr or RAISES:<Exc>.
_DRIVER = '''
import json, sys
{import_line}
_calls = json.loads(sys.argv[1])
_out = []
for c in _calls:
    try:
        _out.append({{"call": c, "expected": repr(eval(c))}})
    except Exception as e:
        _out.append({{"call": c, "expected": "RAISES:" + type(e).__name__}})
print(json.dumps(_out))
'''


def compute_golden_expected(repo_dir, ref_commit: str, src_dir: str, import_line: str,
                            calls: list[str], *, timeout: int = 120) -> list[dict]:
    """Run each call through the GOLDEN to get its expected output. []-safe."""
    from .pytest_runner import checkout_solution, remove_worktree, _env_with_pythonpath
    if not calls:
        return []
    wt = checkout_solution(repo_dir, ref_commit)
    if wt is None:
        return []
    try:
        (wt / "_kaiju_diff_driver.py").write_text(_DRIVER.format(import_line=import_line))
        r = subprocess.run(["python", "_kaiju_diff_driver.py", json.dumps(calls)],
                           cwd=str(wt), capture_output=True, text=True, timeout=timeout,
                           env=_env_with_pythonpath(wt))
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            return []
    finally:
        remove_worktree(repo_dir, wt)


def assemble_differential_test(import_line: str, cases: list[dict]) -> str:
    """A pytest module asserting each call reproduces the golden's output/exception."""
    lines = [import_line, "import pytest", "", ""]
    for i, c in enumerate(cases):
        call, exp = c.get("call", ""), c.get("expected", "")
        lines.append(f"def test_differential_{i}():")
        if isinstance(exp, str) and exp.startswith("RAISES:"):
            exc = exp.split(":", 1)[1]
            lines.append(f"    with pytest.raises(Exception):")
            lines.append(f"        {call}")
        else:
            lines.append(f"    assert repr({call}) == {exp!r}")
        lines.append("")
    return "\n".join(lines) + "\n"


def generate_differential_tests(truth_md: str, import_hint: str, repo_dir, ref_commit: str,
                                src_dir: str, client: ModelClient) -> str:
    """End-to-end: generate inputs → run golden → assemble a differential test module."""
    import_line, calls = generate_input_calls(truth_md, import_hint, client)
    cases = compute_golden_expected(repo_dir, ref_commit, src_dir, import_line, calls)
    return assemble_differential_test(import_line, cases) if cases else ""
