from unittest.mock import patch, MagicMock

import pytest

MODULE = "commit0.harness.get_pytest_ids"


class TestRead:
    @patch(f"{MODULE}.bz2")
    def test_read_returns_file_content(self, mock_bz2):
        from commit0.harness.get_pytest_ids import read

        mock_file = MagicMock()
        mock_file.read.return_value = "test content"
        mock_bz2.open.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_bz2.open.return_value.__exit__ = MagicMock(return_value=False)

        result = read("/some/path.bz2")
        assert result == "test content"

    @patch(f"{MODULE}.bz2")
    def test_read_opens_correct_path(self, mock_bz2):
        from commit0.harness.get_pytest_ids import read

        mock_file = MagicMock()
        mock_file.read.return_value = ""
        mock_bz2.open.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_bz2.open.return_value.__exit__ = MagicMock(return_value=False)

        read("/expected/path.bz2")
        mock_bz2.open.assert_called_once_with("/expected/path.bz2", "rt")

    @patch(f"{MODULE}.bz2")
    def test_read_opens_in_text_mode(self, mock_bz2):
        from commit0.harness.get_pytest_ids import read

        mock_file = MagicMock()
        mock_file.read.return_value = ""
        mock_bz2.open.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_bz2.open.return_value.__exit__ = MagicMock(return_value=False)

        read("/any/file.bz2")
        args, _ = mock_bz2.open.call_args
        assert args[1] == "rt"


class TestMain:
    @pytest.fixture(autouse=True)
    def _resolve_passthrough(self):
        # main() resolves the inventory path via kaiju.paths.find_test_ids_file
        # (added so containerized runs find the mounted KAIJU_TEST_IDS_DIR). These
        # tests exercise main()'s parsing logic, not real file resolution, so make
        # resolution a PASS-THROUGH that returns the requested filename — the path
        # assertions (e.g. "<repo>.bz2", "#fail_to_pass.bz2") then still hold, and
        # read() is mocked per-test.
        with patch(
            "kaiju.paths.find_test_ids_file",
            side_effect=lambda _commit0_path, _subdir, fname: fname,
        ):
            yield

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_lowercases_repo_name(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = ""
        main("MyRepo", 0)
        call_path = mock_read.call_args[0][0]
        assert "myrepo" in call_path

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_replaces_dots_with_dashes(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = ""
        main("web3.py", 0)
        call_path = mock_read.call_args[0][0]
        assert "web3-py" in call_path
        assert "web3.py" not in call_path

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_double_underscore_reads_two_files(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = ""
        main("owner__repo", 0)
        assert mock_read.call_count == 2
        paths = [c[0][0] for c in mock_read.call_args_list]
        assert any("#fail_to_pass.bz2" in p for p in paths)
        assert any("#pass_to_pass.bz2" in p for p in paths)

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_no_double_underscore_reads_one_file(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = ""
        result = main("repo", 0)
        assert mock_read.call_count == 1
        assert result[1] == []

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_splits_newlines_correctly(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = "test1\ntest2\ntest3"
        result = main("repo", 0)
        assert result == [["test1", "test2", "test3"], []]

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_empty_file_returns_empty_lists(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = ""
        result = main("repo", 0)
        assert result == [[], []]

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_filters_empty_strings(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = "test1\n\ntest2\n"
        result = main("repo", 0)
        assert result == [["test1", "test2"], []]

    @patch("builtins.print")
    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_verbose_prints_output(self, mock_read, mock_dirname, mock_print):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = "test1"
        main("repo", 1)
        mock_print.assert_called_once()

    @patch("builtins.print")
    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_verbose_zero_no_print(self, mock_read, mock_dirname, mock_print):
        from commit0.harness.get_pytest_ids import main

        mock_read.return_value = ""
        main("repo", 0)
        mock_print.assert_not_called()

    @patch(f"{MODULE}.os.path.dirname", return_value="/fake/commit0")
    @patch(f"{MODULE}.read")
    def test_double_underscore_both_files_split(self, mock_read, mock_dirname):
        from commit0.harness.get_pytest_ids import main

        mock_read.side_effect = ["fail1\nfail2", "pass1\npass2"]
        result = main("owner__repo", 0)
        assert result == [["fail1", "fail2"], ["pass1", "pass2"]]
