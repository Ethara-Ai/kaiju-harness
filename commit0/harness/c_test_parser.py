"""Parser for CTest JUnit XML output.

CTest emits JUnit-compatible XML when invoked with ``--output-junit <file>``.
We tolerate truncated XML (segfault / timeout kills) by falling back to a
regex-based ``<testcase>`` extractor.

Test ID format: ``<test_name>`` — CTest test names are unique per CMake
project so no package prefix is needed (unlike Go's ``package/TestName`` or
Java's ``classname#method``).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from commit0.harness.constants import TestStatus

logger = logging.getLogger(__name__)


_TESTCASE_RE = re.compile(
    r'<testcase\b[^>]*\bname\s*=\s*"([^"]+)"[^>]*?'
    r'(?:/>|>(.*?)</testcase>)',
    re.DOTALL,
)
_FAILURE_RE = re.compile(r"<failure\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"<error\b", re.IGNORECASE)
_SKIPPED_RE = re.compile(r"<skipped\b", re.IGNORECASE)


def parse_ctest_junit(xml_path_or_text: str) -> Dict[str, TestStatus]:
    """Parse a CTest JUnit XML file (or raw text) into ``{test_id: TestStatus}``.

    Tries ``xml.etree.ElementTree`` first; on ``ParseError`` falls back to a
    regex-based extractor so segfault-truncated reports still produce a useful
    pass/fail count.
    """
    if not xml_path_or_text or not xml_path_or_text.strip():
        return {}

    if (
        len(xml_path_or_text) < 4096
        and "\n" not in xml_path_or_text
        and Path(xml_path_or_text).exists()
    ):
        text = Path(xml_path_or_text).read_text(errors="replace")
    else:
        text = xml_path_or_text

    if not text.strip():
        return {}

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        logger.warning("CTest XML parse failed (%s); falling back to regex", exc)
        return _parse_truncated(text)

    return _parse_etree(root)


def _parse_etree(root: ET.Element) -> Dict[str, TestStatus]:
    results: Dict[str, TestStatus] = {}
    suites = (
        root.findall(".//testsuite")
        if root.tag != "testcase"
        else [root]
    )
    if not suites:
        suites = [root]
    for suite in suites:
        for testcase in suite.findall(".//testcase"):
            name = testcase.get("name", "")
            if not name:
                continue
            if testcase.find("failure") is not None:
                results[name] = TestStatus.FAILED
            elif testcase.find("error") is not None:
                results[name] = TestStatus.ERROR
            elif testcase.find("skipped") is not None:
                results[name] = TestStatus.SKIPPED
            else:
                results[name] = TestStatus.PASSED
    return results


def _parse_truncated(text: str) -> Dict[str, TestStatus]:
    results: Dict[str, TestStatus] = {}
    for match in _TESTCASE_RE.finditer(text):
        name = match.group(1)
        body = match.group(2) or ""
        if _FAILURE_RE.search(body):
            results[name] = TestStatus.FAILED
        elif _ERROR_RE.search(body):
            results[name] = TestStatus.ERROR
        elif _SKIPPED_RE.search(body):
            results[name] = TestStatus.SKIPPED
        else:
            results[name] = TestStatus.PASSED
    return results


def summarize_ctest_results(results: Dict[str, TestStatus]) -> Dict[str, int]:
    """Summary counts for an evaluate report.

    Returns ``{passed, failed, skipped, errored, total}``.
    """
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errored": 0, "total": 0}
    for status in results.values():
        counts["total"] += 1
        if status == TestStatus.PASSED:
            counts["passed"] += 1
        elif status == TestStatus.FAILED:
            counts["failed"] += 1
        elif status == TestStatus.SKIPPED:
            counts["skipped"] += 1
        elif status == TestStatus.ERROR:
            counts["errored"] += 1
    return counts


def failed_test_names(results: Dict[str, TestStatus]) -> List[str]:
    return [n for n, s in results.items() if s in (TestStatus.FAILED, TestStatus.ERROR)]


def compute_c_pass_rate(
    results: Dict[str, TestStatus],
    expected_tests: Optional[Iterable[str]] = None,
) -> float:
    if expected_tests is not None:
        expected = list(expected_tests)
        if not expected:
            return 1.0
        passed = sum(1 for t in expected if results.get(t) == TestStatus.PASSED)
        return passed / len(expected)

    if not results:
        return 0.0
    passed = sum(1 for s in results.values() if s == TestStatus.PASSED)
    return passed / len(results)


def parse_ctest_junit_with_summary(
    xml_path_or_text: str,
) -> Tuple[Dict[str, TestStatus], Dict[str, int]]:
    results = parse_ctest_junit(xml_path_or_text)
    return results, summarize_ctest_results(results)


__all__ = [
    "parse_ctest_junit",
    "parse_ctest_junit_with_summary",
    "summarize_ctest_results",
    "failed_test_names",
    "compute_c_pass_rate",
]
