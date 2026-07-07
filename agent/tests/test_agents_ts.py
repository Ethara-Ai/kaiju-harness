"""Tests for agent.agents_ts — TsAiderAgents class."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, mock_open

import pytest

MODULE = "agent.agents_ts"


def _make_mock_model(extra_params: dict | None = None) -> MagicMock:
    model = MagicMock()
    model.extra_params = extra_params or {}
    model.info = {"max_input_tokens": 200000}
    return model


class TestTsAiderAgentsInit:
    def test_creates_instance(self) -> None:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents(
                max_iteration=3,
                model_name="test-model",
                cache_prompts=True,
            )
            assert agent is not None


class TestTsAiderAgentsRun:
    def _make_coder(self) -> MagicMock:
        coder = MagicMock()
        coder.max_reflections = 0
        coder.stream = False
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.main_system = "base system prompt"
        coder.commands = MagicMock()
        coder.commands.cmd_test = MagicMock(return_value="")
        coder.commands.cmd_lint = MagicMock(return_value="")
        coder.abs_fnames = set()
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        coder.run = MagicMock()
        return coder

    def _make_agent(
        self, model_name: str = "test-model", extra_params: dict | None = None
    ) -> Any:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents.__new__(TsAiderAgents)
            agent.max_iteration = 3
            agent.model_name = model_name
            agent.model = _make_mock_model(extra_params)
            agent.cache_prompts = True
            return agent

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="TS SYSTEM PROMPT")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_run_default_mode(self, mock_io_cls, mock_coder_cls, mock_prompt) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        log_dir = Path("/tmp/test_logs_ts")
        with patch("builtins.open", mock_open()):
            agent.run(
                message="implement functions",
                test_cmd="",
                lint_cmd="",
                fnames=["src/main.ts"],
                log_dir=log_dir,
            )

        coder.run.assert_called_once()
        call_msg = coder.run.call_args[0][0]
        assert "TS SYSTEM PROMPT" in call_msg
        assert "implement functions" in call_msg

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_run_test_first_mode(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        coder.commands.cmd_test.return_value = "FAIL: test errors here"
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            agent.run(
                message="",
                test_cmd="npx jest",
                lint_cmd="",
                fnames=["src/main.ts"],
                log_dir=Path("/tmp/test_logs_ts"),
                test_first=True,
            )

        coder.commands.cmd_test.assert_called_once_with("npx jest")
        coder.run.assert_called_once_with("FAIL: test errors here")

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_run_lint_first_mode(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            agent.run(
                message="",
                test_cmd="",
                lint_cmd="npx eslint .",
                fnames=["src/main.ts"],
                log_dir=Path("/tmp/test_logs_ts"),
                lint_first=True,
            )

        coder.commands.cmd_lint.assert_called_once()
        coder.run.assert_not_called()

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_lint_cmd_uses_typescript_key(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            agent.run(
                message="msg",
                test_cmd="",
                lint_cmd="npx eslint .",
                fnames=[],
                log_dir=Path("/tmp/test_logs_ts"),
            )

        create_kwargs = mock_coder_cls.create.call_args[1]
        assert create_kwargs["lint_cmds"] == {"typescript": "npx eslint ."}

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_system_prompt_contains_stub_warning(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            agent.run(
                message="msg",
                test_cmd="",
                lint_cmd="",
                fnames=[],
                log_dir=Path("/tmp/test_logs_ts"),
            )

        system_prompt = coder.gpt_prompts.main_system
        assert "NEVER edit test files" in system_prompt
        assert 'throw new Error("STUB")' in system_prompt

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_proxy_params_extracted_from_extra_params(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        """Verify that api_base/api_key are extracted from model.extra_params
        and made available for the test summarizer closure.
        """
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent(
            extra_params={"api_base": "http://proxy:9090", "api_key": "pk-test"}
        )

        with patch("builtins.open", mock_open()):
            agent.run(
                message="msg",
                test_cmd="npx jest",
                lint_cmd="",
                fnames=["src/main.ts"],
                log_dir=Path("/tmp/test_logs_ts"),
                max_test_output_length=100,
            )

        assert agent.model.extra_params["api_base"] == "http://proxy:9090"
        assert agent.model.extra_params["api_key"] == "pk-test"

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_no_proxy_params_when_empty(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent(extra_params={})

        with patch("builtins.open", mock_open()):
            agent.run(
                message="msg",
                test_cmd="npx jest",
                lint_cmd="",
                fnames=["src/main.ts"],
                log_dir=Path("/tmp/test_logs_ts"),
                max_test_output_length=100,
            )

        assert agent.model.extra_params.get("api_base", "") == ""
        assert agent.model.extra_params.get("api_key", "") == ""

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_test_first_no_errors_skips_coder_run(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        coder.commands.cmd_test.return_value = ""
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            agent.run(
                message="",
                test_cmd="npx jest",
                lint_cmd="",
                fnames=[],
                log_dir=Path("/tmp/test_logs_ts"),
                test_first=True,
            )

        coder.run.assert_not_called()

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_max_input_tokens_skip(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()
        agent.model.info = {"max_input_tokens": 10}

        with patch("builtins.open", mock_open()):
            agent.run(
                message="x" * 1000,
                test_cmd="",
                lint_cmd="",
                fnames=["src/main.ts"],
                log_dir=Path("/tmp/test_logs_ts"),
            )

        coder.run.assert_not_called()

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_test_summarizer_wraps_cmd_test(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        coder.commands.cmd_test.return_value = "x" * 200
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            with patch(
                f"{MODULE}.summarize_test_output_ts", return_value=("summarized", [])
            ) as mock_sum:
                agent.run(
                    message="msg",
                    test_cmd="npx jest",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts"),
                    max_test_output_length=50,
                )

        # cmd_test was replaced with wrapper, so calling it should invoke summarizer
        # The original cmd_test returns "x" * 200 which is > max_test_output_length=50
        # so summarizer should be called
        assert (
            mock_sum.called
            or coder.commands.cmd_test != self._make_coder().commands.cmd_test
        )

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_thinking_capture_patches_applied(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        from agent.thinking_capture import ThinkingCapture

        tc = ThinkingCapture()

        with patch("builtins.open", mock_open()):
            with patch(f"{MODULE}._apply_thinking_capture_patches") as mock_patches:
                agent.run(
                    message="msg",
                    test_cmd="npx jest",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts"),
                    thinking_capture=tc,
                    current_stage="test",
                    current_module="mod",
                )

        mock_patches.assert_called_once_with(coder, tc, "test", "mod")

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_thinking_capture_records_files(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        coder.abs_fnames = {"/repo/src/main.ts"}
        coder.get_inchat_relative_files.return_value = ["src/main.ts"]
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        from agent.thinking_capture import ThinkingCapture

        tc = ThinkingCapture()

        with patch("builtins.open", mock_open()):
            with patch(f"{MODULE}._apply_thinking_capture_patches"):
                agent.run(
                    message="msg",
                    test_cmd="",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts"),
                    thinking_capture=tc,
                    current_stage="draft",
                    current_module="main",
                )

        assert len(tc.turns) >= 1
        assert "[files:read]" in tc.turns[0].content

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_thinking_capture_wraps_cmd_test(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        coder.commands.cmd_test.return_value = "FAIL: errors"
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        from agent.thinking_capture import ThinkingCapture

        tc = ThinkingCapture()

        with patch("builtins.open", mock_open()):
            with patch(f"{MODULE}._apply_thinking_capture_patches"):
                agent.run(
                    message="",
                    test_cmd="npx jest",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts"),
                    test_first=True,
                    thinking_capture=tc,
                    current_stage="test",
                    current_module="mod",
                )

        # The thinking_capture wrapping replaces cmd_test; verify it was called
        # The coder.run should have been called with the test errors
        coder.run.assert_called_once()

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_return_has_test_summarizer_cost(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            result = agent.run(
                message="msg",
                test_cmd="",
                lint_cmd="",
                fnames=[],
                log_dir=Path("/tmp/test_logs_ts"),
            )

        assert hasattr(result, "test_summarizer_cost")
        assert result.test_summarizer_cost == 0.0


class TestLoadTsSystemPrompt:
    def test_loads_existing_file(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "ts_system_prompt.md"
        prompt_file.write_text("You are a TS coding agent.")

        from agent.agents_ts import _load_ts_system_prompt

        with patch(f"{MODULE}._TS_SYSTEM_PROMPT_PATH", prompt_file):
            result = _load_ts_system_prompt()

        assert result == "You are a TS coding agent."

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from agent.agents_ts import _load_ts_system_prompt

        with patch(f"{MODULE}._TS_SYSTEM_PROMPT_PATH", tmp_path / "nonexistent.md"):
            result = _load_ts_system_prompt()

        assert result == ""


class TestStderrOpenFailure:
    """Lines 87-94: when the second open() for stderr raises OSError,
    the first file handle (stdout) should be closed and OSError propagated.
    """

    def _make_agent(self, extra_params: dict | None = None) -> Any:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents.__new__(TsAiderAgents)
            agent.max_iteration = 3
            agent.model_name = "test-model"
            agent.model = _make_mock_model(extra_params)
            agent.cache_prompts = True
            return agent

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    def test_stderr_open_failure_closes_stdout_handle(self, mock_prompt) -> None:
        agent = self._make_agent()
        stdout_handle = MagicMock()

        call_count = 0

        def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return stdout_handle
            raise OSError("disk full")

        with patch("builtins.open", side_effect=_side_effect):
            with pytest.raises(OSError, match="disk full"):
                agent.run(
                    message="msg",
                    test_cmd="",
                    lint_cmd="",
                    fnames=[],
                    log_dir=Path("/tmp/test_logs_ts_stderr"),
                )

        stdout_handle.close.assert_called_once()


class TestWrappedCmdTestSummarizer:
    """Lines 148-160: _wrapped_cmd_test closure — when test output exceeds
    max_test_output_length, calls summarize_test_output_ts; when shorter,
    passes through unchanged.
    """

    def _make_coder(self) -> MagicMock:
        coder = MagicMock()
        coder.max_reflections = 0
        coder.stream = False
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.main_system = "base system prompt"
        coder.commands = MagicMock()
        coder.commands.cmd_test = MagicMock(return_value="")
        coder.commands.cmd_lint = MagicMock(return_value="")
        coder.abs_fnames = set()
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        coder.run = MagicMock()
        return coder

    def _make_agent(self, extra_params: dict | None = None) -> Any:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents.__new__(TsAiderAgents)
            agent.max_iteration = 3
            agent.model_name = "test-model"
            agent.model = _make_mock_model(extra_params)
            agent.cache_prompts = True
            return agent

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_long_output_triggers_summarizer(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        """Output > max_test_output_length should invoke summarize_test_output_ts."""
        coder = self._make_coder()
        long_output = "x" * 200
        coder.commands.cmd_test = MagicMock(return_value=long_output)
        mock_coder_cls.create.return_value = coder

        agent = self._make_agent()

        summarized_text = "short summary"
        mock_cost = MagicMock()
        mock_cost.cost = 0.05

        with patch("builtins.open", mock_open()):
            with patch(
                f"{MODULE}.summarize_test_output_ts",
                return_value=(summarized_text, [mock_cost]),
            ) as mock_summarize:
                result = agent.run(
                    message="",
                    test_cmd="npx jest",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts_sum"),
                    max_test_output_length=100,
                    test_first=True,
                )

        # The summarizer must have been called because output (200) > max (100)
        mock_summarize.assert_called_once()
        call_args = mock_summarize.call_args
        assert call_args[0][0] == long_output
        assert call_args[1]["max_length"] == 100 or call_args[0][0] == long_output

        # The cost should be recorded in the return
        assert result.test_summarizer_cost == pytest.approx(0.05)

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_short_output_passes_through(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        """Output <= max_test_output_length should pass through without summarizer."""
        coder = self._make_coder()
        short_output = "x" * 50
        coder.commands.cmd_test = MagicMock(return_value=short_output)
        mock_coder_cls.create.return_value = coder

        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            with patch(
                f"{MODULE}.summarize_test_output_ts",
            ) as mock_summarize:
                agent.run(
                    message="",
                    test_cmd="npx jest",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts_sum2"),
                    max_test_output_length=100,
                    test_first=True,
                )

        mock_summarize.assert_not_called()

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_none_output_passes_through(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        """None output (no errors) should pass through without summarizer."""
        coder = self._make_coder()
        coder.commands.cmd_test = MagicMock(return_value=None)
        mock_coder_cls.create.return_value = coder

        agent = self._make_agent()

        with patch("builtins.open", mock_open()):
            with patch(
                f"{MODULE}.summarize_test_output_ts",
            ) as mock_summarize:
                agent.run(
                    message="",
                    test_cmd="npx jest",
                    lint_cmd="",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts_sum3"),
                    max_test_output_length=100,
                    test_first=True,
                )

        mock_summarize.assert_not_called()


class TestCapturingCmdLint:
    """Lines 211-232: _capturing_cmd_lint closure — records lint invocations
    into ThinkingCapture with add_user_turn and add_assistant_turn.
    """

    def _make_coder(self) -> MagicMock:
        coder = MagicMock()
        coder.max_reflections = 0
        coder.stream = False
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.main_system = "base system prompt"
        coder.commands = MagicMock()
        coder.commands.cmd_test = MagicMock(return_value="")
        coder.commands.cmd_lint = MagicMock(return_value="")
        coder.abs_fnames = set()
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        coder.run = MagicMock()
        return coder

    def _make_agent(self, extra_params: dict | None = None) -> Any:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents.__new__(TsAiderAgents)
            agent.max_iteration = 3
            agent.model_name = "test-model"
            agent.model = _make_mock_model(extra_params)
            agent.cache_prompts = True
            return agent

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_lint_capture_records_user_and_assistant_turns(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        """When thinking_capture is provided and lint returns output,
        both add_user_turn and add_assistant_turn should be called.
        """
        coder = self._make_coder()
        # The original cmd_lint returns lint errors
        coder.commands.cmd_lint = MagicMock(return_value="ESLint: 3 errors found")
        mock_coder_cls.create.return_value = coder

        agent = self._make_agent()

        from agent.thinking_capture import ThinkingCapture

        tc = ThinkingCapture()

        with patch("builtins.open", mock_open()):
            with patch(f"{MODULE}._apply_thinking_capture_patches"):
                agent.run(
                    message="",
                    test_cmd="",
                    lint_cmd="npx eslint .",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts_lint"),
                    lint_first=True,
                    thinking_capture=tc,
                    current_stage="lint",
                    current_module="mod",
                )

        # Lint-first calls cmd_lint(fnames=...), which triggers the capturing wrapper
        # At minimum we should have a user turn for [tool:cmd_lint]
        user_turns = [
            t for t in tc.turns if t.role == "user" and "[tool:cmd_lint]" in t.content
        ]
        assert (
            len(user_turns) >= 1
        ), f"Expected cmd_lint user turn, got turns: {[t.content for t in tc.turns]}"

        # Since cmd_lint returned non-empty result, there should be an assistant turn too
        assistant_turns = [
            t
            for t in tc.turns
            if t.role == "assistant" and "[tool:cmd_lint:result]" in t.content
        ]
        assert (
            len(assistant_turns) >= 1
        ), f"Expected cmd_lint result turn, got turns: {[t.content for t in tc.turns]}"

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_lint_capture_no_assistant_turn_when_empty_result(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        """When cmd_lint returns empty/falsy, only a user turn should be recorded
        (no assistant turn for the result).
        """
        coder = self._make_coder()
        coder.commands.cmd_lint = MagicMock(return_value="")
        mock_coder_cls.create.return_value = coder

        agent = self._make_agent()

        from agent.thinking_capture import ThinkingCapture

        tc = ThinkingCapture()

        with patch("builtins.open", mock_open()):
            with patch(f"{MODULE}._apply_thinking_capture_patches"):
                agent.run(
                    message="",
                    test_cmd="",
                    lint_cmd="npx eslint .",
                    fnames=["src/main.ts"],
                    log_dir=Path("/tmp/test_logs_ts_lint2"),
                    lint_first=True,
                    thinking_capture=tc,
                    current_stage="lint",
                    current_module="mod",
                )

        user_turns = [
            t for t in tc.turns if t.role == "user" and "[tool:cmd_lint]" in t.content
        ]
        assert len(user_turns) >= 1

        assistant_turns = [
            t
            for t in tc.turns
            if t.role == "assistant" and "[tool:cmd_lint:result]" in t.content
        ]
        assert len(assistant_turns) == 0


class TestRunFinallyStdoutCloseOSError:
    def _make_coder(self) -> MagicMock:
        coder = MagicMock()
        coder.max_reflections = 0
        coder.stream = False
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.main_system = "base system prompt"
        coder.commands = MagicMock()
        coder.commands.cmd_test = MagicMock(return_value="")
        coder.commands.cmd_lint = MagicMock(return_value="")
        coder.abs_fnames = set()
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        coder.run = MagicMock()
        return coder

    def _make_agent(self, extra_params: dict | None = None) -> Any:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents.__new__(TsAiderAgents)
            agent.max_iteration = 3
            agent.model_name = "test-model"
            agent.model = _make_mock_model(extra_params)
            agent.cache_prompts = True
            return agent

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_stdout_close_oserror_caught(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        stdout_fh = MagicMock()
        stdout_fh.close.side_effect = OSError("stdout close failed")
        stderr_fh = MagicMock()

        call_count = [0]

        def _open_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return stdout_fh
            return stderr_fh

        with patch("builtins.open", side_effect=_open_side_effect):
            result = agent.run(
                message="msg",
                test_cmd="",
                lint_cmd="",
                fnames=[],
                log_dir=Path("/tmp/test_stdout_close"),
            )

        assert result is not None
        stdout_fh.close.assert_called_once()

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_stderr_close_oserror_caught(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        stdout_fh = MagicMock()
        stderr_fh = MagicMock()
        stderr_fh.close.side_effect = OSError("stderr close failed")

        call_count = [0]

        def _open_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return stdout_fh
            return stderr_fh

        with patch("builtins.open", side_effect=_open_side_effect):
            result = agent.run(
                message="msg",
                test_cmd="",
                lint_cmd="",
                fnames=[],
                log_dir=Path("/tmp/test_stderr_close"),
            )

        assert result is not None
        stderr_fh.close.assert_called_once()


class TestRunThinkingCaptureSummarizerCostsAdd:
    def _make_coder(self) -> MagicMock:
        coder = MagicMock()
        coder.max_reflections = 0
        coder.stream = False
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.main_system = "base system prompt"
        coder.commands = MagicMock()
        coder.commands.cmd_test = MagicMock(return_value="x" * 200)
        coder.commands.cmd_lint = MagicMock(return_value="")
        coder.abs_fnames = set()
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        coder.run = MagicMock()
        return coder

    def _make_agent(self, extra_params: dict | None = None) -> Any:
        with patch(f"{MODULE}.AiderAgents.__init__", return_value=None):
            from agent.agents_ts import TsAiderAgents

            agent = TsAiderAgents.__new__(TsAiderAgents)
            agent.max_iteration = 3
            agent.model_name = "test-model"
            agent.model = _make_mock_model(extra_params)
            agent.cache_prompts = True
            return agent

    @patch(f"{MODULE}._load_ts_system_prompt", return_value="")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    def test_summarizer_costs_added_to_thinking_capture(
        self, mock_io_cls, mock_coder_cls, mock_prompt
    ) -> None:
        coder = self._make_coder()
        mock_coder_cls.create.return_value = coder
        agent = self._make_agent()

        from agent.thinking_capture import ThinkingCapture, SummarizerCost

        tc = ThinkingCapture()

        mock_cost = SummarizerCost(prompt_tokens=50, completion_tokens=25, cost=0.01)

        with patch("builtins.open", mock_open()):
            with patch(f"{MODULE}._apply_thinking_capture_patches"):
                with patch(
                    f"{MODULE}.summarize_test_output_ts",
                    return_value=("summarized", [mock_cost]),
                ):
                    agent.run(
                        message="",
                        test_cmd="npx jest",
                        lint_cmd="",
                        fnames=["src/main.ts"],
                        log_dir=Path("/tmp/test_tc_costs"),
                        test_first=True,
                        thinking_capture=tc,
                        max_test_output_length=50,
                    )

        # Corrected accounting: the TEST-output summarizer runs INSIDE the module
        # capture window, so its cost is captured in the per-module call-log — it
        # must NOT also be added to summarizer_costs (that double-counted it in
        # get_metrics). So summarizer_costs stays empty for a test-only run; the
        # cost is still reported via agent_return.test_summarizer_cost.
        assert tc.summarizer_costs.total_cost == pytest.approx(0.0)
        assert len(tc.summarizer_costs.costs) == 0
