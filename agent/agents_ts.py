"""TypeScript-specific Aider agent — uses 'typescript' lint key and TS system prompt."""

import sys
import logging
from pathlib import Path
from typing import Any, Optional

from agent.agents import (
    AiderAgents,
    AgentReturn,
    AiderReturn,
    handle_logging,
    _apply_thinking_capture_patches,
)
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.agent_utils_ts import summarize_test_output_ts

from aider.coders import Coder
from aider.io import InputOutput

_logger = logging.getLogger(__name__)

_TS_SYSTEM_PROMPT_PATH = Path(__file__).parent / "ts_system_prompt.md"


def _load_ts_system_prompt() -> str:
    """Load the TS-specific system prompt from the markdown file."""
    try:
        return _TS_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        _logger.warning(
            "TS system prompt not found at %s — using empty string",
            _TS_SYSTEM_PROMPT_PATH,
        )
        return ""


class TsAiderAgents(AiderAgents):
    """TS-specific Aider agent — uses 'typescript' lint key and TS system prompt."""

    def run(
        self,
        message: str,
        test_cmd: str,
        lint_cmd: str,
        fnames: list[str],
        log_dir: Path,
        test_first: bool = False,
        lint_first: bool = False,
        thinking_capture: Optional[ThinkingCapture] = None,
        current_stage: str = "",
        current_module: str = "",
        max_test_output_length: int = 0,
        spec_summary_max_tokens: int = 4000,
    ) -> AgentReturn:
        """Start aider agent for TypeScript repos."""
        if test_cmd:
            auto_test = True
        else:
            auto_test = False
        if lint_cmd:
            auto_lint = True
        else:
            auto_lint = False
        log_dir = log_dir.resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        input_history_file = log_dir / ".aider.input.history"
        chat_history_file = log_dir / ".aider.chat.history.md"

        log_file = log_dir / "aider.log"

        # Prepend TS system prompt to message
        ts_prompt = _load_ts_system_prompt()
        if ts_prompt and message:
            message = ts_prompt + "\n\n" + message
        elif ts_prompt and not message:
            # For test_first / lint_first modes, message may be empty
            pass

        # Redirect print statements to the log file
        _saved_stdout = sys.stdout
        _saved_stderr = sys.stderr
        try:
            out_fh = open(log_file, "a")
            try:
                err_fh = open(log_file, "a")
            except OSError:
                out_fh.close()
                raise
            sys.stdout = out_fh
            sys.stderr = err_fh
        except OSError as e:
            _logger.error("Failed to redirect stdout/stderr to %s: %s", log_file, e)
            raise

        try:
            # Configure httpx and backoff logging
            handle_logging("httpx", log_file)
            handle_logging("backoff", log_file)

            io = InputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")
            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds={"typescript": lint_cmd},
                test_cmd=test_cmd,
                io=io,
                cache_prompts=self.cache_prompts,
                detect_urls=False,
            )
            coder.max_reflections = self.max_iteration
            coder.stream = True

            # TS-specific system prompt addition
            coder.gpt_prompts.main_system += (
                "\n\nNEVER edit test files. NEVER create new test files. Test files are"
                " read-only reference material. If a test file is provided, use it ONLY"
                " to understand expected behavior. Only modify implementation/source files"
                " to make the tests pass."
                "\n\nIMPORTANT: If you see failing tests, it means the SOURCE code has"
                ' unimplemented functions (bodies with `throw new Error("STUB")`).'
                " Fix failures by IMPLEMENTING the source functions, NOT by writing new tests."
                " The test suite is already complete — your job is to write the implementation"
                " code that makes existing tests pass."
            )

            _test_summarizer_costs: list[SummarizerCost] = []

            _api_base = ""
            _api_key = ""
            if hasattr(self, "model") and self.model.extra_params:
                _api_base = self.model.extra_params.get("api_base", "")
                _api_key = self.model.extra_params.get("api_key", "")

            if max_test_output_length > 0:
                _original_cmd_test = coder.commands.cmd_test
                _max_len = max_test_output_length
                _model = self.model_name
                _max_tok = spec_summary_max_tokens

                def _wrapped_cmd_test(test_cmd_arg: str) -> str:
                    raw = _original_cmd_test(test_cmd_arg)
                    if raw and len(raw) > _max_len:
                        result, costs = summarize_test_output_ts(
                            raw,
                            max_length=_max_len,
                            model=_model,
                            max_tokens=_max_tok,
                            api_base=_api_base,
                            api_key=_api_key,
                        )
                        _test_summarizer_costs.extend(costs)
                        return result
                    return raw

                coder.commands.cmd_test = _wrapped_cmd_test

            if thinking_capture is not None:
                _apply_thinking_capture_patches(
                    coder, thinking_capture, current_stage, current_module
                )

            if thinking_capture is not None and coder.abs_fnames:
                rel_files = sorted(coder.get_inchat_relative_files())
                if rel_files:
                    thinking_capture.add_user_turn(
                        content="[files:read]\n" + "\n".join(rel_files),
                        stage=current_stage,
                        module=current_module,
                        turn_number=0,
                    )

            if thinking_capture is not None:
                _prev_cmd_test = coder.commands.cmd_test

                def _capturing_cmd_test(test_cmd_arg: str) -> str:
                    result = _prev_cmd_test(test_cmd_arg)
                    thinking_capture.add_user_turn(
                        content=f"[tool:cmd_test] {test_cmd_arg}",
                        stage=current_stage,
                        module=current_module,
                        turn_number=len(thinking_capture.turns),
                    )
                    if result:
                        thinking_capture.add_assistant_turn(
                            content=f"[tool:cmd_test:result] {result[:2000]}",
                            thinking=None,
                            thinking_tokens=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cache_hit_tokens=0,
                            cache_write_tokens=0,
                            cost=0.0,
                            stage=current_stage,
                            module=current_module,
                            turn_number=len(thinking_capture.turns),
                        )
                    return result

                coder.commands.cmd_test = _capturing_cmd_test

                _prev_cmd_lint = coder.commands.cmd_lint

                def _capturing_cmd_lint(**kwargs: Any) -> str:
                    result = _prev_cmd_lint(**kwargs)
                    thinking_capture.add_user_turn(
                        content=f"[tool:cmd_lint] {kwargs}",
                        stage=current_stage,
                        module=current_module,
                        turn_number=len(thinking_capture.turns),
                    )
                    if result:
                        thinking_capture.add_assistant_turn(
                            content=f"[tool:cmd_lint:result] {result[:2000]}",
                            thinking=None,
                            thinking_tokens=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cache_hit_tokens=0,
                            cache_write_tokens=0,
                            cost=0.0,
                            stage=current_stage,
                            module=current_module,
                            turn_number=len(thinking_capture.turns),
                        )
                    return result

                coder.commands.cmd_lint = _capturing_cmd_lint

            # Run the agent
            if test_first:
                test_errors = coder.commands.cmd_test(test_cmd)
                if test_errors:
                    _logger.info("Running coder with test errors for %s", fnames)
                    coder.run(test_errors)
                    _logger.info("Coder finished for %s", fnames)
            elif lint_first:
                _logger.info("Running lint-first for %s", fnames)
                coder.commands.cmd_lint(fnames=fnames)
                _logger.info("Lint finished for %s", fnames)
            else:
                max_input = self.model.info.get("max_input_tokens", 0)
                if max_input > 0:
                    estimated_tokens = len(message) // 4
                    if estimated_tokens > max_input:
                        logger = logging.getLogger(__name__)
                        logger.warning(
                            f"Skipping: message ~{estimated_tokens} tokens exceeds "
                            f"max_input_tokens {max_input} for {fnames}"
                        )
                        print(
                            f"WARNING: Skipping {fnames}: ~{estimated_tokens} tokens exceeds max_input_tokens {max_input}",
                            file=_saved_stderr,
                        )
                        return AiderReturn(log_file)
                _logger.info("Running coder for %s", fnames)
                coder.run(message)
                _logger.info("Coder finished for %s", fnames)
        finally:
            if sys.stdout is not _saved_stdout:
                try:
                    sys.stdout.close()
                except Exception:
                    _logger.debug("Failed to close redirected stdout", exc_info=True)
            if sys.stderr is not _saved_stderr:
                try:
                    sys.stderr.close()
                except Exception:
                    _logger.debug("Failed to close redirected stderr", exc_info=True)
            sys.stdout = _saved_stdout
            sys.stderr = _saved_stderr

        agent_return = AiderReturn(log_file)
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        if thinking_capture is not None:
            for c in _test_summarizer_costs:
                thinking_capture.summarizer_costs.add(c)

        return agent_return
