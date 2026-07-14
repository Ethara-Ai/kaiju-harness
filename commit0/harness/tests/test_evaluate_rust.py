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
        # Current message: "<name>: OUTPUT_MISSING — test_output.txt missing at <dir>".
        assert any("test_output.txt missing" in r.message for r in caplog.records)

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


def _libtest_lines(passed=0, failed=0, ignored=0, prefix=""):
    """Build libtest per-test result lines that the REAL unified parser counts.

    The production parser (`parse_libtest_text`) counts individual
    ``test <name> ... <outcome>`` LINES — it does NOT sum the ``test result:``
    summary line. These tests were originally written against an OLD aggregator
    that text-parsed the summary line directly; that standalone fallback was
    removed when the aggregator was refactored to delegate to the unified
    parser. Emitting real per-test lines preserves each test's ORIGINAL INTENT
    (pass/fail/ignored counting, multi-binary accumulation) against the current
    code path. A trailing ``test result:`` summary line is appended for realism;
    the parser ignores it (only the per-test lines are counted).
    """
    lines = []
    for i in range(passed):
        lines.append(f"test {prefix}p{i} ... ok")
    for i in range(failed):
        lines.append(f"test {prefix}f{i} ... FAILED")
    for i in range(ignored):
        lines.append(f"test {prefix}i{i} ... ignored")
    lines.append(
        f"test result: {'ok' if failed == 0 else 'FAILED'}. "
        f"{passed} passed; {failed} failed; {ignored} ignored;"
    )
    return "\n".join(lines)


class TestAggregateCargoFallback:
    """Real libtest-text parsing (per-test result lines).

    Historically these mocked parse_nextest_report to empty and text-parsed the
    ``test result:`` summary. That standalone fallback was removed; the real
    unified parser counts per-test lines instead, so the fixtures now emit them.
    """

    def _write_output(self, tmp_path, text):
        f = tmp_path / "test_output.txt"
        f.write_text(text)
        return tmp_path

    def test_single_result_line_all_passed(self, tmp_path):
        self._write_output(tmp_path, _libtest_lines(passed=5))
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5
        assert out[0]["num_tests"] == 5
        assert out[0]["passed"] == 1.0

    def test_single_result_line_mixed(self, tmp_path):
        self._write_output(tmp_path, _libtest_lines(passed=3, failed=2, ignored=1))
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 3
        assert out[0]["num_tests"] == 6  # ignored counted in total

    def test_all_failed_cargo(self, tmp_path):
        self._write_output(tmp_path, _libtest_lines(failed=5))
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 0
        assert out[0]["passed"] == 0.0

    def test_zero_passed_zero_failed_zero_division(self, tmp_path):
        # No per-test lines at all -> parser matches nothing -> classifier path.
        self._write_output(tmp_path, "test result: ok. 0 passed; 0 failed; 0 ignored;")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["passed"] == 0.0
        assert out[0]["num_tests"] == 0

    def test_multiple_result_lines_accumulate(self, tmp_path):
        # Two test binaries' worth of per-test lines accumulate into one total.
        text = (
            _libtest_lines(passed=3, prefix="binA::")
            + "\n"
            + _libtest_lines(passed=2, failed=1, prefix="binB::")
        )
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5
        assert out[0]["num_tests"] == 6

    def test_cargo_sum_always_zero(self, tmp_path):
        # libtest text carries no per-test durations -> runtime sum is 0.
        self._write_output(tmp_path, _libtest_lines(passed=10))
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["sum"] == 0

    def test_no_summary_line_logs_warning(self, tmp_path, caplog):
        # No parseable test lines -> classifier reports PARSER_NO_MATCH.
        self._write_output(tmp_path, "running 5 tests\nall good")
        out = []
        with caplog.at_level(logging.WARNING):
            _aggregate_rust_results(str(tmp_path), "r", out)
        assert any("PARSER_NO_MATCH" in r.message for r in caplog.records)

    def test_no_summary_line_zero_counts(self, tmp_path):
        self._write_output(tmp_path, "running tests...")
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 0
        assert out[0]["passed"] == 0.0

    def test_ignored_counted_in_total(self, tmp_path):
        self._write_output(tmp_path, _libtest_lines(passed=2, ignored=3))
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_tests"] == 5

    def test_pass_rate_excludes_ignored(self, tmp_path):
        self._write_output(tmp_path, _libtest_lines(passed=2, ignored=3))
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["passed"] == pytest.approx(2 / 5)

    def test_extra_lines_before_result(self, tmp_path):
        text = "running 5 tests\n....." + "\n" + _libtest_lines(passed=5)
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5

    def test_whitespace_stripped_from_line(self, tmp_path):
        # Leading/trailing whitespace around per-test lines must still parse.
        text = "  test p0 ... ok  \n  test f0 ... FAILED  \n"
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 1
        assert out[0]["num_tests"] == 2


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

    def test_malformed_failed_number(self, tmp_path):
        # A malformed line (bad outcome token) must be ignored while the 5 valid
        # ``test ... ok`` lines still count. The real parser is line-oriented and
        # does not sum the ``test result:`` summary, so a garbage summary field
        # cannot corrupt the counts.
        text = _libtest_lines(passed=5) + "\ntest broken ... WOBBLY\n"
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 5
        assert out[0]["num_tests"] == 5  # malformed line not counted

    def test_malformed_ignored_number(self, tmp_path):
        # A non-libtest noise line interleaved with valid results is skipped.
        text = _libtest_lines(passed=5) + "\ntest result: ok. 5 passed; 0 failed; xyz ignored;\n"
        self._write_output(tmp_path, text)
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
        # Current message: "<name>: failed to read <path>: <exc>".
        assert any("failed to read" in r.message for r in caplog.records)

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

    # (passed, failed, ignored, expected_passed, expected_total). Fixtures now
    # emit real per-test libtest lines that the unified parser counts.
    @pytest.mark.parametrize("passed,failed,ignored,expected_passed,expected_total", [
        (10, 0, 0, 10, 10),
        (0, 10, 0, 0, 10),
        (7, 2, 1, 7, 10),
        (1, 0, 0, 1, 1),
        (0, 0, 1, 0, 1),
        (100, 50, 25, 100, 175),
    ])
    def test_cargo_pattern(self, tmp_path, passed, failed, ignored, expected_passed, expected_total):
        self._write_output(tmp_path, _libtest_lines(passed=passed, failed=failed, ignored=ignored))
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
        def fake_agg(log_path, name, out, expected_tests=None):
            out.append({"name": name, "sum": 1.0, "passed": 1.0, "num_passed": 1, "num_tests": 1})
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        # Current header carries the status/detail columns added by Fix #4.
        assert prints[0] == "repo,runtime,num_passed/num_tests,status,detail"

    def test_csv_sorted_by_runtime_descending(self, base_patches):
        e1 = _make_example(repo="Rust-commit0/taffy")
        e2 = _make_example(repo="Rust-commit0/bon")
        base_patches["load"].return_value = [e1, e2]
        call_count = {"n": 0}
        def fake_agg(log_path, name, out, expected_tests=None):
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
        def fake_agg(log_path, name, out, expected_tests=None):
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
        def fake_agg(log_path, name, out, expected_tests=None):
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
        def fake_agg(log_path, name, out, expected_tests=None):
            out.append({
                "name": "taffy", "sum": 3.0, "passed": 0.5,
                "num_passed": 5, "num_tests": 10,
                "status": "TESTS_RAN", "status_detail": "5/10 passed",
            })
        base_patches["agg"].side_effect = fake_agg
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        # CSV line now carries the status/detail columns: "<name>,<rt>,<np>/<nt>,<status>,<detail>".
        assert any(p.startswith("taffy,3.0,5/10,") for p in prints)


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
        base_patches["agg"].side_effect = lambda *a, **k: None
        main(**_default_kwargs())
        prints = [c.args[0] for c in base_patches["print"].call_args_list]
        assert any("average pass rate: 0" in p for p in prints)


class TestMainHashString:
    def test_hash_string_called_with_test_ids(self, base_patches):
        # BEHAVIOR CHANGE (c1ccc94): the log-dir hash is now derived from the
        # SAME key run_rust_tests hashes — the empty test_ids string ("" = full
        # suite, no per-test filter) — NOT the example's test_dir. Hashing the
        # test_dir made the reader (this aggregator) and the writer
        # (run_rust_tests) compute different log dirs, so passing runs were
        # reported as 0/0. Assert the current correct key.
        e1 = _make_example(test_dir="tests/unit/")
        base_patches["load"].return_value = [e1]
        main(**_default_kwargs())
        base_patches["hash"].assert_called_once_with("")


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
        def fake_agg(log_path, name, out, expected_tests=None):
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
    """Per-test lines across three test binaries accumulate into one total.

    Originally three ``test result:`` summary lines; the aggregator no longer
    text-parses that summary, so the fixtures now emit real per-test lines from
    three binaries (distinct name prefixes) which the unified parser counts.
    """

    def _write_output(self, tmp_path, text):
        (tmp_path / "test_output.txt").write_text(text)

    def test_three_lines_accumulate_passed(self, tmp_path):
        text = "\n".join([
            _libtest_lines(passed=2, prefix="a::"),
            _libtest_lines(passed=3, prefix="b::"),
            _libtest_lines(passed=5, prefix="c::"),
        ])
        self._write_output(tmp_path, text)
        out = []
        _aggregate_rust_results(str(tmp_path), "r", out)
        assert out[0]["num_passed"] == 10

    def test_three_lines_accumulate_failed(self, tmp_path):
        text = "\n".join([
            _libtest_lines(failed=1, prefix="a::"),
            _libtest_lines(failed=2, prefix="b::"),
            _libtest_lines(failed=3, prefix="c::"),
        ])
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



class TestClassifyEvalOutcome:
    """Tests for the failure-attribution classifier added by Fix #4."""

    def _write(self, tmp_path, exit_code: str | None, output: str) -> str:
        if exit_code is not None:
            (tmp_path / "cargo_test_exit_code.txt").write_text(exit_code)
        (tmp_path / "test_output.txt").write_text(output)
        return str(tmp_path)

    def test_compile_failure_detected(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_COMPILE_FAILED,
        )
        content = (
            "   Compiling virtio-drivers v0.13.0\n"
            "error: unexpected end of macro invocation\n"
            "  --> src/device/blk.rs:57:60\n"
            "error[E0432]: unresolved imports\n"
            "error: could not compile `virtio-drivers` (lib) due to 2 previous errors\n"
        )
        log_dir = self._write(tmp_path, "101", content)
        outcome, detail = _classify_eval_outcome(log_dir, content)
        assert outcome == OUTCOME_COMPILE_FAILED
        assert "compile error" in detail

    def test_patch_apply_failure_detected(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_PATCH_APPLY_FAILED,
        )
        content = "PATCH APPLY FAILED\nerror: patch failed: src/foo.rs:1\n"
        log_dir = self._write(tmp_path, "1", content)
        outcome, detail = _classify_eval_outcome(log_dir, content)
        assert outcome == OUTCOME_PATCH_APPLY_FAILED
        assert "git_apply_stderr.log" in detail

    def test_patch_apply_takes_priority_over_compile(self, tmp_path):
        # If both PATCH APPLY FAILED sentinel and error lines exist (eval.sh
        # writes the sentinel first), patch failure wins — it's the upstream cause.
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_PATCH_APPLY_FAILED,
        )
        content = "PATCH APPLY FAILED\nerror: patch failed\n"
        log_dir = self._write(tmp_path, "1", content)
        outcome, _ = _classify_eval_outcome(log_dir, content)
        assert outcome == OUTCOME_PATCH_APPLY_FAILED

    def test_timeout_exit_124(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_TEST_SUITE_TIMEOUT,
        )
        content = (
            "running 3 tests\n"
            "test a ... ok\n"
            "test b ... ok\n"
            "test c has been running for over 60 seconds\n"
        )
        log_dir = self._write(tmp_path, "124", content)
        outcome, detail = _classify_eval_outcome(log_dir, content)
        assert outcome == OUTCOME_TEST_SUITE_TIMEOUT
        assert "EVAL_TEST_TIMEOUT" in detail

    def test_timeout_exit_137_sigkill(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_TEST_SUITE_TIMEOUT,
        )
        log_dir = self._write(tmp_path, "137", "some output\n")
        outcome, _ = _classify_eval_outcome(log_dir, "some output\n")
        assert outcome == OUTCOME_TEST_SUITE_TIMEOUT

    def test_no_tests_defined(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_NO_TESTS_DEFINED,
        )
        content = (
            "     Running unittests src/lib.rs\n"
            "\n"
            "running 0 tests\n"
            "\n"
            "test result: ok. 0 passed; 0 failed; 0 ignored\n"
        )
        log_dir = self._write(tmp_path, "0", content)
        outcome, _ = _classify_eval_outcome(log_dir, content)
        assert outcome == OUTCOME_NO_TESTS_DEFINED

    def test_unknown_falls_back_to_parser_no_match(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_PARSER_NO_MATCH,
        )
        content = "some random unparseable output\n"
        log_dir = self._write(tmp_path, "42", content)
        outcome, _ = _classify_eval_outcome(log_dir, content)
        assert outcome == OUTCOME_PARSER_NO_MATCH

    def test_missing_exit_code_file_handled(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _classify_eval_outcome,
            OUTCOME_COMPILE_FAILED,
        )
        # No cargo_test_exit_code.txt written.
        content = "error[E0432]: unresolved import\nerror: aborting\n"
        (tmp_path / "test_output.txt").write_text(content)
        outcome, _ = _classify_eval_outcome(str(tmp_path), content)
        # Compile error lines visible → classified as compile fail despite missing exit code.
        assert outcome == OUTCOME_COMPILE_FAILED


class TestAggregateStatusField:
    """Verify _aggregate_rust_results emits the new `status` field per Fix #4."""

    def test_compile_failure_status_surfaced(self, tmp_path, caplog):
        from commit0.harness.evaluate_rust import (
            _aggregate_rust_results,
            OUTCOME_COMPILE_FAILED,
        )
        (tmp_path / "cargo_test_exit_code.txt").write_text("101")
        (tmp_path / "test_output.txt").write_text(
            "error[E0432]: unresolved import\nerror: could not compile\n"
        )
        out: list = []
        with caplog.at_level(logging.WARNING, logger="commit0.harness.evaluate_rust"):
            _aggregate_rust_results(str(tmp_path), "my-repo", out)
        assert len(out) == 1
        assert out[0]["status"] == OUTCOME_COMPILE_FAILED
        assert out[0]["num_tests"] == 0
        # The warning must NOT be the old uninformative 'no summary' message.
        assert any("COMPILE_FAILED" in r.message for r in caplog.records)
        assert not any("no 'test result:'" in r.message for r in caplog.records)

    def test_tests_ran_status_surfaced(self, tmp_path):
        from commit0.harness.evaluate_rust import (
            _aggregate_rust_results,
            OUTCOME_TESTS_RAN,
        )
        (tmp_path / "cargo_test_exit_code.txt").write_text("1")
        (tmp_path / "test_output.txt").write_text(
            "running 4 tests\n"
            "test a ... ok\n"
            "test b ... ok\n"
            "test c ... FAILED\n"
            "test d ... ok\n"
            "test result: FAILED. 3 passed; 1 failed; 0 ignored\n"
        )
        out: list = []
        _aggregate_rust_results(str(tmp_path), "my-repo", out)
        assert out[0]["status"] == OUTCOME_TESTS_RAN
        assert out[0]["num_passed"] == 3
        assert out[0]["num_tests"] == 4
        assert out[0]["passed"] == 0.75

    def test_partial_results_with_timeout_warning(self, tmp_path, caplog):
        # Tests recovered but cargo exited 124 → partial-results warning fires.
        from commit0.harness.evaluate_rust import _aggregate_rust_results
        (tmp_path / "cargo_test_exit_code.txt").write_text("124")
        (tmp_path / "test_output.txt").write_text(
            "running 4 tests\ntest a ... ok\ntest b ... ok\n"
        )
        out: list = []
        with caplog.at_level(logging.WARNING, logger="commit0.harness.evaluate_rust"):
            _aggregate_rust_results(str(tmp_path), "my-repo", out)
        assert out[0]["num_passed"] == 2
        # Status is TESTS_RAN (we recovered results) but a warning indicates partial.
        assert any("killed by timeout" in r.message for r in caplog.records)

    def test_missing_output_file_status_surfaced(self, tmp_path, caplog):
        from commit0.harness.evaluate_rust import (
            _aggregate_rust_results,
            OUTCOME_OUTPUT_MISSING,
        )
        out: list = []
        with caplog.at_level(logging.WARNING, logger="commit0.harness.evaluate_rust"):
            _aggregate_rust_results(str(tmp_path), "my-repo", out)
        assert out[0]["status"] == OUTCOME_OUTPUT_MISSING

    def test_status_detail_present_on_all_outcomes(self, tmp_path):
        """Every output entry must have a non-empty status_detail for debugging."""
        from commit0.harness.evaluate_rust import _aggregate_rust_results
        (tmp_path / "cargo_test_exit_code.txt").write_text("42")
        (tmp_path / "test_output.txt").write_text("garbage that matches nothing\n")
        out: list = []
        _aggregate_rust_results(str(tmp_path), "x", out)
        assert out[0]["status_detail"], "status_detail must not be empty"

# ---------------------------------------------------------------------------
# Doctest numerator/denominator consistency (regression for silent mis-scoring)
#
# `cargo test` runs doctests and libtest text prints their result lines as
#   `test src/lib.rs - item (line N) ... ok`
# The real parser matches these, so without stripping the numerator counts
# doctests while the canonical inventory (denominator) drops them -> a perfect
# UNIT solution whose doctests are excluded is scored below 1.0.
# ---------------------------------------------------------------------------
class TestDoctestConsistency:
    def _write(self, tmp_path, text):
        (tmp_path / "test_output.txt").write_text(text)
        return str(tmp_path)

    # 2 unit tests + 1 doctest, all pass. Canonical inventory (doctest-blind) = 2.
    _CARGO_WITH_DOCTEST = (
        "     Running unittests src/lib.rs (target/debug/deps/foo-abc)\n"
        "\nrunning 2 tests\n"
        "test a::t1 ... ok\n"
        "test a::t2 ... ok\n"
        "\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
        "\n   Doc-tests foo\n"
        "\nrunning 1 test\n"
        "test src/lib.rs - foo (line 3) ... ok\n"
        "\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )

    def test_doctest_stripped_from_numerator(self, tmp_path):
        log = self._write(tmp_path, self._CARGO_WITH_DOCTEST)
        out = []
        # Canonical inventory has the 2 unit tests only (doctest already dropped
        # by _load_rust_test_ids). A perfect unit solution must score 1.0.
        _aggregate_rust_results(log, "r", out, expected_tests=["a::t1", "a::t2"])
        assert out[0]["num_tests"] == 2, "doctest must not inflate the denominator"
        assert out[0]["num_passed"] == 2, "doctest must not inflate the numerator"
        assert out[0]["passed"] == 1.0, "perfect unit solution must score 1.0"
        assert out[0]["status"] == "TESTS_RAN"

    def test_doctest_failing_does_not_lower_unit_score(self, tmp_path):
        # Unit tests all pass; the doctest FAILS. A failing doctest must not drag
        # a perfect unit solution below 1.0.
        text = self._CARGO_WITH_DOCTEST.replace(
            "test src/lib.rs - foo (line 3) ... ok",
            "test src/lib.rs - foo (line 3) ... FAILED",
        ).replace(
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n",
            "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n",
        )
        log = self._write(tmp_path, text)
        out = []
        _aggregate_rust_results(log, "r", out, expected_tests=["a::t1", "a::t2"])
        assert out[0]["num_tests"] == 2
        assert out[0]["num_passed"] == 2
        assert out[0]["passed"] == 1.0

    def test_no_canonical_still_strips_doctests(self, tmp_path):
        # Without a canonical inventory the observed total must also exclude
        # doctests so the fallback numerator stays doctest-blind and consistent.
        log = self._write(tmp_path, self._CARGO_WITH_DOCTEST)
        out = []
        _aggregate_rust_results(log, "r", out, expected_tests=None)
        assert out[0]["num_tests"] == 2
        assert out[0]["num_passed"] == 2


class TestDoctestInventoryRegex:
    """The inventory doctest filter must drop the NO-ITEM-NAME rustdoc form."""

    def test_drops_noname_doctest(self):
        from commit0.harness.evaluate_rust import _DOCTEST_ID_RE
        # rustdoc lists a module-level (`//!`) doctest with no item name.
        assert _DOCTEST_ID_RE.search("src/lib.rs - (line 5)")

    def test_drops_named_doctest(self):
        from commit0.harness.evaluate_rust import _DOCTEST_ID_RE
        assert _DOCTEST_ID_RE.search("src/lib.rs - foo::bar (line 12)")

    def test_keeps_unit_test_id(self):
        from commit0.harness.evaluate_rust import _DOCTEST_ID_RE
        assert not _DOCTEST_ID_RE.search("queue::tests::add_buffers")
