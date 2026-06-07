"""Exhaustive unit tests for commit0.harness.evaluate_rust."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.evaluate_rust import _aggregate_rust_results, main

MODULE = "commit0.harness.evaluate_rust"


# ── helpers ──


def _make_example(repo="Rust-commit0/taffy", test_dir="tests/"):
    return {
        "repo": repo,
        "base_commit": "aaa",
        "reference_commit": "bbb",
        "setup": {},
        "test": {"test_dir": test_dir},
        "src_dir": "src",
    }


def _default_kwargs(**overrides):
    defaults = dict(
        dataset_name="ds",
        dataset_split="test",
        repo_split="all",
        base_dir="/repos",
        branch="main",
        backend="modal",
        timeout=1800,
        num_cpus=1,
        num_workers=1,
        rebuild_image=False,
    )
    defaults.update(overrides)
    return defaults


# ═══════════════════════════════════════════════════════════
# _aggregate_rust_results
# ═══════════════════════════════════════════════════════════


class TestAggregateRustResultsMissingFile:
    """When test_output.txt does not exist."""

    def test_missing_file_appends_zero_summary(self, tmp_path):
        out = []
        _aggregate_rust_results(str(tmp_path), "repo-x", out)
        assert len(out) == 1
        assert out[0]["num_tests"] == 0

    def test_missing_file_zero_passed(self, tmp_path):
        out = []
        _aggregate_rust_results(str(tmp_path), "repo-x", out)
        assert out[0]["num_passed"] == 0

    def test_missing_file_zero_sum(self, tmp_path):
        out = []
        _aggregate_rust_results(str(tmp_path), "repo-x", out)
        assert out[0]["sum"] == 0

    def test_missing_file_zero_passed_rate(self, tmp_path):
        out = []
        _aggregate_rust_results(str(tmp_path), "repo-x", out)
        assert out[0]["passed"] == 0

    def test_missing_file_name_preserved(self, tmp_path):
        out = []
        _aggregate_rust_results(str(tmp_path), "my-repo", out)
        assert out[0]["name"] == "my-repo"

    def test_missing_file_logs_warning(self, tmp_path, caplog):
        out = []
        with caplog.at_level(logging.WARNING):
            _aggregate_rust_results(str(tmp_path), "repo-x", out)
        assert any("missing test_output.txt" in r.message for r in caplog.records)

    def test_missing_file_multiple_calls_accumulate(self, tmp_path):
        out = []
        _aggregate_rust_results(str(tmp_path), "r1", out)
        _aggregate_rust_results(str(tmp_path), "r2", out)
        assert len(out) == 2


class TestAggregateNextestPath:
    """When parse_nextest_report returns non-empty tests list."""

    def _write_output(self, tmp_path, text="placeholder"):
        f = tmp_path / "test_output.txt"
        f.write_text(text)
        return tmp_path

    @patch(f"{MODULE}.parse_nextest_report")
    def test_all_passed(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.5}, {"name": "t2", "duration": 2.5}],
            "summary": {"passed": 2, "total": 2},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["passed"] == 1.0

    @patch(f"{MODULE}.parse_nextest_report")
    def test_mixed_results(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.0}, {"name": "t2", "duration": 2.0}],
            "summary": {"passed": 1, "total": 2},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["passed"] == 0.5

    @patch(f"{MODULE}.parse_nextest_report")
    def test_all_failed(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.0}],
            "summary": {"passed": 0, "total": 1},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["passed"] == 0.0

    @patch(f"{MODULE}.parse_nextest_report")
    def test_total_runtime_sum(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.5}, {"name": "t2", "duration": 2.5}],
            "summary": {"passed": 2, "total": 2},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["sum"] == 4.0

    @patch(f"{MODULE}.parse_nextest_report")
    def test_num_passed_from_summary(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 0.1}],
            "summary": {"passed": 1, "total": 1},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["num_passed"] == 1

    @patch(f"{MODULE}.parse_nextest_report")
    def test_num_tests_from_summary(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 0.1}],
            "summary": {"passed": 1, "total": 5},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["num_tests"] == 5

    @patch(f"{MODULE}.parse_nextest_report")
    def test_zero_total_no_division_error(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 0.0}],
            "summary": {"passed": 0, "total": 0},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["passed"] == 0.0

    @patch(f"{MODULE}.parse_nextest_report")
    def test_missing_duration_defaults_zero(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1"}, {"name": "t2", "duration": 3.0}],
            "summary": {"passed": 2, "total": 2},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["sum"] == 3.0

    @patch(f"{MODULE}.parse_nextest_report")
    def test_name_preserved_in_output(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.0}],
            "summary": {"passed": 1, "total": 1},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "fancy-repo", out)
        assert out[0]["name"] == "fancy-repo"

    @patch(f"{MODULE}.parse_nextest_report")
    def test_float_precision_duration(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 0.1}, {"name": "t2", "duration": 0.2}],
            "summary": {"passed": 2, "total": 2},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert abs(out[0]["sum"] - 0.3) < 1e-9

    @patch(f"{MODULE}.parse_nextest_report")
    def test_missing_summary_keys_default_zero(self, mock_parse, tmp_path):
        self._write_output(tmp_path)
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.0}],
            "summary": {},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "repo", out)
        assert out[0]["num_passed"] == 0
        assert out[0]["num_tests"] == 0
        assert out[0]["passed"] == 0.0


class TestAggregateCargoFallback:
    """When nextest returns empty tests, fall back to cargo parsing."""

    def _write_output(self, tmp_path, text):
        f = tmp_path / "test_output.txt"
        f.write_text(text)
        return tmp_path

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_single_result_line_all_passed(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 5 passed; 0 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5
        assert out[0]["num_tests"] == 5
        assert out[0]["passed"] == 1.0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_single_result_line_mixed(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: FAILED. 3 passed; 2 failed; 1 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 3
        assert out[0]["num_tests"] == 6

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_all_failed_cargo(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: FAILED. 0 passed; 5 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 0
        assert out[0]["passed"] == 0.0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_zero_passed_zero_failed_zero_division(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 0 passed; 0 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["passed"] == 0.0
        assert out[0]["num_tests"] == 0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_multiple_result_lines_accumulate(self, mock_parse, tmp_path):
        text = "test result: ok. 3 passed; 0 failed; 0 ignored;\ntest result: ok. 2 passed; 1 failed; 0 ignored;"
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5
        assert out[0]["num_tests"] == 6

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_cargo_sum_always_zero(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 10 passed; 0 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["sum"] == 0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_no_summary_line_logs_warning(self, mock_parse, tmp_path, caplog):
        self._write_output(tmp_path, "running 5 tests\nall good")
        out = []
        with caplog.at_level(logging.WARNING):
            _aggregate_rust_results(str(tmp_path), "r", out)
        assert any("no 'test result:' summary" in r.message for r in caplog.records)

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_no_summary_line_zero_counts(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "running tests...")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 0
        assert out[0]["passed"] == 0.0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_ignored_counted_in_total(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 2 passed; 0 failed; 3 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 5

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_pass_rate_excludes_ignored(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 2 passed; 0 failed; 3 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["passed"] == pytest.approx(2 / 5)

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_extra_lines_before_result(self, mock_parse, tmp_path):
        text = "running 5 tests\n.....\ntest result: ok. 5 passed; 0 failed; 0 ignored;"
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_whitespace_stripped_from_line(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "  test result: ok. 4 passed; 1 failed; 0 ignored;  ")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 4
        assert out[0]["num_tests"] == 5


class TestAggregateCargoEdgeCases:
    """Malformed cargo lines, ValueError, IndexError."""

    def _write_output(self, tmp_path, text):
        f = tmp_path / "test_output.txt"
        f.write_text(text)
        return tmp_path

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_malformed_passed_number(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. abc passed; 0 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_malformed_failed_number(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 5 passed; xyz failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5
        assert out[0]["num_tests"] == 5  # only passed counted

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_malformed_ignored_number(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "test result: ok. 5 passed; 0 failed; xyz ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 5

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_passed_at_start_index_error(self, mock_parse, tmp_path):
        # "passed;" at index 0 -> i-1 = -1 -> wraps, catches ValueError
        self._write_output(tmp_path, "test result: passed; 0 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 0  # "result:" is not a number

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_empty_file_no_summary(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_only_newlines(self, mock_parse, tmp_path):
        self._write_output(tmp_path, "\n\n\n")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 0


class TestAggregateOSError:
    """OSError when reading the file in cargo fallback."""

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    @patch("builtins.open", side_effect=OSError("disk fail"))
    def test_oserror_appends_zero(self, mock_open, mock_parse, tmp_path):
        # file must exist for os.path.exists but open raises
        (tmp_path / "test_output.txt").write_text("x")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 0
        assert out[0]["sum"] == 0

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    @patch("builtins.open", side_effect=OSError("perm denied"))
    def test_oserror_logs_warning(self, mock_open, mock_parse, tmp_path, caplog):
        (tmp_path / "test_output.txt").write_text("x")
        out = []
        with caplog.at_level(logging.WARNING):
            _aggregate_rust_results(str(tmp_path), "r", out)
        assert any("Failed to read" in r.message for r in caplog.records)

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    @patch("builtins.open", side_effect=OSError("nope"))
    def test_oserror_name_preserved(self, mock_open, mock_parse, tmp_path):
        (tmp_path / "test_output.txt").write_text("x")
        out = []
        _aggregate_rust_results(str(tmp_path), "myrepo", out)
        assert out[0]["name"] == "myrepo"


class TestAggregateParametrized:
    """Parametrized cargo output patterns."""

    def _write_output(self, tmp_path, text):
        (tmp_path / "test_output.txt").write_text(text)

    @pytest.mark.parametrize("text,expected_passed,expected_total", [
        ("test result: ok. 10 passed; 0 failed; 0 ignored;", 10, 10),
        ("test result: FAILED. 0 passed; 10 failed; 0 ignored;", 0, 10),
        ("test result: ok. 7 passed; 2 failed; 1 ignored;", 7, 10),
        ("test result: ok. 1 passed; 0 failed; 0 ignored;", 1, 1),
        ("test result: ok. 0 passed; 0 failed; 1 ignored;", 0, 1),
        ("test result: ok. 100 passed; 50 failed; 25 ignored;", 100, 175),
    ])
    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_cargo_pattern(self, mock_parse, tmp_path, text, expected_passed, expected_total):
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == expected_passed
        assert out[0]["num_tests"] == expected_total


# ═══════════════════════════════════════════════════════════
# main()
# ═══════════════════════════════════════════════════════════

_FAKE_SPLIT = {
    "all": ["Rust-commit0/taffy", "Rust-commit0/bon"],
    "lite": ["Rust-commit0/taffy"],
}


@pytest.fixture
def base_patches():
    with (
        patch(f"{MODULE}.load_dataset_from_config") as mock_load,
        patch(f"{MODULE}.get_hash_string", return_value="h" * 22) as mock_hash,
        patch(f"{MODULE}.get_active_branch", return_value="main") as mock_branch,
        patch(f"{MODULE}.run_rust_tests") as mock_run,
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
        patch(f"{MODULE}.RUST_SPLIT", _FAKE_SPLIT),
        patch(f"{MODULE}.RUN_RUST_TESTS_LOG_DIR", Path("/tmp/fake_logs")),
        patch(f"{MODULE}._aggregate_rust_results") as mock_agg,
    ):
        mock_executor = MagicMock()
        mock_executor_cls.return_value.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = MagicMock()

        yield {
            "load": mock_load,
            "hash": mock_hash,
            "branch": mock_branch,
            "run": mock_run,
            "tqdm": mock_tqdm,
            "executor_cls": mock_executor_cls,
            "as_completed": mock_as_completed,
            "print": mock_print,
            "executor": mock_executor,
            "agg": mock_agg,
        }


class TestMainDatasetLoading:
    def test_loads_dataset(self, base_patches):
        base_patches["load"].return_value = []
        main(**_default_kwargs())
        base_patches["load"].assert_called_once_with("ds", split="test")

    def test_converts_iterator_to_list(self, base_patches):
        base_patches["load"].return_value = iter([_make_example()])
        main(**_default_kwargs())
        base_patches["hash"].assert_called_once()


class TestMainEmptyDataset:
    def test_empty_dataset_no_triples(self, base_patches, caplog):
        base_patches["load"].return_value = []
        with caplog.at_level(logging.ERROR):
            main(**_default_kwargs())
        assert any("No Rust repos matched" in r.message for r in caplog.records)

    def test_empty_dataset_no_executor(self, base_patches):
        base_patches["load"].return_value = []
        main(**_default_kwargs())
        base_patches["executor"].submit.assert_not_called()


class TestMainRepoSplitAll:
    def test_all_includes_both_repos(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        main(**_default_kwargs(repo_split="all"))
        assert base_patches["executor"].submit.call_count == 2

    def test_all_skips_non_rust_repo(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/unknown")
        base_patches["load"].return_value = [e1, e2]
        main(**_default_kwargs(repo_split="all"))
        assert base_patches["executor"].submit.call_count == 1


class TestMainRepoSplitSpecific:
    def test_lite_split_filters(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        main(**_default_kwargs(repo_split="lite"))
        assert base_patches["executor"].submit.call_count == 1

    def test_nonexistent_split_uses_single_repo(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(repo_split="taffy"))
        assert base_patches["executor"].submit.call_count == 1

    def test_dash_underscore_normalization(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/my-repo")
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(repo_split="my_repo"))
        assert base_patches["executor"].submit.call_count == 1

    def test_reverse_dash_underscore(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/my_repo")
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(repo_split="my-repo"))
        assert base_patches["executor"].submit.call_count == 1

    def test_single_repo_no_match(self, base_patches, caplog):
        e1 = _make_example(repo="Rust-commit0/taffy")
        base_patches["load"].return_value = [e1]
        with caplog.at_level(logging.ERROR):
            main(**_default_kwargs(repo_split="nonexistent"))
        assert any("No Rust repos matched" in r.message for r in caplog.records)


class TestMainBranchHandling:
    def test_branch_none_calls_get_active_branch(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(branch=None))
        base_patches["branch"].assert_called_once_with("/repos/taffy")

    def test_branch_provided_skips_get_active_branch(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(branch="feature-x"))
        base_patches["branch"].assert_not_called()

    def test_branch_resolved_per_repo(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        base_patches["branch"].side_effect = ["br1", "br2"]
        main(**_default_kwargs(branch=None))
        assert base_patches["branch"].call_count == 2


class TestMainThreadPool:
    def test_executor_uses_num_workers(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(num_workers=4))
        base_patches["executor_cls"].assert_called_once_with(max_workers=4)

    def test_submit_called_per_triple(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        main(**_default_kwargs())
        assert base_patches["executor"].submit.call_count == 2

    def test_submit_passes_correct_args(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy", test_dir="tests/")
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(branch="main", backend="modal", timeout=1800, num_cpus=1))
        call_args = base_patches["executor"].submit.call_args
        # First positional arg is run_rust_tests
        args = call_args[0]
        assert args[0] is base_patches["run"]


class TestMainExceptionHandling:
    def test_system_exit_code_0_ignored(self, base_patches, caplog):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        future = MagicMock()
        future.result.side_effect = SystemExit(0)
        base_patches["as_completed"].return_value = iter([future])
        base_patches["executor"].submit.return_value = future
        with caplog.at_level(logging.WARNING):
            main(**_default_kwargs())
        assert not any("exited with code" in r.message for r in caplog.records)

    def test_system_exit_code_1_ignored(self, base_patches, caplog):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        future = MagicMock()
        future.result.side_effect = SystemExit(1)
        base_patches["as_completed"].return_value = iter([future])
        base_patches["executor"].submit.return_value = future
        with caplog.at_level(logging.WARNING):
            main(**_default_kwargs())
        assert not any("exited with code" in r.message for r in caplog.records)

    def test_system_exit_code_2_logged(self, base_patches, caplog):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        future = MagicMock()
        future.result.side_effect = SystemExit(2)
        base_patches["as_completed"].return_value = iter([future])
        base_patches["executor"].submit.return_value = future
        with caplog.at_level(logging.WARNING):
            main(**_default_kwargs())
        assert any("exited with code" in r.message for r in caplog.records)

    def test_system_exit_code_42_logged(self, base_patches, caplog):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        future = MagicMock()
        future.result.side_effect = SystemExit(42)
        base_patches["as_completed"].return_value = iter([future])
        base_patches["executor"].submit.return_value = future
        with caplog.at_level(logging.WARNING):
            main(**_default_kwargs())
        assert any("42" in r.message for r in caplog.records)

    def test_generic_exception_logged(self, base_patches, caplog):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        future = MagicMock()
        future.result.side_effect = RuntimeError("boom")
        base_patches["as_completed"].return_value = iter([future])
        base_patches["executor"].submit.return_value = future
        with caplog.at_level(logging.ERROR):
            main(**_default_kwargs())
        assert any("Rust evaluation failed" in r.message for r in caplog.records)

    def test_generic_exception_does_not_crash(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        future = MagicMock()
        future.result.side_effect = ValueError("bad")
        base_patches["as_completed"].return_value = iter([future])
        base_patches["executor"].submit.return_value = future
        main(**_default_kwargs())  # should not raise


class TestMainCsvOutput:
    """CSV header, sorting, totals."""

    def test_csv_header_printed(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        # agg will add to out list via side_effect
        def fake_agg(log_path, name, out):
            out.append({"name": name, "sum": 1.0, "passed": 1.0, "num_passed": 1, "num_tests": 1})
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert prints[0] == "repo,runtime,num_passed/num_tests"

    def test_csv_sorted_by_runtime_descending(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        call_count = {"n": 0}
        def fake_agg(log_path, name, out):
            call_count["n"] += 1
            runtime = 10.0 if call_count["n"] == 2 else 1.0
            out.append({"name": name, "sum": runtime, "passed": 1.0, "num_passed": 1, "num_tests": 1})
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        csv_lines = [p for p in prints if "," in p and "/" in p and not p.startswith("repo,")]
        assert len(csv_lines) == 2
        first_runtime = float(csv_lines[0].split(",")[1])
        second_runtime = float(csv_lines[1].split(",")[1])
        assert first_runtime >= second_runtime

    def test_total_runtime_printed(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        def fake_agg(log_path, name, out):
            out.append({"name": name, "sum": 5.5, "passed": 1.0, "num_passed": 1, "num_tests": 1})
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert any("total runtime: 5.5" in p for p in prints)

    def test_average_pass_rate_printed(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        call_count = {"n": 0}
        def fake_agg(log_path, name, out):
            call_count["n"] += 1
            rate = 1.0 if call_count["n"] == 1 else 0.5
            out.append({"name": name, "sum": 1.0, "passed": rate, "num_passed": 1, "num_tests": 2})
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert any("average pass rate: 0.75" in p for p in prints)

    def test_csv_line_format(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        def fake_agg(log_path, name, out):
            out.append({"name": "taffy", "sum": 3.0, "passed": 0.5, "num_passed": 5, "num_tests": 10})
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert "taffy,3.0,5/10" in prints


class TestMainAggregation:
    def test_aggregate_called_per_log_dir(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        main(**_default_kwargs())
        assert base_patches["agg"].call_count == 2

    def test_empty_out_prints_zero_avg(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        # agg does nothing (empty out)
        base_patches["agg"].side_effect = lambda *a: None
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert any("average pass rate: 0" in p for p in prints)


class TestMainHashString:
    def test_hash_string_called_with_test_dir(self, base_patches):
        e1 = _make_example(test_dir="tests/unit/")
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs())
        base_patches["hash"].assert_called_once_with("tests/unit/")


class TestMainLogging:
    def test_info_log_on_load(self, base_patches, caplog):
        base_patches["load"].return_value = [_make_example()]
        with caplog.at_level(logging.INFO):
            main(**_default_kwargs())
        assert any("Loaded" in r.message for r in caplog.records)

    def test_info_log_evaluating_count(self, base_patches, caplog):
        base_patches["load"].return_value = [_make_example()]
        with caplog.at_level(logging.INFO):
            main(**_default_kwargs())
        assert any("Evaluating" in r.message for r in caplog.records)

    def test_completion_log(self, base_patches, caplog):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        def fake_agg(log_path, name, out):
            out.append({"name": name, "sum": 0, "passed": 0.0, "num_passed": 0, "num_tests": 0})
        base_patches["agg"].side_effect = fake_agg
        with caplog.at_level(logging.INFO):
            main(**_default_kwargs())
        assert any("Rust evaluation complete" in r.message for r in caplog.records)


class TestMainMultipleErrors:
    def test_multiple_futures_some_fail(self, base_patches, caplog):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        f1 = MagicMock()
        f1.result.return_value = None
        f2 = MagicMock()
        f2.result.side_effect = RuntimeError("fail")
        base_patches["executor"].submit.side_effect = [f1, f2]
        base_patches["as_completed"].return_value = iter([f1, f2])
        with caplog.at_level(logging.ERROR):
            main(**_default_kwargs())
        assert any("Rust evaluation failed" in r.message for r in caplog.records)

    def test_all_futures_succeed(self, base_patches, caplog):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        f1 = MagicMock()
        f1.result.return_value = None
        f2 = MagicMock()
        f2.result.return_value = None
        base_patches["executor"].submit.side_effect = [f1, f2]
        base_patches["as_completed"].return_value = iter([f1, f2])
        with caplog.at_level(logging.WARNING):
            main(**_default_kwargs())
        assert not any("failed" in r.message.lower() for r in caplog.records)


class TestMainTqdm:
    def test_tqdm_called_with_total(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs())
        # tqdm called at least once with total= keyword
        tqdm_calls = base_patches["tqdm"].call_args_list
        context_call = [c for c in tqdm_calls if "total" in (c.kwargs or {})]
        assert len(context_call) >= 1


class TestMainRebuildImageParam:
    def test_rebuild_image_passed_to_submit(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(rebuild_image=True))
        call_args = base_patches["executor"].submit.call_args
        args = call_args[0]
        # rebuild_image is arg index 10 (0-based) in the run_rust_tests call
        assert args[10] is True


class TestAggregateThreeResultLines:
    """Three cargo result lines accumulate."""

    def _write_output(self, tmp_path, text):
        (tmp_path / "test_output.txt").write_text(text)

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_three_lines_accumulate_passed(self, mock_parse, tmp_path):
        text = "test result: ok. 2 passed; 0 failed; 0 ignored;\ntest result: ok. 3 passed; 0 failed; 0 ignored;\ntest result: ok. 5 passed; 0 failed; 0 ignored;"
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 10

    @patch(f"{MODULE}.parse_nextest_report", return_value={"tests": [], "summary": {}})
    def test_three_lines_accumulate_failed(self, mock_parse, tmp_path):
        text = "test result: FAILED. 0 passed; 1 failed; 0 ignored;\ntest result: FAILED. 0 passed; 2 failed; 0 ignored;\ntest result: FAILED. 0 passed; 3 failed; 0 ignored;"
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 6


class TestMainBackendParam:
    def test_backend_passed_to_submit(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(backend="local"))
        call_args = base_patches["executor"].submit.call_args
        args = call_args[0]
        assert args[7] == "local"  # backend arg position

    def test_timeout_passed_to_submit(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(timeout=600))
        call_args = base_patches["executor"].submit.call_args
        args = call_args[0]
        assert args[8] == 600  # timeout arg position

    def test_num_cpus_passed_to_submit(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs(num_cpus=8))
        call_args = base_patches["executor"].submit.call_args
        args = call_args[0]
        assert args[9] == 8  # num_cpus arg position


class TestMainVerboseZero:
    def test_verbose_zero_passed_to_submit(self, base_patches):
        e1 = _make_example()
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs())
        call_args = base_patches["executor"].submit.call_args
        args = call_args[0]
        assert args[11] == 0  # verbose is always 0


class TestAggregateNextestEmptySummary:
    """Nextest returns tests but summary has missing keys."""

    @patch(f"{MODULE}.parse_nextest_report")
    def test_no_passed_key_defaults_zero(self, mock_parse, tmp_path):
        (tmp_path / "test_output.txt").write_text("x")
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.0}],
            "summary": {"total": 5},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 0

    @patch(f"{MODULE}.parse_nextest_report")
    def test_no_total_key_defaults_zero(self, mock_parse, tmp_path):
        (tmp_path / "test_output.txt").write_text("x")
        mock_parse.return_value = {
            "tests": [{"name": "t1", "duration": 1.0}],
            "summary": {"passed": 3},
        }
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 0
        assert out[0]["passed"] == 0.0


class TestMainDatasetSplitParam:
    def test_dataset_split_forwarded(self, base_patches):
        base_patches["load"].return_value = []
        main(**_default_kwargs(dataset_split="train"))
        base_patches["load"].assert_called_once_with("ds", split="train")
