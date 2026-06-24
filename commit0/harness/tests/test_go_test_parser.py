"""Tests for go_test_parser pass-rate scoring — fail closed on zero collection."""

from __future__ import annotations

from commit0.harness.constants import TestStatus
from commit0.harness.go_test_parser import compute_go_pass_rate


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
