"""JavaScript-specific aider agent with thinking capture and trajectory support.

Mirrors agents_go.py — configures the aider Coder for JS source files with
lint_cmds={"javascript": ...}, JS-specific system prompt addendum, full
thinking capture, and trajectory writing. The twin choice (Go, not TS) is
deliberate per JS-PLAN §5: agents_go.py is the closest analogue because Go
agents already handle compiled/test-runner dispatch (matching what JS needs
for jest/vitest/mocha/node_test).

Per JS-PLAN §11 item 7 (option (a) — accept empty-string spec), this module
does NOT reference any ``setup.specification`` field; the JS system prompt
file under ``agent/prompts/js_system_prompt.md`` is appended directly as
augmentation to aider's main system prompt.

Aider Coder attributes patched by ``_apply_thinking_capture_patches``:

- ``show_send_output``
- ``show_send_output_stream``
- ``send_message``
- ``add_assistant_reply_to_cur_messages``
- ``show_usage_report``
- ``apply_updates``
- ``clone``

Verified compatible with aider-chat installed from the Ethara-Ai fork
branch=main as pinned in pyproject.toml. Any Aider upgrade that renames
these attributes will silently break trajectory capture and token
accounting. Audit each patched site on upgrade.
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

_JS_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "js_system_prompt.md"


def _load_js_system_prompt() -> str:
    """Load the JS-specific system prompt from agent/prompts/js_system_prompt.md."""
    try:
        return _JS_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning(
            "JS system prompt not found at %s — using empty string",
            _JS_SYSTEM_PROMPT_PATH,
        )
        return ""


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

    Applies 7 patches that intercept reasoning content at different points in
    aider's processing pipeline (kept in parity with agents.py). Also patches
    clone() so lint_coder clones inherit the patches. NOTE: JS deliberately
    uses ``_turn_counter_ref`` (a shared list) instead of the canonical
    by-value ``_turn_counter`` int so clones share the same monotonic counter
    (pinned by test_agents_js.py::TestPatchedCloneSharesTurnCounter).
    """
    coder._thinking_capture = thinking_capture
    coder._current_stage = current_stage
    coder._current_module = current_module
    if not hasattr(coder, "_turn_counter_ref"):
        coder._turn_counter_ref = [getattr(coder, "_turn_counter", 0)]
    coder._last_reasoning_content = None
    coder._last_completion_usage = None
    coder._last_response_id = None

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
        coder._last_response_id = getattr(completion, "id", None) or coder._last_response_id
        _original_show_send_output(completion)

    def _reasoning_interceptor(completion: Any) -> Any:
        from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

        coder._last_reasoning_content = ""
        saw_finish_reason = False
        completion_iter = iter(completion)
        for chunk in completion_iter:
            try:
                rc = chunk.choices[0].delta.reasoning_content
            except AttributeError:
                try:
                    rc = chunk.choices[0].delta.reasoning
                except AttributeError:
                    rc = None
            if rc:
                coder._last_reasoning_content += rc
            if hasattr(chunk, "usage") and chunk.usage is not None:
                coder._last_completion_usage = chunk.usage
            chunk_id = getattr(chunk, "id", None)
            if chunk_id:
                coder._last_response_id = chunk_id

            if (
                not saw_finish_reason
                and hasattr(chunk, "choices")
                and chunk.choices
                and chunk.choices[0].finish_reason
            ):
                saw_finish_reason = True

            yield chunk

        # Drain any trailing chunks (usage/id often arrive after finish_reason).
        try:
            for trailing in completion_iter:
                if hasattr(trailing, "usage") and trailing.usage is not None:
                    coder._last_completion_usage = trailing.usage
                trailing_id = getattr(trailing, "id", None)
                if trailing_id:
                    coder._last_response_id = trailing_id
        except Exception:
            pass

        if not coder._last_reasoning_content:
            coder._last_reasoning_content = None

        if not saw_finish_reason:
            yield ModelResponseStream(
                choices=[StreamingChoices(finish_reason="length", delta=Delta())]
            )

    def patched_show_send_output_stream(completion: Any) -> Any:
        return _original_show_send_output_stream(_reasoning_interceptor(completion))

    def patched_send_message(message: Any, *args: Any, **kwargs: Any) -> Any:
        coder._turn_counter_ref[0] += 1
        if coder._thinking_capture is not None:
            coder._thinking_capture.add_user_turn(
                content=message,
                stage=coder._current_stage,
                module=coder._current_module,
                turn_number=coder._turn_counter_ref[0],
            )
        return _original_send_message(message, *args, **kwargs)

    def patched_add_assistant_reply() -> None:
        if coder._thinking_capture is not None:
            thinking_tokens = 0
            if coder._last_completion_usage:
                thinking_tokens = (
                    getattr(coder._last_completion_usage, "reasoning_tokens", 0) or 0
                )
            cache_hit_tokens = 0
            if coder._last_completion_usage:
                cache_hit_tokens = getattr(
                    coder._last_completion_usage, "prompt_tokens_details", None
                )
                if cache_hit_tokens and hasattr(cache_hit_tokens, "cached_tokens"):
                    cache_hit_tokens = cache_hit_tokens.cached_tokens or 0
                else:
                    cache_hit_tokens = 0

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
                turn_number=coder._turn_counter_ref[0],
                llm_response_id=coder._last_response_id,
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
        cloned._turn_counter_ref = coder._turn_counter_ref
        if coder._thinking_capture is not None:
            _apply_thinking_capture_patches(
                cloned,
                coder._thinking_capture,
                coder._current_stage,
                coder._current_module,
            )
        return cloned

    coder.show_send_output = patched_show_send_output
    coder.show_send_output_stream = patched_show_send_output_stream
    coder.send_message = patched_send_message
    coder.add_assistant_reply_to_cur_messages = patched_add_assistant_reply
    coder.show_usage_report = patched_show_usage_report
    coder.clone = patched_clone

    # Patch 6: ContextVar propagation into aider's chat-history summarizer thread.
    # aider.coders.base_coder.summarize_start spawns a bare ``threading.Thread``
    # which does NOT inherit Python ContextVar state. Our cost subsystem's
    # ``_current_log`` binding (set by capture_module_calls) is invisible to the
    # worker, so every summarizer call was silently dropped. Wrap the thread
    # target with ``contextvars.copy_context().run(...)`` so the active log
    # propagates into the worker.
    import contextvars as _contextvars
    import threading as _threading

    def patched_summarize_start() -> None:
        if not coder.summarizer.too_big(coder.done_messages):
            return
        coder.summarize_end()
        if getattr(coder, "verbose", False):
            coder.io.tool_output("Starting to summarize chat history.")
        ctx = _contextvars.copy_context()
        coder.summarizer_thread = _threading.Thread(
            target=lambda: ctx.run(coder.summarize_worker)
        )
        coder.summarizer_thread.start()

    coder.summarize_start = patched_summarize_start

    from agent.llm_cost_capture import register_active_coder
    register_active_coder(coder)

    # Patch 7: Ensure cost is calculated even when FinishReasonLength fires.
    # Upstream aider bug: send() calls calculate_and_show_tokens_and_cost()
    # AFTER show_send_output_stream(), but FinishReasonLength raised inside
    # the stream skips the cost line. We wrap send() to catch it.
    # A coder without a ``send`` method (e.g. a minimal test double) cannot raise
    # FinishReasonLength, so there is nothing to wrap — skip defensively. Real
    # aider coders always expose ``send``, so production behaviour is unchanged.
    _original_send = getattr(coder, "send", None)
    if _original_send is not None:

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


class JsAgentReturn(ABC):
    def __init__(self, log_file: str | None = None):
        self.log_file = log_file
        self.last_cost: float = 0.0
        self.test_summarizer_cost: float = 0.0


class JsAgents(ABC):
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
        thinking_capture: ThinkingCapture | None = None,
        current_stage: str = "",
        current_module: str = "",
        max_test_output_length: int = 0,
        spec_summary_max_tokens: int = 4000,
        test_files_readonly: list[str] | None = None,
        inject_test_files_readonly: bool = True,
        # Fix 1 audit: extra read-scope for aider's "Add file to chat?" prompts.
        # `fnames` restricts EDITS to the target stub; this param widens the READ
        # scope so aider can pull sibling source files (util.rs, header.h, .d.ts,
        # etc.) needed for cross-module signatures/imports. Test files stay blocked
        # via protected_paths (takes precedence over allowed_add_paths). When None,
        # `derive_source_pool(fnames)` auto-computes the pool from fnames[0]'s tree.
        allowed_add_paths_extra: Optional[list[str]] = None,
    ) -> JsAgentReturn:
        raise NotImplementedError


class AiderJsReturn(JsAgentReturn):
    def __init__(self, log_file: str | None = None):
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


class AiderJsAgents(JsAgents):
    """JS-specific Aider agent using the 'javascript' lint key.

    The 3-stage budget split (Draft ≈ 30%, Lint refine ≈ 20%, Test refine ≈ 50%)
    documented in JS-PLAN §8 is enforced by the pipeline shell, not by this
    class — this class is the per-stage agent invocation surface.
    """

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
        # (a)+(b): make litellm RETRY a failed/timed-out call and wait out slow
        # reasoning turns BELOW aider, so a transient never surfaces to be swallowed.
        from agent.agents import apply_llm_resilience
        apply_llm_resilience(self.model)
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
        thinking_capture: ThinkingCapture | None = None,
        current_stage: str = "",
        current_module: str = "",
        max_test_output_length: int = 0,
        spec_summary_max_tokens: int = 4000,
        test_files_readonly: list[str] | None = None,
        inject_test_files_readonly: bool = True,
        # Fix 1 audit: extra read-scope for aider's "Add file to chat?" prompts.
        # `fnames` restricts EDITS to the target stub; this param widens the READ
        # scope so aider can pull sibling source files (util.rs, header.h, .d.ts,
        # etc.) needed for cross-module signatures/imports. Test files stay blocked
        # via protected_paths (takes precedence over allowed_add_paths). When None,
        # `derive_source_pool(fnames)` auto-computes the pool from fnames[0]'s tree.
        allowed_add_paths_extra: Optional[list[str]] = None,
    ) -> AiderJsReturn:
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

        js_prompt = _load_js_system_prompt()
        if js_prompt and message:
            message = js_prompt + "\n\n" + message

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

            lint_cmds = {"javascript": lint_cmd} if lint_cmd else None

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
            coder.gpt_prompts.main_system += (
                "\n\nNEVER edit test files (files matching *.test.js, *.test.mjs,"
                " *.test.cjs, *.test.jsx, *.spec.js, *.spec.mjs, *.spec.cjs,"
                " *.spec.jsx, or any file under __tests__/, test/, or tests/"
                " directories). Test files are read-only reference material. Only"
                " modify implementation/source files to make the tests pass."
                '\n\nIMPORTANT: Functions whose body throws `new Error("STUB")`'
                " need implementation. The comment `// __COMMIT0_STUB__` confirms"
                " a stub site. Replace the throw with working JavaScript code."
                " Your job is to write the implementation code that makes existing"
                " tests pass."
                "\n\nDo NOT convert between ESM and CJS. Do NOT introduce a build"
                " step, a bundler, or a transpiler. Do NOT add TypeScript syntax."
                " Do NOT edit the lockfile. Do NOT add new runtime dependencies"
                " unless the existing API documentation requires them."
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
                    from agent.agent_utils_js import summarize_test_output_js

                    raw = _original_cmd_test(test_cmd_arg)
                    if raw and len(raw) > _max_len:
                        result, costs = summarize_test_output_js(
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
                        return AiderJsReturn(str(log_file))
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

        # (c) backstop: if aider SWALLOWED a transient LLM error into the session
        # output (printed but did not re-raise), convert it into a TransientLLMError
        # so run_with_recovery re-runs the module. Read the captured session text
        # from log_file AFTER stdout/stderr are restored. Raised OUTSIDE the try/
        # except above so it propagates to the run_with_recovery wrapper. Only fires
        # on transient signals (helper guards this) — genuine failures aren't retried.
        from agent.agents import raise_if_transient_llm_error
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

        agent_return = AiderJsReturn(str(log_file))
        agent_return.test_summarizer_cost = sum(c.cost for c in _test_summarizer_costs)

        # NOTE: do NOT add _test_summarizer_costs to
        # thinking_capture.summarizer_costs. The test-output summarizer runs
        # INSIDE the module capture window (wrapped cmd_test during agent.run),
        # so its litellm call is already recorded in the per-module call-log
        # (grand_cost). Adding it here too double-counts it in get_metrics
        # (grand_cost + summarizer_costs). It is still reported via
        # agent_return.test_summarizer_cost and shows in by_source['our_summarizer'].

        return agent_return


__all__ = [
    "JsAgentReturn",
    "JsAgents",
    "AiderJsReturn",
    "AiderJsAgents",
    "handle_logging",
]
