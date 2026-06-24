"""Parser for Go test JSON output (go test -json).

Handles the test2json protocol actions: run, pause, cont, pass, fail, skip, output, bench.
Test IDs: package/TestName (e.g., "github.com/user/repo/pkg/TestFoo").
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from commit0.harness.constants import TestStatus

logger = logging.getLogger(__name__)


def parse_go_test_json(raw_output: str) -> Dict[str, TestStatus]:
    """Parse go test -json output into {test_id: TestStatus}."""
    results, _, _ = parse_go_test_json_with_durations(raw_output)
    return results


def parse_go_test_json_with_durations(
    raw_output: str,
) -> Tuple[Dict[str, TestStatus], Dict[str, float], Dict[str, float]]:
    """Parse go test -json into (results, durations, pkg_durations).

    Returns
    -------
        results: {test_id: TestStatus} keyed by package/TestName
        durations: {test_id: float} per-test elapsed seconds (integer-truncated by Go for sub-second tests)
        pkg_durations: {package: float} package-level elapsed seconds (precise, from ``go test -json``)

    """
    results: Dict[str, TestStatus] = {}
    durations: Dict[str, float] = {}
    pkg_durations: Dict[str, float] = {}
    running: Dict[str, bool] = {}  # tests that got "run" but no terminal action yet

    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON line: %s", line[:100])
            continue

        action = event.get("Action")
        package = event.get("Package", "")
        test = event.get("Test")
        elapsed = event.get("Elapsed")

        # Package-level events (no Test field)
        if test is None:
            if action == "fail" and package:
                for key, is_running in list(running.items()):
                    if is_running and key.startswith(package + "/"):
                        results[key] = TestStatus.ERROR
                        del running[key]
            # Capture package-level elapsed on pass or fail (precise timing)
            if action in ("pass", "fail") and package and elapsed is not None:
                pkg_durations[package] = elapsed
            continue

        test_id = f"{package}/{test}"

        if action == "run":
            running[test_id] = True
        elif action == "pass":
            results[test_id] = TestStatus.PASSED
            running.pop(test_id, None)
            if elapsed is not None:
                durations[test_id] = elapsed
        elif action == "fail":
            results[test_id] = TestStatus.FAILED
            running.pop(test_id, None)
            if elapsed is not None:
                durations[test_id] = elapsed
        elif action == "skip":
            results[test_id] = TestStatus.SKIPPED
            running.pop(test_id, None)
            if elapsed is not None:
                durations[test_id] = elapsed
        # pause/cont/output/bench are informational — no status change

    # Orphaned "run" events = crashed tests
    for test_id in running:
        if test_id not in results:
            results[test_id] = TestStatus.ERROR

    return results, durations, pkg_durations


def parse_go_test_plain(raw_output: str) -> Dict[str, TestStatus]:
    """Fallback parser for go test -v (non-JSON) output."""
    results: Dict[str, TestStatus] = {}
    current_package = ""

    for line in raw_output.splitlines():
        line = line.strip()

        # "ok  pkg  0.123s" or "FAIL\tpkg  0.123s"
        if line.startswith("ok ") or line.startswith("FAIL\t"):
            parts = line.split()
            if len(parts) >= 2:
                current_package = parts[1]
            continue

        # "--- PASS: TestFoo (0.00s)" / "--- FAIL: ..." / "--- SKIP: ..."
        if line.startswith("--- PASS:"):
            test_name = line.split(":", 1)[1].strip().split(" ")[0]
            test_id = f"{current_package}/{test_name}" if current_package else test_name
            results[test_id] = TestStatus.PASSED
        elif line.startswith("--- FAIL:"):
            test_name = line.split(":", 1)[1].strip().split(" ")[0]
            test_id = f"{current_package}/{test_name}" if current_package else test_name
            results[test_id] = TestStatus.FAILED
        elif line.startswith("--- SKIP:"):
            test_name = line.split(":", 1)[1].strip().split(" ")[0]
            test_id = f"{current_package}/{test_name}" if current_package else test_name
            results[test_id] = TestStatus.SKIPPED

    return results


def compute_go_pass_rate(
    results: Dict[str, TestStatus],
    expected_tests: Optional[List[str]] = None,
) -> float:
    """Compute pass rate. If expected_tests given, missing tests count as failures."""
    if expected_tests is not None:
        if not expected_tests:
            # Zero-collection (broken/failed run): fail closed to 0.0, never full
            # credit — matches evaluate_go.py:280 and the Python oracle (evaluate.py:269).
            return 0.0
        passed = sum(1 for t in expected_tests if results.get(t) == TestStatus.PASSED)
        return passed / len(expected_tests)

    if not results:
        return 0.0
    passed = sum(1 for s in results.values() if s == TestStatus.PASSED)
    return passed / len(results)


__all__: list = []
