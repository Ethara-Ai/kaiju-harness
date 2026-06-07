"""Tests for commit0.harness.rust_test_parser module."""

from __future__ import annotations

import json

import pytest

from commit0.harness.constants import TestStatus
from commit0.harness.rust_test_parser import (
    RustTestResult,
    parse_nextest_json,
    parse_nextest_report,
)


class TestParseNextestJson:
    def test_single_passed_test(self):
        line = json.dumps(
            {
                "type": "test",
                "event": "ok",
                "name": "test_add",
                "exec_time": 0.5,
                "stdout": "",
            }
        )
        results = parse_nextest_json(line)
        assert len(results) == 1
        assert results[0].name == "test_add"
        assert results[0].status == TestStatus.PASSED
        assert results[0].duration == 0.5

    def test_single_failed_test(self):
        line = json.dumps(
            {
                "type": "test",
                "event": "failed",
                "name": "test_sub",
                "exec_time": 1.2,
                "stdout": "assertion failed",
            }
        )
        results = parse_nextest_json(line)
        assert len(results) == 1
        assert results[0].status == TestStatus.FAILED
        assert results[0].stdout == "assertion failed"

    def test_ignored_test(self):
        line = json.dumps(
            {"type": "test", "event": "ignored", "name": "test_skip", "exec_time": 0.0}
        )
        results = parse_nextest_json(line)
        assert len(results) == 1
        assert results[0].status == TestStatus.SKIPPED

    def test_timeout_test(self):
        line = json.dumps(
            {"type": "test", "event": "timeout", "name": "test_slow", "exec_time": 30.0}
        )
        results = parse_nextest_json(line)
        assert len(results) == 1
        assert results[0].status == TestStatus.ERROR

    def test_multiple_tests(self):
        lines = "\n".join(
            [
                json.dumps({"type": "test", "event": "ok", "name": "test_a"}),
                json.dumps({"type": "test", "event": "failed", "name": "test_b"}),
                json.dumps({"type": "test", "event": "ignored", "name": "test_c"}),
            ]
        )
        results = parse_nextest_json(lines)
        assert len(results) == 3
        assert results[0].status == TestStatus.PASSED
        assert results[1].status == TestStatus.FAILED
        assert results[2].status == TestStatus.SKIPPED

    def test_non_test_types_skipped(self):
        lines = "\n".join(
            [
                json.dumps({"type": "suite", "event": "started"}),
                json.dumps({"type": "test", "event": "ok", "name": "test_real"}),
                json.dumps({"type": "suite", "event": "ok"}),
            ]
        )
        results = parse_nextest_json(lines)
        assert len(results) == 1
        assert results[0].name == "test_real"

    def test_unknown_event_skipped(self):
        line = json.dumps({"type": "test", "event": "started", "name": "test_x"})
        results = parse_nextest_json(line)
        assert len(results) == 0

    def test_empty_string(self):
        assert parse_nextest_json("") == []

    def test_none_input(self):
        assert parse_nextest_json(None) == []

    def test_whitespace_only(self):
        assert parse_nextest_json("   \n  \n  ") == []

    def test_malformed_json_skipped(self):
        lines = "not valid json\n" + json.dumps(
            {"type": "test", "event": "ok", "name": "test_valid"}
        )
        results = parse_nextest_json(lines)
        assert len(results) == 1
        assert results[0].name == "test_valid"

    def test_missing_name_defaults_empty(self):
        line = json.dumps({"type": "test", "event": "ok"})
        results = parse_nextest_json(line)
        assert len(results) == 1
        assert results[0].name == ""

    def test_missing_exec_time_defaults_zero(self):
        line = json.dumps({"type": "test", "event": "ok", "name": "test_x"})
        results = parse_nextest_json(line)
        assert results[0].duration == 0.0

    def test_missing_stdout_defaults_empty(self):
        line = json.dumps({"type": "test", "event": "ok", "name": "test_x"})
        results = parse_nextest_json(line)
        assert results[0].stdout == ""


class TestParseNextestReport:
    def test_valid_report_file(self, tmp_path):
        report = tmp_path / "report.json"
        lines = "\n".join(
            [
                json.dumps(
                    {"type": "test", "event": "ok", "name": "test_a", "exec_time": 0.1}
                ),
                json.dumps(
                    {
                        "type": "test",
                        "event": "failed",
                        "name": "test_b",
                        "exec_time": 0.2,
                    }
                ),
                json.dumps(
                    {
                        "type": "test",
                        "event": "ignored",
                        "name": "test_c",
                        "exec_time": 0.0,
                    }
                ),
            ]
        )
        report.write_text(lines)

        result = parse_nextest_report(str(report))
        assert result["summary"]["total"] == 3
        assert result["summary"]["passed"] == 1
        assert result["summary"]["failed"] == 1
        assert result["summary"]["skipped"] == 1
        assert len(result["tests"]) == 3

    def test_file_not_found(self):
        result = parse_nextest_report("/nonexistent/path/report.json")
        assert result["summary"]["total"] == 0
        assert result["tests"] == []

    def test_empty_file(self, tmp_path):
        report = tmp_path / "empty.json"
        report.write_text("")

        result = parse_nextest_report(str(report))
        assert result["summary"]["total"] == 0

    def test_all_passed(self, tmp_path):
        report = tmp_path / "report.json"
        lines = "\n".join(
            [
                json.dumps({"type": "test", "event": "ok", "name": f"test_{i}"})
                for i in range(5)
            ]
        )
        report.write_text(lines)

        result = parse_nextest_report(str(report))
        assert result["summary"]["passed"] == 5
        assert result["summary"]["failed"] == 0

    def test_test_entry_format(self, tmp_path):
        report = tmp_path / "report.json"
        report.write_text(
            json.dumps(
                {"type": "test", "event": "ok", "name": "test_x", "exec_time": 1.5}
            )
        )

        result = parse_nextest_report(str(report))
        test_entry = result["tests"][0]
        assert test_entry["name"] == "test_x"
        assert test_entry["outcome"] == TestStatus.PASSED.value
        assert test_entry["duration"] == 1.5


class TestRustTestResultDataclass:
    def test_fields_accessible(self):
        r = RustTestResult(
            name="test_a", status=TestStatus.PASSED, duration=1.5, stdout="ok"
        )
        assert r.name == "test_a"
        assert r.status == TestStatus.PASSED
        assert r.duration == 1.5
        assert r.stdout == "ok"

    def test_equality(self):
        a = RustTestResult(name="t", status=TestStatus.PASSED, duration=0.0, stdout="")
        b = RustTestResult(name="t", status=TestStatus.PASSED, duration=0.0, stdout="")
        assert a == b

    def test_inequality(self):
        a = RustTestResult(name="t1", status=TestStatus.PASSED, duration=0.0, stdout="")
        b = RustTestResult(name="t2", status=TestStatus.FAILED, duration=0.0, stdout="")
        assert a != b


class TestParseNextestJsonExpanded:
    def test_large_ndjson(self):
        lines = "\n".join(
            json.dumps(
                {"type": "test", "event": "ok", "name": f"test_{i}", "exec_time": 0.1}
            )
            for i in range(500)
        )
        results = parse_nextest_json(lines)
        assert len(results) == 500

    def test_duration_float_precision(self):
        line = json.dumps(
            {"type": "test", "event": "ok", "name": "t", "exec_time": 0.123456789}
        )
        results = parse_nextest_json(line)
        assert abs(results[0].duration - 0.123456789) < 1e-9

    def test_duration_integer_becomes_float(self):
        line = json.dumps({"type": "test", "event": "ok", "name": "t", "exec_time": 5})
        results = parse_nextest_json(line)
        assert isinstance(results[0].duration, float)
        assert results[0].duration == 5.0

    def test_stdout_with_special_chars(self):
        line = json.dumps(
            {
                "type": "test",
                "event": "failed",
                "name": "t",
                "stdout": "line1\nline2\ttab\r\nwindows",
            }
        )
        results = parse_nextest_json(line)
        assert "line1\nline2\ttab\r\nwindows" == results[0].stdout

    def test_all_four_event_types_in_one_stream(self):
        lines = "\n".join(
            [
                json.dumps({"type": "test", "event": "ok", "name": "pass"}),
                json.dumps({"type": "test", "event": "failed", "name": "fail"}),
                json.dumps({"type": "test", "event": "ignored", "name": "skip"}),
                json.dumps({"type": "test", "event": "timeout", "name": "err"}),
            ]
        )
        results = parse_nextest_json(lines)
        assert len(results) == 4
        statuses = [r.status for r in results]
        assert statuses == [
            TestStatus.PASSED,
            TestStatus.FAILED,
            TestStatus.SKIPPED,
            TestStatus.ERROR,
        ]

    def test_mixed_with_suite_events(self):
        lines = "\n".join(
            [
                json.dumps({"type": "suite", "event": "started", "test_count": 3}),
                json.dumps({"type": "test", "event": "started", "name": "t1"}),
                json.dumps(
                    {"type": "test", "event": "ok", "name": "t1", "exec_time": 0.1}
                ),
                json.dumps({"type": "test", "event": "started", "name": "t2"}),
                json.dumps(
                    {
                        "type": "test",
                        "event": "failed",
                        "name": "t2",
                        "exec_time": 0.2,
                        "stdout": "err",
                    }
                ),
                json.dumps(
                    {"type": "suite", "event": "failed", "passed": 1, "failed": 1}
                ),
            ]
        )
        results = parse_nextest_json(lines)
        assert len(results) == 2

    def test_empty_lines_between_json(self):
        lines = (
            "\n\n" + json.dumps({"type": "test", "event": "ok", "name": "t"}) + "\n\n\n"
        )
        results = parse_nextest_json(lines)
        assert len(results) == 1


class TestParseNextestReportExpanded:
    def test_report_with_all_status_types(self, tmp_path):
        report = tmp_path / "report.json"
        lines = "\n".join(
            [
                json.dumps(
                    {"type": "test", "event": "ok", "name": "t1", "exec_time": 0.1}
                ),
                json.dumps(
                    {"type": "test", "event": "failed", "name": "t2", "exec_time": 0.2}
                ),
                json.dumps(
                    {"type": "test", "event": "ignored", "name": "t3", "exec_time": 0.0}
                ),
                json.dumps(
                    {
                        "type": "test",
                        "event": "timeout",
                        "name": "t4",
                        "exec_time": 30.0,
                    }
                ),
            ]
        )
        report.write_text(lines)

        result = parse_nextest_report(str(report))
        assert result["summary"]["total"] == 4
        assert result["summary"]["passed"] == 1
        assert result["summary"]["failed"] == 1
        assert result["summary"]["skipped"] == 1
        assert result["summary"]["error"] == 1

    def test_report_file_with_only_suite_events(self, tmp_path):
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"type": "suite", "event": "ok"}))

        result = parse_nextest_report(str(report))
        assert result["summary"]["total"] == 0
        assert result["tests"] == []

    def test_report_tests_list_format(self, tmp_path):
        report = tmp_path / "report.json"
        report.write_text(
            json.dumps(
                {
                    "type": "test",
                    "event": "failed",
                    "name": "test_x",
                    "exec_time": 2.5,
                    "stdout": "oops",
                }
            )
        )

        result = parse_nextest_report(str(report))
        t = result["tests"][0]
        assert set(t.keys()) == {"name", "outcome", "duration"}
        assert t["name"] == "test_x"
        assert t["outcome"] == TestStatus.FAILED.value
        assert t["duration"] == 2.5

    def test_report_permission_error(self, tmp_path):
        report = tmp_path / "noperm.json"
        report.write_text("data")
        report.chmod(0o000)

        try:
            with pytest.raises(PermissionError):
                parse_nextest_report(str(report))
        finally:
            report.chmod(0o644)


class TestEventStatusMap:
    def test_ok_maps_to_passed(self):
        from commit0.harness.rust_test_parser import _EVENT_STATUS_MAP

        assert _EVENT_STATUS_MAP["ok"] == TestStatus.PASSED

    def test_failed_maps_to_failed(self):
        from commit0.harness.rust_test_parser import _EVENT_STATUS_MAP

        assert _EVENT_STATUS_MAP["failed"] == TestStatus.FAILED

    def test_ignored_maps_to_skipped(self):
        from commit0.harness.rust_test_parser import _EVENT_STATUS_MAP

        assert _EVENT_STATUS_MAP["ignored"] == TestStatus.SKIPPED

    def test_timeout_maps_to_error(self):
        from commit0.harness.rust_test_parser import _EVENT_STATUS_MAP

        assert _EVENT_STATUS_MAP["timeout"] == TestStatus.ERROR

    def test_map_has_four_entries(self):
        from commit0.harness.rust_test_parser import _EVENT_STATUS_MAP

        assert len(_EVENT_STATUS_MAP) == 4

    def test_unknown_event_not_in_map(self):
        from commit0.harness.rust_test_parser import _EVENT_STATUS_MAP

        assert "started" not in _EVENT_STATUS_MAP


class TestParseNextestJsonEdge:
    def test_multiple_malformed_lines_skipped(self):
        lines = "not json\nalso bad\n"
        results = parse_nextest_json(lines)
        assert results == []

    def test_non_test_type_skipped(self):
        line = json.dumps({"type": "suite", "event": "started"})
        results = parse_nextest_json(line)
        assert results == []

    def test_unknown_event_skipped(self):
        line = json.dumps({"type": "test", "event": "started", "name": "t"})
        results = parse_nextest_json(line)
        assert results == []

    def test_missing_name_defaults_empty(self):
        line = json.dumps({"type": "test", "event": "ok", "exec_time": 1.0})
        results = parse_nextest_json(line)
        assert results[0].name == ""

    def test_missing_exec_time_defaults_zero(self):
        line = json.dumps({"type": "test", "event": "ok", "name": "t"})
        results = parse_nextest_json(line)
        assert results[0].duration == 0.0

    def test_missing_stdout_defaults_empty(self):
        line = json.dumps({"type": "test", "event": "ok", "name": "t"})
        results = parse_nextest_json(line)
        assert results[0].stdout == ""

    def test_whitespace_only_returns_empty(self):
        results = parse_nextest_json("   \n  \n  ")
        assert results == []

    def test_mixed_valid_and_invalid_lines(self):
        valid = json.dumps(
            {"type": "test", "event": "ok", "name": "t1", "exec_time": 0.5}
        )
        lines = f"bad line\n{valid}\nalso bad\n"
        results = parse_nextest_json(lines)
        assert len(results) == 1
        assert results[0].name == "t1"

    def test_all_four_statuses(self):
        lines = []
        for event in ["ok", "failed", "ignored", "timeout"]:
            lines.append(
                json.dumps({"type": "test", "event": event, "name": f"t_{event}"})
            )
        results = parse_nextest_json("\n".join(lines))
        assert len(results) == 4
        statuses = {r.status for r in results}
        assert statuses == {
            TestStatus.PASSED,
            TestStatus.FAILED,
            TestStatus.SKIPPED,
            TestStatus.ERROR,
        }

    def test_large_number_of_tests(self):
        lines = []
        for i in range(100):
            lines.append(
                json.dumps(
                    {
                        "type": "test",
                        "event": "ok",
                        "name": f"test_{i}",
                        "exec_time": 0.1,
                    }
                )
            )
        results = parse_nextest_json("\n".join(lines))
        assert len(results) == 100


class TestParseNextestReportEdge:
    def test_empty_file_returns_empty(self, tmp_path):
        report = tmp_path / "empty.json"
        report.write_text("")
        result = parse_nextest_report(str(report))
        assert result["tests"] == []
        assert result["summary"]["total"] == 0

    def test_summary_counts_correct(self, tmp_path):
        lines = [
            json.dumps({"type": "test", "event": "ok", "name": "t1"}),
            json.dumps({"type": "test", "event": "ok", "name": "t2"}),
            json.dumps({"type": "test", "event": "failed", "name": "t3"}),
            json.dumps({"type": "test", "event": "ignored", "name": "t4"}),
            json.dumps({"type": "test", "event": "timeout", "name": "t5"}),
        ]
        report = tmp_path / "report.json"
        report.write_text("\n".join(lines))
        result = parse_nextest_report(str(report))
        assert result["summary"]["total"] == 5
        assert result["summary"]["passed"] == 2
        assert result["summary"]["failed"] == 1
        assert result["summary"]["skipped"] == 1
        assert result["summary"]["error"] == 1

    def test_test_outcome_is_status_value(self, tmp_path):
        line = json.dumps(
            {"type": "test", "event": "ok", "name": "t1", "exec_time": 0.5}
        )
        report = tmp_path / "report.json"
        report.write_text(line)
        result = parse_nextest_report(str(report))
        assert result["tests"][0]["outcome"] == TestStatus.PASSED.value

    def test_test_has_name_outcome_duration(self, tmp_path):
        line = json.dumps(
            {"type": "test", "event": "ok", "name": "t1", "exec_time": 1.5}
        )
        report = tmp_path / "report.json"
        report.write_text(line)
        t = parse_nextest_report(str(report))["tests"][0]
        assert set(t.keys()) == {"name", "outcome", "duration"}

    def test_nonexistent_file_returns_empty(self):
        result = parse_nextest_report("/no/such/path.json")
        assert result["tests"] == []
        assert result["summary"]["total"] == 0


class TestRustTestResultDataclassExtended:
    def test_fields(self):
        r = RustTestResult(
            name="t", status=TestStatus.PASSED, duration=1.0, stdout="out"
        )
        assert r.name == "t"
        assert r.status == TestStatus.PASSED
        assert r.duration == 1.0
        assert r.stdout == "out"

    def test_equality(self):
        a = RustTestResult(name="t", status=TestStatus.PASSED, duration=1.0, stdout="")
        b = RustTestResult(name="t", status=TestStatus.PASSED, duration=1.0, stdout="")
        assert a == b

    def test_inequality(self):
        a = RustTestResult(name="t1", status=TestStatus.PASSED, duration=1.0, stdout="")
        b = RustTestResult(name="t2", status=TestStatus.PASSED, duration=1.0, stdout="")
        assert a != b


class TestModuleExportsParser:
    def test_all_exports(self):
        from commit0.harness import rust_test_parser as mod

        assert set(mod.__all__) == {
            "RustTestResult",
            "parse_nextest_json",
            "parse_nextest_report",
        }

    def test_all_accessible(self):
        from commit0.harness import rust_test_parser as mod

        for name in mod.__all__:
            assert hasattr(mod, name)
