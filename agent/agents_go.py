"""Go-specific aider agent with thinking capture and trajectory support.

Mirrors agents.py — configures aider Coder for Go source files with
lint_cmds={"go": ...}, Go-specific system prompt, and full thinking
capture/trajectory support matching the Python implementation.
"""

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
    # Remove + close any FileHandler we attached on a PRIOR module's run(). These
    # library loggers ("httpx"/"backoff") are module-level singletons, so without
    # this every module adds another handler: each new log line is then written
    # to EVERY prior module's aider.log (cross-module contamination that also
    # feeds the transient scan) and one file descriptor leaks per module.
    for _h in list(log.handlers):
        if isinstance(_h, logging.FileHandler):
            log.removeHandler(_h)
            try:
                _h.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass
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

    Applies 4 patches that intercept reasoning content at different points
    in aider's processing pipeline. Also patches clone() so lint_coder
    clones inherit the patches.
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
                if not thinking_tokens:
                    _od = getattr(coder._last_completion_usage, "output_tokens_details", None)
                    if _od and hasattr(_od, "get"):
                        thinking_tokens = _od.get("thinking_tokens", 0) or 0
            cache_hit_tokens = 0
            if coder._last_completion_usage:
                (
                    getattr(coder._last_completion_usage, "prompt_tokens", 0) or 0
                )
                (
                    getattr(coder._last_completion_usage, "completion_tokens", 0) or 0
                )
                cache_hit_tokens = getattr(
                    coder._last_completion_usage, "prompt_tokens_details", None
                )
                if cache_hit_tokens and hasattr(cache_hit_tokens, "cached_tokens"):
                    cache_hit_tokens = cache_hit_tokens.cached_tokens or 0
                else:
                    cache_hit_tokens = 0

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
                provider=_provider,
            )

        _original_add_assistant_reply()

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


class GoAgentReturn(ABC):
    def __init__(self, log_file: Optional[str] = None):
        self.log_file = log_file
        self.last_cost: float = 0.0
        self.test_summarizer_cost: float = 0.0


class GoAgents(ABC):
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
        test_files_readonly: Optional[list[str]] = None,
        inject_test_files_readonly: bool = True,
        # Fix 1 audit: extra read-scope for aider's "Add file to chat?" prompts.
        # `fnames` restricts EDITS to the target stub; this param widens the READ
        # scope so aider can pull sibling source files (util.rs, header.h, .d.ts,
        # etc.) needed for cross-module signatures/imports. Test files stay blocked
        # via protected_paths (takes precedence over allowed_add_paths). When None,
        # `derive_source_pool(fnames)` auto-computes the pool from fnames[0]'s tree.
        allowed_add_paths_extra: Optional[list[str]] = None,
    ) -> GoAgentReturn:
        raise NotImplementedError


class AiderGoReturn(GoAgentReturn):
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


class AiderGoAgents(GoAgents):
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

        from agent.agents import apply_llm_resilience

        self.model = Model(model_name)
        apply_llm_resilience(self.model)
        self.model_name = model_name
        self.cache_prompts = cache_prompts

        if "bedrock" in model_name:
            api_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get(
                "AWS_BEARER_TOKEN_BEDROCK"
            )
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
        test_files_readonly: Optional[list[str]] = None,
        inject_test_files_readonly: bool = True,
        # Fix 1 audit: extra read-scope for aider's "Add file to chat?" prompts.
        # `fnames` restricts EDITS to the target stub; this param widens the READ
        # scope so aider can pull sibling source files (util.rs, header.h, .d.ts,
        # etc.) needed for cross-module signatures/imports. Test files stay blocked
        # via protected_paths (takes precedence over allowed_add_paths). When None,
        # `derive_source_pool(fnames)` auto-computes the pool from fnames[0]'s tree.
        allowed_add_paths_extra: Optional[list[str]] = None,
    ) -> AiderGoReturn:
        from aider.coders import Coder
        from agent.guarded_io import GuardedInputOutput
        from agent._source_pool import derive_source_pool

        auto_test = bool(test_cmd)
        auto_lint = bool(lint_cmd)

        log_dir = Path(log_dir).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        input_history_file = log_dir / ".aider.input.history"
        chat_history_file = log_dir / ".aider.chat.history.md"
        log_file = log_dir / "aider.log"

        # Record the pre-run byte size of every stream the post-run transient
        # scan reads. run_with_recovery re-invokes this run() with the SAME
        # log_dir on each retry, and the logs are opened in APPEND mode, so a
        # transient signal ("timed out", "max retries exceeded", …) left by a
        # PRIOR attempt — or by litellm's own internal retry that then SUCCEEDED
        # — would otherwise be re-read every retry and re-raise TransientLLMError
        # forever (a full, real-cost module re-run each time). Scoping the scan
        # to text appended DURING this attempt fixes the cross-retry contamination.
        _scan_start_offsets = {
            p: (p.stat().st_size if p.exists() else 0)
            for p in (chat_history_file, log_file, log_dir / "llm_history.txt")
        }

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

            # Fix 1 audit: auto-derive read-scope pool if runner didn't pass one.
            # Runners can override by passing an explicit list to run().
            if allowed_add_paths_extra is None:
                allowed_add_paths_extra = derive_source_pool(fnames)
            io = GuardedInputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
                allowed_add_paths=list(fnames) + list(allowed_add_paths_extra or []),  # restrict edits to the target module only
                protected_paths=set(test_files_readonly or []),
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")

            lint_cmds = {"go": lint_cmd} if lint_cmd else None

            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                read_only_fnames=(test_files_readonly or []) if inject_test_files_readonly else [],
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds=lint_cmds,
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
            if inject_test_files_readonly:
                coder.gpt_prompts.main_system += (
                    "\n\nNEVER edit test files (files ending with _test.go). NEVER create new test files. "
                    "Test files are read-only reference material \u2014 use ONLY to understand expected behavior. "
                    "Modify implementation/source files to make tests pass."
                    '\n\nIMPORTANT: Functions containing `"STUB: not implemented"` need'
                    " implementation. Replace the stub body with working Go code."
                )
            else:
                coder.gpt_prompts.main_system += (
                    "\n\nTest files are UNAVAILABLE. NEVER ask to see them. NEVER request paths ending in _test.go. "
                    "If aider prompts you to add a test file, the request will be REFUSED \u2014 do not retry."
                    "\n\nYour job is SPEC-DRIVEN implementation:"
                    '\n  1. Read the source files in /chat; identify unimplemented stubs (functions containing `"STUB: not implemented"`).'
                    "\n  2. Infer expected behavior from function signatures, type hints, comments, and the library specification."
                    "\n  3. Implement from first principles \u2014 do NOT reverse-engineer from test outputs."
                    "\n  4. Test feedback is intentionally minimal (counts only). Use it as a yes/no signal, not as a debugging aid."
                    "\n  5. If you cannot infer behavior for a function, leave a TODO comment and move on. Do not stall."
                    "\n\nThe test suite is complete and frozen. Your only output is implementation code in Go source files."
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
                            f"WARNING: Skipping {fnames}: ~{estimated_tokens} tokens exceeds max_input_tokens {max_input}",
                            file=_saved_stderr,
                        )
                        return AiderGoReturn(str(log_file))
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

        # (c) backstop: aider may have CAUGHT a transient LLM/network error
        # (e.g. MidStreamFallbackError -> APIConnectionError: timed out), PRINTED
        # it into the session, and NOT re-raised — leaving the module marked done
        # with the turn's work lost. Now that stdout/stderr are restored and the
        # per-module log file is flushed/closed, scan THIS module's session text
        # and re-raise it as TransientLLMError. This propagates out of run()
        # (it is placed OUTSIDE the try/finally above, so nothing swallows it) to
        # run_with_recovery, which recognizes TransientLLMError as transient and
        # re-runs the whole module with backoff. Only fires on the transient
        # signal list — a genuine model/edit failure is NOT retried this way.
        from agent.agents import raise_if_transient_llm_error

        # Scan ALL streams aider may record a swallowed transient in — aider.log
        # AND the chat/llm history. A MidStreamFallbackError lands in
        # .aider.chat.history.md but NOT aider.log, so reading only log_file
        # missed it -> no retry -> silently incomplete module.
        session_text = ""
        for _p in (log_file, chat_history_file, log_dir / "llm_history.txt"):
            try:
                # Read ONLY the bytes appended during THIS attempt (from the
                # offset captured before the run) so a prior attempt's persisted
                # transient signal can't re-fire the retry loop.
                _start = _scan_start_offsets.get(_p, 0)
                with open(_p, "r", errors="replace") as _fh:
                    _fh.seek(_start)
                    session_text += "\n" + _fh.read()
            except OSError:
                continue
        raise_if_transient_llm_error(
            session_text, context=f"module {current_module}"
        )

        agent_return = AiderGoReturn(str(log_file))
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        # NOTE: do NOT add _test_summarizer_costs to
        # thinking_capture.summarizer_costs. The test-output summarizer runs
        # INSIDE the module capture window (wrapped cmd_test during agent.run),
        # so its litellm call is already recorded in the per-module call-log
        # (grand_cost). Adding it here too double-counts it in get_metrics
        # (grand_cost + summarizer_costs). It is still reported via
        # agent_return.test_summarizer_cost and shows in by_source['our_summarizer'].

        return agent_return
