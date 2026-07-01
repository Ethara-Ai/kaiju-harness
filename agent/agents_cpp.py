"""C++ variant of AiderAgents.

Copies ``AiderAgents.run()`` with exactly two changes:

1. ``lint_cmds={"c++": lint_cmd}`` (aider's ``filename_to_lang()`` returns ``"c++"`` for .cpp/.hpp)
2. Python-specific system prompt replaced with C++ system prompt from
   ``agent/prompts/cpp_system_prompt.md``

Everything else (thinking capture, cost tracking, test summarization) is inherited
unchanged from the parent class.

MAINTENANCE NOTE: This file copies the body of ``AiderAgents.run()`` from
``agent/agents.py``.  Lines changed from the original are marked with
``# CPP CHANGE``.  If the parent method changes, this copy must be updated.
"""

import sys
import logging
from pathlib import Path
from typing import Any, Optional

from aider.coders import Coder
from agent.guarded_io import GuardedInputOutput

from agent.agents import AiderAgents, AiderReturn, AgentReturn, handle_logging, _apply_thinking_capture_patches
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.agent_utils import summarize_test_output

_logger = logging.getLogger(__name__)

_CPP_PROMPT_PATH = Path(__file__).parent / "prompts" / "cpp_system_prompt.md"


class CppAiderAgents(AiderAgents):
    """AiderAgents subclass for C++ repositories.

    Inherits ``__init__`` unchanged.  Overrides ``run()`` only.
    """

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
        """Start aider agent (C++ variant)."""
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

        _saved_stdout = sys.stdout
        _saved_stderr = sys.stderr
        try:
            _log_handle = open(log_file, "a")
        except OSError as e:
            _logger.error("Failed to redirect stdout/stderr to %s: %s", log_file, e)
            raise
        sys.stdout = _log_handle
        sys.stderr = _log_handle

        try:
            handle_logging("httpx", log_file)
            handle_logging("backoff", log_file)

            io = GuardedInputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
                protected_paths=set(test_files_readonly or []),
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")
            _read_only = (test_files_readonly or []) if inject_test_files_readonly else []
            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                read_only_fnames=_read_only,
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds={"c++": lint_cmd},  # CPP CHANGE 1: key is "c++", not "python"
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

            # CPP CHANGE 2: System prompt — read-only vs blind mode
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
                    "\n  1. Read the source files in /chat; identify unimplemented stubs (`throw std::runtime_error(\"STUB: not implemented\")`).",
                    "\n  2. Infer expected behavior from function signatures, type hints, docstrings, and the library specification."
                    "\n  3. Implement from first principles \u2014 do NOT reverse-engineer from test outputs."
                    "\n  4. Test feedback is intentionally minimal (counts only). Use it as a yes/no signal, not as a debugging aid."
                    "\n  5. If you cannot infer behavior for a function, leave a TODO comment and move on. Do not stall."
                    "\n\nThe test suite is complete and frozen. Your only output is implementation code in src/."
                )

            _test_summarizer_costs: list[SummarizerCost] = []

            if max_test_output_length > 0:
                _original_cmd_test = coder.commands.cmd_test
                _max_len = max_test_output_length
                _model = self.model_name
                _max_tok = spec_summary_max_tokens

                def _wrapped_cmd_test(test_cmd_arg: str) -> str:
                    raw = _original_cmd_test(test_cmd_arg)
                    if raw and len(raw) > _max_len:
                        result, costs = summarize_test_output(
                            raw,
                            max_length=_max_len,
                            model=_model,
                            max_tokens=_max_tok,
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
            sys.stdout = _saved_stdout
            sys.stderr = _saved_stderr
            try:
                _log_handle.close()
            except Exception:
                _logger.debug("Failed to close redirected stdout/stderr", exc_info=True)

        agent_return = AiderReturn(log_file)
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        if thinking_capture is not None:
            for c in _test_summarizer_costs:
                thinking_capture.summarizer_costs.add(c)

        return agent_return
