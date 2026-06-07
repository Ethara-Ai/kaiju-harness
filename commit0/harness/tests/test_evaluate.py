from __future__ import annotations

import json
from unittest.mock import MagicMock, mock_open, patch

import pytest

from commit0.harness.evaluate import main

MODULE = "commit0.harness.evaluate"


def _make_example(instance_id="org/repo", repo="github/repo", test_dir="tests/"):
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": "aaa",
        "reference_commit": "bbb",
        "setup": {"python": "3.12"},
        "test": {"test_cmd": "pytest", "test_dir": test_dir},
        "src_dir": "src",
    }


def _default_kwargs(**overrides):
    defaults = dict(
        dataset_name="wentingzhao/commit0_combined",
        dataset_split="test",
        repo_split="all",
        base_dir="/repos",
        branch="main",
        coverage=False,
        backend="modal",
        timeout=1800,
        num_cpus=1,
        num_workers=1,
        rebuild_image=False,
    )
    defaults.update(overrides)
    return defaults


@pytest.fixture
def base_patches():
    with (
        patch(f"{MODULE}.load_dataset_from_config") as mock_load,
        patch(f"{MODULE}.get_tests") as mock_get_tests,
        patch(f"{MODULE}.get_hash_string", return_value="h" * 22) as mock_hash,
        patch(f"{MODULE}.get_active_branch", return_value="main") as mock_branch,
        patch(f"{MODULE}.run_tests") as mock_run,
        patch(f"{MODULE}.os.path.exists") as mock_exists,
        patch(
            f"{MODULE}.os.path.join", side_effect=lambda *a: "/".join(a)
        ) as mock_join,
        patch(
            f"{MODULE}.tqdm",
            side_effect=lambda iterable=None, **kw: (
                iterable
                if iterable is not None
                else MagicMock(
                    __enter__=MagicMock(return_value=MagicMock()),
                    __exit__=MagicMock(return_value=False),
                )
            ),
        ) as mock_tqdm,
        patch(f"{MODULE}.ThreadPoolExecutor") as mock_executor_cls,
        patch(f"{MODULE}.as_completed", return_value=list([])) as mock_as_completed,
        patch("builtins.print") as mock_print,
        patch(f"{MODULE}.SPLIT", {"all": ["repo"], "lite": ["repo"]}) as mock_split,
    ):
        mock_executor = MagicMock()
        mock_executor_cls.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = MagicMock()

        mock_pbar = MagicMock()
        mock_tqdm_ctx = MagicMock()
        mock_tqdm_ctx.__enter__ = MagicMock(return_value=mock_pbar)
        mock_tqdm_ctx.__exit__ = MagicMock(return_value=False)

        yield {
            "load": mock_load,
            "get_tests": mock_get_tests,
            "hash": mock_hash,
            "branch": mock_branch,
            "run": mock_run,
            "exists": mock_exists,
            "join": mock_join,
            "tqdm": mock_tqdm,
            "executor_cls": mock_executor_cls,
            "as_completed": mock_as_completed,
            "print": mock_print,
            "split": mock_split,
            "executor": mock_executor,
        }


class TestMainDatasetLoading:
    def test_loads_dataset_with_config(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs())

        base_patches["load"].assert_called_with(
            "wentingzhao/commit0_combined", split="test"
        )

    def test_swe_dataset_all_uses_instance_ids(self, base_patches):
        example = _make_example(instance_id="swe-bench/inst1", repo="github/r1")
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = [["test_a"]]

        main(**_default_kwargs(dataset_name="swe-bench-dataset", repo_split="all"))

        assert base_patches["executor"].submit.call_count == 1

    def test_swe_dataset_specific_split_filters(self, base_patches):
        example1 = _make_example(instance_id="swe-bench/inst1", repo="github/r1")
        example2 = _make_example(instance_id="other/inst2", repo="github/r2")
        base_patches["load"].return_value = [example1, example2]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = [["test_a"]]

        main(**_default_kwargs(dataset_name="SWE-data", repo_split="swe-bench"))

        assert base_patches["executor"].submit.call_count == 1


class TestMainRepoSplit:
    def test_commit0_repo_split_in_split_dict(self, base_patches):
        base_patches["split"]["lite"] = ["repo"]
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs(repo_split="lite"))

        base_patches["hash"].assert_called_once()

    def test_commit0_repo_split_not_in_split_filters_by_name(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs(repo_split="custom_unknown"))

        base_patches["executor"].submit.assert_not_called()

    def test_commit0_repo_split_not_in_split_matches_normalized(self, base_patches):
        example = _make_example(repo="github/my-repo")
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = [["test_a"]]

        main(**_default_kwargs(repo_split="my_repo"))

        assert base_patches["executor"].submit.call_count == 1

    def test_commit0_split_filters_out_non_matching_repo(self, base_patches):
        base_patches["split"]["lite"] = ["other-repo"]
        example = _make_example(repo="github/repo")
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs(repo_split="lite"))

        base_patches["executor"].submit.assert_not_called()


class TestMainBranchHandling:
    def test_branch_none_triggers_get_active_branch(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []
        base_patches["branch"].return_value = "auto-detected"

        main(**_default_kwargs(branch=None))

        base_patches["branch"].assert_called_once_with("/repos/org/repo")

    def test_branch_provided_skips_get_active_branch(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs(branch="feature-x"))

        base_patches["branch"].assert_not_called()


class TestMainThreadPoolExecution:
    def test_submits_run_tests_for_each_triple(self, base_patches):
        e1 = _make_example(instance_id="org/r1", repo="github/r1", test_dir="t1/")
        e2 = _make_example(instance_id="org/r2", repo="github/r2", test_dir="t2/")
        base_patches["load"].return_value = [e1, e2]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs())

        assert base_patches["executor"].submit.call_count == 2

    def test_executor_uses_num_workers(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = [["test_a"]]

        main(**_default_kwargs(num_workers=4))

        base_patches["executor_cls"].assert_called_once_with(max_workers=4)


class TestReportParsing:
    @pytest.mark.parametrize(
        "report_data,expected_format",
        [
            pytest.param(
                {
                    "created": "2024-01-01",
                    "tests": [
                        {
                            "nodeid": "test_a",
                            "call": {"outcome": "passed", "duration": 1.0},
                        },
                        {
                            "nodeid": "test_b",
                            "call": {"outcome": "failed", "duration": 2.0},
                        },
                    ],
                },
                "new",
                id="new_format",
            ),
            pytest.param(
                [
                    {
                        "nodeid": "test_a",
                        "when": "call",
                        "outcome": "passed",
                        "duration": 1.0,
                    },
                    {
                        "nodeid": "test_b",
                        "when": "call",
                        "outcome": "failed",
                        "duration": 2.0,
                    },
                ],
                "old",
                id="old_format",
            ),
        ],
    )
    def test_report_format_parsing(self, base_patches, report_data, expected_format):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a", "test_b"]]

        m = mock_open(read_data=json.dumps(report_data))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report_data):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_line = [p for p in prints if "repo" in p and "," in p and "/" in p]
        assert len(csv_line) >= 1

    def test_new_format_skips_entries_without_call(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a"]]
        report = {
            "created": "2024-01-01",
            "tests": [
                {"nodeid": "test_a", "setup": {"outcome": "error", "duration": 0}},
            ],
        }
        m = mock_open(read_data=json.dumps(report))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [p for p in prints if "," in p and "/" in p]
        assert any("0/" in line for line in csv_lines)

    def test_old_format_filters_by_when_call(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a"]]
        report = [
            {"nodeid": "test_a", "when": "setup", "outcome": "passed", "duration": 0.1},
            {"nodeid": "test_a", "when": "call", "outcome": "failed", "duration": 0.5},
        ]
        m = mock_open(read_data=json.dumps(report))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [p for p in prints if "," in p and "/" in p]
        assert any("0/" in line for line in csv_lines)


class TestMissingReport:
    def test_missing_report_gives_zero_sum_and_passed(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = [["test_a", "test_b"]]

        main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [p for p in prints if "," in p and "/" in p]
        assert any(",0," in line for line in csv_lines)
        assert any("0/2" in line for line in csv_lines)


class TestXfailCounting:
    def test_xfail_counted_as_passed(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a", "test_b"]]
        report = {
            "created": "2024-01-01",
            "tests": [
                {"nodeid": "test_a", "call": {"outcome": "xfail", "duration": 1.0}},
                {"nodeid": "test_b", "call": {"outcome": "passed", "duration": 1.0}},
            ],
        }
        m = mock_open(read_data=json.dumps(report))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [p for p in prints if "," in p and "/" in p]
        assert any("2/2" in line for line in csv_lines)


class TestZeroTestIds:
    def test_zero_test_ids_no_runs_returns_zero_pass_rate(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [[]]
        report = {"created": "2024-01-01", "tests": []}
        m = mock_open(read_data=json.dumps(report))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report):
                main(**_default_kwargs())
        prints = [str(c) for c in base_patches["print"].call_args_list]
        assert any("0.0" in p or "average pass rate" in p for p in prints)


class TestTestIdNotInReport:
    def test_test_id_missing_from_report_counted_as_failed(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a", "test_missing"]]
        report = {
            "created": "2024-01-01",
            "tests": [
                {"nodeid": "test_a", "call": {"outcome": "passed", "duration": 1.0}},
            ],
        }
        m = mock_open(read_data=json.dumps(report))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [p for p in prints if "," in p and "/" in p]
        assert any("1/2" in line for line in csv_lines)


class TestPassRateComputation:
    def test_averaged_pass_rate_printed(self, base_patches):
        e1 = _make_example(instance_id="org/r1", repo="github/r1")
        e2 = _make_example(instance_id="org/r2", repo="github/r2")
        base_patches["load"].return_value = [e1, e2]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a"]]
        report = {
            "created": "2024-01-01",
            "tests": [
                {"nodeid": "test_a", "call": {"outcome": "passed", "duration": 1.0}},
            ],
        }
        m = mock_open(read_data=json.dumps(report))
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", return_value=report):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert any("average pass rate: 1.0" in p for p in prints)


class TestCsvOutput:
    def test_csv_header_printed(self, base_patches):
        example = _make_example()
        base_patches["load"].return_value = [example]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = [["test_a"]]

        main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert prints[0] == "repo,runtime,num_passed/num_tests"

    def test_csv_sorted_by_runtime_descending(self, base_patches):
        e1 = _make_example(instance_id="org/r1", repo="github/r1")
        e2 = _make_example(instance_id="org/r2", repo="github/r2")
        base_patches["load"].return_value = [e1, e2]
        base_patches["exists"].return_value = True
        base_patches["get_tests"].return_value = [["test_a"]]
        report_fast = {
            "created": "2024-01-01",
            "tests": [
                {"nodeid": "test_a", "call": {"outcome": "passed", "duration": 1.0}},
            ],
        }
        report_slow = {
            "created": "2024-01-01",
            "tests": [
                {"nodeid": "test_a", "call": {"outcome": "passed", "duration": 10.0}},
            ],
        }
        reports = [report_fast, report_slow]
        report_idx = {"idx": 0}

        def load_side_effect(fp):
            r = reports[report_idx["idx"]]
            report_idx["idx"] += 1
            return r

        m = mock_open()
        with patch("builtins.open", m):
            with patch(f"{MODULE}.json.load", side_effect=load_side_effect):
                main(**_default_kwargs())

        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [
            p for p in prints if "," in p and "/" in p and not p.startswith("repo,")
        ]
        assert len(csv_lines) == 2
        first_runtime = float(csv_lines[0].split(",")[1])
        second_runtime = float(csv_lines[1].split(",")[1])
        assert first_runtime >= second_runtime


class TestEmptyDataset:
    def test_empty_dataset_no_triples(self, base_patches):
        base_patches["load"].return_value = []
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs())


class TestSweFilterInTripleLoop:
    def test_swe_non_all_skips_non_matching(self, base_patches):
        e1 = _make_example(instance_id="swe-bench/inst1", repo="github/r1")
        e2 = _make_example(instance_id="other/inst2", repo="github/r2")
        base_patches["load"].return_value = [e1, e2]
        base_patches["exists"].return_value = False
        base_patches["get_tests"].return_value = []

        main(**_default_kwargs(dataset_name="SWE-data", repo_split="swe-bench"))

        assert base_patches["hash"].call_count == 1
