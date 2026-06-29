import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, List

from commit0.harness.constants import TestStatus

logger = logging.getLogger(__name__)

__all__ = [
    "RustTestResult",
    "parse_nextest_json",
    "parse_libtest_text",
    "parse_test_output",
    "parse_nextest_report",
]

_EVENT_STATUS_MAP: Dict[str, TestStatus] = {
    "ok": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "ignored": TestStatus.SKIPPED,
    "timeout": TestStatus.ERROR,
}

# libtest stable output format (the default `cargo test` emits this).
# Examples:
#   test queue::tests::add_buffers ... ok
#   test transport::pci::tests::offset_device_ids ... FAILED
#   test some_ignored_test ... ignored
# We tolerate `--report-time` durations: `... ok <0.05s>` or `... ok (0.05s)`.
# `name` is non-greedy (`.+?`) so it also matches doctest lines whose name
# contains spaces, e.g. `test src/lib.rs - foo (line 12) ... ok`. The mandatory
# ` ... <outcome>` anchor keeps it from matching the `test result:` summary.
_LIBTEST_LINE_RE = re.compile(
    r"^test\s+(?P<name>.+?)\s+\.\.\.\s+(?P<outcome>ok|FAILED|ignored)"
    r"(?:\s+[<\(]\s*(?P<duration>[0-9]+(?:\.[0-9]+)?)s\s*[>\)])?\s*$"
)
# Final summary line:
#   test result: ok. 25 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out
_LIBTEST_SUMMARY_RE = re.compile(
    r"^test\s+result:.*?(?P<passed>\d+)\s+passed.*?(?P<failed>\d+)\s+failed.*?(?P<ignored>\d+)\s+ignored",
    re.IGNORECASE,
)

_LIBTEST_OUTCOME_MAP: Dict[str, TestStatus] = {
    "ok": TestStatus.PASSED,
    "FAILED": TestStatus.FAILED,
    "ignored": TestStatus.SKIPPED,
}


@dataclass
class RustTestResult:
    name: str
    status: TestStatus
    duration: float
    stdout: str


def parse_nextest_json(json_str: str) -> List[RustTestResult]:
    """Parse cargo-nextest's libtest-json output (one JSON object per line)."""
    results: List[RustTestResult] = []
    if not json_str or not json_str.strip():
        return results

    for line in json_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # In mixed-format streams (text output from `cargo test` plus stray JSON),
            # text lines outnumber JSON lines so we keep this at DEBUG. The unified
            # `parse_test_output` detects format first and avoids this path entirely
            # when input is libtest text.
            logger.debug("Skipping non-JSON line: %s", line[:200])
            continue

        if obj.get("type") != "test":
            continue

        event = obj.get("event")
        status = _EVENT_STATUS_MAP.get(event)  # type: ignore[arg-type]
        if status is None:
            continue

        results.append(
            RustTestResult(
                name=obj.get("name", ""),
                status=status,
                duration=float(obj.get("exec_time", 0.0)),
                stdout=obj.get("stdout", ""),
            )
        )

    return results


def parse_libtest_text(text: str) -> List[RustTestResult]:
    """Parse stable cargo/libtest text output.

    Handles the default `cargo test` output where each test result is a line
    of the form `test <name> ... (ok|FAILED|ignored)` and the run ends with a
    `test result: ok. N passed; M failed; K ignored; ...` summary line.

    Resilient to partial output: when the test process is killed mid-run by
    a timeout, the lines that *did* complete are still recovered."""
    results: List[RustTestResult] = []
    if not text:
        return results
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("test "):
            continue
        m = _LIBTEST_LINE_RE.match(line)
        if not m:
            continue
        outcome = m.group("outcome")
        status = _LIBTEST_OUTCOME_MAP.get(outcome)
        if status is None:
            continue
        dur_raw = m.group("duration")
        try:
            duration = float(dur_raw) if dur_raw else 0.0
        except ValueError:
            duration = 0.0
        results.append(
            RustTestResult(
                name=m.group("name"),
                status=status,
                duration=duration,
                stdout="",
            )
        )
    return results


def _detect_format(content: str) -> str:
    """Sniff the test-output format. Returns 'json', 'libtest', or 'unknown'."""
    if not content or not content.strip():
        return "unknown"
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("{"):
            return "json"
        if line.startswith("test ") or line.startswith("running "):
            return "libtest"
    return "unknown"


def parse_test_output(content: str) -> List[RustTestResult]:
    """Auto-detect format (JSON vs libtest text) and dispatch.

    Use this for cargo test output of unknown origin. Falls back to libtest
    text if format sniffing is ambiguous (text is the stable cargo default)."""
    fmt = _detect_format(content)
    if fmt == "json":
        results = parse_nextest_json(content)
        # Some test runs emit interleaved JSON + plain text (e.g. when --message-format=json
        # is combined with non-cargo wrappers). If JSON parsing yielded nothing, fall
        # back to libtest so partial signal is recoverable.
        if results:
            return results
        return parse_libtest_text(content)
    if fmt == "libtest":
        return parse_libtest_text(content)
    # Unknown: try both, prefer whichever returns non-empty.
    json_results = parse_nextest_json(content)
    if json_results:
        return json_results
    return parse_libtest_text(content)

def parse_nextest_report(report_path: str) -> Dict:
    empty: Dict = {
        "tests": [],
        "summary": {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0},
    }
    try:
        # cargo/test output frequently contains non-UTF8 bytes (panic payloads,
        # locale text, terminal escapes); replace rather than raise so one bad
        # byte can't abort aggregation of every remaining repo.
        with open(report_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        logger.error("Report file not readable: %s", report_path)
        return empty

    results = parse_test_output(content)
    if not results:
        return empty

    tests = [
        {"name": r.name, "outcome": r.status.value, "duration": r.duration}
        for r in results
    ]
    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r.status == TestStatus.PASSED),
        "failed": sum(1 for r in results if r.status == TestStatus.FAILED),
        "skipped": sum(1 for r in results if r.status == TestStatus.SKIPPED),
        "error": sum(1 for r in results if r.status == TestStatus.ERROR),
    }
    return {"tests": tests, "summary": summary}
