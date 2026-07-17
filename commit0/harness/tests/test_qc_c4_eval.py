"""QC C4_eval cluster regression / parity tests.

Pins the cross-language scoring-integrity fixes so the sibling drift these
findings describe cannot silently return:

* C4-002 / C4-007 — non-measured rows (CHEAT_DETECTED / SUITE_CRASHED /
  INVENTORY_MISMATCH / infra) are EXCLUDED from the micro-average, and the
  Python ``passed=None`` sentinel never raises ``TypeError``.
* C4-001 — the Python numerator is anchored to the canonical inventory (a forged
  non-canonical pass cannot inflate it); the shared stdout-injection guard flags
  structurally-impossible "all passed but exited non-zero" runs.
* C4-004 — spec.py / spec_js.py apply the patch with a ``--recount`` fallback and
  write a PATCH_APPLY_FAILED sentinel on failure (parity with the canonical
  spec_ts.py / spec_cpp.py / spec_rust.py siblings).
* C4-005 — spec_js.py reverts/deletes model-added sitecustomize.py /
  usercustomize.py, matching the 6 sibling spec_*.py revert lists.
* C4-006 — every fixed aggregator routes through the single shared
  reward_hack.average_pass_rate helper (no per-language hand-rolled formula).

All assertions are source-text / pure-function checks so no Docker or network is
required.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from commit0.harness.reward_hack import (
    average_pass_rate,
    detect_stdout_result_injection,
)

_HARNESS = Path(__file__).resolve().parent.parent


def _src(name: str) -> str:
    return (_HARNESS / name).read_text()


# --------------------------------------------------------------------------- #
# C4-007 — Python passed=None sentinel must never raise on the average          #
# --------------------------------------------------------------------------- #
def test_none_passed_sentinel_does_not_raise():
    out = [
        {"name": "a", "passed": 0.5},
        {"name": "b", "passed": None, "status": "INVENTORY_MISMATCH"},
    ]
    mean, excluded, scored = average_pass_rate(out, {"INVENTORY_MISMATCH"})
    assert mean == pytest.approx(0.5)  # averaged over the one scored row only
    assert excluded == 1
    assert scored == 1


def test_none_passed_excluded_even_without_status():
    # Defence in depth: a None passed value is dropped regardless of status.
    mean, excluded, _ = average_pass_rate([{"passed": None}, {"passed": 1.0}])
    assert mean == pytest.approx(1.0)
    assert excluded == 1


# --------------------------------------------------------------------------- #
# C4-002 — one scored + one excluded row yields the SAME mean for every         #
# language's exclusion spelling (status set OR infra_failed predicate).         #
# --------------------------------------------------------------------------- #
def test_exclusion_parity_across_languages():
    scored_row = {"passed": 0.5, "status": "TESTS_RAN"}

    # (rows, excluded_statuses, kwargs) per language — each excluded row is the
    # language's own "not a measured model score" spelling.
    cases = {
        "cpp": ([scored_row, {"passed": 0.0, "status": "CHEAT_DETECTED"}],
                {"CHEAT_DETECTED", "COMPILE_FAILED", "OUTPUT_MISSING"}, {}),
        "ts": ([scored_row, {"passed": 0.0, "status": "SUITE_CRASHED"}],
               {"SUITE_CRASHED", "PATCH_APPLY_FAILED"}, {}),
        "python_inv": ([scored_row, {"passed": None, "status": "INVENTORY_MISMATCH"}],
                       {"INVENTORY_MISMATCH", "PYTEST_INFRA_ERROR"}, {}),
        "python_infra": ([scored_row, {"passed": 0.0, "status": "PYTEST_INFRA_ERROR"}],
                         {"INVENTORY_MISMATCH", "PYTEST_INFRA_ERROR"}, {}),
    }
    for lang, (rows, excl, kw) in cases.items():
        mean, excluded, scored = average_pass_rate(rows, excl, **kw)
        assert mean == pytest.approx(0.5), lang
        assert excluded == 1, lang
        assert scored == 1, lang

    # JS flags exclusion with the infra_failed boolean, not a status string.
    js_rows = [
        {"passed_rate": 0.5, "infra_failed": False},
        {"passed_rate": 0.0, "infra_failed": True},
    ]
    mean, excluded, scored = average_pass_rate(
        js_rows, passed_key="passed_rate",
        exclude_if=lambda r: bool(r.get("infra_failed")),
    )
    assert mean == pytest.approx(0.5)
    assert excluded == 1 and scored == 1


def test_all_excluded_yields_zero_not_crash():
    out = [{"passed": 0.0, "status": "CHEAT_DETECTED"}]
    mean, excluded, scored = average_pass_rate(out, {"CHEAT_DETECTED"})
    assert mean == 0.0 and excluded == 1 and scored == 0


def test_helper_matches_inline_scored_formula():
    # The helper must reproduce the canonical Go/Rust/C inline formula exactly
    # (scored = rows whose status not in excluded; mean of their `passed`).
    out = [
        {"passed": 1.0, "status": "TESTS_RAN"},
        {"passed": 0.0, "status": "TESTS_RAN"},
        {"passed": 0.0, "status": "COMPILE_FAILED"},
    ]
    excluded_statuses = {"COMPILE_FAILED"}
    inline_scored = [x for x in out if x.get("status") not in excluded_statuses]
    inline_mean = sum(x["passed"] for x in inline_scored) / len(inline_scored)
    mean, _, _ = average_pass_rate(out, excluded_statuses)
    assert mean == pytest.approx(inline_mean)


# --------------------------------------------------------------------------- #
# C4-002 — every fixed evaluator that appends an infra/cheat status must define  #
# _EXCLUDED_STATUSES and route the average through the shared helper.            #
# --------------------------------------------------------------------------- #
_FIXED_EVALUATORS = ["evaluate.py", "evaluate_cpp.py", "evaluate_ts.py"]


@pytest.mark.parametrize("mod", _FIXED_EVALUATORS)
def test_fixed_evaluators_route_through_helper(mod):
    src = _src(mod)
    assert "from commit0.harness.reward_hack import average_pass_rate" in src, mod
    assert "average_pass_rate(" in src, mod
    assert "_EXCLUDED_STATUSES" in src, mod
    # No hand-rolled unfiltered `sum(... for x in out) / len(out)` survives.
    assert not re.search(r"sum\([^\n]*for x in out\)\s*/\s*len\(out\)", src), mod


def test_js_routes_through_helper_with_predicate():
    src = _src("evaluate_js.py")
    assert "from commit0.harness.reward_hack import average_pass_rate" in src
    assert "average_pass_rate(" in src
    assert "infra_failed" in src
    assert not re.search(r"sum\([^\n]*passed_rate[^\n]*\)\s*/\s*len\(out_sorted\)", src)


# --------------------------------------------------------------------------- #
# C4-002 (Java) — an eval-harness CRASH must be EXCLUDED from the aggregate,      #
# not scored as a genuine 0/canonical. evaluate_java._eval_single_repo returns    #
# num_total=0 on crash so cli_java's `r[3] > 0` guard drops it.                   #
# --------------------------------------------------------------------------- #
def test_java_crash_returns_excluded_denominator():
    ej = pytest.importorskip("commit0.harness.evaluate_java")
    with patch.object(ej, "evaluate_java_repo", side_effect=RuntimeError("boom")):
        name, elapsed, num_passed, num_total = ej._eval_single_repo(
            {"repo": "acme/foo"}, "patch.diff", 60, 1
        )
    assert num_passed == 0
    assert num_total == 0, "crash must yield num_total=0 so cli_java excludes it"


# --------------------------------------------------------------------------- #
# C4-001 — Python numerator anchored to the canonical inventory + shared         #
# stdout-injection guard.                                                        #
# --------------------------------------------------------------------------- #
def test_python_numerator_is_canonical_anchored():
    src = _src("evaluate.py")
    # The forgeable form counted passes over EVERY report nodeid; the fix counts
    # only over the frozen canonical test_ids.
    assert "for tid in test_ids" in src
    assert "for v in tests.values()" not in src or "observed_passed_or_xfail = sum(\n            1 for v in tests.values()" not in src


def test_stdout_injection_guard():
    # Genuine all-pass exits 0 -> not flagged.
    assert detect_stdout_result_injection(10, 10, 0) is False
    assert detect_stdout_result_injection(10, 10, None) is False
    # Claims all/over passed but exited non-zero -> forged.
    assert detect_stdout_result_injection(10, 10, 1) is True
    assert detect_stdout_result_injection(11, 10, 1) is True
    # Fewer passes than tests with a non-zero exit is a legit failing run.
    assert detect_stdout_result_injection(3, 10, 1) is False
    # Nothing to forge.
    assert detect_stdout_result_injection(0, 0, 1) is False


# --------------------------------------------------------------------------- #
# C4-004 — patch-apply --recount fallback + PATCH_APPLY_FAILED sentinel parity.  #
# --------------------------------------------------------------------------- #
# Files owned/fixed by this cluster plus the already-canonical siblings. Go/C
# (spec_go.py / spec_c.py) already WRITE the sentinel but lack --recount; they
# are OUT of this cluster's file ownership, so their --recount gap is de-scoped
# and deliberately NOT asserted here (documented, not silently dropped).
_SPEC_RECOUNT_FILES = [
    "spec.py", "spec_js.py",            # fixed in this cluster
    "spec_cpp.py", "spec_ts.py", "spec_java.py", "spec_rust.py",  # canonical
]
# spec_rust.py signals patch-apply failure via exit code + --3way, not the
# literal PATCH_APPLY_FAILED string, so it is excluded from the sentinel check.
_SPEC_SENTINEL_FILES = [
    "spec.py", "spec_js.py", "spec_cpp.py", "spec_ts.py", "spec_java.py",
]


@pytest.mark.parametrize("spec", _SPEC_RECOUNT_FILES)
def test_spec_has_recount_fallback(spec):
    assert "--recount" in _src(spec), f"{spec} missing git apply --recount fallback"


@pytest.mark.parametrize("spec", _SPEC_SENTINEL_FILES)
def test_spec_writes_patch_apply_failed_sentinel(spec):
    assert "PATCH_APPLY_FAILED" in _src(spec), f"{spec} missing PATCH_APPLY_FAILED sentinel"


def test_python_spec_sentinel_lands_where_evaluate_reads_it():
    # evaluate.py reads the sentinel from test_output.txt; spec.py must write it
    # there (both make_eval_script_list overrides).
    spec_src = _src("spec.py")
    assert spec_src.count("echo PATCH_APPLY_FAILED > test_output.txt") >= 2
    assert "from commit0.harness._eval_common import" in _src("evaluate.py")


def test_js_spec_sentinel_lands_where_evaluate_reads_it():
    # evaluate_js.py scans test_stdout.txt for the sentinel; spec_js.py writes it
    # there.
    assert "echo PATCH_APPLY_FAILED > test_stdout.txt" in _src("spec_js.py")
    assert "test_stdout.txt" in _src("evaluate_js.py")


# --------------------------------------------------------------------------- #
# C4-005 — spec_js.py reverts sitecustomize.py / usercustomize.py (sibling       #
# parity).                                                                       #
# --------------------------------------------------------------------------- #
def test_js_reverts_python_import_hooks_like_siblings():
    js = _src("spec_js.py")
    assert "sitecustomize.py" in js
    assert "usercustomize.py" in js
    # Parity: every python-invoking sibling lists them too.
    for sib in ("spec.py", "spec_c.py", "spec_cpp.py", "spec_go.py",
                "spec_java.py", "spec_ts.py"):
        s = _src(sib)
        assert "sitecustomize.py" in s and "usercustomize.py" in s, sib
