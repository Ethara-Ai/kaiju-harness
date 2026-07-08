"""Tests for go_test_parser pass-rate scoring — fail closed on zero collection.

Also covers the forged-output (reward-hack) defense: the model's implementation
code runs during ``go test`` and can print ``--- PASS:``/``--- SKIP:`` lines that
test2json turns into terminal test events. The parser applies a fail-wins /
sticky-fail precedence rule so a forged pass cannot overwrite a real fail.
"""

from __future__ import annotations

import json

from commit0.harness.constants import TestStatus
from commit0.harness.go_test_parser import (
    compute_go_pass_rate,
    parse_go_test_json,
    parse_go_test_json_with_durations,
    parse_go_test_plain,
)


def _ev(action, package="pkg", test=None, elapsed=None):
    """Build a single test2json event line."""
    e = {"Action": action, "Package": package}
    if test is not None:
        e["Test"] = test
    if elapsed is not None:
        e["Elapsed"] = elapsed
    return json.dumps(e)


def _stream(*events):
    return "\n".join(events) + "\n"


class TestGoPassRate:
    def test_zero_when_empty_results(self):
        assert compute_go_pass_rate({}) == 0.0

    def test_zero_when_empty_expected_list(self):
        # empty expected set = zero-collection (broken run): no credit
        assert compute_go_pass_rate({}, expected_tests=[]) == 0.0

    def test_pass_rate_against_expected(self):
        results = {"pkg/TestA": TestStatus.PASSED, "pkg/TestB": TestStatus.FAILED}
        assert compute_go_pass_rate(results, expected_tests=["pkg/TestA", "pkg/TestB"]) == 0.5

    def test_missing_expected_counts_as_fail(self):
        results = {"pkg/TestA": TestStatus.PASSED}
        assert compute_go_pass_rate(results, expected_tests=["pkg/TestA", "pkg/TestB"]) == 0.5


class TestForgedOutputDefense:
    """Adversarial tests proving the sticky-fail / fail-wins vector is closed."""

    # (a) real fail THEN forged pass → stays FAILED (the primary attack).
    def test_real_fail_then_forged_pass_stays_failed(self):
        stream = _stream(
            _ev("run", test="TestFoo"),
            _ev("fail", test="TestFoo", elapsed=0.01),   # real terminal fail
            _ev("pass", test="TestFoo", elapsed=0.0),    # forged pass printed after
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestFoo"] == TestStatus.FAILED

    # (b) forged pass THEN real fail → FAILED (fail always wins, even late).
    def test_forged_pass_then_real_fail_is_failed(self):
        stream = _stream(
            _ev("run", test="TestFoo"),
            _ev("pass", test="TestFoo", elapsed=0.0),    # forged pass printed early
            _ev("fail", test="TestFoo", elapsed=0.02),   # real terminal fail
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestFoo"] == TestStatus.FAILED

    # (c) a legit single pass stays PASSED (no false positive).
    def test_legit_pass_stays_passed(self):
        stream = _stream(
            _ev("run", test="TestFoo"),
            _ev("pass", test="TestFoo", elapsed=0.05),
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestFoo"] == TestStatus.PASSED

    # (d) legit subtest: parent fails because a child failed. Parent-fail is a
    #     REAL fail and must be kept; sibling child-pass is untouched.
    def test_subtest_parent_fail_with_child_pass(self):
        stream = _stream(
            _ev("run", test="TestParent"),
            _ev("run", test="TestParent/child_ok"),
            _ev("run", test="TestParent/child_bad"),
            _ev("pass", test="TestParent/child_ok", elapsed=0.0),
            _ev("fail", test="TestParent/child_bad", elapsed=0.0),
            _ev("fail", test="TestParent", elapsed=0.01),   # real parent fail
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestParent"] == TestStatus.FAILED
        assert results["pkg/TestParent/child_ok"] == TestStatus.PASSED
        assert results["pkg/TestParent/child_bad"] == TestStatus.FAILED

    # (e-1) forged skip after a real fail must NOT override → stays FAILED.
    def test_skip_does_not_override_real_fail(self):
        stream = _stream(
            _ev("run", test="TestFoo"),
            _ev("fail", test="TestFoo", elapsed=0.01),
            _ev("skip", test="TestFoo", elapsed=0.0),    # forged skip
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestFoo"] == TestStatus.FAILED

    # (e-2) a real pass then a forged skip must stay PASSED (no downgrade).
    def test_forged_skip_does_not_downgrade_pass(self):
        stream = _stream(
            _ev("run", test="TestFoo"),
            _ev("pass", test="TestFoo", elapsed=0.03),
            _ev("skip", test="TestFoo", elapsed=0.0),    # forged skip
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestFoo"] == TestStatus.PASSED

    # (e-3) a legit skip alone is SKIPPED.
    def test_legit_skip_is_skipped(self):
        stream = _stream(
            _ev("run", test="TestFoo"),
            _ev("skip", test="TestFoo", elapsed=0.0),
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestFoo"] == TestStatus.SKIPPED

    # (f) a normal all-pass run is unchanged by the new precedence logic.
    def test_normal_all_pass_run_unchanged(self):
        stream = _stream(
            _ev("run", test="TestA"),
            _ev("pass", test="TestA", elapsed=0.01),
            _ev("run", test="TestB"),
            _ev("pass", test="TestB", elapsed=0.02),
            _ev("pass"),  # package-level pass (no Test)
        )
        results = parse_go_test_json(stream)
        assert results == {
            "pkg/TestA": TestStatus.PASSED,
            "pkg/TestB": TestStatus.PASSED,
        }

    # (g) duration parsing still works, and the forged pass's elapsed does NOT
    #     overwrite the real fail's duration.
    def test_durations_preserved_and_forged_elapsed_ignored(self):
        stream = _stream(
            _ev("run", test="TestSlow"),
            _ev("fail", test="TestSlow", elapsed=1.5),   # real fail, 1.5s
            _ev("pass", test="TestSlow", elapsed=0.0),   # forged pass, fake 0s
            _ev("fail", package="pkg", elapsed=1.6),     # package-level timing
        )
        results, durations, pkg_durations = parse_go_test_json_with_durations(stream)
        assert results["pkg/TestSlow"] == TestStatus.FAILED
        assert durations["pkg/TestSlow"] == 1.5          # real fail's duration kept
        assert pkg_durations["pkg"] == 1.6

    def test_legit_durations_recorded(self):
        stream = _stream(
            _ev("run", test="TestA"),
            _ev("pass", test="TestA", elapsed=0.25),
            _ev("pass", package="pkg", elapsed=0.30),
        )
        _, durations, pkg_durations = parse_go_test_json_with_durations(stream)
        assert durations["pkg/TestA"] == 0.25
        assert pkg_durations["pkg"] == 0.30

    # Orphaned run (crash) → ERROR, and a later forged pass can't rescue it.
    def test_crashed_test_is_error_and_forged_pass_cannot_rescue(self):
        stream = _stream(
            _ev("run", test="TestCrash"),
            _ev("fail", package="pkg", elapsed=0.5),     # package fails, test still running
            _ev("pass", test="TestCrash", elapsed=0.0),  # forged pass after crash
        )
        results = parse_go_test_json(stream)
        assert results["pkg/TestCrash"] == TestStatus.ERROR

    # The fix flows through compute_go_pass_rate: forged-pass-after-fail is not
    # counted as passed against the canonical inventory.
    def test_vector_closed_end_to_end_pass_rate(self):
        stream = _stream(
            _ev("run", test="TestForged"),
            _ev("fail", test="TestForged", elapsed=0.01),
            _ev("pass", test="TestForged", elapsed=0.0),
        )
        results = parse_go_test_json(stream)
        rate = compute_go_pass_rate(results, expected_tests=["pkg/TestForged"])
        assert rate == 0.0


class TestPlainParserForgedOutputDefense:
    """The non-JSON fallback parser gets the same sticky-fail treatment."""

    def test_plain_real_fail_then_forged_pass_stays_failed(self):
        # Go prints "--- FAIL" before the package "FAIL\t" line, so these tests
        # resolve to an unpackaged test_id ("TestFoo") — that's fine, the point
        # is that the forged PASS after the real FAIL does not override it.
        raw = "\n".join([
            "=== RUN   TestFoo",
            "--- FAIL: TestFoo (0.01s)",   # real fail
            "--- PASS: TestFoo (0.00s)",   # forged pass printed after
            "FAIL\tpkg\t0.02s",
        ])
        results = parse_go_test_plain(raw)
        assert results["TestFoo"] == TestStatus.FAILED

    def test_plain_forged_skip_does_not_downgrade_pass(self):
        raw = "\n".join([
            "ok  \tpkg\t0.02s",
            "--- PASS: TestFoo (0.01s)",
            "--- SKIP: TestFoo (0.00s)",   # forged skip
        ])
        results = parse_go_test_plain(raw)
        assert results["pkg/TestFoo"] == TestStatus.PASSED

    def test_plain_legit_pass_unchanged(self):
        raw = "\n".join([
            "--- PASS: TestFoo (0.01s)",   # printed before package line → unpackaged id
            "ok  \tpkg\t0.02s",
        ])
        results = parse_go_test_plain(raw)
        assert results["TestFoo"] == TestStatus.PASSED
