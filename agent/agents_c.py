"""C-specific aider agent with thinking capture and trajectory support.

Mirrors agents_go.py — configures aider Coder for C source files with
``lint_cmds={"c": ...}``, a C-specific system prompt addition, and full
thinking capture / trajectory support.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from agent.thinking_capture import SummarizerCost, ThinkingCapture

logger = logging.getLogger(__name__)


def handle_logging(logger_name: str, log_file: Path) -> None:
    log = logging.getLogger(logger_name)
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)


def _apply_thinking_capture_patches(
    coder: Any,
    thinking_capture: ThinkingCapture,
    current_stage: str,
    current_module: str,
) -> None:
    """Monkey-patch a Coder instance to capture reasoning tokens.

    Language-agnostic — copied directly from agents_go.py. Applies 6 patches
    that intercept reasoning content at different points in aider's pipeline
    and propagate them through clone() so lint_coder inherits the patches.
    """
    coder._thinking_capture = thinking_capture
    coder._current_stage = current_stage
    coder._current_module = current_module
    coder._turn_counter = getattr(coder, "_turn_counter", 0)
    coder._last_reasoning_content = None
    coder._last_completion_usage = None

    _original_show_send_output = coder.show_send_output
    _original_show_send_output_stream = coder.show_send_output_stream
    _original_add_assistant_reply = coder.add_assistant_reply_to_cur_messages
    _original_send_message = coder.send_message
    _original_show_usage_report = coder.show_usage_report

    coder._snapshot_prompt_tokens = 0
    coder._snapshot_completion_tokens = 0
    coder._snapshot_cost = 0.0
    coder._snapshot_cache_hit_tokens = 0
    coder._snapshot_cache_write_tokens = 0

    def patched_show_send_output(completion: Any) -> None:
        try:
            coder._last_reasoning_content = completion.choices[
                0
            ].message.reasoning_content
        except AttributeError:
            try:
                coder._last_reasoning_content = completion.choices[0].message.reasoning
            except AttributeError:
                coder._last_reasoning_content = None
        coder._last_completion_usage = getattr(completion, "usage", None)
        _original_show_send_output(completion)

    def _reasoning_interceptor(completion: Any) -> Any:
        coder._last_reasoning_content = ""
        for chunk in completion:
            try:
                rc = chunk.choices[0].delta.reasoning_content
            except AttributeError:
                try:
                    rc = chunk.choices[0].delta.reasoning
                except AttributeError:
                    rc = None
            if rc:
                coder._last_reasoning_content += rc
            if hasattr(chunk, "usage") and chunk.usage:
                coder._last_completion_usage = chunk.usage
            yield chunk
        if not coder._last_reasoning_content:
            coder._last_reasoning_content = None

    def patched_show_send_output_stream(completion: Any) -> Any:
        return _original_show_send_output_stream(_reasoning_interceptor(completion))

    def patched_send_message(message: Any, *args: Any, **kwargs: Any) -> Any:
        coder._turn_counter += 1
        if coder._thinking_capture is not None:
            coder._thinking_capture.add_user_turn(
                content=message,
                stage=coder._current_stage,
                module=coder._current_module,
                turn_number=coder._turn_counter,
            )
        return _original_send_message(message, *args, **kwargs)

    def patched_add_assistant_reply() -> None:
        if coder._thinking_capture is not None:
            thinking_tokens = 0
            if coder._last_completion_usage:
                thinking_tokens = (
                    getattr(coder._last_completion_usage, "reasoning_tokens", 0) or 0
                )
            if coder._last_completion_usage:
                getattr(coder._last_completion_usage, "prompt_tokens", 0) or 0
                getattr(coder._last_completion_usage, "completion_tokens", 0) or 0

            coder._thinking_capture.add_assistant_turn(
                content=coder.partial_response_content or "",
                thinking=coder._last_reasoning_content,
                thinking_tokens=thinking_tokens,
                prompt_tokens=coder._snapshot_prompt_tokens,
                completion_tokens=coder._snapshot_completion_tokens,
                cache_hit_tokens=coder._snapshot_cache_hit_tokens,
                cache_write_tokens=coder._snapshot_cache_write_tokens,
                cost=coder._snapshot_cost,
                stage=coder._current_stage,
                module=coder._current_module,
                turn_number=coder._turn_counter,
            )

        _original_add_assistant_reply()

    def patched_show_usage_report() -> None:
        coder._snapshot_prompt_tokens = getattr(coder, "message_tokens_sent", 0)
        coder._snapshot_completion_tokens = getattr(
            coder, "message_tokens_received", 0
        )
        coder._snapshot_cost = getattr(coder, "message_cost", 0.0)

        usage = coder._last_completion_usage
        if usage:
            coder._snapshot_cache_hit_tokens = (
                getattr(usage, "prompt_cache_hit_tokens", 0)
                or getattr(usage, "cache_read_input_tokens", 0)
                or 0
            )
            coder._snapshot_cache_write_tokens = (
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )

        _original_show_usage_report()

    _original_clone = coder.clone

    def patched_clone(*args: Any, **kwargs: Any) -> Any:
        cloned = _original_clone(*args, **kwargs)
        if coder._thinking_capture is not None:
            _apply_thinking_capture_patches(
                cloned,
                coder._thinking_capture,
                coder._current_stage,
                coder._current_module,
            )
        cloned._turn_counter = coder._turn_counter
        return cloned

    coder.show_send_output = patched_show_send_output
    coder.show_send_output_stream = patched_show_send_output_stream
    coder.send_message = patched_send_message
    coder.add_assistant_reply_to_cur_messages = patched_add_assistant_reply
    coder.show_usage_report = patched_show_usage_report
    coder.clone = patched_clone

    _original_apply_updates = coder.apply_updates

    def patched_apply_updates() -> set:
        edited = _original_apply_updates()
        reflected = getattr(coder, "reflected_message", None)
        if reflected and thinking_capture.turns:
            for turn in reversed(thinking_capture.turns):
                if turn.role == "assistant" and turn.module == current_module:
                    turn.edit_error = reflected
                    break
        return edited

    coder.apply_updates = patched_apply_updates


class CAgentReturn(ABC):
    def __init__(self, log_file: Optional[str] = None):
        self.log_file = log_file
        self.last_cost: float = 0.0
        self.test_summarizer_cost: float = 0.0


class CAgents(ABC):
    def __init__(self, max_iteration: int):
        self.max_iteration = max_iteration

    @abstractmethod
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
    ) -> CAgentReturn:
        raise NotImplementedError


class AiderCReturn(CAgentReturn):
    def __init__(self, log_file: Optional[str] = None):
        super().__init__(log_file)
        self.last_cost = self._parse_cost()

    def _parse_cost(self) -> float:
        if not self.log_file or not os.path.exists(self.log_file):
            return 0.0
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            costs = re.findall(r"\$(\d+\.\d+)", content)
            return float(costs[-1]) if costs else 0.0
        except Exception:
            return 0.0


def _register_bedrock_arn_pricing(model_name: str) -> None:
    try:
        from agent.agents import register_bedrock_arn_pricing

        register_bedrock_arn_pricing(model_name)
    except ImportError:
        pass


class AiderCAgents(CAgents):
    def __init__(
        self,
        max_iteration: int,
        model_name: str,
        cache_prompts: bool = True,
    ):
        super().__init__(max_iteration)
        _register_bedrock_arn_pricing(model_name)
        self._load_model_settings()

        from aider.models import Model

        self.model = Model(model_name)
        self.model_name = model_name
        self.cache_prompts = cache_prompts

        if "bedrock" in model_name:
            api_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get(
                "AWS_BEARER_TOKEN_BEDROCK"
            )
        elif any(k in model_name for k in ("gpt", "openai", "o1", "o3", "o4", "ft:")):
            api_key = os.environ.get("OPENAI_API_KEY")
        elif "claude" in model_name or "anthropic" in model_name:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        elif "gemini" in model_name or "google" in model_name:
            api_key = os.environ.get("API_KEY")
        else:
            logger.warning(
                "Unknown model provider for '%s', skipping API key check", model_name
            )
            api_key = "assumed_present"

        if not api_key:
            raise ValueError(
                "API Key Error: No API key found for model. "
                "Export API key for that model and try again."
            )

    @staticmethod
    def _load_model_settings() -> None:
        from aider import models as aider_models

        settings_file = Path(".aider.model.settings.yml")
        if settings_file.exists():
            aider_models.register_models([str(settings_file)])

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
    ) -> AiderCReturn:
        from aider.coders import Coder
        from aider.io import InputOutput

        auto_test = bool(test_cmd)
        auto_lint = bool(lint_cmd)

        log_dir = Path(log_dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        input_history_file = log_dir / ".aider.input.history"
        chat_history_file = log_dir / ".aider.chat.history.md"
        log_file = log_dir / "aider.log"

        _saved_stdout = sys.stdout
        _saved_stderr = sys.stderr
        try:
            sys.stdout = open(log_file, "a")
            sys.stderr = open(log_file, "a")
        except OSError as e:
            logger.error("Failed to redirect stdout/stderr to %s: %s", log_file, e)
            raise

        try:
            handle_logging("httpx", log_file)
            handle_logging("backoff", log_file)

            io = InputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")

            lint_cmds = {"c": lint_cmd} if lint_cmd else None

            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds=lint_cmds,
                test_cmd=test_cmd,
                io=io,
                cache_prompts=self.cache_prompts,
            )
            coder.max_reflections = self.max_iteration
            coder.stream = True
            coder.gpt_prompts.main_system += (
                "\n\nNEVER edit test files (files under tests/, test/, or matching "
                "test_*.c / *_test.c / check_*.c). Test files are read-only reference "
                "material. Only modify implementation/source files to make the tests pass."
                '\n\nIMPORTANT: Functions whose body calls `STUB_PANIC("...")` need'
                " implementation. Replace the stub body with working C code. The code"
                " must compile before any tests will run — if a function is incomplete,"
                " leave a minimal placeholder that builds rather than a broken stub."
                " Your job is to write the implementation code that makes existing tests pass."
            )

            _test_summarizer_costs: list[SummarizerCost] = []

            if max_test_output_length > 0:
                _original_cmd_test = coder.commands.cmd_test
                _max_len = max_test_output_length
                _model = self.model_name
                _max_tok = spec_summary_max_tokens

                def _wrapped_cmd_test(test_cmd_arg: str) -> str:
                    from agent.agent_utils import summarize_test_output

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
                    coder.run(test_errors)
            elif lint_first:
                coder.commands.cmd_lint(fnames=fnames)
            else:
                max_input = self.model.info.get("max_input_tokens", 0)
                if max_input > 0:
                    estimated_tokens = len(message) // 4
                    if estimated_tokens > max_input:
                        logger.warning(
                            "Skipping: message ~%d tokens exceeds max_input_tokens %d for %s",
                            estimated_tokens,
                            max_input,
                            fnames,
                        )
                        print(
                            f"WARNING: Skipping {fnames}: ~{estimated_tokens} tokens "
                            f"exceeds max_input_tokens {max_input}",
                            file=_saved_stderr,
                        )
                        return AiderCReturn(str(log_file))
                coder.run(message)
        finally:
            if sys.stdout is not _saved_stdout:
                try:
                    sys.stdout.close()
                except Exception:
                    pass
            if sys.stderr is not _saved_stderr:
                try:
                    sys.stderr.close()
                except Exception:
                    pass
            sys.stdout = _saved_stdout
            sys.stderr = _saved_stderr

        agent_return = AiderCReturn(str(log_file))
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        if thinking_capture is not None:
            for c in _test_summarizer_costs:
                thinking_capture.summarizer_costs.add(c)

        return agent_return
