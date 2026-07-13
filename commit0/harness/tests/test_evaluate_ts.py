"""Tests for commit0.harness.evaluate_ts — Jest/Vitest report parsing and
evaluation orchestration.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE = "commit0.harness.evaluate_ts"


# ---------------------------------------------------------------------------
# parse_jest_vitest_report
# ---------------------------------------------------------------------------


class TestParseJestVitestReport:
    def test_all_passed(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "test 1", "duration": 100},
                        {"status": "passed", "fullName": "test 2", "duration": 200},
                    ]
                }
            ]
        }
        counter, duration = parse_jest_vitest_report(report, ["test 1", "test 2"])
        assert counter["passed"] == 2
        assert counter.get("failed", 0) == 0
        assert duration == pytest.approx(0.3)

    def test_mixed_results(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "test 1", "duration": 50},
                        {"status": "failed", "fullName": "test 2", "duration": 75},
                        {"status": "pending", "fullName": "test 3", "duration": 0},
                    ]
                }
            ]
        }
        counter, duration = parse_jest_vitest_report(
            report, ["test 1", "test 2", "test 3"]
        )
        assert counter["passed"] == 1
        assert counter["failed"] == 1
        assert counter["skipped"] == 1
        assert duration == pytest.approx(0.125)

    def test_missing_test_ids_counted_as_failed(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "test 1", "duration": 10},
                    ]
                }
            ]
        }
        test_ids = ["test 1", "test 2", "file.ts > describe > test 3"]
        counter, _ = parse_jest_vitest_report(report, test_ids)
        assert counter["passed"] == 1
        assert counter["failed"] == 2

    def test_bare_name_matching(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "should work", "duration": 10},
                    ]
                }
            ]
        }
        test_ids = ["file.ts > should work"]
        counter, _ = parse_jest_vitest_report(report, test_ids)
        assert counter.get("failed", 0) == 0

    def test_empty_report(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {"testResults": []}
        counter, duration = parse_jest_vitest_report(report, [])
        assert sum(counter.values()) == 0
        assert duration == 0.0

    def test_empty_report_with_test_ids(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {"testResults": []}
        counter, _ = parse_jest_vitest_report(report, ["test 1", "test 2"])
        assert counter["failed"] == 2

    def test_skips_empty_test_ids(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {"testResults": []}
        counter, _ = parse_jest_vitest_report(report, ["", "", "real test"])
        assert counter["failed"] == 1

    def test_status_mapping(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "todo", "fullName": "a", "duration": 0},
                        {"status": "disabled", "fullName": "b", "duration": 0},
                        {"status": "focused", "fullName": "c", "duration": 0},
                        {"status": "skipped", "fullName": "d", "duration": 0},
                    ]
                }
            ]
        }
        counter, _ = parse_jest_vitest_report(report, ["a", "b", "c", "d"])
        assert counter["skipped"] == 3
        assert counter["passed"] == 1

    def test_unknown_status_maps_to_failed(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "broken", "fullName": "a", "duration": 10},
                    ]
                }
            ]
        }
        counter, _ = parse_jest_vitest_report(report, ["a"])
        assert counter["failed"] == 1

    def test_missing_duration_defaults_to_zero(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "a"},
                    ]
                }
            ]
        }
        _, duration = parse_jest_vitest_report(report, [])
        assert duration == 0.0

    def test_multiple_test_results(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": "suite1 > test1",
                            "duration": 100,
                        },
                    ]
                },
                {
                    "assertionResults": [
                        {
                            "status": "failed",
                            "fullName": "suite2 > test2",
                            "duration": 200,
                        },
                    ]
                },
            ]
        }
        counter, duration = parse_jest_vitest_report(
            report, ["suite1 > test1", "suite2 > test2"]
        )
        assert counter["passed"] == 1
        assert counter["failed"] == 1
        assert duration == pytest.approx(0.3)

    def test_missing_fullname(self) -> None:
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "duration": 10},
                    ]
                }
            ]
        }
        # The assertion has no fullName, so it can never match a canonical
        # test_id and is ignored. "my test" therefore has no matching
        # assertion and is scored failed.
        counter, _ = parse_jest_vitest_report(report, ["my test"])
        assert counter.get("passed", 0) == 0
        assert counter["failed"] == 1


# ---------------------------------------------------------------------------
# STATUS_MAP
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# evaluate_ts.main — orchestration
# ---------------------------------------------------------------------------


class TestEvaluateTsMain:
    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["test1", "test2"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_repos_matched_returns_early(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        main(
            dataset_name="test.json",
            dataset_split="test",
            repo_split="nonexistent-repo",
            base_dir="/repos",
            branch="commit0",
            backend="local",
            timeout=300,
            num_cpus=1,
            num_workers=1,
            rebuild_image=False,
        )

        mock_run.assert_not_called()

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_all_split_processes_everything(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["repo-a", "repo-b"])

        with patch(f"{MODULE}.get_active_branch", return_value="commit0"):
            with patch("os.path.exists", return_value=False):
                main(
                    dataset_name="test.json",
                    dataset_split="test",
                    repo_split="all",
                    base_dir="/repos",
                    branch=None,
                    backend="local",
                    timeout=300,
                    num_cpus=1,
                    num_workers=1,
                    rebuild_image=False,
                )

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["test1"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_missing_report_handled(self, mock_load, mock_ids, mock_run) -> None:
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        with patch("os.path.exists", return_value=False):
            main(
                dataset_name="test.json",
                dataset_split="test",
                repo_split="all",
                base_dir="/repos",
                branch="commit0",
                backend="local",
                timeout=300,
                num_cpus=1,
                num_workers=1,
                rebuild_image=False,
            )


# ---------------------------------------------------------------------------
# STATUS_MAP
# ---------------------------------------------------------------------------


class TestHyphenUnderscoreNormalization:
    """Lines 114-115: when repo_split is a repo name (not in TS_SPLIT),
    matching normalizes hyphens/underscores.
    """

    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_hyphen_underscore_match(self, mock_load, mock_ids, mock_run) -> None:
        """repo_split='my_repo' should match dataset repo named 'my-repo'."""
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        with patch("os.path.exists", return_value=False):
            main(
                dataset_name="test.json",
                dataset_split="test",
                repo_split="my_repo",
                base_dir="/repos",
                branch="commit0",
                backend="local",
                timeout=300,
                num_cpus=1,
                num_workers=1,
                rebuild_image=False,
            )

        # run_ts_tests should have been called — the repo matched via normalization
        mock_run.assert_called_once()

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_underscore_hyphen_match(self, mock_load, mock_ids, mock_run) -> None:
        """repo_split='my-repo' should match dataset repo named 'my_repo'."""
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my_repo"])

        with patch("os.path.exists", return_value=False):
            main(
                dataset_name="test.json",
                dataset_split="test",
                repo_split="my-repo",
                base_dir="/repos",
                branch="commit0",
                backend="local",
                timeout=300,
                num_cpus=1,
                num_workers=1,
                rebuild_image=False,
            )

        mock_run.assert_called_once()

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_match_when_names_differ(self, mock_load, mock_ids, mock_run) -> None:
        """repo_split='other-repo' should NOT match 'my-repo'."""
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        main(
            dataset_name="test.json",
            dataset_split="test",
            repo_split="other-repo",
            base_dir="/repos",
            branch="commit0",
            backend="local",
            timeout=300,
            num_cpus=1,
            num_workers=1,
            rebuild_image=False,
        )

        mock_run.assert_not_called()


class TestFutureErrorHandling:
    """Lines 180-188: SystemExit(2) gets a warning, generic Exception gets logger.error."""

    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_system_exit_2_logs_warning(self, mock_load, mock_ids) -> None:
        """SystemExit with code != 0 or 1 should log a warning, not crash."""
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        def _raise_system_exit(*args, **kwargs):
            raise SystemExit(2)

        with patch(f"{MODULE}.run_ts_tests", side_effect=_raise_system_exit):
            with patch("os.path.exists", return_value=False):
                with patch(f"{MODULE}.logger") as mock_logger:
                    main(
                        dataset_name="test.json",
                        dataset_split="test",
                        repo_split="all",
                        base_dir="/repos",
                        branch="commit0",
                        backend="local",
                        timeout=300,
                        num_cpus=1,
                        num_workers=1,
                        rebuild_image=False,
                    )

        mock_logger.warning.assert_any_call(
            "Evaluation for %s exited with code %s (possible OOM or infra failure)",
            "commit-0/my-repo",
            2,
        )

    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_system_exit_0_no_warning(self, mock_load, mock_ids) -> None:
        """SystemExit with code 0 should NOT log a warning."""
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        def _raise_system_exit(*args, **kwargs):
            raise SystemExit(0)

        with patch(f"{MODULE}.run_ts_tests", side_effect=_raise_system_exit):
            with patch("os.path.exists", return_value=False):
                with patch(f"{MODULE}.logger") as mock_logger:
                    main(
                        dataset_name="test.json",
                        dataset_split="test",
                        repo_split="all",
                        base_dir="/repos",
                        branch="commit0",
                        backend="local",
                        timeout=300,
                        num_cpus=1,
                        num_workers=1,
                        rebuild_image=False,
                    )

        # warning should not have been called with the "exited with code" message
        for c in mock_logger.warning.call_args_list:
            if len(c.args) >= 3:
                assert c.args[2] != 0 or "exited with code" not in str(c.args[0])

    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_generic_exception_logs_error(self, mock_load, mock_ids) -> None:
        """A generic Exception from a future should log an error."""
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        def _raise_runtime(*args, **kwargs):
            raise RuntimeError("npm install failed")

        with patch(f"{MODULE}.run_ts_tests", side_effect=_raise_runtime):
            with patch("os.path.exists", return_value=False):
                with patch(f"{MODULE}.logger") as mock_logger:
                    main(
                        dataset_name="test.json",
                        dataset_split="test",
                        repo_split="all",
                        base_dir="/repos",
                        branch="commit0",
                        backend="local",
                        timeout=300,
                        num_cpus=1,
                        num_workers=1,
                        rebuild_image=False,
                    )

        # Should have called logger.error with exc_info=True
        assert mock_logger.error.called
        error_call = mock_logger.error.call_args
        assert error_call[1].get("exc_info") is True


class TestReportParsingAndPassRate:
    """Lines 221-237: report parsing via parse_jest_vitest_report, num_total
    calculation, and pass-rate computation.
    """

    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["test1"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_report_parsing_pass_rate(self, mock_load, mock_ids, mock_run) -> None:
        """When report.json exists, parse_jest_vitest_report is used and
        num_total = max(report_count, len(test_ids)).
        """
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        report_data = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "test1", "duration": 100},
                        {"status": "passed", "fullName": "test2", "duration": 200},
                        {"status": "failed", "fullName": "test3", "duration": 50},
                    ]
                }
            ]
        }

        def _exists_side_effect(path):
            if "report.json" in str(path):
                return True
            return False

        captured_out = []

        with patch(f"{MODULE}.run_ts_tests"):
            with patch("os.path.exists", side_effect=_exists_side_effect):
                with patch("builtins.open", MagicMock()):
                    with patch("json.load", return_value=report_data):
                        with patch(
                            "builtins.print",
                            side_effect=lambda *a, **kw: captured_out.append(str(a)),
                        ):
                            main(
                                dataset_name="test.json",
                                dataset_split="test",
                                repo_split="all",
                                base_dir="/repos",
                                branch="commit0",
                                backend="local",
                                timeout=300,
                                num_cpus=1,
                                num_workers=1,
                                rebuild_image=False,
                            )

        # parse_jest_vitest_report is canonical-only: only report assertions
        # whose fullName matches a test_id (or its bare name) are counted, and
        # num_total = len(test_ids). test_ids has 1 entry ("test1"); test2/test3
        # in the report are agent-added and ignored (anti-cheat). So test1
        # passed => num_passed=1, num_total=1 => "1/1".
        output_str = " ".join(captured_out)
        assert "1/1" in output_str

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["t1", "t2", "t3", "t4", "t5"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_num_total_uses_max_of_report_and_test_ids(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        """num_total should be max(report_count, len(test_ids)) — when test_ids
        is larger than the report count, test_ids count wins.
        """
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        # Only 2 assertion results in report, but 5 test_ids
        report_data = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "t1", "duration": 10},
                        {"status": "passed", "fullName": "t2", "duration": 20},
                    ]
                }
            ]
        }

        # t3, t4, t5 missing from report => counted as failed
        # status_counter: passed=2, failed=3 => sum=5
        # num_total = max(5, 5) = 5, num_passed = 2

        def _exists_side_effect(path):
            return "report.json" in str(path)

        captured_out = []

        with patch("os.path.exists", side_effect=_exists_side_effect):
            with patch("builtins.open", MagicMock()):
                with patch("json.load", return_value=report_data):
                    with patch(
                        "builtins.print",
                        side_effect=lambda *a, **kw: captured_out.append(str(a)),
                    ):
                        main(
                            dataset_name="test.json",
                            dataset_split="test",
                            repo_split="all",
                            base_dir="/repos",
                            branch="commit0",
                            backend="local",
                            timeout=300,
                            num_cpus=1,
                            num_workers=1,
                            rebuild_image=False,
                        )

        output_str = " ".join(captured_out)
        assert "2/5" in output_str


class TestStatusMap:
    def test_all_expected_keys(self) -> None:
        from commit0.harness.evaluate_ts import STATUS_MAP

        expected = {
            "passed",
            "failed",
            "pending",
            "skipped",
            "todo",
            "disabled",
            "focused",
        }
        assert set(STATUS_MAP.keys()) == expected

    def test_values_are_normalized(self) -> None:
        from commit0.harness.evaluate_ts import STATUS_MAP

        for v in STATUS_MAP.values():
            assert v in {"passed", "failed", "skipped"}


# ---------------------------------------------------------------------------
# parse_jest_vitest_report — additional edge cases
# ---------------------------------------------------------------------------


class TestParseJestVitestReportEdgeCases:
    """Additional coverage for duplicate fullNames, bare_name mismatch,
    and num_total logic.
    """

    def test_duplicate_full_names_in_report(self) -> None:
        """When the same fullName appears twice in the report, the single
        matching test_id is scored once. The parser upgrades failed->passed
        for a duplicated name, so the test_id is scored ``passed``. Both
        assertions' durations are still accumulated.
        """
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "dup test", "duration": 10},
                        {"status": "failed", "fullName": "dup test", "duration": 20},
                    ]
                }
            ]
        }
        counter, duration = parse_jest_vitest_report(report, ["dup test"])
        assert counter["passed"] == 1
        assert counter.get("failed", 0) == 0
        assert duration == pytest.approx(0.03)

    def test_duplicate_full_names_not_double_counted_as_missing(self) -> None:
        """A test_id whose bare_name matches a duplicated fullName should
        NOT be counted as missing/failed.
        """
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "my test", "duration": 10},
                        {"status": "passed", "fullName": "my test", "duration": 10},
                    ]
                }
            ]
        }
        test_ids = ["file.ts > my test"]
        counter, _ = parse_jest_vitest_report(report, test_ids)
        # One test_id whose bare name matches the (duplicated) fullName is
        # scored once as passed; it is NOT counted as missing/failed.
        assert counter["passed"] == 1
        assert counter.get("failed", 0) == 0

    def test_bare_name_matches_but_full_tid_does_not(self) -> None:
        """When tid has ' > ' separator, the bare_name (after split) is
        checked.  If bare_name matches a fullName, the tid is NOT counted
        as missing even though the full tid string doesn't match.
        """
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": "should add numbers",
                            "duration": 5,
                        },
                    ]
                }
            ]
        }
        test_ids = ["math.test.ts > should add numbers"]
        counter, _ = parse_jest_vitest_report(report, test_ids)
        assert counter["passed"] == 1
        assert counter.get("failed", 0) == 0

    def test_bare_name_no_match_and_full_tid_no_match(self) -> None:
        """When bare_name doesn't match AND full tid doesn't match, the
        tid is counted as failed.
        """
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": "something else",
                            "duration": 5,
                        },
                    ]
                }
            ]
        }
        test_ids = ["file.ts > totally different"]
        counter, _ = parse_jest_vitest_report(report, test_ids)
        # "something else" is not a canonical test_id, so it is ignored
        # (anti-cheat). The one test_id has no match => scored failed.
        assert counter.get("passed", 0) == 0
        assert counter["failed"] == 1

    def test_tid_without_separator_checks_fullname_directly(self) -> None:
        """When a tid has no ' > ' separator, it's checked directly against
        seen_full_names (bare_name == tid in this case).
        """
        from commit0.harness.evaluate_ts import parse_jest_vitest_report

        report = {
            "testResults": [
                {
                    "assertionResults": [
                        {
                            "status": "passed",
                            "fullName": "standalone test",
                            "duration": 5,
                        },
                    ]
                }
            ]
        }
        test_ids = ["standalone test"]
        counter, _ = parse_jest_vitest_report(report, test_ids)
        assert counter["passed"] == 1
        assert counter.get("failed", 0) == 0


class TestNumTotalLogic:
    """Test the num_total calculation in main():
    report_test_count = sum(status_counter.values())
    num_total = report_test_count if report_test_count > 0 else len(test_ids)
    """

    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["t1", "t2", "t3", "t4", "t5"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_report_assertions_plus_phantom_failures(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        """Report has 3 assertions, test_ids has 5 entries (2 not in report).
        status_counter = passed:3 + failed:2(phantom) = 5 total.
        report_test_count = 5, so num_total = 5. num_passed = 3.
        """
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        report_data = {
            "testResults": [
                {
                    "assertionResults": [
                        {"status": "passed", "fullName": "t1", "duration": 10},
                        {"status": "passed", "fullName": "t2", "duration": 10},
                        {"status": "passed", "fullName": "t3", "duration": 10},
                    ]
                }
            ]
        }

        def _exists_side_effect(path):
            return "report.json" in str(path)

        captured_out = []

        with patch("os.path.exists", side_effect=_exists_side_effect):
            with patch("builtins.open", MagicMock()):
                with patch("json.load", return_value=report_data):
                    with patch(
                        "builtins.print",
                        side_effect=lambda *a, **kw: captured_out.append(str(a)),
                    ):
                        main(
                            dataset_name="test.json",
                            dataset_split="test",
                            repo_split="all",
                            base_dir="/repos",
                            branch="commit0",
                            backend="local",
                            timeout=300,
                            num_cpus=1,
                            num_workers=1,
                            rebuild_image=False,
                        )

        output_str = " ".join(captured_out)
        assert "3/5" in output_str

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["t1", "t2", "t3"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_zero_report_assertions_falls_back_to_test_ids(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        """When report has zero assertions (e.g. collection error), num_total
        falls back to len(test_ids).
        """
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        report_data = {"testResults": []}

        def _exists_side_effect(path):
            return "report.json" in str(path)

        captured_out = []

        with patch("os.path.exists", side_effect=_exists_side_effect):
            with patch("builtins.open", MagicMock()):
                with patch("json.load", return_value=report_data):
                    with patch(
                        "builtins.print",
                        side_effect=lambda *a, **kw: captured_out.append(str(a)),
                    ):
                        main(
                            dataset_name="test.json",
                            dataset_split="test",
                            repo_split="all",
                            base_dir="/repos",
                            branch="commit0",
                            backend="local",
                            timeout=300,
                            num_cpus=1,
                            num_workers=1,
                            rebuild_image=False,
                        )

        output_str = " ".join(captured_out)
        assert "0/3" in output_str


class TestDisplayNameExtraction:
    """Test display_name = name.split('/')[2] with edge cases."""

    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_display_name_short_path_fallback(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        """When log_dir path has fewer than 3 '/' segments,
        display_name falls back to the full path string.
        """
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        captured_out = []

        short_log_dir = Path("ab")
        with patch(f"{MODULE}.RUN_TS_TEST_LOG_DIR", short_log_dir):
            with patch(f"{MODULE}.get_hash_string", return_value="h"):
                with patch("os.path.exists", return_value=False):
                    with patch(
                        "builtins.print",
                        side_effect=lambda *a, **kw: captured_out.append(str(a)),
                    ):
                        main(
                            dataset_name="test.json",
                            dataset_split="test",
                            repo_split="all",
                            base_dir="/repos",
                            branch="commit0",
                            backend="local",
                            timeout=300,
                            num_cpus=1,
                            num_workers=1,
                            rebuild_image=False,
                        )

        output_str = " ".join(captured_out)
        assert "commit0" in output_str


class TestAllTsSplit:
    """Test that repo_split='all_ts' processes all repos via the TS_SPLIT lookup."""

    def _make_dataset(self, repo_names: list[str]):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"commit-0/{name}",
                "base_commit": "abc123",
                "reference_commit": "def456",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[[]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_all_ts_split_processes_all_repos(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        """repo_split='all_ts' is in TS_SPLIT with empty list, meaning
        split_repos is empty → the `split_repos and repo_name not in split_repos`
        check short-circuits to False → all repos are included.
        """
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["repo-a", "repo-b", "repo-c"])

        with patch("os.path.exists", return_value=False):
            with patch("builtins.print"):
                main(
                    dataset_name="test.json",
                    dataset_split="test",
                    repo_split="all_ts",
                    base_dir="/repos",
                    branch="commit0",
                    backend="local",
                    timeout=300,
                    num_cpus=1,
                    num_workers=1,
                    rebuild_image=False,
                )

        assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# Corrupt report.json handling
# ---------------------------------------------------------------------------


class TestCorruptReportJson:
    """evaluate_ts.main must survive a truncated/invalid report.json without
    crashing the entire evaluation loop.
    """

    @staticmethod
    def _make_dataset(repo_names):
        return [
            {
                "repo": f"org/{name}",
                "instance_id": f"org/{name}",
                "test": {"test_dir": "__tests__", "test_cmd": "npx jest"},
                "setup": {"node": "20", "install": "npm install"},
            }
            for name in repo_names
        ]

    @patch(f"{MODULE}.run_ts_tests")
    @patch(f"{MODULE}.get_ts_test_ids", return_value=[["a.test.ts > test one"]])
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_corrupt_report_json_treated_as_zero_pass(
        self, mock_load, mock_ids, mock_run
    ) -> None:
        import json as _json
        from commit0.harness.evaluate_ts import main

        mock_load.return_value = self._make_dataset(["my-repo"])

        def _exists_side_effect(path):
            if "report.json" in str(path):
                return True
            return False

        captured_out: list[str] = []

        with patch(f"{MODULE}.run_ts_tests"):
            with patch("os.path.exists", side_effect=_exists_side_effect):
                with patch("builtins.open", MagicMock()):
                    with patch(
                        "json.load", side_effect=_json.JSONDecodeError("bad", "", 0)
                    ):
                        with patch(
                            "builtins.print",
                            side_effect=lambda *a, **kw: captured_out.append(str(a)),
                        ):
                            main(
                                dataset_name="test.json",
                                dataset_split="test",
                                repo_split="all",
                                base_dir="/repos",
                                branch="commit0",
                                backend="local",
                                timeout=300,
                                num_cpus=1,
                                num_workers=1,
                                rebuild_image=False,
                            )

        output_str = " ".join(captured_out)
        assert "0/1" in output_str
