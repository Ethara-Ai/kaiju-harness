import sys
from abc import ABC, abstractmethod
from pathlib import Path
import logging

from aider.coders import Coder
from aider.models import Model
from agent.guarded_io import GuardedInputOutput
from agent._source_pool import derive_source_pool
import re
import os
from typing import Any, Optional
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.agent_utils import summarize_test_output

logger = logging.getLogger(__name__)


class TransientLLMError(Exception):
    """A transient LLM/network error (timeout, mid-stream abort, connection drop)
    that AIDER SWALLOWED (printed but did not re-raise). Raising this from an
    agent's run() converts the swallowed error into an exception that
    run_with_recovery recognizes (it is in _TRANSIENT_EXC_NAMES) and RETRIES the
    whole module — so no litellm error is ever left unhandled."""


# Substrings that identify a retryable transient LLM/network failure in aider's
# swallowed output. Kept in sync with the recovery layer's signal list.
# Substring signals identifying a retryable transient LLM/network failure in
# aider's swallowed output. Kept in sync with the recovery layer's signal list.
# NOTE: HTTP status-code checks are done via a WORD-BOUNDED regex separately,
# not as raw substrings, so 'C2504 bug' in a source comment does NOT match '504'.
_LLM_TRANSIENT_SIGNALS = (
    "midstreamfallbackerror", "apiconnectionerror", "apitimeouterror",
    "timed out", "read timeout", "connection aborted", "connection reset",
    "server disconnected", "remoteprotocolerror", "incomplete chunked read",
    "service unavailable", "bad gateway",
    # "overloaded" alone was too permissive (matched C++ 'operator overloaded' in
    # source files). Only fire on JSON-shaped error payloads.
    '"overloaded_error"', "error type: overloaded", "anthropic api overloaded",
    # Bridge/daemon briefly down or restarting (monitor respawns it): the raw
    # socket error can surface WITHOUT the litellm exception name, so match the
    # connection-refused forms directly. Provider-agnostic (both bridges).
    "connection refused", "errno 111", "econnrefused", "connectionrefusederror",
    "remote end closed connection", "max retries exceeded",
)

# Word-bounded HTTP status codes for transient errors. Compiled once.
# `re.IGNORECASE` isn't needed since we lowercase before matching, but the
# word boundaries ARE critical: without them, `C2504 bug` (a code comment in
# fmt/base.h) matches `504 ` as a plain substring and triggers a false positive.
_HTTP_TRANSIENT_CODE_RE = re.compile(r"(?:\b(?:http|status|code|error)[\s:/-]*|/1\.[01]\s+|/2(?:\.0)?\s+)(502|503|504|520|521|522|523|524|525|526|527|528|529)\b", re.IGNORECASE)

# Word-bounded internal-server-error patterns. Matches ONLY in real LLM/HTTP
# error contexts. The 500-status numeric prose form was REMOVED after the
# httprouter false positive: its stub docstring says `return HTTP '500 Internal
# Server Error'` (describing a panic handler) which matched the old regex and
# spuriously re-ran a completed module. Any repo dealing with HTTP handling
# (Go, Rust, Python web servers) can carry the same prose in comments/docs.
# Instead we key off structural signals ONLY:
#   1. module.ClassName paths — real SDK errors always have them
#      (openai.InternalServerError, litellm.exceptions.InternalServerError,
#      httpx.InternalServerError, etc.)
#   2. Exception-format `InternalServerError:` at start-of-line — Python's
#      standard `str(exc)` output when a bare exception is printed
#   3. Structured API error payload (`error type: internal_server_error`) —
#      the JSON envelope OpenAI/Anthropic/Bedrock use for structured errors
# Real transient 500s are ALWAYS accompanied by one of these signals; a plain
# `500 Internal Server Error` phrase is unreliable and MUST NOT trigger.
_INTERNAL_SERVER_ERR_RE = re.compile(
    r"(?:"
    r"\.internalservererror\b|"          # module.ClassName (openai.internalservererror)
    r"\.internal_server_error\b|"         # module.snake_case_name variant
    r"(?:^|\n)\s*internalservererror\s*[:(]|"  # exception-format at line start
    r"\berror\s+(?:type|class|code)[:\s]+(?:internalservererror|internal[_\s]server[_\s]error)\b|"  # structured API error marker (prose form)
    r"""["']type["']\s*:\s*["']internal_server_error["']"""  # JSON error payload: {"type": "internal_server_error"}
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Substrings that flag a message as coming from an EXTERNAL TOOL (Docker,
# kaiju harness subprocess) rather than the LLM/aider network stack. When one
# of _LLM_TRANSIENT_SIGNALS appears NEAR one of these markers, we suppress the
# transient-retry raise: the underlying failure is infrastructure (missing
# docker.sock, subprocess ECONNRESET on a linter call, etc.) and re-running
# the module will fail identically until the operator fixes the environment.
# Without this guard, a Docker error text like ('Connection aborted.',
# FileNotFoundError(2, ...)) triggered infinite retries on Go stage 2.
_TOOL_ERROR_CONTEXT_MARKERS = (
    "commit0.harness.",           # kaiju harness module log source
    "docker.errors",              # docker SDK exception module
    "docker.from_env",            # docker SDK entrypoint
    "cannot connect to docker",   # lint_go/build_go error phrasing
    "docker daemon",              # docker CLI error phrasing
    "docker.apiclient",           # docker SDK low-level client
    "docker.dockerclient",        # docker SDK high-level client
)


def _is_tool_error_context(text_lower: str, match_pos: int, window: int = 500) -> bool:
    """Return True if a transient-pattern match is surrounded by tool-error
    markers (Docker socket, kaiju harness subprocess) — i.e. not really an LLM
    transient. The window is chosen to span a stack trace / a multi-line log
    record without leaking into unrelated aider chat turns."""
    start = max(0, match_pos - window)
    end = min(len(text_lower), match_pos + window)
    snippet = text_lower[start:end]
    return any(marker in snippet for marker in _TOOL_ERROR_CONTEXT_MARKERS)


def _find_llm_transient(text_lower: str, needle: str) -> int:
    """Return the first position of `needle` that is NOT in a tool-error
    context. Returns -1 if the substring is absent or every occurrence is
    accompanied by a tool-error marker."""
    idx = text_lower.find(needle)
    while idx != -1:
        if not _is_tool_error_context(text_lower, idx):
            return idx
        idx = text_lower.find(needle, idx + len(needle))
    return -1


def apply_llm_resilience(model: "Model") -> None:
    """(a)+(b) client-side: make litellm RETRY a failed/timed-out call and wait
    out slow reasoning turns, BELOW aider (so a transient never surfaces to be
    swallowed). Sets num_retries + a generous request timeout in the model's
    extra_params. Env-overridable: KAIJU_LLM_NUM_RETRIES (default 5),
    KAIJU_LLM_TIMEOUT_SEC (default 1800 — matches the codex bridge's read timeout,
    so the client rides out a ChatGPT stall instead of tripping)."""
    try:
        num_retries = int(os.environ.get("KAIJU_LLM_NUM_RETRIES", "5"))
    except ValueError:
        num_retries = 5
    try:
        timeout_s = int(os.environ.get("KAIJU_LLM_TIMEOUT_SEC", "1800"))
    except ValueError:
        timeout_s = 1800
    params = dict(getattr(model, "extra_params", None) or {})
    # litellm honors num_retries (with exponential backoff) + timeout on completion.
    params.setdefault("num_retries", num_retries)
    params.setdefault("timeout", timeout_s)
    model.extra_params = params
    logger.info("LLM resilience: num_retries=%s timeout=%ss (extra_params)", num_retries, timeout_s)


def transient_scan_lines_from_chat_history(chunk: str) -> str:
    """Keep only aider's OWN output (`> ` blockquote lines) from a chat-history
    chunk. The chat history ALSO contains the model's response (its SEARCH/REPLACE
    code + prose); scanning that for transient phrases false-matches any module
    whose CODE deals with timeouts (HTTP/middleware/retries). aider prints its own
    errors — including MidStreamFallbackError — as `> ` lines, so those are kept."""
    return "\n".join(
        ln for ln in chunk.splitlines() if ln.lstrip().startswith(">")
    )


def raise_if_transient_llm_error(text: str, context: str = "") -> None:
    """(c) backstop: if aider SWALLOWED a transient LLM error (printed it into the
    session output instead of re-raising), raise TransientLLMError so
    run_with_recovery re-runs the module. Only fires on the transient signal list
    — a genuine model/edit failure is NOT retried this way. Callers must pass
    aider's OWN output (aider.log + chat-history `> ` lines), NOT the prompt or the
    model's response, or code that merely mentions 'timeout' would mis-fire."""
    if not text:
        return
    low = text.lower()
    for sig in _LLM_TRANSIENT_SIGNALS:
        if _find_llm_transient(low, sig) != -1:
            raise TransientLLMError(
                f"aider swallowed a transient LLM error{(' in ' + context) if context else ''}: "
                f"matched {sig!r} — re-running module (timed out)."
            )
    for m in _HTTP_TRANSIENT_CODE_RE.finditer(low):
        if not _is_tool_error_context(low, m.start()):
            code = m.group(1)
            raise TransientLLMError(
                f"aider swallowed a transient LLM error{(' in ' + context) if context else ''}: "
                f"matched HTTP status {code} — re-running module (timed out)."
            )
    for m in _INTERNAL_SERVER_ERR_RE.finditer(low):
        if not _is_tool_error_context(low, m.start()):
            raise TransientLLMError(
                f"aider swallowed a transient LLM error{(' in ' + context) if context else ''}: "
                f"matched internal server error pattern — re-running module (timed out)."
            )


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


def _patch_litellm_responses_bridge_reasoning_capture() -> None:
    """Recover OpenAI reasoning summaries that litellm's bridge silently drops.

    litellm's responses-API bridge has a TODO in
    ``completion_extras/litellm_responses_transformation/transformation.py``:
    ``_handle_raw_dict_response_item`` returns ``(None, index)`` for any item
    with ``type == "reasoning"``. OpenAI's Responses API returns reasoning items
    as raw dicts (not SDK ``ResponseReasoningItem`` instances), so the
    ``isinstance``-guarded extraction path never fires and summaries vanish.

    This patch wraps the callback to (a) stash reasoning ``summary[].text`` on
    the handler instance when a reasoning dict appears, then (b) attach it to
    the next message-typed Choice's ``message.reasoning_content``.
    """
    try:
        from litellm.completion_extras.litellm_responses_transformation.transformation import (
            LiteLLMResponsesTransformationHandler,
        )
    except ImportError:
        return
    if getattr(LiteLLMResponsesTransformationHandler, "_reasoning_capture_patched", False):
        return

    _orig = LiteLLMResponsesTransformationHandler._handle_raw_dict_response_item

    def _wrapped(self, item, index):
        item_type = item.get("type") if isinstance(item, dict) else None
        if item_type == "reasoning":
            parts: list[str] = []
            for s in (item.get("summary") or []) if isinstance(item, dict) else []:
                t = s.get("text") if isinstance(s, dict) else getattr(s, "text", "")
                if t:
                    parts.append(t)
            self._pending_reasoning_content = "\n\n".join(parts) if parts else None
            return None, index
        choice, new_index = _orig(self, item, index)
        if item_type == "message" and choice is not None:
            pending = getattr(self, "_pending_reasoning_content", None)
            if pending and hasattr(choice, "message"):
                try:
                    choice.message.reasoning_content = pending
                except Exception:
                    pass
                self._pending_reasoning_content = None
        return choice, new_index

    LiteLLMResponsesTransformationHandler._handle_raw_dict_response_item = _wrapped
    LiteLLMResponsesTransformationHandler._reasoning_capture_patched = True


_patch_litellm_output_config_passthrough()
_patch_litellm_responses_bridge_reasoning_capture()

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
                if not thinking_tokens:
                    _od = getattr(
                        coder._last_completion_usage,
                        "output_tokens_details",
                        None,
                    )
                    if _od and hasattr(_od, "get"):
                        thinking_tokens = _od.get("thinking_tokens", 0) or 0

            # Anthropic/Vertex AI Claude does not expose thinking_tokens in usage. Count from text.
            if not thinking_tokens and coder._last_reasoning_content:
                try:
                    import litellm as _litellm
                    _mn = getattr(getattr(coder, "main_model", None), "name", "") or ""
                    thinking_tokens = _litellm.token_counter(
                        model=_mn or "claude-3-opus-20240229",
                        text=coder._last_reasoning_content,
                    )
                except Exception:
                    thinking_tokens = max(1, len(coder._last_reasoning_content) // 4)
            from datetime import datetime, timezone
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
            elif _model_name.startswith("anthropic/"):
                _provider = "anthropic"
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
                provider=_provider,
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


class AiderAgents(Agents):
    def __init__(
        self, max_iteration: int, model_name: str, cache_prompts: bool = False
    ):
        super().__init__(max_iteration)
        register_bedrock_arn_pricing(model_name)
        self._load_model_settings()
        self.model = Model(model_name)
        apply_llm_resilience(self.model)
        self.model_name = model_name
        self.cache_prompts = cache_prompts

        # Required for reasoning_effort=high to also emit a summary through litellm's
        # Responses-API bridge (litellm transformation.py: auto-summary is gated on this).
        if model_name.startswith("openai/gpt-5"):
            import litellm
            litellm.reasoning_auto_summary = True

        # Check if API key is set for the model. ``known_provider`` tracks
        # whether we matched a provider whose credential we know how to check;
        # unknown providers are assumed to carry their own credentials and are
        # not blocked here.
        api_key = None
        known_provider = True
        if "bedrock" in model_name:
            api_key = os.environ.get("AWS_ACCESS_KEY_ID", None) or os.environ.get(
                "AWS_BEARER_TOKEN_BEDROCK", None
            )
        elif any(k in model_name for k in ("gpt", "openai", "o1", "o3", "o4", "ft:")):
            api_key = os.environ.get("OPENAI_API_KEY", None)
        elif model_name.startswith("vertex_ai/") or model_name.startswith("vertex_ai_beta/"):
            api_key = os.environ.get("VERTEX_AI_API_KEY", None)
            adc_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", None)
            if not api_key and not adc_path:
                _logger.error("No API key or ADC credentials found for model %s", model_name)
                raise ValueError(
                    "API Key Error: set VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS for vertex_ai/ models."
                )
            # ADC (application-default credentials) is a valid credential for
            # vertex_ai/ even without an API key — satisfy the fall-through guard.
            api_key = api_key or adc_path
        elif "claude" in model_name or "anthropic" in model_name:
            api_key = os.environ.get("ANTHROPIC_API_KEY", None)
        elif "gemini" in model_name:
            api_key = os.environ.get("API_KEY", None)
        else:
            known_provider = False
            _logger.warning(
                "Unknown model provider for %s; assuming credentials are present.",
                model_name,
            )

        if known_provider and not api_key:
            raise ValueError(
                "API Key Error: There is no API key associated with the model for this agent. "
                "Edit model_name parameter in .agent.yaml, export API key for that model, and try again."
            )

    @staticmethod
    def _load_model_settings() -> None:
        from aider import models as aider_models
        from pathlib import Path
        import json

        settings_file = Path(".aider.model.settings.yml")
        if settings_file.exists():
            aider_models.register_models([str(settings_file)])

        # Aider's register_litellm_models does NOT mutate litellm.model_cost,
        # so the bridge's `mode: "responses"` check fails. Merge the JSON
        # metadata into litellm.model_cost ourselves.
        metadata_file = Path(".aider.model.metadata.json")
        if metadata_file.exists():
            import litellm
            try:
                meta = json.loads(metadata_file.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
            for model, info in meta.items():
                litellm.model_cost[model] = info

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
        inject_test_files_readonly: bool = False,
        # Fix 1 audit: extra read-scope for aider's "Add file to chat?" prompts.
        # `fnames` restricts EDITS to the target stub; this param widens the READ
        # scope so aider can pull sibling source files (util.rs, header.h, .d.ts,
        # etc.) needed for cross-module signatures/imports. Test files stay blocked
        # via protected_paths (takes precedence over allowed_add_paths). When None,
        # `derive_source_pool(fnames)` auto-computes the pool from fnames[0]'s tree.
        allowed_add_paths_extra: Optional[list[str]] = None,
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

            # Fix 1 audit: auto-derive read-scope pool if runner didn't pass one.
            # Runners can override by passing an explicit list to run().
            if allowed_add_paths_extra is None:
                allowed_add_paths_extra = derive_source_pool(fnames)
            io = GuardedInputOutput(
                yes=True,
                input_history_file=input_history_file,
                chat_history_file=chat_history_file,
                # Restrict edits to the TARGET module only. SDE-I implements ONE
                # module (fnames = the single stubbed file) per run; the agent
                # must not read-in and implement OTHER modules. Any file-add/edit
                # prompt for a path outside fnames is refused, so a run stays
                # scoped to its module. Test files stay hard-blocked via
                # protected_paths (which takes precedence over the allowlist).
                allowed_add_paths=list(fnames) + list(allowed_add_paths_extra or []),
                protected_paths=set(test_files_readonly or []),
            )
            io.llm_history_file = str(log_dir / "llm_history.txt")
            coder = Coder.create(
                main_model=self.model,
                fnames=fnames,
                read_only_fnames=(test_files_readonly or []) if inject_test_files_readonly else [],
                auto_lint=auto_lint,
                auto_test=auto_test,
                lint_cmds={"python": lint_cmd},
                test_cmd=test_cmd,
                io=io,
                cache_prompts=self.cache_prompts,
                detect_urls=False,
            )
            # Clamp max_reflections when read_only_fnames is active to prevent
            # infinite reflection loops on stubborn models refusing protected-path edits.
            if test_files_readonly and inject_test_files_readonly:
                coder.max_reflections = min(self.max_iteration, 5)
            else:
                coder.max_reflections = self.max_iteration
            coder.stream = True
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
                    "\n  1. Read the source files in /chat; identify unimplemented stubs (`pass`, `NotImplementedError`, `TODO`)."
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

        # Backstop: if aider SWALLOWED a transient LLM error (printed but did not
        # re-raise), convert it to TransientLLMError so run_with_recovery re-runs
        # the module. Scan ALL streams — aider.log AND the chat/llm history; a
        # MidStreamFallbackError ("peer closed connection ... incomplete chunked
        # read") lands in .aider.chat.history.md but NOT aider.log. Placed OUTSIDE
        # the try/finally so it propagates to the recovery wrapper.
        _session_text = ""
        # QC: do NOT scan llm_history.txt — it is the PROMPT (source/test code +
        # spec), not aider error output. Any module whose code mentions
        # "timed out"/"timeout" (HTTP, middleware, retries — very common) was
        # false-matched as a transient network error -> a 900s retry loop with
        # ZERO progress. Genuine swallowed transients land in aider.log /
        # .aider.chat.history.md (MidStreamFallbackError), which we still scan.
        for _p in (log_file, chat_history_file):
            try:
                _chunk = Path(_p).read_text(errors="replace")
                _session_text += "\n" + (transient_scan_lines_from_chat_history(_chunk) if _p == chat_history_file else _chunk)
            except OSError:
                continue
        raise_if_transient_llm_error(_session_text, context=f"module {fnames}")

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
