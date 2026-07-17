"""QC-C6-005: V5 base-validation negative-sentinel parity.

The degenerate-base sentinel (`-len(test_ids)` when the stubbed base can no
longer enumerate its own tests) previously existed ONLY in Java/JS/TS, with C
and Python merely logging and cpp/go/rust lacking the check entirely. This
pins the single-sourced contract + its adoption so the semantics cannot drift.
"""
import ast
from pathlib import Path

import pytest

from tools._test_id_sentinel import is_degenerate, result_count

TOOLS = Path(__file__).resolve().parents[1]

# Languages that validate the base at TEST-ID-GENERATION time and therefore must
# route through the shared sentinel helper (no inline `-len` re-implementation).
_SENTINEL_LANGS = {
    "generate_test_ids.py": "python",
    "generate_test_ids_c.py": "c",
    "generate_test_ids_cpp.py": "cpp",
    "generate_test_ids_java.py": "java",
    "generate_test_ids_js.py": "js",
    "generate_test_ids_ts.py": "ts",
}
# go/rust validate base HEALTH at prepare time (go build ./... / cargo check
# --tests) — an equivalent guarantee — so they do NOT re-run it here.
_PREPARE_TIME_BASE_LANGS = {"generate_test_ids_go.py", "generate_test_ids_rust.py"}


def test_result_count_contract():
    assert result_count(["a", "b", "c"], None) == 3   # not validated -> positive
    assert result_count(["a", "b", "c"], 7) == 3       # base healthy -> positive
    assert result_count(["a", "b", "c"], 0) == -3      # degenerate -> sentinel
    assert result_count(60, 0) == -60                  # accepts an int count
    assert result_count([], 0) == 0                    # empty stays 0
    assert is_degenerate(-1) and not is_degenerate(0) and not is_degenerate(5)


@pytest.mark.parametrize("fname", sorted(_SENTINEL_LANGS))
def test_validating_langs_use_shared_sentinel(fname):
    """Each test-id-time validator imports the shared helper and does NOT carry
    an inline `-len(test_ids)` re-implementation (which is how they drifted)."""
    src = (TOOLS / fname).read_text(encoding="utf-8")
    assert "_test_id_sentinel" in src and "result_count" in src, (
        f"{fname} must import the shared result_count sentinel helper"
    )
    assert "-len(test_ids)" not in src, (
        f"{fname} still inlines `-len(test_ids)`; use the shared result_count() "
        f"so the degenerate-base contract is single-sourced"
    )


@pytest.mark.parametrize("fname", sorted(_PREPARE_TIME_BASE_LANGS))
def test_prepare_time_base_langs_documented(fname):
    """go/rust intentionally validate base health at PREPARE time; assert the
    generator still exists (so this decision is a conscious, findable one)."""
    assert (TOOLS / fname).exists()


def test_shared_helper_has_no_inline_duplication_left():
    """No generate_test_ids_* should define its own private sentinel constant."""
    for f in TOOLS.glob("generate_test_ids*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {
                "result_count",
                "_result_count",
            }:
                pytest.fail(f"{f.name} redefines result_count locally — use the "
                            f"shared tools._test_id_sentinel instead")
