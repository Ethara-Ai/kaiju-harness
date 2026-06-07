import json
import logging
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from commit0.harness.utils import (
    EvaluationError,
    clone_repo,
    close_logger,
    create_repo_on_github,
    extract_code_blocks,
    extract_test_output,
    generate_patch_between_commits,
    get_active_branch,
    get_hash_string,
    load_dataset_from_config,
    setup_logger,
)

MODULE = "commit0.harness.utils"


class TestGetHashString:
    def test_deterministic(self):
        assert get_hash_string("hello") == get_hash_string("hello")

    def test_different_inputs_differ(self):
        assert get_hash_string("abc") != get_hash_string("def")

    def test_returns_22_chars(self):
        assert len(get_hash_string("anything")) == 22

    def test_hex_characters_only(self):
        result = get_hash_string("test123")
        assert all(c in "0123456789abcdef" for c in result)


class TestExtractTestOutput:
    def test_pattern_found(self):
        text = "+run_tests some_pattern\noutput line 1\noutput line 2\n+next_command"
        result = extract_test_output(text, "some_pattern")
        assert result == "output line 1\noutput line 2"

    def test_pattern_not_found(self):
        text = "no match here\njust lines\n"
        assert extract_test_output(text, "missing") == ""

    def test_empty_input(self):
        assert extract_test_output("", "pattern") == ""

    def test_multiple_sections(self):
        text = (
            "+cmd first_pattern\nfirst output\n+end\n"
            "+cmd first_pattern\nsecond output\n+end"
        )
        result = extract_test_output(text, "first_pattern")
        assert result == "first output"

    def test_no_closing_marker(self):
        text = "+cmd my_pattern\nsome output\nmore output"
        assert extract_test_output(text, "my_pattern") == ""


class TestExtractCodeBlocks:
    def test_single_block(self):
        text = "```python\nprint('hi')\n```"
        assert extract_code_blocks(text) == ["print('hi')"]

    def test_multiple_blocks(self):
        text = "```python\na = 1\n```\ntext\n```python\nb = 2\n```"
        result = extract_code_blocks(text)
        assert len(result) == 2
        assert result[0] == "a = 1"
        assert result[1] == "b = 2"

    def test_no_blocks(self):
        assert extract_code_blocks("just text") == []

    def test_nested_backticks_only_python(self):
        text = "```javascript\nvar x = 1;\n```\n```python\ny = 2\n```"
        result = extract_code_blocks(text)
        assert len(result) == 1
        assert result[0] == "y = 2"


class TestEvaluationError:
    def _make_error(self):
        logger = logging.getLogger("test_eval_error")
        return EvaluationError(repo="my/repo", message="something broke", logger=logger)

    def test_str_contains_repo(self):
        err = self._make_error()
        assert "my/repo" in str(err)

    def test_str_contains_log_file_info(self):
        err = self._make_error()
        assert "Check (" in str(err)
        assert "for more information" in str(err)

    def test_inherits_from_exception(self):
        err = self._make_error()
        assert isinstance(err, Exception)


class TestSetupLogger:
    @staticmethod
    def _unique_log(tmp_path, name_suffix, **kwargs):
        log_file = tmp_path / f"{name_suffix}.log"
        return setup_logger(f"test_{name_suffix}", log_file, **kwargs)

    def test_creates_logger_with_handler(self, tmp_path):
        logger = self._unique_log(tmp_path, "handler_check")
        try:
            assert any(isinstance(h, logging.FileHandler) for h in logger.handlers)
        finally:
            close_logger(logger)

    def test_log_file_attribute_set(self, tmp_path):
        logger = self._unique_log(tmp_path, "attr_check")
        try:
            assert hasattr(logger, "log_file")
        finally:
            close_logger(logger)

    def test_verbose_2_adds_stdout_handler(self, tmp_path):
        logger = self._unique_log(tmp_path, "verbose2", verbose=2)
        try:
            assert len(logger.handlers) == 2
        finally:
            close_logger(logger)

    def test_verbose_1_single_handler(self, tmp_path):
        logger = self._unique_log(tmp_path, "verbose1", verbose=1)
        try:
            assert len(logger.handlers) == 1
        finally:
            close_logger(logger)


class TestCloseLogger:
    def test_removes_handler(self, tmp_path):
        log_file = tmp_path / "close_single.log"
        logger = setup_logger("test_close_single", log_file, verbose=1)
        assert len(logger.handlers) == 1
        close_logger(logger)
        assert len(logger.handlers) == 0

    def test_handlers_flushed_before_removal(self, tmp_path):
        log_file = tmp_path / "close_flush.log"
        logger = setup_logger("test_close_flush", log_file)
        logger.info("test message")
        close_logger(logger)
        assert log_file.read_text() != ""


class TestCloneRepo:
    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    def test_clones_new_repo(self, mock_exists, mock_repo_cls):
        mock_repo = MagicMock()
        mock_repo_cls.clone_from.return_value = mock_repo
        logger = MagicMock()

        result = clone_repo("https://url", "/dir", "main", logger)

        mock_repo_cls.clone_from.assert_called_once_with("https://url", "/dir")
        mock_repo.git.checkout.assert_called_once_with("main")
        assert result is mock_repo

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    def test_fetches_existing_repo(self, mock_exists, mock_repo_cls):
        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        logger = MagicMock()

        result = clone_repo("https://url", "/dir", "main", logger)

        mock_repo_cls.assert_called_once_with("/dir")
        mock_repo.git.fetch.assert_called_once()
        mock_repo.git.checkout.assert_called_once_with("main")
        assert result is mock_repo

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=True)
    def test_checkout_failure_raises(self, mock_exists, mock_repo_cls):
        import git.exc

        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.git.checkout.side_effect = git.exc.GitCommandError("checkout", "err")
        logger = MagicMock()

        with pytest.raises(RuntimeError, match="Failed to check out"):
            clone_repo("https://url", "/dir", "bad-branch", logger)

    @patch(f"{MODULE}.git.Repo")
    @patch(f"{MODULE}.os.path.exists", return_value=False)
    def test_clone_failure_raises(self, mock_exists, mock_repo_cls):
        import git.exc

        mock_repo_cls.clone_from.side_effect = git.exc.GitCommandError("clone", "err")
        logger = MagicMock()

        with pytest.raises(RuntimeError, match="Failed to clone"):
            clone_repo("https://url", "/dir", "main", logger)


class TestCreateRepoOnGithub:
    @patch(f"{MODULE}.GhApi")
    def test_repo_exists_logs_info(self, mock_ghapi_cls):
        mock_api = MagicMock()
        mock_ghapi_cls.return_value = mock_api
        logger = MagicMock()

        create_repo_on_github("org", "repo", logger, token="tok")

        mock_api.repos.get.assert_called_once_with(owner="org", repo="repo")
        logger.info.assert_called()
        assert "already exists" in logger.info.call_args[0][0]

    @patch(f"{MODULE}.GhApi")
    def test_404_creates_repo(self, mock_ghapi_cls):
        from fastcore.net import HTTP404NotFoundError

        mock_api = MagicMock()
        mock_ghapi_cls.return_value = mock_api
        mock_api.repos.get.side_effect = HTTP404NotFoundError("url", {}, "")
        logger = MagicMock()

        create_repo_on_github("org", "repo", logger, token="tok")

        mock_api.repos.create_in_org.assert_called_once_with(org="org", name="repo")

    @patch(f"{MODULE}.time.sleep")
    @patch(f"{MODULE}.GhApi")
    def test_403_waits_for_rate_limit(self, mock_ghapi_cls, mock_sleep):
        from fastcore.net import HTTP403ForbiddenError

        mock_api = MagicMock()
        mock_ghapi_cls.return_value = mock_api

        mock_api.repos.get.side_effect = [
            HTTP403ForbiddenError("url", {}, ""),
            None,
        ]
        rl = MagicMock()
        rl.resources.core.remaining = 10
        mock_api.rate_limit.get.return_value = rl
        logger = MagicMock()

        create_repo_on_github("org", "repo", logger, token="tok")

        mock_api.rate_limit.get.assert_called()


class TestGeneratePatch:
    def test_returns_patch_with_newlines(self):
        mock_repo = MagicMock()
        mock_repo.git.diff.return_value = "patch content"

        result = generate_patch_between_commits(mock_repo, "abc", "def")

        assert result == "patch content\n\n"

    def test_git_error_raises_exception(self):
        import git

        mock_repo = MagicMock()
        mock_repo.git.diff.side_effect = git.GitCommandError("diff", "err")

        with pytest.raises(Exception, match="Error generating patch"):
            generate_patch_between_commits(mock_repo, "abc", "def")

    def test_excludes_pdf_files(self):
        mock_repo = MagicMock()
        mock_repo.git.diff.return_value = ""

        generate_patch_between_commits(mock_repo, "abc", "def")

        call_args = mock_repo.git.diff.call_args[0]
        assert ":(exclude)*.pdf" in call_args


class TestGetActiveBranch:
    @patch(f"{MODULE}.git.Repo")
    def test_returns_branch_name(self, mock_repo_cls):
        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        mock_repo.active_branch.name = "main"

        assert get_active_branch("/some/path") == "main"

    @patch(f"{MODULE}.git.Repo")
    def test_detached_head_raises(self, mock_repo_cls):
        mock_repo = MagicMock()
        mock_repo_cls.return_value = mock_repo
        type(mock_repo).active_branch = PropertyMock(
            side_effect=TypeError("HEAD is a detached symbolic reference")
        )

        with pytest.raises(Exception, match="detached HEAD"):
            get_active_branch("/some/path")


class TestLoadDatasetFromConfig:
    def test_json_file_loads(self, tmp_path):
        data = [{"id": 1}, {"id": 2}]
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data))

        result = load_dataset_from_config(str(f))
        assert result == data

    def test_json_with_data_key(self, tmp_path):
        data = {"data": [1, 2, 3]}
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data))

        result = load_dataset_from_config(str(f))
        assert result == [1, 2, 3]

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            load_dataset_from_config("/nonexistent/path/data.json")

    def test_invalid_json_structure_raises(self, tmp_path):
        data = {"other": "stuff"}
        f = tmp_path / "data.json"
        f.write_text(json.dumps(data))

        with pytest.raises(ValueError, match="Unexpected JSON structure"):
            load_dataset_from_config(str(f))

    @patch("datasets.load_dataset")
    def test_hf_dataset_fallback(self, mock_load_ds):
        mock_load_ds.return_value = [{"text": "hello"}]

        result = load_dataset_from_config("org/dataset_name")

        mock_load_ds.assert_called_once_with("org/dataset_name", split="test")
        assert result == [{"text": "hello"}]
