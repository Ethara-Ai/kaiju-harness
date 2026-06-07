"""Unified Java test report parser.

Handles JUnit 4, JUnit 5, and TestNG XML reports.
All three produce Surefire-compatible XML (when run via Maven/Gradle).
"""
import defusedxml.ElementTree as ET
from pathlib import Path
from typing import Dict
from enum import Enum


class JavaTestResult(Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


def detect_test_framework(repo_path: str) -> str:
    """Detect which test framework a Java project uses."""
    p = Path(repo_path)
    pom_path = p / "pom.xml"
    gradle_path = p / "build.gradle"

    content = ""
    if pom_path.exists():
        content = pom_path.read_text()
    elif gradle_path.exists():
        content = gradle_path.read_text()

    if "org.testng" in content:
        return "testng"
    if "junit-jupiter" in content or "org.junit.jupiter" in content:
        return "junit5"
    if "junit" in content.lower() or "org.junit" in content:
        return "junit4"
    return "junit5"  # default


def parse_surefire_reports(report_dir: str) -> Dict[str, JavaTestResult]:
    """Parse Surefire/Failsafe XML reports into test results.

    Works for JUnit 4, JUnit 5, and TestNG — all produce
    Surefire-compatible XML when run via Maven/Gradle plugins.

    Returns: Dict mapping test_id -> result
        test_id format: "com.example.MyTest#testMethod"
    """
    results: Dict[str, JavaTestResult] = {}
    report_path = Path(report_dir)

    if not report_path.exists():
        return results

    for xml_file in report_path.glob("*.xml"):
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()

            # Handle both <testsuite> and <testsuites> root elements
            testsuites = (
                root.findall("testsuite")
                if root.tag == "testsuites"
                else [root]
            )

            for suite in testsuites:
                suite_name = suite.get("name", "")
                for testcase in suite.findall("testcase"):
                    class_name = testcase.get("classname", suite_name)
                    method_name = testcase.get("name", "")
                    test_id = f"{class_name}#{method_name}"

                    if testcase.find("failure") is not None:
                        results[test_id] = JavaTestResult.FAILED
                    elif testcase.find("error") is not None:
                        results[test_id] = JavaTestResult.ERROR
                    elif testcase.find("skipped") is not None:
                        results[test_id] = JavaTestResult.SKIPPED
                    else:
                        results[test_id] = JavaTestResult.PASSED
        except ET.ParseError:
            continue

    return results


def summarize_results(
    results: Dict[str, JavaTestResult]
) -> Dict[str, int]:
    """Summarize test results by status."""
    summary = {status.value: 0 for status in JavaTestResult}
    for result in results.values():
        summary[result.value] += 1
    return summary
