"""Tests for commit0/harness/get_ts_test_ids.py — TypeScript test ID reader."""

import bz2
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE = "commit0.harness.get_ts_test_ids"


class TestRead:
    def test_read_valid_bz2(self, tmp_path: Path):
        from commit0.harness.get_ts_test_ids import read

        content = "__tests__/math.test.ts > adds numbers\n__tests__/math.test.ts > subtracts\n"
        bz2_file = tmp_path / "test.bz2"
        with bz2.open(bz2_file, "wt") as f:
            f.write(content)

        result = read(str(bz2_file))
        assert result == content

    def test_read_corrupt_bz2(self, tmp_path: Path):
        from commit0.harness.get_ts_test_ids import read

        bad_file = tmp_path / "corrupt.bz2"
        bad_file.write_bytes(b"\x00\x01\x02\x03garbage")

        with pytest.raises((OSError, EOFError)):
            read(str(bad_file))

    def test_read_missing_file(self):
        from commit0.harness.get_ts_test_ids import read

        with pytest.raises(OSError):
            read("/nonexistent/path/file.bz2")

    @patch(f"{MODULE}.bz2")
    def test_read_opens_in_text_mode(self, mock_bz2: MagicMock):
        from commit0.harness.get_ts_test_ids import read

        mock_file = MagicMock()
        mock_file.read.return_value = ""
        mock_bz2.open.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_bz2.open.return_value.__exit__ = MagicMock(return_value=False)

        read("/any/file.bz2")
        args, _ = mock_bz2.open.call_args
        assert args[1] == "rt"


class TestMain:
    @pytest.fixture(autouse=True)
    def _patch_resolver(self):
        # main() now resolves the ids file via kaiju.paths.find_test_ids_file.
        # Patch it as a PASS-THROUGH that returns the joined path so the
        # existing path/read assertions (which inspect the argument passed to
        # read()) still hold. find_test_ids_file is imported inside main() from
        # kaiju.paths, so patch it at its definition site.
        import os

        def _passthrough(commit0_path, subdir, filename):
            return os.path.join(commit0_path, "data", subdir, filename)

        with patch("kaiju.paths.find_test_ids_file", side_effect=_passthrough):
            yield

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_main_returns_single_list(
        self, mock_read: MagicMock, mock_dirname: MagicMock
    ):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = "a\nb\nc"
        result = main("repo", 0)
        assert result == [["a", "b", "c"]]

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_main_normalizes_repo_name(
        self, mock_read: MagicMock, mock_dirname: MagicMock
    ):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = "x"
        main("My.Repo.Name", 0)
        call_path = mock_read.call_args[0][0]
        assert "my-repo-name.bz2" in call_path

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_main_filters_empty_lines(
        self, mock_read: MagicMock, mock_dirname: MagicMock
    ):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = "test1\n\ntest2\n"
        result = main("repo", 0)
        assert result == [["test1", "test2"]]

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_main_empty_file(self, mock_read: MagicMock, mock_dirname: MagicMock):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = ""
        result = main("repo", 0)
        assert result == [[]]

    @patch("builtins.print")
    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_verbose_prints_output(
        self, mock_read: MagicMock, mock_dirname: MagicMock, mock_print: MagicMock
    ):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = "test1"
        main("repo", 1)
        mock_print.assert_called_once()

    @patch("builtins.print")
    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_verbose_zero_no_print(
        self, mock_read: MagicMock, mock_dirname: MagicMock, mock_print: MagicMock
    ):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = ""
        main("repo", 0)
        mock_print.assert_not_called()

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_lowercases_repo_name(self, mock_read: MagicMock, mock_dirname: MagicMock):
        from commit0.harness.get_ts_test_ids import main

        mock_read.return_value = ""
        main("MyRepo", 0)
        call_path = mock_read.call_args[0][0]
        assert "myrepo" in call_path
