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
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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


@dataclass
class LlmCallRecord:
    source: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    timestamp: str = ""
    status: str = "success"

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cost_usd": self.cost_usd,
            "duration_s": self.duration_s,
            "timestamp": self.timestamp,
            "status": self.status,
        }


@dataclass
class LlmCallLog:
    calls: list[LlmCallRecord] = field(default_factory=list)

    def add(self, record: LlmCallRecord) -> None:
        self.calls.append(record)

    def by_source(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            b = out.setdefault(
                c.source,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "thinking_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            b["calls"] += 1
            b["prompt_tokens"] += c.prompt_tokens
            b["completion_tokens"] += c.completion_tokens
            b["cache_read_tokens"] += c.cache_read_tokens
            b["cache_write_tokens"] += c.cache_write_tokens
            b["thinking_tokens"] += c.thinking_tokens
            b["cost_usd"] += c.cost_usd
        return out

    def grand_totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.calls),
            "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
            "completion_tokens": sum(c.completion_tokens for c in self.calls),
            "cache_read_tokens": sum(c.cache_read_tokens for c in self.calls),
            "cache_write_tokens": sum(c.cache_write_tokens for c in self.calls),
            "thinking_tokens": sum(c.thinking_tokens for c in self.calls),
            "cost_usd": sum(c.cost_usd for c in self.calls),
        }


_current_log: ContextVar[Optional[LlmCallLog]] = ContextVar(
    "_current_llm_log", default=None
)
_registered_lock = threading.Lock()
_registered = False
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
        if "summarize_test_output" in name or "summarize_specification" in name:
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


def _load_pricing(model: str) -> dict[str, float]:
    """Return per-token pricing for a model from .aider.model.metadata.json.

    Falls back to whatever litellm.model_cost has for the model. Returns dict
    with keys: input, output, cache_read, cache_write. All floats USD per token.
    """
    cached = _PRICING_CACHE.get(model)
    if cached is not None:
        return cached
    import json
    p = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    metadata_paths = [
        Path("/Users/macbookpro/code/kaiju/merge/.aider.model.metadata.json"),
    ]
    for mp in metadata_paths:
        if not mp.exists():
            continue
        try:
            data = json.loads(mp.read_text())
        except Exception:
            continue
        entry = data.get(model) or data.get(model.replace("bedrock/", ""))
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
            p["input"] = float(entry.get("input_cost_per_token") or 0)
            p["output"] = float(entry.get("output_cost_per_token") or 0)
            p["cache_read"] = float(entry.get("cache_read_input_token_cost") or 0)
            p["cache_write"] = float(entry.get("cache_creation_input_token_cost") or 0)
        except Exception:
            pass
    _PRICING_CACHE[model] = p
    return p


def _compute_cost(model: str, p_tok: int, c_tok: int, cr_tok: int, cw_tok: int) -> float:
    """Compute cost from token counts, correcting for litellm's cache-in-input folding.

    Why: litellm's bedrock _transform_usage adds cache_read + cache_write into
    prompt_tokens to match OpenAI's wire shape. Billing them at the regular
    input rate then again at cache_read/cache_write rates double-counts. The
    canonical Bedrock-billed input is prompt_tokens minus the cache portions.
    """
    regular_input = max(0, p_tok - cr_tok - cw_tok)
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

        thinking_t = _extract_int(
            usage, "reasoning_tokens", "completion_tokens_details_reasoning"
        )
        if thinking_t == 0:
            details = getattr(usage, "completion_tokens_details", None)
            if details is not None:
                thinking_t = _extract_int(details, "reasoning_tokens")

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
                cost_usd=cost,
                duration_s=duration,
                timestamp=datetime.now(timezone.utc).isoformat(),
                status=status,
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
            )
        )
    except Exception:
        _logger.debug("_record_stream_chunk_usage failed", exc_info=True)


def _record_response_object(model: str, response: Any) -> None:
    """Record a fully-realized litellm response (stream-rebuilt or non-stream).

    Uses the same extraction + dedup logic as _record_call, but skips callback
    indirection. The wrapper around litellm.completion routes both streaming
    (rebuilt via stream_chunk_builder) and non-streaming responses here.
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

        dedup_key = f"call:{_normalize_model(model)}:{prompt_t}:{completion_t}:{cache_r}:{cache_w}"
        with _seen_lock:
            if dedup_key in _seen_call_ids:
                return
            _seen_call_ids.add(dedup_key)

        try:
            import traceback as _tb
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
            duration_s=0.0,
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="success",
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

            def _wrap_stream(stream_iter: Any, model: str) -> Any:
                chunks: list = []
                for chunk in stream_iter:
                    chunks.append(chunk)
                    yield chunk
                try:
                    rebuilt = litellm.stream_chunk_builder(chunks)
                    if rebuilt is not None:
                        _record_response_object(model, rebuilt)
                except Exception:
                    _logger.debug("stream_chunk_builder rebuild failed", exc_info=True)

            def wrapped_completion(*args: Any, **kwargs: Any) -> Any:
                result = original(*args, **kwargs)
                model = kwargs.get("model") or (args[0] if args else "unknown")
                is_stream = bool(kwargs.get("stream"))
                if is_stream:
                    return _wrap_stream(result, model)
                try:
                    _record_response_object(model, result)
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
        pass

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


_HTTPX_POST_PATTERN = re.compile(
    r"httpx\s*-\s*INFO\s*-\s*HTTP Request:\s*POST\s+https?://"
    r"(bedrock-runtime\.|api\.openai\.com|api\.anthropic\.com)",
    re.IGNORECASE,
)


def audit_against_httpx_log(
    log_path: Path, captured_calls: int
) -> Optional[dict[str, Any]]:
    try:
        if not log_path.exists():
            return None
        text = log_path.read_text(encoding="utf-8", errors="replace")
        http_count = sum(
            1 for line in text.splitlines() if _HTTPX_POST_PATTERN.search(line)
        )
        if http_count != captured_calls:
            return {
                "httpx_post_count": http_count,
                "captured_calls": captured_calls,
                "delta": http_count - captured_calls,
                "log_path": str(log_path),
            }
    except Exception:
        _logger.debug("audit_against_httpx_log failed", exc_info=True)
    return None


@contextmanager
def capture_module_calls(
    thinking_capture: Optional["ThinkingCapture"],
    module: str,
    log_dir: Optional[Path] = None,
) -> Iterator[LlmCallLog]:
    """Wrap an `agent.run(...)` invocation to capture every LLM call it makes.

    On exit, attaches the captured log to thinking_capture.module_llm_calls and
    runs the httpx-log tripwire against `<log_dir>/aider.log`. Any mismatch is
    recorded on thinking_capture.capture_mismatches.
    """
    register_litellm_callbacks()
    _wrap_litellm_completion()
    log = LlmCallLog()
    token = _current_log.set(log)
    try:
        yield log
    finally:
        _current_log.reset(token)
        if thinking_capture is not None:
            tc_calls = getattr(thinking_capture, "module_llm_calls", None)
            if tc_calls is None:
                tc_calls = {}
                thinking_capture.module_llm_calls = tc_calls
            tc_calls[module] = log
            if log_dir is not None:
                aider_log = Path(log_dir) / "aider.log"
                mismatch = audit_against_httpx_log(aider_log, len(log.calls))
                if mismatch:
                    mm = getattr(thinking_capture, "capture_mismatches", None)
                    if mm is None:
                        mm = {}
                        thinking_capture.capture_mismatches = mm
                    mm[module] = mismatch


register_litellm_callbacks()
_wrap_litellm_completion()
