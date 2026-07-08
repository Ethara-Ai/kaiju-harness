"""Parser for Go test JSON output (go test -json).

Handles the test2json protocol actions: run, pause, cont, pass, fail, skip, output, bench.
Test IDs: package/TestName (e.g., "github.com/user/repo/pkg/TestFoo").
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from commit0.harness.constants import TestStatus

logger = logging.getLogger(__name__)

# Terminal statuses that a later event must NOT be able to downgrade. This is the
# core of the forged-output (reward-hack) defense: the model's IMPLEMENTATION code
# runs during `go test` and can print `--- PASS: TestFoo` / `=== RUN` / `--- SKIP:`
# lines that test2json converts into terminal test events. A forged `pass` (or
# `skip`) printed AFTER a real `fail` must not overwrite the real fail.
#
# Precedence rule (fail-wins / sticky-fail):
#   * FAILED and ERROR are STICKY and DOMINANT: once a test_id is FAILED/ERROR, a
#     later `pass` or `skip` is IGNORED; a later `fail`/`error` ALWAYS wins (so a
#     real fail printed AFTER a forged pass also flips it back to FAILED).
#   * Among PASSED/SKIPPED (neither is a failure), once PASSED do not downgrade to
#     SKIPPED — a test that really ran and passed then gets a forged `skip` stays
#     PASSED.
#
# Zero false positives: `go test` runs with `-count=1` (no retries), so a
# legitimate run NEVER emits both a real fail and a real pass for the same
# test_id. A real failing canonical test always emits its real `fail` event
# (the model can only ADD a forged pass, it cannot SUPPRESS the real fail), so
# sticky-fail neutralizes the forgery while never mis-scoring a legit run.
_STICKY_FAIL = (TestStatus.FAILED, TestStatus.ERROR)


def _apply_status(
    results: Dict[str, TestStatus],
    test_id: str,
    new_status: TestStatus,
) -> bool:
    """Apply *new_status* to *test_id* under the fail-wins precedence rule.

    Returns True if the stored status was written/updated (so callers may record
    a duration), False if the new event was suppressed by an existing terminal
    status.
    """
    prev = results.get(test_id)

    # fail/error always win — even after a (possibly forged) pass.
    if new_status in _STICKY_FAIL:
        results[test_id] = new_status
        return True

    # new_status is PASSED or SKIPPED (a non-failure).
    # Never let a non-failure override a sticky FAILED/ERROR.
    if prev in _STICKY_FAIL:
        return False

    # Among pass/skip: don't downgrade a real PASSED to SKIPPED (forged skip).
    if prev == TestStatus.PASSED and new_status == TestStatus.SKIPPED:
        return False

    results[test_id] = new_status
    return True


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
                        # A still-running test when its package fails crashed;
                        # ERROR is sticky/dominant so this cannot be overwritten.
                        _apply_status(results, key, TestStatus.ERROR)
                        del running[key]
            # Capture package-level elapsed on pass or fail (precise timing)
            if action in ("pass", "fail") and package and elapsed is not None:
                pkg_durations[package] = elapsed
            continue

        test_id = f"{package}/{test}"

        if action == "run":
            running[test_id] = True
        elif action in ("pass", "fail", "skip"):
            status = {
                "pass": TestStatus.PASSED,
                "fail": TestStatus.FAILED,
                "skip": TestStatus.SKIPPED,
            }[action]
            wrote = _apply_status(results, test_id, status)
            running.pop(test_id, None)
            # Only record the elapsed of the event we actually accepted, so a
            # forged pass's duration can't overwrite the real fail's timing.
            if wrote and elapsed is not None:
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
        # Same fail-wins / sticky-fail precedence as the JSON parser: a forged
        # "--- PASS:"/"--- SKIP:" line printed after a real "--- FAIL:" must not
        # override it.
        if line.startswith("--- PASS:"):
            test_name = line.split(":", 1)[1].strip().split(" ")[0]
            test_id = f"{current_package}/{test_name}" if current_package else test_name
            _apply_status(results, test_id, TestStatus.PASSED)
        elif line.startswith("--- FAIL:"):
            test_name = line.split(":", 1)[1].strip().split(" ")[0]
            test_id = f"{current_package}/{test_name}" if current_package else test_name
            _apply_status(results, test_id, TestStatus.FAILED)
        elif line.startswith("--- SKIP:"):
            test_name = line.split(":", 1)[1].strip().split(" ")[0]
            test_id = f"{current_package}/{test_name}" if current_package else test_name
            _apply_status(results, test_id, TestStatus.SKIPPED)

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
