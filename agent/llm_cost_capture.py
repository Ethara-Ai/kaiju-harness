"""Capture every litellm LLM call made during a pipeline stage.

Hooks `litellm.success_callback` / `failure_callback` so that calls aider makes
outside its main edit loop (chat summarizer, commit-message generation,
weak_model invocations) are recorded alongside the main-loop turns. Without
this, the trajectory under-reports cost because `Coder.total_cost` only tracks
the main edit loop.

Per-module isolation uses a `ContextVar`; the active log is bound around each
`agent.run(...)` call via `capture_module_calls(...)`. A tripwire compares the
captured call count against the `httpx` POST count in `aider.log` and records a
mismatch on the `ThinkingCapture` instance if they diverge.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import traceback
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from commit0.harness.utils import relativize
from typing import TYPE_CHECKING, Any, Iterator, Optional

if TYPE_CHECKING:
    from agent.thinking_capture import ThinkingCapture

_logger = logging.getLogger(__name__)


SRC_MAIN_LOOP = "main_loop"
SRC_AIDER_SUMMARIZER = "aider_summarizer"
SRC_AIDER_COMMIT_MSG = "aider_commit_msg"
SRC_AIDER_REPOMAP = "aider_repomap"
SRC_OUR_SUMMARIZER = "our_summarizer"
SRC_UNKNOWN = "unknown"


def _provider_from_model(model: str) -> str:
    if not model:
        return ""
    if model.startswith("vertex_ai/") or model.startswith("vertex_ai_beta/"):
        return "vertex_ai_gemini" if "gemini" in model.lower() else "vertex_ai"
    if model.startswith("bedrock/"):
        return "bedrock"
    if model.startswith("openai/"):
        return "openai"
    if model.startswith("gemini/"):
        return "gemini"
    if model.startswith("anthropic/"):
        return "anthropic"
    return ""


@dataclass
class LlmCallRecord:
    source: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    # True when thinking_tokens was ESTIMATED by counting the (possibly
    # summarized) reasoning text rather than read from a provider-exact
    # reasoning-token usage field. Anthropic with display:summarized never
    # returns exact reasoning tokens, so the value is a text-length proxy that
    # undercounts the real reasoning; flag it so consumers don't treat it as billed.
    thinking_tokens_estimated: bool = False
    cost_usd: float = 0.0
    duration_s: float = 0.0
    timestamp: str = ""
    status: str = "success"
    provider: str = ""

    def to_dict(self) -> dict:
        base: dict = {
            "source": self.source,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "thinking_tokens": self.thinking_tokens,
            "thinking_tokens_estimated": self.thinking_tokens_estimated,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "timestamp": self.timestamp,
            "status": self.status,
        }
        if self.provider == "vertex_ai_gemini":
            base["cached_content_tokens"] = self.cache_read_tokens
        else:
            base["cache_read_tokens"] = self.cache_read_tokens
            base["cache_write_tokens"] = self.cache_write_tokens
        return base


@dataclass
class LlmCallLog:
    calls: list[LlmCallRecord] = field(default_factory=list)
    # Optional short label used to redact provider-specific model identifiers
    # (e.g. full Bedrock ARN) in shipped artifacts. When set, every record added
    # via `add()` has its `.model` rewritten to this value before storage so that
    # output.json and other downstream serializations never leak the ARN.
    model_short: str = ""
    # Counter incremented by _CaptureLogger.log_post_api_call for every litellm
    # completion. Compared against len(calls) by audit_against_callback_counter()
    # to detect silent capture loss (callback fired but record never landed).
    # Replaces the deprecated httpx-INFO-log scanner which broke when httpx logs
    # were suppressed to prevent Bedrock ARN leakage.
    callback_event_count: int = 0
    # Wall-clock seconds of the agent.run() bracketed by capture_module_calls.
    # Set on context-manager exit. Used as the per-module runtime for languages
    # (go/c) whose run loop doesn't measure a per-module elapsed itself; the other
    # languages pass a slightly more inclusive module_elapsed explicitly and that
    # takes precedence in write_module_output_json.
    wall_seconds: float = 0.0

    def add(self, record: LlmCallRecord) -> None:
        if self.model_short:
            record.model = self.model_short
        self.calls.append(record)

    def by_source(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            is_vertex = c.provider == "vertex_ai_gemini"
            if c.source not in out:
                base: dict[str, Any] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "thinking_tokens": 0,
                    "cost_usd": 0.0,
                }
                if is_vertex:
                    base["cached_content_tokens"] = 0
                else:
                    base["cache_read_tokens"] = 0
                    base["cache_write_tokens"] = 0
                out[c.source] = base
            b = out[c.source]
            b["calls"] += 1
            b["prompt_tokens"] += c.prompt_tokens
            b["completion_tokens"] += c.completion_tokens
            b["thinking_tokens"] += c.thinking_tokens
            b["cost_usd"] += c.cost_usd
            if is_vertex:
                b["cached_content_tokens"] = b.get("cached_content_tokens", 0) + c.cache_read_tokens
            else:
                b["cache_read_tokens"] = b.get("cache_read_tokens", 0) + c.cache_read_tokens
                b["cache_write_tokens"] = b.get("cache_write_tokens", 0) + c.cache_write_tokens
        return out

    def grand_totals(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "calls": len(self.calls),
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "completion_tokens": sum(c.completion_tokens for c in self.calls),
            "thinking_tokens": sum(c.thinking_tokens for c in self.calls),
            "cost_usd": sum(c.cost_usd for c in self.calls),
        }
        providers = {c.provider for c in self.calls}
        if providers and providers <= {"vertex_ai_gemini"}:
            out["cached_content_tokens"] = sum(c.cache_read_tokens for c in self.calls)
        else:
            out["cache_read_tokens"] = sum(c.cache_read_tokens for c in self.calls)
            out["cache_write_tokens"] = sum(c.cache_write_tokens for c in self.calls)
        # E2 invariant: tokens were spent but cost is $0 ⇒ pricing did not resolve
        # (bridge/alias model name not in metadata). This is the difference between
        # a real subscription $0 and a broken cost pipeline — surface it explicitly
        # so a "successful" run can't silently lie about cost.
        out["cost_resolved"] = not (
            out["prompt_tokens"] + out["completion_tokens"] > 0 and out["cost_usd"] == 0.0
        )
        if not out["cost_resolved"]:
            models = sorted({c.model for c in self.calls})
            _logger.error(
                "COST UNRESOLVED: %d tokens spent but cost is $0 — pricing missing for "
                "model(s) %s. Reported cost is NOT a real $0. Add a metadata entry / "
                "normalize the model name.", out["prompt_tokens"] + out["completion_tokens"], models,
            )
        return out


_current_log: ContextVar[Optional[LlmCallLog]] = ContextVar(
    "_current_llm_log", default=None
)
_registered_lock = threading.Lock()
_registered = False

# Coder registry — populated by `agent.agents._apply_thinking_capture_patches`
# so `capture_module_calls.__exit__` can join any aider summarizer threads
# that are still running. Aider spawns these in a bare `threading.Thread` and
# joins them lazily on the *next* turn; without an explicit join here, records
# from in-flight summarizer calls land in the log AFTER our scope exits.
_active_coders: "weakref.WeakSet[Any]" = weakref.WeakSet()
_active_coders_lock = threading.Lock()


def register_active_coder(coder: Any) -> None:
    with _active_coders_lock:
        _active_coders.add(coder)


def _drain_summarizer_threads() -> None:
    with _active_coders_lock:
        coders = list(_active_coders)
    for c in coders:
        try:
            t = getattr(c, "summarizer_thread", None)
            if t is not None and t.is_alive():
                end = getattr(c, "summarize_end", None)
                if callable(end):
                    end()
                else:
                    t.join(timeout=60)
        except Exception as e:
            _logger.warning("summarizer drain failed: %s", e)
_seen_call_ids: set[str] = set()
_seen_lock = threading.Lock()


def _classify_source(_kwargs: Any) -> str:
    """Classify a litellm call by walking the Python call stack.

    Stack-based so it's language-pipeline-agnostic and survives aider renames
    without code changes here.
    """
    try:
        stack = traceback.extract_stack()
    except Exception:
        return SRC_UNKNOWN

    our_seen = False
    summarize_seen = False
    commit_seen = False
    repomap_seen = False

    for frame in stack:
        name = frame.name or ""
        fn = (frame.filename or "").lower()
        # Match every language's summarizer: the generic `summarize_test_output`,
        # the per-language variants (`summarize_rust_test_output`,
        # `summarize_cpp_test_output`, …), and `summarize_specification`. The old
        # exact `summarize_test_output` substring missed the `_rust_`/`_cpp_`
        # variants, so their calls were mislabeled `main_loop` in by_source.
        if (
            ("summarize" in name and "test_output" in name)
            or "summarize_specification" in name
        ):
            our_seen = True
        if (
            "summarize_chat_history" in name
            or "summarize_from_coder" in name
            or "summarize_messages" in name
            or "summarizer" in name.lower()
        ):
            summarize_seen = True
        if "cmd_commit" in name or "get_commit_message" in name:
            commit_seen = True
        if "repomap" in fn or "repomap" in name.lower():
            repomap_seen = True

    if our_seen:
        return SRC_OUR_SUMMARIZER
    if summarize_seen:
        return SRC_AIDER_SUMMARIZER
    if commit_seen:
        return SRC_AIDER_COMMIT_MSG
    if repomap_seen:
        return SRC_AIDER_REPOMAP
    return SRC_MAIN_LOOP


_PRICING_CACHE: dict[str, dict[str, float]] = {}
_MISSING_PRICING_WARNED: set[str] = set()


def _load_pricing(model: str) -> dict[str, float]:
    """Return per-token pricing for a model from .aider.model.metadata.json.

    Falls back to whatever litellm.model_cost has for the model. Returns dict
    with keys: input, output, cache_read, cache_write. All floats USD per token.
    """
    cached = _PRICING_CACHE.get(model)
    if cached is not None:
        return cached
    import json
    import re as _re
    p = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    # E2: normalize runtime model-name variants to the base pricing key. The
    # bridge / 1M-context aliases surface as e.g. `anthropic/claude-opus-4-8[1m]`
    # or `…-v1:0` which aren't in the metadata, so pricing silently resolved to
    # $0 and the whole run reported free. Strip a trailing `[...]` / `:N` suffix
    # and retry the lookup with the cleaned name in addition to the raw one.
    _clean = _re.sub(r"\[[^\]]*\]$", "", model)
    # Do NOT strip a trailing `:N` for Bedrock — there the `:0`/`:1` is the model
    # VERSION, part of the canonical id (e.g. `...claude-3-5-sonnet-20240620-v1:0`);
    # stripping it would resolve to the wrong (or no) pricing entry. Only the
    # `[1m]`-style context-alias suffix is safe to strip universally.
    if not model.startswith("bedrock/"):
        _clean = _re.sub(r":\d+$", "", _clean)
    _lookup_names = [model] if _clean == model else [model, _clean]
    metadata_paths = [
        Path(__file__).resolve().parents[1] / ".aider.model.metadata.json",
    ]
    for mp in metadata_paths:
        if not mp.exists():
            continue
        try:
            data = json.loads(mp.read_text())
        except Exception:
            continue
        entry = None
        for _nm in _lookup_names:
            entry = data.get(_nm) or data.get(_nm.replace("bedrock/", "")) or data.get(_nm.replace("anthropic/", ""))
            if entry:
                break
        if not entry and model.startswith("bedrock/converse/"):
            suffix = model[len("bedrock/converse/"):]
            for key, val in data.items():
                if suffix in key:
                    entry = val
                    break
        if entry:
            p["input"] = float(entry.get("input_cost_per_token") or 0)
            p["output"] = float(entry.get("output_cost_per_token") or 0)
            p["cache_read"] = float(entry.get("cache_read_input_token_cost") or 0)
            p["cache_write"] = float(entry.get("cache_creation_input_token_cost") or 0)
            break
    if p["input"] == 0.0:
        try:
            import litellm
            entry = litellm.model_cost.get(model) or {}
            if not entry and model.startswith("anthropic/"):
                entry = litellm.model_cost.get(model[len("anthropic/"):]) or {}
            p["input"] = float(entry.get("input_cost_per_token") or 0)
            p["output"] = float(entry.get("output_cost_per_token") or 0)
            p["cache_read"] = float(entry.get("cache_read_input_token_cost") or 0)
            p["cache_write"] = float(entry.get("cache_creation_input_token_cost") or 0)
        except Exception:
            pass
    if p["input"] == 0.0 and model not in _MISSING_PRICING_WARNED:
        _MISSING_PRICING_WARNED.add(model)
        _logger.warning(
            "No pricing found for model %s — cost will report as $0. "
            "Add an entry to .aider.model.metadata.json or check "
            "register_bedrock_arn_pricing.",
            model,
        )
    _PRICING_CACHE[model] = p
    return p


def _compute_cost(model: str, p_tok: int, c_tok: int, cr_tok: int, cw_tok: int) -> float:
    """Compute cost from token counts.

    Bedrock's _transform_usage folds cache_read + cache_write into prompt_tokens.
    Subtract them to avoid double-billing. All other providers (Anthropic, Vertex AI
    Anthropic) return prompt_tokens as fresh-only input; subtracting would undercount.
    """
    if model.startswith("bedrock/"):
        regular_input = max(0, p_tok - cr_tok - cw_tok)
    else:
        regular_input = p_tok
    pr = _load_pricing(model)
    return (
        regular_input * pr["input"]
        + c_tok * pr["output"]
        + cr_tok * pr["cache_read"]
        + cw_tok * pr["cache_write"]
    )


def _normalize_model(model: str) -> str:
    if not model:
        return ""
    m = model
    while m.startswith("bedrock/"):
        m = m[len("bedrock/"):]
    while m.startswith("anthropic/"):
        m = m[len("anthropic/"):]
    while m.startswith("vertex_ai/"):
        m = m[len("vertex_ai/"):]
    return m


def _extract_int(usage: Any, *names: str) -> int:
    if usage is None:
        return 0
    for n in names:
        v = getattr(usage, n, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(n)
        if isinstance(v, int):
            return v
    return 0


_DEBUG_DUMP_PATH = Path("/tmp/llm_capture_debug.jsonl")
_DEBUG_DUMP_COUNT = 0
_DEBUG_DUMP_MAX = 20


def _dump_debug_usage(kwargs: Any, response: Any, status: str) -> None:
    global _DEBUG_DUMP_COUNT
    if _DEBUG_DUMP_COUNT >= _DEBUG_DUMP_MAX:
        return
    _DEBUG_DUMP_COUNT += 1
    try:
        import json
        usage = getattr(response, "usage", None) if response is not None else None
        hidden = getattr(response, "_hidden_params", None) if response is not None else None
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "model": (kwargs or {}).get("model") if isinstance(kwargs, dict) else None,
            "stream": (kwargs or {}).get("stream") if isinstance(kwargs, dict) else None,
            "response_type": type(response).__name__ if response is not None else None,
            "usage_type": type(usage).__name__ if usage is not None else None,
            "usage_attrs": sorted(
                a for a in dir(usage) if not a.startswith("_")
            ) if usage is not None else [],
            "usage_dump": {
                a: repr(getattr(usage, a, None))[:200]
                for a in dir(usage)
                if not a.startswith("_") and not callable(getattr(usage, a, None))
            } if usage is not None else {},
            "hidden_keys": sorted(hidden.keys()) if isinstance(hidden, dict) else None,
            "hidden_response_cost": (
                hidden.get("response_cost") if isinstance(hidden, dict) else None
            ),
        }
        with open(_DEBUG_DUMP_PATH, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        _logger.debug("debug dump failed", exc_info=True)


def _record_call(
    kwargs: Any,
    response: Any,
    start_time: Any,
    end_time: Any,
    status: str,
) -> None:
    _dump_debug_usage(kwargs, response, status)
    log = _current_log.get()
    if log is None:
        return
    try:
        usage = getattr(response, "usage", None) if response is not None else None
        hidden = (
            getattr(response, "_hidden_params", None) if response is not None else None
        )

        call_id = None
        if isinstance(hidden, dict):
            call_id = hidden.get("litellm_call_id")
        prompt_t = _extract_int(usage, "prompt_tokens", "input_tokens")
        completion_t = _extract_int(usage, "completion_tokens", "output_tokens")
        if prompt_t == 0 and completion_t == 0:
            return
        cache_r_pre = _extract_int(
            usage, "cache_read_input_tokens", "cacheReadInputTokens",
            "cache_read_input_token_count", "cacheReadInputTokenCount",
            "cached_content_token_count", "cachedContentTokenCount",
        )
        cache_w_pre = _extract_int(
            usage, "cache_creation_input_tokens", "cacheWriteInputTokens",
            "cache_write_input_tokens", "cache_creation_input_token_count",
            "cacheWriteInputTokenCount",
        )
        model_pre = "unknown"
        if isinstance(kwargs, dict):
            mp = kwargs.get("model")
            if isinstance(mp, str):
                model_pre = mp
        canonical_key = f"call:{_normalize_model(model_pre)}:{prompt_t}:{completion_t}:{cache_r_pre}:{cache_w_pre}"
        with _seen_lock:
            if canonical_key in _seen_call_ids:
                return
            if call_id and call_id in _seen_call_ids:
                _seen_call_ids.add(canonical_key)
                return
            _seen_call_ids.add(canonical_key)
            if call_id:
                _seen_call_ids.add(call_id)
        cost = 0.0
        if isinstance(hidden, dict):
            raw = hidden.get("response_cost")
            if isinstance(raw, (int, float)):
                cost = float(raw)

        duration = 0.0
        try:
            if start_time is not None and end_time is not None:
                duration = float((end_time - start_time).total_seconds())
        except Exception:
            pass

        model = "unknown"
        if isinstance(kwargs, dict):
            m = kwargs.get("model")
            if isinstance(m, str):
                model = m

        cache_r = _extract_int(
            usage,
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cache_read_input_token_count",
            "cacheReadInputTokenCount",
            "cached_content_token_count",
            "cachedContentTokenCount",
        )
        if cache_r == 0:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_r = _extract_int(details, "cached_tokens", "cache_read_tokens")

        cache_w = _extract_int(
            usage,
            "cache_creation_input_tokens",
            "cacheWriteInputTokens",
            "cache_write_input_tokens",
            "cache_creation_input_token_count",
            "cacheWriteInputTokenCount",
        )

        # Prefer the provider's EXACT reasoning-token usage field.
        thinking_t = _extract_int(
            usage, "reasoning_tokens", "completion_tokens_details_reasoning"
        )
        if thinking_t == 0:
            details = getattr(usage, "completion_tokens_details", None)
            if details is not None:
                thinking_t = _extract_int(details, "reasoning_tokens")
        if thinking_t == 0:
            _od = getattr(usage, "output_tokens_details", None)
            if _od is not None:
                thinking_t = _extract_int(_od, "thinking_tokens")
        thinking_estimated = False

        # Claude adaptive thinking: no exact usage field, so ESTIMATE by counting
        # the reasoning text. With display:summarized this text is a SUMMARY, so
        # the count undercounts the true (billed) reasoning — flag it as estimated.
        if thinking_t == 0 and response is not None:
            try:
                _msg = response.choices[0].message
                _rc = getattr(_msg, "reasoning_content", None) or getattr(_msg, "reasoning", None)
                if _rc and isinstance(_rc, str):
                    import litellm as _litellm
                    thinking_t = _litellm.token_counter(model=model, text=_rc)
                    thinking_estimated = thinking_t > 0
            except Exception:
                pass
        computed_cost = _compute_cost(model, prompt_t, completion_t, cache_r, cache_w)
        if computed_cost > 0:
            cost = computed_cost

        log.add(
            LlmCallRecord(
                source=_classify_source(kwargs),
                model=model,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                cache_read_tokens=cache_r,
                cache_write_tokens=cache_w,
                thinking_tokens=thinking_t,
                thinking_tokens_estimated=thinking_estimated,
                cost_usd=cost,
                duration_s=duration,
                timestamp=datetime.now(timezone.utc).isoformat(),
                status=status,
                provider=_provider_from_model(model),
            )
        )
    except Exception:
        _logger.debug("llm_cost_capture._record_call failed", exc_info=True)


def _record_stream_chunk_usage(model: str, usage: Any) -> None:
    """Push a record directly from aider's stream interceptor.

    Bypasses litellm callbacks (which fire per-chunk for streams without usage).
    Bedrock's messageMetadata chunk arrives with the final aggregated usage
    object; the interceptor calls this once it sees a chunk with .usage set.
    The (model, prompt+completion+cache) tuple is used as a de-dup key so
    re-emissions or a redundant litellm callback don't double-count.
    """
    log = _current_log.get()
    if log is None:
        return
    try:
        prompt_t = _extract_int(usage, "prompt_tokens", "input_tokens")
        completion_t = _extract_int(usage, "completion_tokens", "output_tokens")
        if prompt_t == 0 and completion_t == 0:
            return
        cache_r = _extract_int(
            usage,
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cache_read_input_token_count",
            "cacheReadInputTokenCount",
            "cached_content_token_count",
            "cachedContentTokenCount",
        )
        if cache_r == 0:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_r = _extract_int(details, "cached_tokens", "cache_read_tokens")
        cache_w = _extract_int(
            usage,
            "cache_creation_input_tokens",
            "cacheWriteInputTokens",
            "cache_write_input_tokens",
            "cache_creation_input_token_count",
            "cacheWriteInputTokenCount",
        )
        thinking_t = _extract_int(usage, "reasoning_tokens")
        details = getattr(usage, "completion_tokens_details", None)
        if thinking_t == 0 and details is not None:
            thinking_t = _extract_int(details, "reasoning_tokens")
        if thinking_t == 0:
            _od = getattr(usage, "output_tokens_details", None)
            if _od is not None:
                thinking_t = _extract_int(_od, "thinking_tokens")

        dedup_key = f"call:{_normalize_model(model)}:{prompt_t}:{completion_t}:{cache_r}:{cache_w}"
        with _seen_lock:
            if dedup_key in _seen_call_ids:
                return
            _seen_call_ids.add(dedup_key)

        cost = _compute_cost(model, prompt_t, completion_t, cache_r, cache_w)
        log.add(
            LlmCallRecord(
                source=SRC_MAIN_LOOP,
                model=model,
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                cache_read_tokens=cache_r,
                cache_write_tokens=cache_w,
                thinking_tokens=thinking_t,

                cost_usd=cost,
                duration_s=0.0,
                timestamp=datetime.now(timezone.utc).isoformat(),
                status="success",
                provider=_provider_from_model(model),
            )
        )
    except Exception:
        _logger.debug("_record_stream_chunk_usage failed", exc_info=True)


def _record_response_object(model: str, response: Any, duration_s: float = 0.0) -> None:
    """Record a fully-realized litellm response (stream-rebuilt or non-stream).

    Uses the same extraction + dedup logic as _record_call, but skips callback
    indirection. The wrapper around litellm.completion routes both streaming
    (rebuilt via stream_chunk_builder) and non-streaming responses here.

    ``duration_s`` is measured by the wrap via ``time.perf_counter()`` around
    the underlying ``litellm.completion(...)`` call. The wrap wins the dedup
    race against litellm's success callback (which has the same canonical key
    but fires after), so this is the value that lands in output.json — without
    threading duration through here, every wrap-recorded call would show 0.0.
    """
    log = _current_log.get()
    if log is None:
        return
    try:
        usage = getattr(response, "usage", None)
        prompt_t = _extract_int(usage, "prompt_tokens", "input_tokens")
        completion_t = _extract_int(usage, "completion_tokens", "output_tokens")
        if prompt_t == 0 and completion_t == 0:
            return

        cache_r = _extract_int(
            usage, "cache_read_input_tokens", "cacheReadInputTokens",
            "cache_read_input_token_count", "cacheReadInputTokenCount",
            "cached_content_token_count", "cachedContentTokenCount",
        )
        if cache_r == 0:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_r = _extract_int(details, "cached_tokens", "cache_read_tokens")
        cache_w = _extract_int(
            usage, "cache_creation_input_tokens", "cacheWriteInputTokens",
            "cache_write_input_tokens", "cache_creation_input_token_count",
            "cacheWriteInputTokenCount",
        )
        thinking_t = _extract_int(usage, "reasoning_tokens")
        details = getattr(usage, "completion_tokens_details", None)
        if thinking_t == 0 and details is not None:
            thinking_t = _extract_int(details, "reasoning_tokens")
        if thinking_t == 0:
            _od = getattr(usage, "output_tokens_details", None)
            if _od is not None:
                thinking_t = _extract_int(_od, "thinking_tokens")

        # Claude adaptive thinking: count from response body when usage gives 0.
        if thinking_t == 0 and response is not None:
            try:
                _msg = response.choices[0].message
                _rc = getattr(_msg, "reasoning_content", None) or getattr(_msg, "reasoning", None)
                if _rc and isinstance(_rc, str):
                    import litellm as _litellm
                    thinking_t = _litellm.token_counter(model=model, text=_rc)
            except Exception:
                pass
        dedup_key = f"call:{_normalize_model(model)}:{prompt_t}:{completion_t}:{cache_r}:{cache_w}"
        with _seen_lock:
            if dedup_key in _seen_call_ids:
                return
            _seen_call_ids.add(dedup_key)

        try:
            stack_kwargs = {"model": model}
            source = _classify_source(stack_kwargs)
        except Exception:
            source = SRC_MAIN_LOOP

        cost = _compute_cost(model, prompt_t, completion_t, cache_r, cache_w)
        log.add(LlmCallRecord(
            source=source, model=model,
            prompt_tokens=prompt_t, completion_tokens=completion_t,
            cache_read_tokens=cache_r, cache_write_tokens=cache_w,
            thinking_tokens=thinking_t, cost_usd=cost,

            duration_s=duration_s,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="success",
            provider=_provider_from_model(model),
        ))
    except Exception:
        _logger.debug("_record_response_object failed", exc_info=True)


_completion_wrapped = False
_completion_wrap_lock = threading.Lock()


def _wrap_litellm_completion() -> None:
    """Monkey-patch litellm.completion to capture every Bedrock invocation.

    Catches all paths uniformly: main stream loop, aider commit-msg generator,
    chat summarizer, weak_model, repomap, plus any future code that calls
    litellm.completion. For streams we collect chunks and rebuild via
    litellm.stream_chunk_builder to get the complete Usage object (including
    cache_read_input_tokens and cache_creation_input_tokens). Idempotent.
    """
    global _completion_wrapped
    with _completion_wrap_lock:
        if _completion_wrapped:
            return
        try:
            import litellm
            original = litellm.completion

            def _wrap_stream(stream_iter: Any, model: str, t0: float) -> Any:
                chunks: list = []
                for chunk in stream_iter:
                    chunks.append(chunk)
                    yield chunk
                duration = max(0.0, time.perf_counter() - t0)
                try:
                    rebuilt = litellm.stream_chunk_builder(chunks)
                    if rebuilt is not None:
                        _record_response_object(model, rebuilt, duration_s=duration)
                except Exception:
                    _logger.debug("stream_chunk_builder rebuild failed", exc_info=True)

            def wrapped_completion(*args: Any, **kwargs: Any) -> Any:
                t0 = time.perf_counter()
                _attempt_log = _current_log.get()
                if _attempt_log is not None:
                    try:
                        _attempt_log.callback_event_count += 1
                    except Exception:
                        pass  # never block a completion on a counter bookkeeping error
                result = original(*args, **kwargs)
                model = kwargs.get("model") or (args[0] if args else "unknown")
                is_stream = bool(kwargs.get("stream"))
                if is_stream:
                    return _wrap_stream(result, model, t0)
                duration = max(0.0, time.perf_counter() - t0)
                try:
                    _record_response_object(model, result, duration_s=duration)
                except Exception:
                    _logger.debug("non-stream record failed", exc_info=True)
                return result

            litellm.completion = wrapped_completion
            _completion_wrapped = True
            _logger.info("llm_cost_capture: wrapped litellm.completion")
        except Exception:
            _logger.warning(
                "llm_cost_capture: failed to wrap litellm.completion", exc_info=True
            )


def _success_cb(kwargs: Any, response: Any, start_time: Any, end_time: Any) -> None:
    _record_call(kwargs, response, start_time, end_time, status="success")


def _failure_cb(kwargs: Any, response: Any, start_time: Any, end_time: Any) -> None:
    _record_call(kwargs, response, start_time, end_time, status="failure")


class _CaptureLogger:
    """litellm CustomLogger that fires log_success_event with the full aggregated response.

    The legacy success_callback list fires per-stream-chunk, so the final usage
    (with cache tokens) was never visible. CustomLogger.log_success_event receives
    the rebuilt response_obj after litellm aggregates all chunks.
    """

    def log_pre_api_call(self, model, messages, kwargs):
        pass

    def log_post_api_call(self, kwargs, response_obj, start_time, end_time):
        # Increment the callback-event counter on the active LlmCallLog so the
        # divergence tripwire can detect captures lost to silent litellm bugs.
        # Runs for EVERY litellm completion regardless of httpx log level.
        log = _current_log.get()
        if log is not None:
            try:
                log.callback_event_count += 1
            except Exception:
                pass  # never block a callback on a counter bookkeeping error

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _record_call(kwargs, response_obj, start_time, end_time, "success")

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _record_call(kwargs, response_obj, start_time, end_time, "failure")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _record_call(kwargs, response_obj, start_time, end_time, "success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        _record_call(kwargs, response_obj, start_time, end_time, "failure")


_capture_logger_instance: Optional[_CaptureLogger] = None


def register_litellm_callbacks() -> None:
    """Idempotent. Safe across subprocess re-imports.

    Registers BOTH CustomLogger (modern, fires once per aggregated response
    including streams) AND legacy success_callback / failure_callback (fallback
    for litellm versions where CustomLogger doesn't fire for some paths). The
    `_recently_seen` de-dup in `_record_call` prevents double-counting when both
    fire for the same call.
    """
    global _registered, _capture_logger_instance
    with _registered_lock:
        if _registered:
            return
        try:
            import litellm

            if _capture_logger_instance is None:
                _capture_logger_instance = _CaptureLogger()
            if not any(
                isinstance(cb, _CaptureLogger) for cb in (litellm.callbacks or [])
            ):
                litellm.callbacks = list(litellm.callbacks or []) + [
                    _capture_logger_instance
                ]
            if _success_cb not in (litellm.success_callback or []):
                litellm.success_callback = list(litellm.success_callback or []) + [
                    _success_cb
                ]
            if _failure_cb not in (litellm.failure_callback or []):
                litellm.failure_callback = list(litellm.failure_callback or []) + [
                    _failure_cb
                ]
            _registered = True
            _logger.info(
                "llm_cost_capture: registered CustomLogger + legacy callbacks "
                "(callbacks=%d, success_callback=%d)",
                len(litellm.callbacks or []),
                len(litellm.success_callback or []),
            )
        except Exception:
            _logger.warning(
                "llm_cost_capture: failed to register litellm callbacks",
                exc_info=True,
            )


# NOTE: _HTTPX_POST_PATTERN previously fed audit_against_httpx_log. It is
# retained here ONLY for backward compatibility with any external tooling that
# imported it. The active divergence detector is now
# audit_against_callback_counter(), which uses LlmCallLog.callback_event_count
# populated by _CaptureLogger.log_post_api_call. This change was forced by the
# ARN-redaction effort: httpx is suppressed to WARNING in agents.py to prevent
# URL-encoded Bedrock ARN leakage into aider.log, which made the httpx-regex
# audit emit a false capture_mismatch for every module.
_HTTPX_POST_PATTERN = re.compile(
    r"httpx\s*-\s*INFO\s*-\s*HTTP Request:\s*POST\s+https?://"
    r"(bedrock-runtime\.|api\.openai\.com|api\.anthropic\.com)",
    re.IGNORECASE,
)


def audit_against_httpx_log(
    log_path: Path, captured_calls: int
) -> Optional[dict[str, Any]]:
    """DEPRECATED. Kept for external import compatibility. Now always returns None.

    Replaced by audit_against_callback_counter() which uses litellm's
    log_post_api_call hook instead of scanning httpx INFO logs (which are now
    suppressed in production to prevent Bedrock ARN leakage).
    """
    return None


def audit_against_callback_counter(
    log: "LlmCallLog",
    log_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Detect divergence between litellm-callback-fired events and captured records.

    Source of truth: `_CaptureLogger.log_post_api_call` increments
    `log.callback_event_count` for EVERY litellm completion call. Captured
    records are appended to `log.calls` by the success/failure event handlers.
    A delta between the two indicates a silent capture loss (callback fired but
    record never landed) or vice versa.

    Independent of httpx INFO logging, which is suppressed in production to
    keep URL-encoded Bedrock ARN out of aider.log.
    """
    try:
        event_count = log.callback_event_count
        captured_calls = len(log.calls)
        if event_count != captured_calls:
            return {
                "callback_event_count": event_count,
                "captured_calls": captured_calls,
                "delta": event_count - captured_calls,
                "log_dir": relativize(log_dir) if log_dir is not None else None,
            }
    except Exception:
        _logger.debug("audit_against_callback_counter failed", exc_info=True)
    return None


@contextmanager
def capture_module_calls(
    thinking_capture: Optional["ThinkingCapture"],
    module: str,
    log_dir: Optional[Path] = None,
    model_short: str = "",
) -> Iterator[LlmCallLog]:
    """Wrap an `agent.run(...)` invocation to capture every LLM call it makes.

    On exit, attaches the captured log to thinking_capture.module_llm_calls and
    runs the litellm-callback-counter tripwire. Any mismatch is recorded on
    thinking_capture.capture_mismatches.

    If `model_short` is set, every recorded LLM call has its `model` field
    rewritten to that short label before storage — prevents shipped artifacts
    (output.json) from leaking the full provider model identifier (e.g. Bedrock ARN).
    """
    if not model_short:
        provider_model_in_play: Optional[str] = None
        try:
            import litellm

            current = getattr(litellm, "_active_model_for_redaction_check", "")
            if isinstance(current, str) and (
                current.startswith("bedrock/") or current.startswith("vertex_ai/")
            ):
                provider_model_in_play = current
        except Exception:
            pass

        if provider_model_in_play is not None:
            raise RuntimeError(
                f"capture_module_calls: model_short is empty while "
                f"model='{provider_model_in_play}' looks like a provider-prefixed "
                "identifier (Bedrock ARN or Vertex AI model). "
                "Shipped output.json would leak the full provider identifier in "
                "metrics.llm_calls[].model. Set --model-short or "
                "AgentConfig.model_short to enable redaction."
            )

    register_litellm_callbacks()
    _wrap_litellm_completion()
    log = LlmCallLog(model_short=model_short)
    token = _current_log.set(log)
    _wall_t0 = time.monotonic()
    try:
        yield log
    finally:
        log.wall_seconds = time.monotonic() - _wall_t0
        _drain_summarizer_threads()
        _current_log.reset(token)
        if thinking_capture is not None:
            tc_calls = getattr(thinking_capture, "module_llm_calls", None)
            if tc_calls is None:
                tc_calls = {}
                thinking_capture.module_llm_calls = tc_calls
            tc_calls[module] = log
            # Divergence audit: callback-counter vs captured-records.
            # log_dir included for traceability only.
            mismatch = audit_against_callback_counter(log, log_dir)
            if mismatch:
                mm = getattr(thinking_capture, "capture_mismatches", None)
                if mm is None:
                    mm = {}
                    thinking_capture.capture_mismatches = mm
                mm[module] = mismatch


register_litellm_callbacks()
_wrap_litellm_completion()
