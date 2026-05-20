"""Tests for c_test_parser — JUnit XML well-formed + truncated."""

from __future__ import annotations

import pytest

from commit0.harness.constants import TestStatus
from commit0.harness.c_test_parser import (
    compute_c_pass_rate,
    failed_test_names,
    parse_ctest_junit,
    parse_ctest_junit_with_summary,
    summarize_ctest_results,
)


WELL_FORMED = """<?xml version="1.0"?>
<testsuites>
  <testsuite name="cJSON_tests" tests="4">
    <testcase name="parse_object"/>
    <testcase name="parse_array"><failure message="AssertionError"/></testcase>
    <testcase name="parse_null"><skipped/></testcase>
    <testcase name="segfault_one"><error message="sigsegv"/></testcase>
  </testsuite>
</testsuites>
"""

TRUNCATED = """<?xml version="1.0"?>
<testsuites>
  <testsuite>
    <testcase name="ok_one"/>
    <testcase name="ok_two"/>
    <testcase name="fails_one"><failure/></testcase>
    <testcase name="truncated"
"""

NO_SUITES_FLAT = """<?xml version="1.0"?>
<testsuite name="flat">
  <testcase name="a"/>
  <testcase name="b"><failure/></testcase>
</testsuite>
"""


class TestWellFormed:
    def test_all_statuses(self):
        results = parse_ctest_junit(WELL_FORMED)
        assert results["parse_object"] == TestStatus.PASSED
        assert results["parse_array"] == TestStatus.FAILED
        assert results["parse_null"] == TestStatus.SKIPPED
        assert results["segfault_one"] == TestStatus.ERROR

    def test_summary(self):
        results, summary = parse_ctest_junit_with_summary(WELL_FORMED)
        assert summary == {
            "passed": 1,
            "failed": 1,
            "skipped": 1,
            "errored": 1,
            "total": 4,
        }

    def test_flat_testsuite(self):
        results = parse_ctest_junit(NO_SUITES_FLAT)
        assert results["a"] == TestStatus.PASSED
        assert results["b"] == TestStatus.FAILED


class TestTruncatedXmlFallback:
    """CR-12: CTest sometimes emits unclosed XML on segfault/timeout."""

    def test_regex_fallback_recovers_complete_testcases(self):
        results = parse_ctest_junit(TRUNCATED)
        assert results["ok_one"] == TestStatus.PASSED
        assert results["ok_two"] == TestStatus.PASSED
        assert results["fails_one"] == TestStatus.FAILED
        # truncated_test has no closing /> or </testcase> — should be dropped
        assert "truncated" not in results

    def test_empty_input(self):
        assert parse_ctest_junit("") == {}

    def test_whitespace_only(self):
        assert parse_ctest_junit("   \n  ") == {}

    def test_unparseable_returns_dict(self):
        # garbage in -> empty dict, not exception
        results = parse_ctest_junit("not<xml>at all")
        assert isinstance(results, dict)


class TestFailedTestNames:
    def test_only_failed_and_errored(self):
        results = parse_ctest_junit(WELL_FORMED)
        fails = failed_test_names(results)
        assert set(fails) == {"parse_array", "segfault_one"}

    def test_empty_when_all_pass(self):
        results = {
            "a": TestStatus.PASSED,
            "b": TestStatus.PASSED,
        }
        assert failed_test_names(results) == []


class TestPassRate:
    def test_zero_when_empty(self):
        assert compute_c_pass_rate({}) == 0.0

    def test_one_when_empty_expected_list(self):
        assert compute_c_pass_rate({}, expected_tests=[]) == 1.0

    def test_pass_rate_against_expected(self):
        results = {
            "a": TestStatus.PASSED,
            "b": TestStatus.FAILED,
            "c": TestStatus.PASSED,
        }
        # only 'a' and 'b' expected; 1/2 = 0.5
        assert compute_c_pass_rate(results, expected_tests=["a", "b"]) == 0.5

    def test_missing_expected_counts_as_fail(self):
        results = {"a": TestStatus.PASSED}
        # 'b' expected but missing -> 1/2 = 0.5
        assert compute_c_pass_rate(results, expected_tests=["a", "b"]) == 0.5

    def test_default_pass_rate(self):
        results = {
            "a": TestStatus.PASSED,
            "b": TestStatus.PASSED,
            "c": TestStatus.FAILED,
        }
        assert compute_c_pass_rate(results) == pytest.approx(2 / 3)


class TestSummarize:
    def test_counts_all_states(self):
        results = {
            "p": TestStatus.PASSED,
            "f": TestStatus.FAILED,
            "s": TestStatus.SKIPPED,
            "e": TestStatus.ERROR,
        }
        s = summarize_ctest_results(results)
        assert s == {
            "passed": 1,
            "failed": 1,
            "skipped": 1,
            "errored": 1,
            "total": 4,
        }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
