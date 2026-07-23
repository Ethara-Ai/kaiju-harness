"""Real generated PYTEST — actual executable test functions authored from TRUTH.md
that import and exercise the solution's BEHAVIOR (not regex on the diff text).

This is the "generate pytest from TRUTH.md" the request asked for. The generated
suite tests behavior BEYOND the frozen tests, so it doubles as the held-out /
oracle-strength signal: it must PASS on a correct implementation and FAIL on the
unmodified stub. Execution is in `pytest_runner` (needs the repo runtime).
"""
from __future__ import annotations

import ast
import re

from .model_client import ModelClient


def split_test_functions(code: str) -> tuple[str, dict[str, str]]:
    """(header, {test_name: source}) — separate the module preamble (imports,
    fixtures, helpers) from each top-level `test_*` function, so unsound tests can
    be pruned individually. Falls back to (code, {}) on a parse error."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, {}
    lines = code.splitlines(keepends=True)
    funcs: dict[str, str] = {}
    covered: set[int] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            start = node.lineno - 1
            if node.decorator_list:
                start = min(d.lineno - 1 for d in node.decorator_list)
            end = node.end_lineno
            funcs[node.name] = "".join(lines[start:end])
            covered.update(range(start, end))
    header = "".join(l for i, l in enumerate(lines) if i not in covered)
    return header.rstrip() + "\n", funcs


def assemble_tests(header: str, func_sources: list[str]) -> str:
    return header.rstrip() + "\n\n\n" + "\n\n".join(s.rstrip() for s in func_sources) + "\n"


def prune_tests(code: str, keep: set[str]) -> str:
    """Return the module with only the `keep` test functions (drop the rest)."""
    header, funcs = split_test_functions(code)
    kept = [src for name, src in funcs.items() if name in keep]
    return assemble_tests(header, kept) if kept else header

_SYSTEM = (
    "You write a REAL pytest test module that verifies the BEHAVIOR described in a "
    "TRUTH.md answer key, by importing the actual solution module and asserting on real "
    "outputs. Requirements:\n"
    "- Real functional tests: import the module, call its public API, assert concrete "
    "expected behavior drawn from TRUTH.md's behavioral contract + pitfalls.\n"
    "- Test behavior the frozen tests might miss (edge cases, the contract's invariants) — "
    "so a weak/overfit solution fails.\n"
    "- Each test must PASS on a correct implementation and FAIL on a broken/stub one.\n"
    "- Use ONLY the standard library + pytest + the solution's own imports. No network.\n"
    "- Do NOT hardcode to a specific wrong output; assert the CONTRACT.\n"
    "- If unsure of an exact value, assert a robust property (type, invariant, idempotence) "
    "rather than a brittle literal.\n"
    "Return ONLY the python test module in a single ```python code block."
)


def build_pytest_prompt(truth_md: str, *, import_hint: str = "",
                        stub_files: list[str] | None = None) -> tuple[str, str]:
    hint = ""
    if import_hint:
        hint += f"\nImport the solution as: {import_hint}"
    if stub_files:
        hint += f"\nThe implemented files are: {', '.join(stub_files)}"
    user = (f"# TRUTH.md\n\n{truth_md}\n{hint}\n\n"
            "Write the pytest module now (```python ... ```).")
    return _SYSTEM, user


def extract_code(text: str) -> str:
    """Pull the python source from a ```python fenced block (or the whole text)."""
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    code = m.group(1) if m else text
    return code.strip() + "\n"


def infer_import_hint(stub_files: list[str]) -> str:
    """Best-effort module import path from a stubbed file (slugify/slugify.py -> slugify)."""
    for f in stub_files or []:
        if f.endswith(".py"):
            parts = f[:-3].split("/")
            # drop a leading src dir if it duplicates the package name
            mod = ".".join(p for p in parts if p not in ("src",))
            return f"from {parts[-1]} import *   # module: {mod}"
    return ""


def generate_pytest(truth_md: str, client: ModelClient, *,
                    stub_files: list[str] | None = None, feedback: str = "",
                    max_tokens: int = 6144) -> str:
    system, user = build_pytest_prompt(
        truth_md, import_hint=infer_import_hint(stub_files or []), stub_files=stub_files)
    if feedback:
        user += f"\n\n## Your previous suite was unsound — FIX it:\n{feedback}"
    return extract_code(client.complete(system, user, max_tokens=max_tokens).text)
