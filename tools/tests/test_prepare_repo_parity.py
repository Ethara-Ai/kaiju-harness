"""Cross-language parity guards for the tools/prepare_repo_*.py preparers.

Pins invariants that drifted between the copy-then-edit sibling preparers
(QC C1 / C5 axes):
  * git-auth helpers (`git`, `push_to_fork`) must come from tools._git_auth,
    not be re-imported transitively through tools.prepare_repo (C1-005).
  * every emitted dataset entry's instance_id is `commit-0/{basename}` (C5-006).
  * the spec cache dir basename is `specs`, never `specs_<lang>` (C1-006).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]

# The 7 per-language preparers (prepare_repo.py is the canonical Python source
# and DEFINES git/push_to_fork's re-export, so it is handled separately).
LANG_PREPARERS = {
    "c": TOOLS / "prepare_repo_c.py",
    "cpp": TOOLS / "prepare_repo_cpp.py",
    "go": TOOLS / "prepare_repo_go.py",
    "java": TOOLS / "prepare_repo_java.py",
    "js": TOOLS / "prepare_repo_js.py",
    "rust": TOOLS / "prepare_repo_rust.py",
    "ts": TOOLS / "prepare_repo_ts.py",
}
ALL_PREPARERS = {**LANG_PREPARERS, "python": TOOLS / "prepare_repo.py"}

_LANG_IDS = sorted(LANG_PREPARERS)
_ALL_IDS = sorted(ALL_PREPARERS)


def _git_auth_import_symbols(src: str) -> set[str]:
    """Symbols imported from tools._git_auth, parsed robustly via AST."""
    symbols: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module == "tools._git_auth":
            symbols |= {alias.name for alias in node.names}
    return symbols


@pytest.mark.parametrize("lang", _LANG_IDS)
def test_git_helpers_come_from_git_auth(lang):
    src = LANG_PREPARERS[lang].read_text(encoding="utf-8")
    if "push_to_fork" not in src:
        pytest.skip(f"{lang} preparer does not push")
    symbols = _git_auth_import_symbols(src)
    for helper in ("git", "push_to_fork"):
        assert helper in symbols, (
            f"prepare_repo_{lang}.py must import `{helper}` directly from "
            f"tools._git_auth (found imported elsewhere/transitively). "
            f"_git_auth symbols seen: {sorted(symbols)}"
        )


@pytest.mark.parametrize("lang", _ALL_IDS)
def test_instance_id_is_commit0_namespaced(lang):
    src = ALL_PREPARERS[lang].read_text(encoding="utf-8")
    assert re.search(r'"instance_id":\s*f"commit-0/', src), (
        f"{ALL_PREPARERS[lang].name} must emit instance_id as "
        f'f"commit-0/{{basename}}" (canonical), not a language-suffixed form'
    )


@pytest.mark.parametrize("lang", _ALL_IDS)
def test_specs_dir_basename_is_specs(lang):
    src = ALL_PREPARERS[lang].read_text(encoding="utf-8")
    m = re.search(r'SPECS_DIR\s*=\s*PROJECT_ROOT\s*/\s*"([^"]+)"', src)
    if not m:
        pytest.skip(f"{lang} preparer has no PROJECT_ROOT-based SPECS_DIR")
    assert m.group(1) == "specs", (
        f"{ALL_PREPARERS[lang].name} SPECS_DIR basename is {m.group(1)!r}; must "
        f'be "specs" so copy_inference_inputs can resolve it like every sibling'
    )


@pytest.mark.parametrize("lang", _ALL_IDS)
def test_push_is_opt_out_dry_run_not_opt_in(lang):
    """QC-C1-002: push must be DEFAULT-ON (opt-out via --dry-run). An opt-in
    --push meant the dataset was UNBUILDABLE by default. C was the sole outlier;
    all 8 must expose --dry-run and NONE an opt-in --push."""
    src = ALL_PREPARERS[lang].read_text(encoding="utf-8")
    assert '"--dry-run"' in src, (
        f"{ALL_PREPARERS[lang].name} must expose an opt-out --dry-run flag"
    )
    assert '"--push"' not in src, (
        f"{ALL_PREPARERS[lang].name} still defines an opt-in --push flag; push "
        f"must be default-on so the emitted dataset is buildable without a flag"
    )
