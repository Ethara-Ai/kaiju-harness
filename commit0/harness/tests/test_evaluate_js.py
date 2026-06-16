from __future__ import annotations

import json
from pathlib import Path

from commit0.harness.evaluate_js import _summarize_log_dir


def _write_results_json(log_dir: Path, payload: dict) -> None:
    (log_dir / "test_results.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_exit_codes(
    log_dir: Path,
    install_rc: int | None = None,
    syntax_rc: int | None = None,
    test_rc: int | None = None,
) -> None:
    if install_rc is not None:
        (log_dir / "install_exit_code.txt").write_text(str(install_rc))
    if syntax_rc is not None:
        (log_dir / "syntax_exit_code.txt").write_text(str(syntax_rc))
    if test_rc is not None:
        (log_dir / "test_exit_code.txt").write_text(str(test_rc))


class TestCompileVsTestDistinction:
    def test_infra_failure_yields_none_compile_and_tests(
        self, tmp_path: Path
    ) -> None:
        summary = _summarize_log_dir(tmp_path, "jest")
        assert summary["infra_failed"] is True
        assert summary["compile_failed"] is None
        assert summary["tests_failed"] is None

    def test_install_failure_marks_compile_failed_only(
        self, tmp_path: Path
    ) -> None:
        _write_exit_codes(tmp_path, install_rc=1, syntax_rc=0, test_rc=0)
        _write_results_json(
            tmp_path,
            {
                "numTotalTests": 0,
                "numPassedTests": 0,
                "numFailedTests": 0,
                "numPendingTests": 0,
                "testResults": [],
            },
        )
        summary = _summarize_log_dir(tmp_path, "jest")
        assert summary["compile_failed"] is True
        assert summary["tests_failed"] is False

    def test_syntax_failure_marks_compile_failed(
        self, tmp_path: Path
    ) -> None:
        _write_exit_codes(tmp_path, install_rc=0, syntax_rc=1, test_rc=0)
        _write_results_json(
            tmp_path,
            {
                "numTotalTests": 0,
                "numPassedTests": 0,
                "numFailedTests": 0,
                "numPendingTests": 0,
                "testResults": [],
            },
        )
        summary = _summarize_log_dir(tmp_path, "jest")
        assert summary["compile_failed"] is True

    def test_test_failure_only_marks_tests_failed_not_compile(
        self, tmp_path: Path
    ) -> None:
        _write_exit_codes(tmp_path, install_rc=0, syntax_rc=0, test_rc=1)
        _write_results_json(
            tmp_path,
            {
                "numTotalTests": 2,
                "numPassedTests": 1,
                "numFailedTests": 1,
                "numPendingTests": 0,
                "testResults": [
                    {
                        "name": "/repo/foo.test.js",
                        "assertionResults": [
                            {
                                "fullName": "foo > a",
                                "status": "failed",
                                "duration": 0,
                            },
                            {
                                "fullName": "foo > b",
                                "status": "passed",
                                "duration": 0,
                            },
                        ],
                    }
                ],
            },
        )
        summary = _summarize_log_dir(tmp_path, "jest")
        assert summary["compile_failed"] is False
        assert summary["tests_failed"] is True

    def test_compile_failed_with_test_rc_nonzero_not_double_attributed(
        self, tmp_path: Path
    ) -> None:
        _write_exit_codes(tmp_path, install_rc=1, syntax_rc=0, test_rc=2)
        _write_results_json(
            tmp_path,
            {
                "numTotalTests": 0,
                "numPassedTests": 0,
                "numFailedTests": 0,
                "numPendingTests": 0,
                "testResults": [],
            },
        )
        summary = _summarize_log_dir(tmp_path, "jest")
        assert summary["compile_failed"] is True
        assert summary["tests_failed"] is False

    def test_passing_run_yields_no_failures(self, tmp_path: Path) -> None:
        _write_exit_codes(tmp_path, install_rc=0, syntax_rc=0, test_rc=0)
        _write_results_json(
            tmp_path,
            {
                "numTotalTests": 2,
                "numPassedTests": 2,
                "numFailedTests": 0,
                "numPendingTests": 0,
                "testResults": [
                    {
                        "name": "/repo/foo.test.js",
                        "assertionResults": [
                            {
                                "fullName": "a",
                                "status": "passed",
                                "duration": 0,
                            },
                            {
                                "fullName": "b",
                                "status": "passed",
                                "duration": 0,
                            },
                        ],
                    }
                ],
            },
        )
        summary = _summarize_log_dir(tmp_path, "jest")
        assert summary["compile_failed"] is False
        assert summary["tests_failed"] is False
        assert summary["num_passed"] == 2
        assert summary["passed_rate"] == 1.0


class TestSummaryFieldsPresent:
    def test_all_expected_keys_returned(self, tmp_path: Path) -> None:
        summary = _summarize_log_dir(tmp_path, "vitest")
        for key in (
            "framework",
            "install_exit_code",
            "syntax_exit_code",
            "test_exit_code",
            "infra_failed",
            "compile_failed",
            "tests_failed",
            "num_passed",
            "num_failed",
            "num_skipped",
            "num_total",
            "duration_seconds",
            "passed_rate",
            "parse_error",
            "truncated",
            "raw_empty",
            "failed_tests",
        ):
            assert key in summary, f"missing key {key!r}"
