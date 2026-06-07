import logging
import re
from dataclasses import dataclass
from typing import Dict, List

from commit0.harness.constants import TestStatus

logger = logging.getLogger(__name__)

__all__ = [
    "CppTestResult",
    "detect_test_framework",
    "parse_gtest_output",
    "parse_catch2_output",
    "parse_doctest_output",
    "parse_boost_test_output",
    "parse_ctest_output",
    "parse_cpp_test_output",
]


@dataclass
class CppTestResult:
    name: str
    status: TestStatus
    duration: float = 0.0
    stdout: str = ""
    framework: str = "unknown"


def detect_test_framework(output: str) -> str:
    """Detect the C++ test framework from output patterns."""
    if "[==========]" in output or "[ RUN      ]" in output or "[  PASSED  ]" in output:
        return "gtest"
    if "[doctest]" in output:
        return "doctest"
    if "test cases:" in output and "assertions:" in output:
        return "catch2"
    if "*** No errors detected ***" in output or "*** Errors were detected" in output:
        return "boost_test"
    if "Running" in output and "*** No errors detected ***" in output:
        return "boost_test"
    if "Test project" in output or re.search(r"\d+% tests passed", output):
        return "ctest"
    return "unknown"


_CTEST_VERBOSE_PREFIX = re.compile(r"^\s*\d+:\s?")


def parse_gtest_output(output: str) -> Dict:
    """Parse Google Test text output.

    Also handles `ctest --verbose` output where each test binary's stdout is
    prefixed with `N: ` (the test number assigned by CTest).
    """
    tests: List[Dict] = []

    for raw_line in output.splitlines():
        line = _CTEST_VERBOSE_PREFIX.sub("", raw_line)
        run_match = re.match(r"\[ RUN      \] (.+)", line)
        if run_match:
            run_match.group(1).strip()
            continue

        ok_match = re.match(r"\[       OK \] (.+?)(?:\s+\((\d+)\s*ms\))?$", line)
        if ok_match:
            name = ok_match.group(1).strip()
            duration_ms = int(ok_match.group(2)) if ok_match.group(2) else 0
            tests.append({
                "name": name,
                "outcome": TestStatus.PASSED.value,
                "duration": duration_ms / 1000.0,
            })
            continue

        fail_match = re.match(r"\[  FAILED  \] (.+?)(?:\s+\((\d+)\s*ms\))?$", line)
        if fail_match:
            name = fail_match.group(1).strip()
            duration_ms = int(fail_match.group(2)) if fail_match.group(2) else 0
            tests.append({
                "name": name,
                "outcome": TestStatus.FAILED.value,
                "duration": duration_ms / 1000.0,
            })
            continue

    passed = sum(1 for t in tests if t["outcome"] == TestStatus.PASSED.value)
    failed = sum(1 for t in tests if t["outcome"] == TestStatus.FAILED.value)

    return {
        "tests": tests,
        "summary": {
            "total": len(tests),
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "framework": "gtest",
        },
    }


def parse_catch2_output(output: str) -> Dict:
    """Parse Catch2 text output."""
    tests: List[Dict] = []
    total = 0
    passed = 0
    failed = 0

    if "All tests passed" in output:
        match = re.search(r"(\d+)\s+test case", output)
        if match:
            total = int(match.group(1))
            passed = total
    else:
        pass_match = re.search(r"(\d+)\s+test case(?:s)?\s*(?:passed|:)", output)
        fail_match = re.search(r"(\d+)\s+test case(?:s)?\s+failed", output)
        if pass_match:
            total = int(pass_match.group(1))
        if fail_match:
            failed = int(fail_match.group(1))

        summary_match = re.search(
            r"test cases:\s*(\d+)\s*\|\s*(\d+)\s*passed\s*\|\s*(\d+)\s*failed", output
        )
        if summary_match:
            total = int(summary_match.group(1))
            passed = int(summary_match.group(2))
            failed = int(summary_match.group(3))
        elif failed > 0:
            passed = total - failed
        else:
            passed = total

    for i in range(passed):
        tests.append({
            "name": f"catch2_test_{i + 1}",
            "outcome": TestStatus.PASSED.value,
            "duration": 0.0,
        })
    for i in range(failed):
        tests.append({
            "name": f"catch2_failed_{i + 1}",
            "outcome": TestStatus.FAILED.value,
            "duration": 0.0,
        })

    return {
        "tests": tests,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "framework": "catch2",
        },
    }


def parse_doctest_output(output: str) -> Dict:
    """Parse doctest text output."""
    tests: List[Dict] = []
    total = 0
    passed = 0
    failed = 0

    summary_match = re.search(
        r"\[doctest\]\s*test cases:\s*(\d+)\s*\|\s*(\d+)\s*passed\s*\|\s*(\d+)\s*failed",
        output,
    )
    if summary_match:
        total = int(summary_match.group(1))
        passed = int(summary_match.group(2))
        failed = int(summary_match.group(3))
    else:
        cases_match = re.search(r"\[doctest\]\s*test cases:\s*(\d+)", output)
        if cases_match:
            total = int(cases_match.group(1))
            passed = total

    for i in range(passed):
        tests.append({
            "name": f"doctest_test_{i + 1}",
            "outcome": TestStatus.PASSED.value,
            "duration": 0.0,
        })
    for i in range(failed):
        tests.append({
            "name": f"doctest_failed_{i + 1}",
            "outcome": TestStatus.FAILED.value,
            "duration": 0.0,
        })

    return {
        "tests": tests,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "framework": "doctest",
        },
    }


def parse_boost_test_output(output: str) -> Dict:
    """Parse Boost.Test text output."""
    tests: List[Dict] = []
    total = 0
    passed = 0
    failed = 0

    if "*** No errors detected ***" in output:
        cases_match = re.search(r"Running\s+(\d+)\s+test case", output)
        if cases_match:
            total = int(cases_match.group(1))
        else:
            total = 1
        passed = total
    elif "*** Errors were detected" in output or "*** failures" in output.lower():
        cases_match = re.search(r"Running\s+(\d+)\s+test case", output)
        if cases_match:
            total = int(cases_match.group(1))
        fail_match = re.search(r"(\d+)\s+failure", output)
        if fail_match:
            failed = int(fail_match.group(1))
        passed = max(0, total - failed)
    else:
        cases_match = re.search(r"Running\s+(\d+)\s+test case", output)
        if cases_match:
            total = int(cases_match.group(1))
            passed = total

    for i in range(passed):
        tests.append({
            "name": f"boost_test_{i + 1}",
            "outcome": TestStatus.PASSED.value,
            "duration": 0.0,
        })
    for i in range(failed):
        tests.append({
            "name": f"boost_failed_{i + 1}",
            "outcome": TestStatus.FAILED.value,
            "duration": 0.0,
        })

    return {
        "tests": tests,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "framework": "boost_test",
        },
    }


def parse_ctest_output(output: str) -> Dict:
    """Parse CTest text output."""
    tests: List[Dict] = []
    total = 0
    passed = 0
    failed = 0

    summary_match = re.search(
        r"(\d+)%\s+tests passed,\s+(\d+)\s+tests failed out of\s+(\d+)", output
    )
    if summary_match:
        failed = int(summary_match.group(2))
        total = int(summary_match.group(3))
        passed = total - failed

    for line in output.splitlines():
        test_match = re.match(
            r"\s*Test\s+#(\d+):\s+(.+?)\s+\.+\s*(Passed|Failed|\*\*\*Failed)", line
        )
        if test_match:
            name = test_match.group(2).strip()
            result = test_match.group(3)
            status = TestStatus.PASSED if result == "Passed" else TestStatus.FAILED
            tests.append({
                "name": name,
                "outcome": status.value,
                "duration": 0.0,
            })

    if not tests and total > 0:
        for i in range(passed):
            tests.append({
                "name": f"ctest_test_{i + 1}",
                "outcome": TestStatus.PASSED.value,
                "duration": 0.0,
            })
        for i in range(failed):
            tests.append({
                "name": f"ctest_failed_{i + 1}",
                "outcome": TestStatus.FAILED.value,
                "duration": 0.0,
            })

    if not summary_match and tests:
        total = len(tests)
        passed = sum(1 for t in tests if t["outcome"] == TestStatus.PASSED.value)
        failed = sum(1 for t in tests if t["outcome"] == TestStatus.FAILED.value)

    return {
        "tests": tests,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "framework": "ctest",
        },
    }


def parse_cpp_test_output(output: str, exit_code: int = -1) -> Dict:
    """Parse C++ test output, auto-detecting the framework.

    Returns a dict with 'tests' list and 'summary' containing total, passed,
    failed, skipped, and framework name.
    """
    if not output or not output.strip():
        if exit_code == 0:
            return {
                "tests": [{"name": "all", "outcome": TestStatus.PASSED.value, "duration": 0.0}],
                "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "framework": "unknown"},
            }
        return {
            "tests": [],
            "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "framework": "unknown"},
        }

    framework = detect_test_framework(output)

    if framework == "gtest":
        return parse_gtest_output(output)
    elif framework == "catch2":
        return parse_catch2_output(output)
    elif framework == "doctest":
        return parse_doctest_output(output)
    elif framework == "boost_test":
        return parse_boost_test_output(output)
    elif framework == "ctest":
        return parse_ctest_output(output)

    if exit_code == 0:
        return {
            "tests": [{"name": "all", "outcome": TestStatus.PASSED.value, "duration": 0.0}],
            "summary": {"total": 1, "passed": 1, "failed": 0, "skipped": 0, "framework": "unknown"},
        }
    elif exit_code > 0:
        return {
            "tests": [{"name": "all", "outcome": TestStatus.FAILED.value, "duration": 0.0}],
            "summary": {"total": 1, "passed": 0, "failed": 1, "skipped": 0, "framework": "unknown"},
        }

    return {
        "tests": [],
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "framework": "unknown"},
    }
