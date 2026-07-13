from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.js_test_parser import (
    JsTestResult,
    JsTestStatus,
    parse_js_test_output,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "js_test_output"


def _fixture(framework: str, name: str) -> Path:
    return FIXTURE_ROOT / framework / name


class TestDispatch:
    def test_unknown_framework_raises(self, tmp_path: Path) -> None:
        report = tmp_path / "out.json"
        report.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="unknown JS test framework"):
            parse_js_test_output(report, "qunit")

    def test_missing_file_returns_parse_error(self, tmp_path: Path) -> None:
        result = parse_js_test_output(tmp_path / "missing.json", "jest")
        assert isinstance(result, JsTestResult)
        assert result.parse_error is not None
        assert "report missing" in result.parse_error
        assert result.num_total == 0

    def test_empty_text_marks_raw_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "e.json"
        f.write_text("", encoding="utf-8")
        result = parse_js_test_output(f, "jest")
        assert result.raw_empty is True
        assert result.parse_error is None

    def test_empty_marker_returns_raw_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "e.json"
        f.write_text("EMPTY_RESULTS", encoding="utf-8")
        result = parse_js_test_output(f, "vitest")
        assert result.raw_empty is True


class TestJsTestStatus:
    def test_enum_values(self) -> None:
        assert JsTestStatus.PASSED == "passed"
        assert JsTestStatus.FAILED == "failed"
        assert JsTestStatus.SKIPPED == "skipped"
        assert JsTestStatus.ERROR == "error"

    def test_is_str_subclass(self) -> None:
        assert isinstance(JsTestStatus.PASSED, str)


class TestJestParser:
    def test_success_fixture(self) -> None:
        result = parse_js_test_output(_fixture("jest", "success.json"), "jest")
        assert result.framework == "jest"
        assert result.truncated is False
        assert result.parse_error is None
        assert result.num_total == 2
        assert result.num_passed == 2
        assert result.num_failed == 0
        assert result.duration_seconds == pytest.approx((12 + 18) / 1000.0)

    def test_success_per_test_names_recorded(self) -> None:
        result = parse_js_test_output(_fixture("jest", "success.json"), "jest")
        joined = " | ".join(result.statuses.keys())
        assert "processes tasks in order" in joined
        assert "respects concurrency" in joined

    def test_mixed_fixture_counts(self) -> None:
        result = parse_js_test_output(
            _fixture("jest", "mixed_pass_fail.json"), "jest"
        )
        assert result.num_total == 4
        assert result.num_passed == 2
        assert result.num_failed == 1
        assert result.num_skipped == 1

    def test_mixed_summary_method(self) -> None:
        result = parse_js_test_output(
            _fixture("jest", "mixed_pass_fail.json"), "jest"
        )
        s = result.summary()
        assert s == {"passed": 2, "failed": 1, "skipped": 1, "total": 4}

    def test_truncated_fixture_does_not_raise(self) -> None:
        result = parse_js_test_output(_fixture("jest", "truncated.json"), "jest")
        assert isinstance(result, JsTestResult)
        assert result.truncated is True
        assert result.parse_error is not None

    def test_truncated_fixture_regex_fallback_extracts_statuses(self) -> None:
        result = parse_js_test_output(_fixture("jest", "truncated.json"), "jest")
        assert result.num_passed >= 1
        assert result.num_failed >= 1


class TestVitestParser:
    def test_success_fixture(self) -> None:
        result = parse_js_test_output(_fixture("vitest", "success.json"), "vitest")
        assert result.framework == "vitest"
        assert result.truncated is False
        assert result.num_total == 2
        assert result.num_passed == 2
        assert result.num_failed == 0

    def test_mixed_fixture(self) -> None:
        result = parse_js_test_output(
            _fixture("vitest", "mixed_pass_fail.json"), "vitest"
        )
        assert result.num_passed == 1
        assert result.num_failed == 1
        assert result.num_skipped == 1

    def test_truncated_fixture_falls_back(self) -> None:
        result = parse_js_test_output(_fixture("vitest", "truncated.json"), "vitest")
        assert result.truncated is True
        assert result.num_passed + result.num_failed >= 1


class TestMochaParser:
    def test_success_fixture(self) -> None:
        result = parse_js_test_output(_fixture("mocha", "success.json"), "mocha")
        assert result.framework == "mocha"
        assert result.num_passed == 2
        assert result.num_failed == 0
        assert result.duration_seconds == pytest.approx(25 / 1000.0)

    def test_success_per_test_names(self) -> None:
        result = parse_js_test_output(_fixture("mocha", "success.json"), "mocha")
        names = list(result.statuses.keys())
        assert "math adds" in names
        assert "math subtracts" in names

    def test_mixed_fixture(self) -> None:
        result = parse_js_test_output(
            _fixture("mocha", "mixed_pass_fail.json"), "mocha"
        )
        assert result.num_failed == 1
        assert result.num_passed >= 2
        assert result.num_skipped >= 1

    def test_truncated_fixture_falls_back(self) -> None:
        result = parse_js_test_output(
            _fixture("mocha", "truncated.json"), "mocha"
        )
        assert result.truncated is True
        assert result.num_passed >= 1


class TestNodeTestTapParser:
    def test_success_fixture(self) -> None:
        result = parse_js_test_output(
            _fixture("node_test", "success.tap"), "node_test"
        )
        assert result.framework == "node_test"
        assert result.num_total == 2
        assert result.num_passed == 2
        assert result.num_failed == 0
        assert result.duration_seconds == pytest.approx((4.5 + 3.2) / 1000.0)

    def test_success_test_names_extracted(self) -> None:
        result = parse_js_test_output(
            _fixture("node_test", "success.tap"), "node_test"
        )
        names = list(result.statuses.keys())
        # Names now carry the subtest hierarchy (e.g. "...top-level > alpha") from
        # the shared subtest-aware TAP parser, so match by substring.
        assert any("alpha" in n for n in names)
        assert any("beta" in n for n in names)

    def test_mixed_fixture_counts(self) -> None:
        result = parse_js_test_output(
            _fixture("node_test", "mixed_pass_fail.tap"), "node_test"
        )
        assert result.num_passed == 1
        assert result.num_failed == 1
        assert result.num_skipped == 2

    def test_mixed_skip_directives_recognised(self) -> None:
        result = parse_js_test_output(
            _fixture("node_test", "mixed_pass_fail.tap"), "node_test"
        )
        # Keys carry the subtest hierarchy now; match by substring.
        def _status(substr: str) -> JsTestStatus:
            return next(v for k, v in result.statuses.items() if substr in k)

        assert _status("third skip") == JsTestStatus.SKIPPED
        assert _status("fourth todo") == JsTestStatus.SKIPPED

    def test_truncated_fixture_does_not_raise(self) -> None:
        result = parse_js_test_output(
            _fixture("node_test", "truncated.tap"), "node_test"
        )
        assert isinstance(result, JsTestResult)
        assert result.num_passed + result.num_failed >= 1


class TestPerTestStatusMapping:
    def test_pass_maps_to_passed(self, tmp_path: Path) -> None:
        f = tmp_path / "j.json"
        f.write_text(
            '{"testResults":[{"name":"a","assertionResults":'
            '[{"fullName":"t1","status":"pass"}]}]}',
            encoding="utf-8",
        )
        result = parse_js_test_output(f, "jest")
        assert result.num_passed == 1

    def test_fail_maps_to_failed(self, tmp_path: Path) -> None:
        f = tmp_path / "j.json"
        f.write_text(
            '{"testResults":[{"name":"a","assertionResults":'
            '[{"fullName":"t1","status":"fail"}]}]}',
            encoding="utf-8",
        )
        result = parse_js_test_output(f, "jest")
        assert result.num_failed == 1

    @pytest.mark.parametrize("raw", ["pending", "skipped", "todo", "disabled"])
    def test_pending_variants_map_to_skipped(self, tmp_path: Path, raw: str) -> None:
        f = tmp_path / "j.json"
        f.write_text(
            f'{{"testResults":[{{"name":"a","assertionResults":'
            f'[{{"fullName":"t1","status":"{raw}"}}]}}]}}',
            encoding="utf-8",
        )
        result = parse_js_test_output(f, "jest")
        assert result.num_skipped == 1

    def test_unknown_status_defaults_to_failed(self, tmp_path: Path) -> None:
        f = tmp_path / "j.json"
        f.write_text(
            '{"testResults":[{"name":"a","assertionResults":'
            '[{"fullName":"t1","status":"weird"}]}]}',
            encoding="utf-8",
        )
        result = parse_js_test_output(f, "jest")
        assert result.num_failed == 1


class TestNonDictRoot:
    def test_jest_array_root_records_parse_error(self, tmp_path: Path) -> None:
        f = tmp_path / "j.json"
        f.write_text("[1,2,3]", encoding="utf-8")
        result = parse_js_test_output(f, "jest")
        assert result.parse_error is not None
        assert result.num_total == 0

    def test_mocha_non_dict_root(self, tmp_path: Path) -> None:
        f = tmp_path / "m.json"
        f.write_text('"a string"', encoding="utf-8")
        result = parse_js_test_output(f, "mocha")
        assert result.parse_error is not None


class TestDataclassDefaults:
    def test_default_factory_dict(self) -> None:
        r = JsTestResult(framework="jest")
        assert r.statuses == {}
        assert r.duration_seconds == 0.0
        assert r.truncated is False
        assert r.parse_error is None
        assert r.raw_empty is False

    def test_summary_on_empty(self) -> None:
        r = JsTestResult(framework="mocha")
        assert r.summary() == {"passed": 0, "failed": 0, "skipped": 0, "total": 0}

    def test_num_failed_counts_errors_too(self) -> None:
        r = JsTestResult(framework="jest")
        r.statuses["t1"] = JsTestStatus.FAILED
        r.statuses["t2"] = JsTestStatus.ERROR
        r.statuses["t3"] = JsTestStatus.PASSED
        assert r.num_failed == 2
        assert r.num_passed == 1
