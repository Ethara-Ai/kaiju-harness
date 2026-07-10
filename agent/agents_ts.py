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
    raise_if_transient_llm_error,
)
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.agent_utils_ts import summarize_test_output_ts

from aider.coders import Coder
from agent.guarded_io import GuardedInputOutput

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
        test_files_readonly: Optional[list[str]] = None,
        inject_test_files_readonly: bool = True,
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

            io = GuardedInputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
                allowed_add_paths=fnames,  # restrict edits to the target module only
                protected_paths=set(test_files_readonly or []),
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")
            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                read_only_fnames=test_files_readonly if inject_test_files_readonly and test_files_readonly else [],
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds={"typescript": lint_cmd},
                test_cmd=test_cmd,
                io=io,
                cache_prompts=self.cache_prompts,
                detect_urls=False,
            )
            if test_files_readonly and inject_test_files_readonly:
                coder.max_reflections = min(self.max_iteration, 5)
            else:
                coder.max_reflections = self.max_iteration
            coder.stream = True

            # TS-specific system prompt addition
            if inject_test_files_readonly:
                coder.gpt_prompts.main_system += (
                    "\n\nNEVER edit test files. NEVER create new test files. "
                    "Test files are read-only reference material \u2014 use ONLY to understand expected behavior. "
                    "Modify implementation/source files to make tests pass."
                )
            else:
                coder.gpt_prompts.main_system += (
                    "\n\nTest files are UNAVAILABLE. NEVER ask to see them. NEVER request paths under tests/. "
                    "If aider prompts you to add a test file, the request will be REFUSED \u2014 do not retry."
                    "\n\nYour job is SPEC-DRIVEN implementation:"
                    "\n  1. Read the source files in /chat; identify unimplemented stubs (`throw new Error(\"STUB\")`)."
                    "\n  2. Infer expected behavior from function signatures, type hints, docstrings, and the library specification."
                    "\n  3. Implement from first principles \u2014 do NOT reverse-engineer from test outputs."
                    "\n  4. Test feedback is intentionally minimal (counts only). Use it as a yes/no signal, not as a debugging aid."
                    "\n  5. If you cannot infer behavior for a function, leave a TODO comment and move on. Do not stall."
                    "\n\nThe test suite is complete and frozen. Your only output is implementation code in src/."
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

        # (c) backstop: if aider SWALLOWED a transient LLM error into the session
        # output (printed but did not re-raise), convert it into a TransientLLMError
        # so run_with_recovery re-runs the module. Read the captured session text
        # from log_file AFTER stdout/stderr are restored. Raised OUTSIDE the try/
        # except above so it propagates to the run_with_recovery wrapper. Only fires
        # on transient signals (helper guards this) — genuine failures aren't retried.
        # Scan ALL streams aider may record a swallowed transient in — aider.log
        # AND the chat/llm history. A MidStreamFallbackError lands in
        # .aider.chat.history.md but NOT aider.log, so reading only log_file
        # missed it -> no retry -> silently incomplete module.
        _session_text = ""
        for _p in (log_file, chat_history_file, log_dir / "llm_history.txt"):
            try:
                _session_text += "\n" + Path(_p).read_text(errors="replace")
            except OSError:
                continue
        raise_if_transient_llm_error(_session_text, context=f"module {current_module}")

        agent_return = AiderReturn(log_file)
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        # NOTE: do NOT add _test_summarizer_costs to
        # thinking_capture.summarizer_costs. The test-output summarizer runs
        # INSIDE the module capture window (wrapped cmd_test during agent.run),
        # so its litellm call is already recorded in the per-module call-log
        # (grand_cost). Adding it here too double-counts it in get_metrics
        # (grand_cost + summarizer_costs). It is still reported via
        # agent_return.test_summarizer_cost and shows in by_source['our_summarizer'].

        return agent_return
