"""Hostile/boundary audit for commit0.harness.evaluate_ts.parse_jest_vitest_report
and STATUS_MAP behaviour.

Covers malformed Jest/Vitest JSON shapes that real test runners have produced:
truncated assertion objects, wrong types, None values, float-as-string
durations, unexpected but real statuses, very large counts, and very long
fullNames.
"""

from __future__ import annotations

import pytest

from commit0.harness.evaluate_ts import STATUS_MAP, parse_jest_vitest_report


# ---------------------------------------------------------------------------
# STATUS_MAP: case sensitivity and lookup safety
# ---------------------------------------------------------------------------


class TestStatusMapShape:
    def test_is_flat_dict(self) -> None:
        assert isinstance(STATUS_MAP, dict)

    def test_all_keys_lowercase(self) -> None:
        for k in STATUS_MAP:
            assert k == k.lower(), k

    def test_all_values_are_known_normalized_statuses(self) -> None:
        assert set(STATUS_MAP.values()) <= {"passed", "failed", "skipped"}

    def test_case_variants_not_auto_mapped(self) -> None:
        """Mixed-case statuses from an exotic Jest fork fall through to 'failed'.

        This pins the fact that matching is case-sensitive; any upstream
        change to lower-case the key before lookup would flip this test.
        The assertion's ``fullName`` must be a canonical test_id, otherwise the
        parser (canonical-only) never inspects its status at all.
        """
        report = {
            "testResults": [
                {"assertionResults": [{"status": "PASSED", "fullName": "x"}]}
            ]
        }
        counter, _ = parse_jest_vitest_report(report, ["x"])
        assert counter["failed"] == 1
        assert "passed" not in counter


# ---------------------------------------------------------------------------
# Malformed report: parse_jest_vitest_report must tolerate missing / wrong
# types without crashing. Any KeyError / TypeError in this function aborts
# an entire evaluate run (one repo).
# ---------------------------------------------------------------------------


class TestMalformedReport:
    @pytest.mark.parametrize(
        "report",
        [
            {},
            {"testResults": []},
            {"testResults": [{}]},
            {"testResults": [{"assertionResults": []}]},
        ],
    )
    def test_empty_shapes_yield_empty_counter(self, report: dict) -> None:
        counter, duration = parse_jest_vitest_report(report, [])
        assert sum(counter.values()) == 0
        assert duration == 0.0

    def test_missing_status_field_maps_to_failed(self) -> None:
        report = {
            "testResults": [{"assertionResults": [{"fullName": "a", "duration": 1}]}]
        }
        counter, _ = parse_jest_vitest_report(report, ["a"])
        assert counter["failed"] == 1

    def test_none_fullname_does_not_crash_and_is_not_matched(self) -> None:
        """fullName=None is legal JSON but must never match a canonical test_id.

        The ``if not full_name`` guard filters None out before the membership
        test, so the assertion is ignored. The (unmatched) canonical test_id is
        then scored ``failed`` — a None-named assertion cannot satisfy it.
        """
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": None, "duration": 1}
                    ]
                }
            ]
        }
        counter, _ = parse_jest_vitest_report(report, ["file.ts > test_name"])
        # None fullName is skipped -> nothing passes; the canonical id is missing.
        assert counter.get("passed", 0) == 0
        assert counter["failed"] == 1

    def test_numeric_status_falls_through_to_failed(self) -> None:
        """A numeric status (protocol misuse) must not raise."""
        report = {
            "testResults": [
                {"assertionResults": [{"status": 1, "fullName": "a", "duration": 0}]}
            ]
        }
        counter, _ = parse_jest_vitest_report(report, ["a"])
        # STATUS_MAP.get(1, "failed") returns "failed"
        assert counter["failed"] == 1

    @pytest.mark.parametrize(
        "duration_value, expected_seconds",
        [
            (0, 0.0),
            (1, 0.001),
            (1000, 1.0),
            (1500.5, 1.5005),
            # Edge: very large duration (10 minutes in ms)
            (600_000, 600.0),
        ],
    )
    def test_duration_conversion_ms_to_s(
        self, duration_value: float, expected_seconds: float
    ) -> None:
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": "x",
                            "duration": duration_value,
                        }
                    ]
                }
            ]
        }
        # Only durations of assertions that match a canonical test_id are summed.
        _, duration = parse_jest_vitest_report(report, ["x"])
        assert duration == pytest.approx(expected_seconds)

    def test_duration_accepts_int_as_float(self) -> None:
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "x", "duration": 42}
                    ]
                }
            ]
        }
        _, duration = parse_jest_vitest_report(report, ["x"])
        assert duration == 0.042

    def test_missing_duration_defaults_zero(self) -> None:
        report = {
            "testResults": [
                {"assertionResults": [{"status": "passed", "fullName": "x"}]}
            ]
        }
        _, duration = parse_jest_vitest_report(report, [])
        assert duration == 0.0

    def test_very_long_fullname_handled(self) -> None:
        long_name = "x" * 10_000
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": long_name, "duration": 1}
                    ]
                }
            ]
        }
        counter, _ = parse_jest_vitest_report(report, [long_name])
        # Matches by fullName exactly
        assert counter["passed"] == 1
        assert counter.get("failed", 0) == 0

    def test_unicode_fullname_matches_exactly(self) -> None:
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": "描述 > テスト 🧪",
                            "duration": 1,
                        }
                    ]
                }
            ]
        }
        counter, _ = parse_jest_vitest_report(
            report, ["src/x.test.ts > 描述 > テスト 🧪"]
        )
        assert counter["passed"] == 1
        assert counter.get("failed", 0) == 0

    def test_multiple_testresults_accumulate(self) -> None:
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "a", "duration": 10}
                    ]
                },
                {
                    "assertionResults": [
                        {"status": "failed", "fullName": "b", "duration": 20}
                    ]
                },
                {
                    "assertionResults": [
                        {"status": "skipped", "fullName": "c", "duration": 0}
                    ]
                },
            ]
        }
        counter, duration = parse_jest_vitest_report(report, ["a", "b", "c"])
        assert counter["passed"] == 1
        assert counter["failed"] == 1
        assert counter["skipped"] == 1
        assert duration == pytest.approx(0.030)


# ---------------------------------------------------------------------------
# test_id matching semantics
# ---------------------------------------------------------------------------


class TestTestIdMatching:
    @pytest.mark.parametrize(
        "test_id, fullname_in_report, expect_match",
        [
            # Exact match on bare_name
            ("src/x.test.ts > my test", "my test", True),
            # Exact match on full tid
            ("src/x.test.ts > my test", "src/x.test.ts > my test", True),
            # No match → the canonical id is scored failed, the assertion ignored
            ("src/x.test.ts > missing", "other test", False),
            # Whitespace difference breaks match
            ("src/x.test.ts > my test", "my  test", False),
            # Different separator (uses first ' > ')
            ("a > b > c", "b > c", True),  # bare_name is "b > c"
            ("a > b > c", "b", False),  # only first split taken
            # Empty string tid is explicitly skipped
        ],
    )
    def test_bare_name_matching_semantics(
        self,
        test_id: str,
        fullname_in_report: str,
        expect_match: bool,
    ) -> None:
        """Canonical-only scoring: a report assertion counts ONLY when its
        ``fullName`` matches a canonical test_id (the full id or the bare name
        after the first ``' > '``). A non-matching assertion is ignored (it is
        NOT counted as an extra pass — anti-cheat), and the unsatisfied
        canonical id is scored ``failed``.
        """
        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": fullname_in_report,
                            "duration": 0,
                        }
                    ]
                }
            ]
        }
        counter, _ = parse_jest_vitest_report(report, [test_id])
        if expect_match:
            assert counter["passed"] == 1
            assert counter.get("failed", 0) == 0
        else:
            assert counter.get("passed", 0) == 0
            assert counter["failed"] == 1

    def test_empty_test_id_is_skipped(self) -> None:
        report = {"testResults": []}
        counter, _ = parse_jest_vitest_report(report, ["", "", ""])
        assert sum(counter.values()) == 0

    def test_duplicate_test_ids_counted_once_per_id(self) -> None:
        """Duplicates in the test_ids list each contribute a phantom failure."""
        report = {"testResults": []}
        counter, _ = parse_jest_vitest_report(report, ["a > t", "a > t", "a > t"])
        assert counter["failed"] == 3
