"""Regression tests for evaluate_go scoring/classification hardening.

Covers the paths audited against the hardened Rust reference:
  - denominator inflation via observed count (fallback path)
  - timeout partial-score is NOT trusted
  - compile / patch-apply / empty-suite / missing-output classification
  - num_passed can never exceed num_tests (pass_rate <= 1.0)
"""

from __future__ import annotations

import json

from commit0.harness.evaluate_go import (
    _aggregate_go_results,
    OUTCOME_TESTS_RAN,
    OUTCOME_COMPILE_FAILED,
    OUTCOME_PATCH_APPLY_FAILED,
    OUTCOME_TEST_SUITE_TIMEOUT,
    OUTCOME_NO_TESTS_DEFINED,
    OUTCOME_OUTPUT_MISSING,
    OUTCOME_CRASH,
)


def _write_run(tmp_path, events, exit_code=0, stderr=None):
    """Write a fake go test -json log dir; return its path."""
    d = tmp_path / "log"
    d.mkdir()
    lines = "\n".join(json.dumps(e) for e in events)
    (d / "test_output.json").write_text(lines)
    if exit_code is not None:
        (d / "go_test_exit_code.txt").write_text(str(exit_code))
    if stderr is not None:
        (d / "test_stderr.txt").write_text(stderr)
    return str(d)


def _pass(pkg, test):
    return [
        {"Action": "run", "Package": pkg, "Test": test},
        {"Action": "pass", "Package": pkg, "Test": test, "Elapsed": 0.01},
    ]


def _fail(pkg, test):
    return [
        {"Action": "run", "Package": pkg, "Test": test},
        {"Action": "fail", "Package": pkg, "Test": test, "Elapsed": 0.01},
    ]


class TestDenominator:
    def test_canonical_inventory_is_denominator(self, tmp_path):
        # 1 of 2 canonical tests passes; a THIRD non-canonical test also passes.
        events = _pass("pkg", "TestA") + _fail("pkg", "TestB") + _pass("pkg", "TestExtra")
        log = _write_run(tmp_path, events, exit_code=1)
        out: list = []
        _aggregate_go_results(log, "repo", ["pkg/TestA", "pkg/TestB"], out)
        r = out[0]
        assert r["num_tests"] == 2  # denominator = canonical inventory, NOT 3
        assert r["num_passed"] == 1  # extra passing test does NOT inflate
        assert r["passed"] == 0.5
        assert r["status"] == OUTCOME_TESTS_RAN

    def test_no_inflation_in_fallback(self, tmp_path):
        # No canonical inventory: fallback uses observed TOTAL (len results),
        # and num_passed is capped to it so pass_rate can never exceed 1.0.
        events = _pass("pkg", "TestA") + _pass("pkg", "TestB")
        log = _write_run(tmp_path, events, exit_code=0)
        out: list = []
        _aggregate_go_results(log, "repo", [], out)
        r = out[0]
        assert r["num_passed"] <= r["num_tests"]
        assert r["passed"] <= 1.0

    def test_pass_rate_never_exceeds_one(self, tmp_path):
        events = _pass("pkg", "TestA")
        log = _write_run(tmp_path, events, exit_code=0)
        out: list = []
        _aggregate_go_results(log, "repo", ["pkg/TestA"], out)
        assert out[0]["passed"] == 1.0
        assert out[0]["num_passed"] == out[0]["num_tests"] == 1


class TestClassification:
    def test_timeout_partial_not_trusted(self, tmp_path):
        # Suite killed by timeout (exit 124) after 1/2 — must NOT score 0.5.
        events = _pass("pkg", "TestA")  # TestB never ran (killed)
        log = _write_run(tmp_path, events, exit_code=124)
        out: list = []
        _aggregate_go_results(log, "repo", ["pkg/TestA", "pkg/TestB"], out)
        r = out[0]
        assert r["status"] == OUTCOME_TEST_SUITE_TIMEOUT
        assert r["passed"] == 0.0  # partial pass count NOT trusted

    def test_compile_failure_flagged(self, tmp_path):
        # go build failed: package FAIL, no per-test events, non-zero exit.
        events = [{"Action": "fail", "Package": "pkg", "Elapsed": 0.0}]
        log = _write_run(tmp_path, events, exit_code=2)
        out: list = []
        _aggregate_go_results(log, "repo", ["pkg/TestA", "pkg/TestB"], out)
        r = out[0]
        assert r["status"] == OUTCOME_COMPILE_FAILED
        assert r["num_passed"] == 0
        # compile failure is a MODEL failure → scored 0 (still included).
        assert r["passed"] == 0.0

    def test_patch_apply_failed_flagged(self, tmp_path):
        events = [{"Action": "fail", "Package": "PATCH_APPLY_FAILED",
                   "Output": "git apply failed"}]
        log = _write_run(tmp_path, events, exit_code=1)
        out: list = []
        _aggregate_go_results(log, "repo", ["pkg/TestA"], out)
        assert out[0]["status"] == OUTCOME_PATCH_APPLY_FAILED
        assert out[0]["passed"] == 0.0

    def test_empty_suite_flagged(self, tmp_path):
        # go test ran cleanly (exit 0) but produced no test events.
        log = _write_run(tmp_path, [], exit_code=0)
        out: list = []
        _aggregate_go_results(log, "repo", ["pkg/TestA"], out)
        assert out[0]["status"] == OUTCOME_NO_TESTS_DEFINED

    def test_missing_output_is_infra(self, tmp_path):
        d = tmp_path / "log"
        d.mkdir()  # no test_output.json at all
        out: list = []
        _aggregate_go_results(str(d), "repo", ["pkg/TestA"], out)
        assert out[0]["status"] == OUTCOME_OUTPUT_MISSING
        assert out[0]["passed"] == 0.0

    def test_crash_when_stderr_present(self, tmp_path):
        d = tmp_path / "log"
        d.mkdir()
        (d / "test_stderr.txt").write_text("panic: runtime error")
        out: list = []
        _aggregate_go_results(str(d), "repo", ["pkg/TestA"], out)
        assert out[0]["status"] == OUTCOME_CRASH


class TestGoSpecificParsing:
    def test_subtests_and_examples_and_skips(self, tmp_path):
        events = (
            _pass("pkg", "TestParent")
            + _pass("pkg", "TestParent/child_a")
            + _fail("pkg", "TestParent/child_b")
            + _pass("pkg", "ExampleFoo")
            + [
                {"Action": "run", "Package": "pkg", "Test": "TestSkipped"},
                {"Action": "skip", "Package": "pkg", "Test": "TestSkipped", "Elapsed": 0.0},
            ]
        )
        log = _write_run(tmp_path, events, exit_code=1)
        canonical = [
            "pkg/TestParent",
            "pkg/TestParent/child_a",
            "pkg/TestParent/child_b",
            "pkg/ExampleFoo",
            "pkg/TestSkipped",
        ]
        out: list = []
        _aggregate_go_results(log, "repo", canonical, out)
        r = out[0]
        assert r["num_tests"] == 5
        # PASSED counts; SKIPPED is NOT counted as passed (matches rust, where
        # num_passed = summary.passed and skipped is tracked separately). Parent,
        # child_a and the Example pass (3); child_b fails; TestSkipped skipped.
        assert r["num_passed"] == 3
        assert r["status"] == OUTCOME_TESTS_RAN
