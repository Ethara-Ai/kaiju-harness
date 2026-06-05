import sys
from abc import ABC, abstractmethod
from pathlib import Path
import logging

from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput
from agent.guarded_io import GuardedInputOutput
import re
import os
from typing import Any, Optional
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.agent_utils import summarize_test_output


def _patch_litellm_output_config_passthrough() -> None:
    """Preserve ``output_config`` through litellm's Bedrock Converse transform.

    Litellm 1.42 drops ``output_config`` in ``_transform_inference_params``
    with the comment "Bedrock Converse doesn't support it". For opus 4.7 that
    is empirically false — ``additionalModelRequestFields.output_config.effort``
    controls adaptive-thinking depth. This patch re-injects the value into
    ``additionalModelRequestFields`` after the original transform runs.
    """
    try:
        from litellm.llms.bedrock.chat import converse_transformation as _ct
    except ImportError:
        return
    cls = _ct.AmazonConverseConfig
    if getattr(cls, "_output_config_patched", False):
        return
    _orig = cls._transform_request_helper

    def _wrapped(self, model, system_content_blocks, optional_params, messages=None, headers=None):
        oc = optional_params.get("output_config")
        result = _orig(self, model, system_content_blocks, optional_params, messages, headers)
        if oc is not None:
            amrf = result.setdefault("additionalModelRequestFields", {})
            amrf["output_config"] = oc
        return result

    cls._transform_request_helper = _wrapped
    cls._output_config_patched = True


_patch_litellm_output_config_passthrough()

_logger = logging.getLogger(__name__)


# Map ``BEDROCK_<ALIAS>_ARN`` env var names to the underlying base-model ID
# that litellm uses as a pricing key. Application inference profiles are
# per-account AWS resources, so the profile ID portion of each ARN lives in
# ``.env`` (see ``.env.example``). Prices themselves come from
# ``litellm.model_cost`` at resolution time -- we never hardcode numbers here.
#
# To add a new Bedrock alias:
#   1. Add ``BEDROCK_<NEW>_ARN=`` to ``.env.example``
#   2. Add an entry below mapping the env var to its base-model ID
#   3. Add the matching case to ``commit0/harness/resolve_model.sh``
_BEDROCK_ENV_TO_BASE_MODEL: dict[str, str] = {
    "BEDROCK_OPUS_ARN": "anthropic.claude-opus-4-6-v1",
    "BEDROCK_OPUS47_ARN": "anthropic.claude-opus-4-7-v1",
    "BEDROCK_GLM5_ARN": "zai.glm-5",
    "BEDROCK_KIMI_ARN": "moonshotai.kimi-k2.5",
    "BEDROCK_MINIMAX_ARN": "minimax.minimax-m2.5",
    "BEDROCK_NOVA2_LITE_ARN": "amazon.nova-2-lite-v1:0",
    "BEDROCK_NOVA_PREMIER_ARN": "amazon.nova-premier-v1:0",
}

_ARN_PROFILE_STATIC: dict[str, str] = {
    "up13zed8728o": "anthropic.claude-opus-4-7-v1",
}


def _extract_profile_id(arn: str) -> Optional[str]:
    """Return the 12-char profile ID suffix of a bedrock inference-profile ARN.

    Accepts the bare ARN (``arn:aws:bedrock:...``) or a routed form
    (``bedrock/converse/arn:...``). Returns ``None`` unless the input is
    shaped like an inference-profile ARN.
    """
    if not arn or "arn:aws:bedrock:" not in arn or "/" not in arn:
        return None
    suffix = arn.rsplit("/", 1)[-1].strip()
    return suffix or None


def _build_arn_profile_map() -> dict[str, str]:
    """Collect ``profile_id -> base_model`` from env vars + static defaults.

    Static entries cover ARNs that may be hit via tokenless fallback (when the
    matching BEDROCK_*_ARN env var is unset). Env-var entries take precedence
    if both define the same profile id.
    """
    out: dict[str, str] = dict(_ARN_PROFILE_STATIC)
    for env_key, base_model in _BEDROCK_ENV_TO_BASE_MODEL.items():
        arn = os.environ.get(env_key, "").strip()
        profile_id = _extract_profile_id(arn)
        if profile_id:
            out[profile_id] = base_model
    return out


_ARN_PROFILE_TO_BASE_MODEL: dict[str, str] = _build_arn_profile_map()


def _resolve_base_model_from_arn(model_name: str) -> Optional[str]:
    """Return the underlying base-model ID for a bedrock inference-profile ARN.

    Tries two strategies in order:
      1. boto3 ``get_inference_profile`` (authoritative, requires the
         ``bedrock:GetInferenceProfile`` IAM permission).
      2. Static suffix match against ``_ARN_PROFILE_TO_BASE_MODEL`` (works
         under bearer-token auth that does not carry that permission).
    """
    try:
        import boto3

        region = "us-east-1"
        for part in model_name.split(":"):
            if part.startswith(("ap-", "us-", "eu-", "sa-")):
                region = part
                break

        arn = model_name.split("bedrock/")[-1]
        if arn.startswith("converse/"):
            arn = arn[len("converse/"):]

        client = boto3.client("bedrock", region_name=region)
        resp = client.get_inference_profile(inferenceProfileIdentifier=arn)
        models = resp.get("models", [])
        if models:
            base = models[0].get("modelArn", "").split("/")[-1] or models[0].get("modelId", "")
            if base:
                return base
    except Exception:
        _logger.debug(
            "boto3 inference-profile lookup failed for %s, falling back to static map",
            model_name,
            exc_info=True,
        )

    # Rebuild from env on every call so late-set env vars (e.g. from a
    # subprocess that sources .env after import time) are picked up.
    profile_map = _ARN_PROFILE_TO_BASE_MODEL or _build_arn_profile_map()
    for profile_id, base in profile_map.items():
        if profile_id in model_name:
            return base
    return None


def _litellm_pricing_for_base_model(base_model: str) -> Optional[dict]:
    """Look up pricing for ``base_model`` in ``litellm.model_cost``.

    Tries the exact key, common Bedrock prefixes (``bedrock/``, ``us.``,
    ``global.``, ``bedrock/<region>/``), and a substring scan. Returns the
    first entry whose ``input_cost_per_token`` is populated.
    """
    import litellm

    candidates = [
        base_model,
        f"bedrock/{base_model}",
        f"us.{base_model}",
        f"global.{base_model}",
        f"bedrock/us-east-1/{base_model}",
    ]
    for key in candidates:
        entry = litellm.model_cost.get(key)
        if entry and entry.get("input_cost_per_token"):
            return entry

    for key, entry in litellm.model_cost.items():
        if base_model in key and entry.get("input_cost_per_token"):
            return entry

    return None


def register_bedrock_arn_pricing(model_name: str) -> None:
    """Register pricing for a Bedrock inference-profile ARN in ``litellm.model_cost``.

    Inference-profile ARNs end in an opaque 12-character identifier that
    litellm cannot map to a base model. This resolves the ARN to its
    underlying base-model ID, copies the matching pricing entry from
    ``litellm.model_cost``, and registers it under both the routed key
    (``bedrock/converse/arn:...``) and the bare ARN so that both
    ``litellm.cost_per_token()`` and ``response._hidden_params.response_cost``
    (used by aider) resolve correctly.
    """
    if "arn:aws:bedrock:" not in model_name:
        return

    import litellm

    if model_name in litellm.model_cost and litellm.model_cost[model_name].get(
        "input_cost_per_token"
    ):
        return

    base_model = _resolve_base_model_from_arn(model_name)
    if not base_model:
        _logger.warning(
            "Could not resolve base model for %s -- costs will report as $0.00",
            model_name,
        )
        return

    pricing = _litellm_pricing_for_base_model(base_model)
    if not pricing:
        _logger.warning(
            "litellm has no pricing for base model %s (from %s) -- costs will report as $0.00",
            base_model,
            model_name,
        )
        return

    entry = pricing.copy()
    entry["litellm_provider"] = "bedrock"

    # Register pricing under every form aider / litellm might query:
    #   1. the full string passed in (routed or bare),
    #   2. the routed form (bedrock/converse/arn:...),
    #   3. the bare ARN (arn:aws:bedrock:...).
    # This covers both litellm.cost_per_token(model=...) and aider's
    # response._hidden_params.response_cost path, which sees the bare form.
    litellm.model_cost[model_name] = entry

    if model_name.startswith("bedrock/converse/"):
        bare = model_name[len("bedrock/converse/"):]
        routed = model_name
    elif model_name.startswith("bedrock/"):
        bare = model_name[len("bedrock/"):]
        routed = f"bedrock/converse/{bare}"
    else:
        bare = model_name
        routed = f"bedrock/converse/{bare}"

    litellm.model_cost.setdefault(bare, entry)
    litellm.model_cost.setdefault(routed, entry)

    _logger.debug("Registered bedrock pricing: %s -> %s", model_name, base_model)


def handle_logging(logging_name: str, log_file: Path, level: int = logging.INFO) -> None:
    """Handle logging for agent"""
    logger = logging.getLogger(logging_name)
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers.clear()  # Prevent handler accumulation
    logger_handler = logging.FileHandler(log_file)
    logger_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(logger_handler)


class AgentReturn(ABC):
    def __init__(self, log_file: Path):
        self.log_file = log_file

        self.last_cost = 0.0


class Agents(ABC):
    def __init__(self, max_iteration: int):
        self.max_iteration = max_iteration

    @abstractmethod
    def run(self) -> AgentReturn:
        """Start agent"""
        raise NotImplementedError


class AiderReturn(AgentReturn):
    def __init__(self, log_file: Path):
        super().__init__(log_file)
        self.last_cost = self.get_money_cost()
        self.test_summarizer_cost: float = 0.0

    def get_money_cost(self) -> float:
        """Get accumulated money cost from log file"""
        last_cost = 0.0
        with open(self.log_file, "r") as file:
            for line in file:
                if "Tokens:" in line and "Cost:" in line:
                    match = re.search(
                        r"Cost: \$\d+\.\d+ message, \$(\d+\.\d+) session", line
                    )
                    if match:
                        last_cost = float(match.group(1))
        return last_cost


def _apply_thinking_capture_patches(
    coder: Any,
    thinking_capture: ThinkingCapture,
    current_stage: str,
    current_module: str,
) -> None:
    """Monkey-patch a Coder instance to capture reasoning tokens.

    Applies 4 patches that intercept reasoning content at different points
    in aider's processing pipeline, BEFORE aider strips it.
    Also patches clone() so lint_coder clones inherit the patches.
    """
    coder._thinking_capture = thinking_capture
    coder._current_stage = current_stage
    coder._current_module = current_module
    coder._turn_counter = getattr(coder, "_turn_counter", 0)
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

    # Patch 1: Non-streaming response (captures reasoning_content)
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

    # Patch 2: Streaming response — intercept reasoning from chunks
    # coder.stream=True is the default; without this the non-streaming path never runs.
    # The original show_send_output_stream is a generator that builds
    # partial_response_content incrementally. We wrap the raw LLM stream
    # with an interceptor that captures reasoning while passing chunks through.

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

            if hasattr(chunk, "usage") and chunk.usage:
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

        try:
            for trailing in completion_iter:
                if hasattr(trailing, "usage") and trailing.usage:
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

    # Patch 3: User turn capture
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

    # Patch 4: Assistant reply capture (with thinking + token counts)
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

            from datetime import datetime, timezone
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
                timestamp=datetime.now(timezone.utc).isoformat(),
                llm_response_id=coder._last_response_id,
            )
        _original_add_assistant_reply()

    # Patch 5: Propagate thinking patches to clones (used by cmd_lint)
    _original_clone = coder.clone
    def patched_show_usage_report() -> None:
        usage = coder._last_completion_usage
        if usage is not None:
            coder._snapshot_prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            coder._snapshot_completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            coder._snapshot_cache_hit_tokens = (
                getattr(usage, "prompt_cache_hit_tokens", 0)
                or getattr(usage, "cache_read_input_tokens", 0)
                or 0
            )
            coder._snapshot_cache_write_tokens = (
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
            try:
                from agent.llm_cost_capture import _compute_cost
                model_name = getattr(coder, "main_model", None)
                model_str = getattr(model_name, "name", None) or str(model_name) or ""
                coder._snapshot_cost = _compute_cost(
                    model_str,
                    coder._snapshot_prompt_tokens,
                    coder._snapshot_completion_tokens,
                    coder._snapshot_cache_hit_tokens,
                    coder._snapshot_cache_write_tokens,
                )
            except Exception:
                coder._snapshot_cost = getattr(coder, "message_cost", 0.0)
        else:
            coder._snapshot_prompt_tokens = getattr(coder, "message_tokens_sent", 0)
            coder._snapshot_completion_tokens = getattr(coder, "message_tokens_received", 0)
            coder._snapshot_cache_hit_tokens = 0
            coder._snapshot_cache_write_tokens = 0
            coder._snapshot_cost = getattr(coder, "message_cost", 0.0)

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


class AiderAgents(Agents):
    def __init__(
        self, max_iteration: int, model_name: str, cache_prompts: bool = False
    ):
        super().__init__(max_iteration)
        register_bedrock_arn_pricing(model_name)
        self._load_model_settings()
        self.model = Model(model_name)
        self.model_name = model_name
        self.cache_prompts = cache_prompts

        # Check if API key is set for the model
        if "bedrock" in model_name:
            api_key = os.environ.get("AWS_ACCESS_KEY_ID", None) or os.environ.get(
                "AWS_BEARER_TOKEN_BEDROCK", None
            )
        elif any(k in model_name for k in ("gpt", "openai", "o1", "o3", "o4", "ft:")):
            api_key = os.environ.get("OPENAI_API_KEY", None)
        elif "claude" in model_name or "anthropic" in model_name:
            api_key = os.environ.get("ANTHROPIC_API_KEY", None)
        elif "gemini" in model_name or "google" in model_name:
            api_key = os.environ.get("API_KEY", None)
        else:
            _logger.warning(
                "Unknown model provider for '%s', skipping API key check", model_name
            )
            api_key = "assumed_present"

        if not api_key:
            _logger.error("No API key found for model %s", model_name)
            raise ValueError(
                "API Key Error: There is no API key associated with the model for this agent. "
                "Edit model_name parameter in .agent.yaml, export API key for that model, and try again."
            )

    @staticmethod
    def _load_model_settings() -> None:
        from aider import models as aider_models
        from pathlib import Path

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
    ) -> AgentReturn:
        """Start aider agent"""
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

        # Redirect print statements to the log file
        _saved_stdout = sys.stdout
        _saved_stderr = sys.stderr
        try:
            sys.stdout = open(log_file, "a")
            sys.stderr = open(log_file, "a")
        except OSError as e:
            _logger.error("Failed to redirect stdout/stderr to %s: %s", log_file, e)
            raise

        try:
            handle_logging("httpx", log_file)
            handle_logging("backoff", log_file)

            io = GuardedInputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
                # allowed_add_paths intentionally None: agent must be free to add
                # any non-test source file (esp. in draft stage where fnames=[f]
                # is a single file per iteration). Only test files are restricted
                # via protected_paths — matching user intent: 'make test files read-only'.
                allowed_add_paths=None,
                protected_paths=set(test_files_readonly or []),
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")
            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                read_only_fnames=test_files_readonly or [],
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds={"python": lint_cmd},
                test_cmd=test_cmd,
                io=io,
                cache_prompts=self.cache_prompts,
            )
            # Clamp max_reflections when read_only_fnames is active to prevent
            # infinite reflection loops on stubborn models refusing protected-path edits.
            if test_files_readonly:
                coder.max_reflections = min(self.max_iteration, 5)
            else:
                coder.max_reflections = self.max_iteration
            coder.stream = True
            coder.gpt_prompts.main_system += (
                "\n\nNEVER edit test files. NEVER create new test files. Test files are"
                " read-only reference material. If a test file is provided, use it ONLY"
                " to understand expected behavior. Only modify implementation/source files"
                " to make the tests pass."
                "\n\nIMPORTANT: If you see low code coverage, it means the SOURCE code has"
                " unimplemented functions (bodies with `pass` or `NotImplementedError`)."
                " Fix coverage by IMPLEMENTING the source functions, NOT by writing new tests."
                " The test suite is already complete — your job is to write the implementation"
                " code that makes existing tests pass and raises coverage."
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
