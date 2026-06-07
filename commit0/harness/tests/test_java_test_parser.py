from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.java_test_parser import (
    JavaTestResult,
    detect_test_framework,
    parse_surefire_reports,
    summarize_results,
)

MODULE = "commit0.harness.java_test_parser"


class TestJavaTestResult:
    def test_enum_values(self) -> None:
        assert JavaTestResult.PASSED.value == "passed"
        assert JavaTestResult.FAILED.value == "failed"
        assert JavaTestResult.ERROR.value == "error"
        assert JavaTestResult.SKIPPED.value == "skipped"


class TestDetectTestFramework:
    def test_testng(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies>"
            '<dependency><groupId>org.testng</groupId></dependency>'
            "</dependencies></project>"
        )
        assert detect_test_framework(str(tmp_path)) == "testng"

    def test_junit5(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies>"
            '<dependency><artifactId>junit-jupiter</artifactId></dependency>'
            "</dependencies></project>"
        )
        assert detect_test_framework(str(tmp_path)) == "junit5"

    def test_junit4(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><dependencies>"
            '<dependency><groupId>org.junit</groupId></dependency>'
            "</dependencies></project>"
        )
        assert detect_test_framework(str(tmp_path)) == "junit4"

    def test_gradle_junit5(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text(
            'testImplementation "org.junit.jupiter:junit-jupiter:5.10.0"'
        )
        assert detect_test_framework(str(tmp_path)) == "junit5"

    def test_default_no_build_file(self, tmp_path: Path) -> None:
        assert detect_test_framework(str(tmp_path)) == "junit5"


class TestParseSurefireReports:
    def _write_xml(self, tmp_path: Path, filename: str, content: str) -> None:
        (tmp_path / filename).write_text(content)

    def test_passed_test(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "TEST-Foo.xml", (
            '<?xml version="1.0"?>'
            '<testsuite name="com.Foo">'
            '<testcase classname="com.Foo" name="testBar"/>'
            '</testsuite>'
        ))
        result = parse_surefire_reports(str(tmp_path))
        assert result["com.Foo#testBar"] is JavaTestResult.PASSED

    def test_failed_test(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "TEST-Foo.xml", (
            '<?xml version="1.0"?>'
            '<testsuite name="com.Foo">'
            '<testcase classname="com.Foo" name="testFail">'
            '<failure message="oops"/>'
            '</testcase>'
            '</testsuite>'
        ))
        result = parse_surefire_reports(str(tmp_path))
        assert result["com.Foo#testFail"] is JavaTestResult.FAILED

    def test_error_test(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "TEST-Foo.xml", (
            '<?xml version="1.0"?>'
            '<testsuite name="com.Foo">'
            '<testcase classname="com.Foo" name="testErr">'
            '<error message="npe"/>'
            '</testcase>'
            '</testsuite>'
        ))
        result = parse_surefire_reports(str(tmp_path))
        assert result["com.Foo#testErr"] is JavaTestResult.ERROR

    def test_skipped_test(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "TEST-Foo.xml", (
            '<?xml version="1.0"?>'
            '<testsuite name="com.Foo">'
            '<testcase classname="com.Foo" name="testSkip">'
            '<skipped/>'
            '</testcase>'
            '</testsuite>'
        ))
        result = parse_surefire_reports(str(tmp_path))
        assert result["com.Foo#testSkip"] is JavaTestResult.SKIPPED

    def test_testsuites_root(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "TEST-Multi.xml", (
            '<?xml version="1.0"?>'
            '<testsuites>'
            '<testsuite name="com.A">'
            '<testcase classname="com.A" name="m1"/>'
            '</testsuite>'
            '<testsuite name="com.B">'
            '<testcase classname="com.B" name="m2"/>'
            '</testsuite>'
            '</testsuites>'
        ))
        result = parse_surefire_reports(str(tmp_path))
        assert "com.A#m1" in result
        assert "com.B#m2" in result

    def test_invalid_xml_skipped(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "BAD.xml", "<<< not xml >>>")
        result = parse_surefire_reports(str(tmp_path))
        assert result == {}

    def test_nonexistent_dir_returns_empty(self) -> None:
        result = parse_surefire_reports("/nonexistent/dir/xyz")
        assert result == {}

    def test_classname_from_suite_attr(self, tmp_path: Path) -> None:
        self._write_xml(tmp_path, "TEST-Suite.xml", (
            '<?xml version="1.0"?>'
            '<testsuite name="com.Suite">'
            '<testcase name="testIt"/>'
            '</testsuite>'
        ))
        result = parse_surefire_reports(str(tmp_path))
        assert result["com.Suite#testIt"] is JavaTestResult.PASSED


class TestSummarizeResults:
    def test_counts_per_status(self) -> None:
        results = {
            "A#m1": JavaTestResult.PASSED,
            "A#m2": JavaTestResult.PASSED,
            "B#m1": JavaTestResult.FAILED,
            "C#m1": JavaTestResult.ERROR,
            "D#m1": JavaTestResult.SKIPPED,
        }
        summary = summarize_results(results)
        assert summary["passed"] == 2
        assert summary["failed"] == 1
        assert summary["error"] == 1
        assert summary["skipped"] == 1

    def test_empty_dict_returns_zeros(self) -> None:
        summary = summarize_results({})
        assert summary["passed"] == 0
        assert summary["failed"] == 0
        assert summary["error"] == 0
        assert summary["skipped"] == 0


class TestEdgeCases:
    def test_detect_framework_none_raises(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            detect_test_framework(None)  # type: ignore[arg-type]

    def test_parse_reports_none_raises(self) -> None:
        with pytest.raises((TypeError, AttributeError)):
            parse_surefire_reports(None)  # type: ignore[arg-type]

    def test_empty_xml_file_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "empty.xml").write_text("")
        result = parse_surefire_reports(str(tmp_path))
        assert result == {}

    def test_non_xml_files_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "report.txt").write_text("not xml")
        (tmp_path / "data.json").write_text("{}")
        result = parse_surefire_reports(str(tmp_path))
        assert result == {}

    def test_missing_name_attribute_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "TEST-X.xml").write_text(
            '<?xml version="1.0"?>'
            '<testsuite name="com.X">'
            '<testcase classname="com.X"/>'
            '</testsuite>'
        )
        result = parse_surefire_reports(str(tmp_path))
        assert "com.X#" in result

    def test_multiple_xml_files_merged(self, tmp_path: Path) -> None:
        (tmp_path / "TEST-A.xml").write_text(
            '<?xml version="1.0"?>'
            '<testsuite name="com.A">'
            '<testcase classname="com.A" name="m1"/>'
            '</testsuite>'
        )
        (tmp_path / "TEST-B.xml").write_text(
            '<?xml version="1.0"?>'
            '<testsuite name="com.B">'
            '<testcase classname="com.B" name="m2"/>'
            '</testsuite>'
        )
        result = parse_surefire_reports(str(tmp_path))
        assert "com.A#m1" in result
        assert "com.B#m2" in result

    def test_detect_framework_empty_pom_defaults_junit5(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project></project>")
        assert detect_test_framework(str(tmp_path)) == "junit5"

    def test_missing_testcase_attrs_handled(self, tmp_path: Path) -> None:
        (tmp_path / "TEST-Y.xml").write_text(
            '<?xml version="1.0"?>'
            '<testsuite>'
            '<testcase/>'
            '</testsuite>'
        )
        result = parse_surefire_reports(str(tmp_path))
        assert "#" in result

    def test_nested_testsuites_parsed(self, tmp_path: Path) -> None:
        (tmp_path / "TEST-N.xml").write_text(
            '<?xml version="1.0"?>'
            '<testsuites>'
            '<testsuite name="com.N">'
            '<testcase classname="com.N" name="nested1"/>'
            '</testsuite>'
            '</testsuites>'
        )
        result = parse_surefire_reports(str(tmp_path))
        assert result["com.N#nested1"] is JavaTestResult.PASSED
