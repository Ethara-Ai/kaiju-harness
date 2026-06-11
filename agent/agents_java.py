import logging
import sys
from pathlib import Path
from typing import Any, Optional

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model

from agent.agents import (
    Agents,
    AiderAgents,
    AiderReturn,
    AgentReturn,
    handle_logging,
    register_bedrock_arn_pricing,
)
from agent.config_java import JavaAgentConfig
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from commit0.harness.constants_java import resolve_build_cmd
from commit0.harness.spec_java import Commit0JavaSpec

logger = logging.getLogger(__name__)


def _apply_thinking_capture_patches(
    coder: Any,
    thinking_capture: ThinkingCapture,
    current_stage: str,
    current_module: str,
) -> None:
    """Monkey-patch a Coder to capture reasoning tokens at 7 interception points."""
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
        from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

        coder._last_reasoning_content = ""
        saw_finish_reason = False
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

            if (
                not saw_finish_reason
                and hasattr(chunk, "choices")
                and chunk.choices
                and chunk.choices[0].finish_reason
            ):
                saw_finish_reason = True

            yield chunk

        if not coder._last_reasoning_content:
            coder._last_reasoning_content = None

        if not saw_finish_reason:
            yield ModelResponseStream(
                choices=[StreamingChoices(finish_reason="length", delta=Delta())]
            )

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
                if not thinking_tokens:
                    details = getattr(
                        coder._last_completion_usage,
                        "completion_tokens_details",
                        None,
                    )
                    if details and hasattr(details, "get"):
                        thinking_tokens = details.get("reasoning_tokens", 0) or 0

            _model_name = getattr(getattr(coder, "main_model", None), "name", "") or ""
            _provider = ""
            if _model_name.startswith("vertex_ai/") or _model_name.startswith("vertex_ai_beta/"):
                _provider = "vertex_ai_gemini" if "gemini" in _model_name.lower() else "vertex_ai"
            elif _model_name.startswith("bedrock/"):
                _provider = "bedrock"
            elif _model_name.startswith("openai/"):
                _provider = "openai"
            elif _model_name.startswith("gemini/"):
                _provider = "gemini"
            coder._thinking_capture.add_assistant_turn(
                content=coder.partial_response_content,
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
                provider=_provider,
            )
        _original_add_assistant_reply()

    _original_clone = coder.clone

    def patched_show_usage_report() -> None:
        coder._snapshot_prompt_tokens = getattr(coder, "message_tokens_sent", 0)
        coder._snapshot_completion_tokens = getattr(coder, "message_tokens_received", 0)
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

    def patched_clone(*args: Any, **kwargs: Any) -> Any:
        cloned = _original_clone(*args, **kwargs)
        _apply_thinking_capture_patches(
            cloned, thinking_capture, current_stage, current_module
        )
        cloned._turn_counter = coder._turn_counter
        return cloned

    coder.show_send_output = patched_show_send_output
    coder.show_send_output_stream = patched_show_send_output_stream
    coder.send_message = patched_send_message
    coder.add_assistant_reply_to_cur_messages = patched_add_assistant_reply
    coder.show_usage_report = patched_show_usage_report
    coder.clone = patched_clone

    _original_send = coder.send

    def patched_send(messages: Any, model: Any = None, functions: Any = None) -> Any:
        from aider.coders.base_coder import FinishReasonLength

        try:
            yield from _original_send(messages, model=model, functions=functions)
        except FinishReasonLength:
            try:
                coder.calculate_and_show_tokens_and_cost(messages, None)
            except Exception:
                pass
            raise

    coder.send = patched_send

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


class JavaAgents(Agents):
    """Java-specific agent wrapper around aider.

    Unlike the Python path which delegates entirely to AiderAgents.run(),
    this creates the Coder directly so lint_cmds uses the "java" key
    instead of the hardcoded "python" key in AiderAgents.run().
    """

    def __init__(self, config: JavaAgentConfig):
        super().__init__(config.max_iteration)
        self.config = config
        self._system_prompt: Optional[str] = None
        self._prompt_path_resolved = Path(config.system_prompt_path).resolve()
        register_bedrock_arn_pricing(config.model)
        AiderAgents._load_model_settings()

        import os
        model_name = config.model
        if "bedrock" in model_name:
            api_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        elif any(k in model_name for k in ("gpt", "openai", "o1", "o3", "o4", "ft:")):
            api_key = os.environ.get("OPENAI_API_KEY")
        elif model_name.startswith("vertex_ai/") or model_name.startswith("vertex_ai_beta/"):
            api_key = os.environ.get("VERTEX_AI_API_KEY", None)
            adc_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", None)
            if not api_key and not adc_path:
                raise ValueError(
                    "API Key Error: set VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS for vertex_ai/ models."
                )
            if not api_key:
                api_key = "adc"
        elif "claude" in model_name or "anthropic" in model_name:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        elif "gemini" in model_name or "google" in model_name:
            api_key = os.environ.get("GOOGLE_API_KEY")
        else:
            api_key = "assumed_present"
        if not api_key:
            raise ValueError(
                f"No API key found for model {model_name}. "
                "Export the appropriate API key and try again."
            )

        self.model = Model(config.model)

    @property
    def system_prompt(self) -> str:
        if self._system_prompt is None:
            if self._prompt_path_resolved.exists():
                self._system_prompt = self._prompt_path_resolved.read_text()
            else:
                self._system_prompt = ""
                logger.warning("System prompt file not found: %s", self._prompt_path_resolved)
        return self._system_prompt

    def _make_wrapper_script(self, repo_path: str, build_cmd: str, goal_args: str) -> str:
        """Create a wrapper script that ignores the filename argument aider appends.

        Aider's linter always does ``cmd += " " + quote(rel_fname)`` before
        execution.  Maven/Gradle do not accept source file paths as positional
        arguments so we need a wrapper that silently drops it.
        """
        import hashlib

        wrapper_dir = Path(repo_path) / ".commit0_scripts"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        tag = hashlib.md5(goal_args.encode()).hexdigest()[:8]
        wrapper = wrapper_dir / f"lint_{tag}.sh"
        wrapper.write_text(
            f"#!/usr/bin/env bash\n"
            f'exec {build_cmd} {goal_args}\n'
        )
        wrapper.chmod(0o755)
        return str(wrapper)

    # Single source of truth: spec_java.py._MVN_SKIP_FLAGS
    _MVN_SKIP_FLAGS = Commit0JavaSpec._MVN_SKIP_FLAGS

    def get_compile_command(self, build_system: str, repo_path: Optional[str] = None) -> str:
        build_cmd = resolve_build_cmd(build_system, repo_path)
        if build_system == "gradle":
            goal_args = "classes --no-daemon -q"
        else:
            goal_args = f"compile -q -B {self._MVN_SKIP_FLAGS}"
        if repo_path:
            return self._make_wrapper_script(repo_path, build_cmd, goal_args)
        return f"{build_cmd} {goal_args}"

    def get_test_command(self, build_system: str, repo_path: Optional[str] = None) -> str:
        build_cmd = resolve_build_cmd(build_system, repo_path)
        if build_system == "gradle":
            return f"{build_cmd} test --no-daemon"
        return f"{build_cmd} test -B"

    def run(
        self,
        message: str,
        test_cmd: str,
        lint_cmd: str,
        fnames: list,
        log_dir: Path,
        test_first: bool = False,
        thinking_capture: Optional[ThinkingCapture] = None,
        current_stage: str = "",
        current_module: str = "",
        max_test_output_length: int = 0,
        spec_summary_max_tokens: int = 4000,
    ) -> AgentReturn:
        auto_test = bool(test_cmd)
        auto_lint = bool(lint_cmd)

        log_dir = Path(log_dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        input_history_file = log_dir / ".aider.input.history"
        chat_history_file = log_dir / ".aider.chat.history.md"
        log_file = log_dir / "aider.log"

        _saved_stdout = sys.stdout
        _saved_stderr = sys.stderr
        _log_fh = None
        try:
            _log_fh = open(log_file, "a")
            sys.stdout = _log_fh
            sys.stderr = _log_fh
        except OSError as e:
            if _log_fh is not None:
                _log_fh.close()
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

            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds={"java": lint_cmd},
                test_cmd=test_cmd,
                io=io,
                cache_prompts=self.config.cache_prompts,
            )
            coder.max_reflections = self.config.max_iteration
            coder.stream = True

            if thinking_capture is not None:
                _apply_thinking_capture_patches(
                    coder, thinking_capture, current_stage, current_module
                )

            if self.system_prompt:
                coder.gpt_prompts.main_system += "\n\n" + self.system_prompt

            coder.gpt_prompts.main_system += (
                "\n\nNEVER edit test files. NEVER create new test files. Test files are"
                " read-only reference material. If a test file is provided, use it ONLY"
                " to understand expected behavior. Only modify implementation/source files"
                " to make the tests pass."
            )

            _test_summarizer_costs: list[SummarizerCost] = []

            if max_test_output_length > 0:
                from agent.agent_utils import summarize_test_output

                _original_cmd_test = coder.commands.cmd_test
                _max_len = max_test_output_length
                _model = self.config.model
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
                    logger.info("Running coder with test errors for %s", fnames)
                    coder.run(test_errors)
                    logger.info("Coder finished for %s", fnames)
            else:
                max_input = self.model.info.get("max_input_tokens", 0)
                if max_input > 0:
                    estimated_tokens = len(message) // 4
                    if estimated_tokens > max_input:
                        logger.warning(
                            "Skipping: message ~%d tokens exceeds max_input_tokens %d for %s",
                            estimated_tokens, max_input, fnames,
                        )
                        return AiderReturn(log_file)
                logger.info("Running coder for %s", fnames)
                coder.run(message)
                logger.info("Coder finished for %s", fnames)
        finally:
            sys.stdout = _saved_stdout
            sys.stderr = _saved_stderr
            if _log_fh is not None:
                try:
                    _log_fh.close()
                except Exception:
                    pass

        agent_return = AiderReturn(log_file)
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        if thinking_capture is not None:
            for c in _test_summarizer_costs:
                thinking_capture.summarizer_costs.add(c)

        return agent_return
