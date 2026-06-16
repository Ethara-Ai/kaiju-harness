"""Unified JavaScript test report parser.

Handles Jest JSON, Vitest JSON, Mocha JSON, and Node ``--test-reporter=tap``
output. Each parser returns a single :class:`JsTestResult` dataclass.

JSON parsing is wrapped in ``try/except json.JSONDecodeError`` with a regex
fallback for truncated/malformed reports — analogous to the XML-truncation
handling in :mod:`commit0.harness.c_test_parser`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from commit0.harness.constants_js import SUPPORTED_TEST_FRAMEWORKS

logger = logging.getLogger(__name__)


class JsTestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class JsTestResult:
    framework: str
    statuses: dict[str, JsTestStatus] = field(default_factory=dict)
    duration_seconds: float = 0.0
    truncated: bool = False
    parse_error: str | None = None
    raw_empty: bool = False

    @property
    def num_total(self) -> int:
        return len(self.statuses)

    @property
    def num_passed(self) -> int:
        return sum(1 for s in self.statuses.values() if s == JsTestStatus.PASSED)

    @property
    def num_failed(self) -> int:
        return sum(
            1
            for s in self.statuses.values()
            if s in (JsTestStatus.FAILED, JsTestStatus.ERROR)
        )

    @property
    def num_skipped(self) -> int:
        return sum(1 for s in self.statuses.values() if s == JsTestStatus.SKIPPED)

    def summary(self) -> dict[str, int]:
        return {
            "passed": self.num_passed,
            "failed": self.num_failed,
            "skipped": self.num_skipped,
            "total": self.num_total,
        }


_JEST_VITEST_STATUS_MAP: dict[str, JsTestStatus] = {
    "passed": JsTestStatus.PASSED,
    "pass": JsTestStatus.PASSED,
    "failed": JsTestStatus.FAILED,
    "fail": JsTestStatus.FAILED,
    "pending": JsTestStatus.SKIPPED,
    "skipped": JsTestStatus.SKIPPED,
    "todo": JsTestStatus.SKIPPED,
    "disabled": JsTestStatus.SKIPPED,
    "focused": JsTestStatus.PASSED,
}

_MOCHA_STATE_MAP: dict[str, JsTestStatus] = {
    "passed": JsTestStatus.PASSED,
    "failed": JsTestStatus.FAILED,
    "pending": JsTestStatus.SKIPPED,
    "skipped": JsTestStatus.SKIPPED,
}

_EMPTY_MARKER = "EMPTY_RESULTS"


def parse_js_test_output(report_path: Path, framework: str) -> JsTestResult:
    """Dispatch to the framework-specific parser.

    ``framework`` must be one of :data:`SUPPORTED_TEST_FRAMEWORKS`. Returns a
    :class:`JsTestResult` even when the file is missing, empty, or corrupt —
    callers inspect ``parse_error``, ``truncated``, and ``raw_empty`` to
    distinguish failure modes from a real all-fail run.
    """
    if framework not in SUPPORTED_TEST_FRAMEWORKS:
        raise ValueError(
            f"unknown JS test framework: {framework!r}; "
            f"valid: {sorted(SUPPORTED_TEST_FRAMEWORKS)}"
        )

    if not report_path.exists():
        return JsTestResult(
            framework=framework, parse_error=f"report missing: {report_path}"
        )

    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JsTestResult(framework=framework, parse_error=f"read error: {exc}")

    if not text.strip() or text.strip() == _EMPTY_MARKER:
        return JsTestResult(framework=framework, raw_empty=True)

    if framework == "jest":
        return _parse_jest(text)
    if framework == "vitest":
        return _parse_vitest(text)
    if framework == "mocha":
        return _parse_mocha(text)
    return _parse_node_test_tap(text)


def _parse_jest(text: str) -> JsTestResult:
    result = JsTestResult(framework="jest")
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Jest JSON parse failed (%s); falling back to regex", exc)
        result.truncated = True
        result.parse_error = str(exc)
        _regex_assertion_fallback(text, result)
        return result

    _harvest_jest_vitest_assertions(report, result)
    return result


def _parse_vitest(text: str) -> JsTestResult:
    result = JsTestResult(framework="vitest")
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Vitest JSON parse failed (%s); falling back to regex", exc)
        result.truncated = True
        result.parse_error = str(exc)
        _regex_assertion_fallback(text, result)
        return result

    _harvest_jest_vitest_assertions(report, result)
    return result


def _harvest_jest_vitest_assertions(report: object, result: JsTestResult) -> None:
    if not isinstance(report, dict):
        result.parse_error = (
            result.parse_error or f"unexpected report root: {type(report).__name__}"
        )
        return

    durations_ms: list[float] = []
    test_results = report.get("testResults") or []
    if not isinstance(test_results, list):
        test_results = []

    for suite in test_results:
        if not isinstance(suite, dict):
            continue
        suite_name = str(suite.get("name") or suite.get("testFilePath") or "")
        assertions = suite.get("assertionResults") or []
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            raw_status = str(assertion.get("status", "failed"))
            mapped = _JEST_VITEST_STATUS_MAP.get(raw_status, JsTestStatus.FAILED)
            full_name = str(assertion.get("fullName") or assertion.get("title") or "")
            key = (
                f"{suite_name}::{full_name}" if suite_name and full_name else full_name
            )
            if not key:
                key = f"<unnamed-{len(result.statuses)}>"
            result.statuses[key] = mapped
            try:
                durations_ms.append(float(assertion.get("duration") or 0))
            except (TypeError, ValueError):
                continue

    if durations_ms:
        result.duration_seconds = sum(durations_ms) / 1000.0


def _parse_mocha(text: str) -> JsTestResult:
    result = JsTestResult(framework="mocha")
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Mocha JSON parse failed (%s); falling back to regex", exc)
        result.truncated = True
        result.parse_error = str(exc)
        _regex_assertion_fallback(text, result)
        return result

    if not isinstance(report, dict):
        result.parse_error = f"unexpected mocha root: {type(report).__name__}"
        return result

    durations_ms: list[float] = []
    for bucket_key in ("passes", "failures", "pending", "tests"):
        bucket = report.get(bucket_key) or []
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            full_title = str(
                entry.get("fullTitle") or entry.get("title") or entry.get("file") or ""
            )
            if not full_title:
                full_title = f"<unnamed-{len(result.statuses)}>"
            state_raw = str(entry.get("state") or "")
            if state_raw:
                status = _MOCHA_STATE_MAP.get(state_raw, JsTestStatus.FAILED)
            elif bucket_key == "passes":
                status = JsTestStatus.PASSED
            elif bucket_key == "failures":
                status = JsTestStatus.FAILED
            elif bucket_key == "pending":
                status = JsTestStatus.SKIPPED
            else:
                continue
            existing = result.statuses.get(full_title)
            if (
                existing is None
                or status == JsTestStatus.FAILED
                or existing == JsTestStatus.SKIPPED
            ):
                result.statuses[full_title] = status
            try:
                durations_ms.append(float(entry.get("duration") or 0))
            except (TypeError, ValueError):
                continue

    stats = report.get("stats") or {}
    if isinstance(stats, dict):
        try:
            stat_duration = float(stats.get("duration") or 0)
        except (TypeError, ValueError):
            stat_duration = 0.0
        if stat_duration > 0:
            result.duration_seconds = stat_duration / 1000.0
        elif durations_ms:
            result.duration_seconds = sum(durations_ms) / 1000.0
    elif durations_ms:
        result.duration_seconds = sum(durations_ms) / 1000.0

    return result


_TAP_TEST_LINE_RE = re.compile(
    r"^(ok|not ok)\s+(\d+)\s+-\s+(.+?)(?:\s+#\s+(SKIP|TODO)\b.*)?$"
)
_TAP_DURATION_RE = re.compile(r"^\s*duration_ms:\s*([0-9.]+)\s*$", re.IGNORECASE)


def _parse_node_test_tap(text: str) -> JsTestResult:
    result = JsTestResult(framework="node_test")
    duration_ms_total = 0.0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        match = _TAP_TEST_LINE_RE.match(stripped)
        if match:
            if indent > 0:
                continue
            verdict, _index, raw_name, directive = match.groups()
            name = raw_name.strip()
            if directive:
                status = JsTestStatus.SKIPPED
            elif verdict == "ok":
                status = JsTestStatus.PASSED
            else:
                status = JsTestStatus.FAILED
            key = name or f"<unnamed-{len(result.statuses)}>"
            result.statuses[key] = status
            continue
        dur = _TAP_DURATION_RE.match(stripped)
        if dur:
            try:
                duration_ms_total += float(dur.group(1))
            except ValueError:
                continue

    result.duration_seconds = duration_ms_total / 1000.0
    if not result.statuses:
        result.parse_error = "no TAP test lines matched"
    return result


_ASSERTION_FALLBACK_RE = re.compile(
    r'"(?:status|state)"\s*:\s*"(passed|failed|pass|fail|pending|skipped|todo|disabled)"',
    re.IGNORECASE,
)


def _regex_assertion_fallback(text: str, result: JsTestResult) -> None:
    for index, match in enumerate(_ASSERTION_FALLBACK_RE.finditer(text)):
        raw = match.group(1).lower()
        mapped = _JEST_VITEST_STATUS_MAP.get(raw, JsTestStatus.FAILED)
        result.statuses[f"<regex-{index}>"] = mapped


__all__ = [
    "JsTestResult",
    "JsTestStatus",
    "parse_js_test_output",
]
