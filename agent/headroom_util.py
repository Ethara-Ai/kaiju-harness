"""Headroom prompt-compression integration for the kaiju-harness agent.

Compresses messages sent to litellm before they reach the provider so we get
real token headroom against per-model ``max_input_tokens`` caps. Best-effort:
NEVER raises into the agent loop. On any failure (Headroom not installed,
bad config, provider/tokenizer hiccup, internal Headroom exception) we
return the input unchanged with empty stats and let the call proceed.

Two entry points:
  * ``maybe_compress_text(text, *, model, kind) -> (text, stats)``
      Compress a single flat string blob (spec, repo info, lint output, etc.).
      Intended for the blob-loader integration in ``agent_utils.py``.
  * ``maybe_compress_messages(messages, *, model, kind) -> (messages, stats)``
      Compress a litellm-style ``[{role, content}, ...]`` list, leaving the
      system prompt untouched (Aider's system prompt has SEARCH/REPLACE
      directives that would silently break the edit flow if compressed).
      Used by the monkey-patch of ``litellm.completion`` in ``agents.py``.

Env vars (read live every call; NOT cached at import):

  KAIJU_HEADROOM_ENABLED          bool   default true   master switch
  KAIJU_HEADROOM_TARGET_RATIO     float  default 0.4    target compress ratio
  KAIJU_HEADROOM_MIN_TOKENS       int    default 2000   skip below this size
  KAIJU_HEADROOM_PROTECT_RECENT   int    default 2      keep last N msgs untouched

Telemetry: each successful compression returns a ``stats`` dict (empty on
skip/failure) with keys ``kind, tokens_before, tokens_after, tokens_saved,
compression_ratio``. Call ``record_stats(stats)`` to append to a thread-safe
module buffer and ``aggregate_stats()`` at the per-run write site to roll
the buffer into the existing ``logs/pipeline_<run_id>_results.json``.
"""
from __future__ import annotations

import logging
import os
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


def _truthy(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def headroom_enabled() -> bool:
    """Default ON when unset/empty so opting in is just installing the dep.
    Set ``KAIJU_HEADROOM_ENABLED=false`` to disable without uninstalling."""
    raw = os.environ.get("KAIJU_HEADROOM_ENABLED")
    if raw is None or raw.strip() == "":
        return True
    return _truthy(raw)


def _target_ratio() -> float:
    raw = os.environ.get("KAIJU_HEADROOM_TARGET_RATIO")
    if not raw:
        return 0.4
    try:
        return float(raw)
    except ValueError:
        return 0.4


def _min_tokens() -> int:
    raw = os.environ.get("KAIJU_HEADROOM_MIN_TOKENS")
    if not raw:
        return 2000
    try:
        return int(raw)
    except ValueError:
        return 2000


def _protect_recent() -> int:
    raw = os.environ.get("KAIJU_HEADROOM_PROTECT_RECENT")
    if not raw:
        return 2
    try:
        return int(raw)
    except ValueError:
        return 2


def _tokenizer_model_hint(model: str) -> str:
    """Headroom cannot infer a tokenizer from an opaque
    application-inference-profile ARN, so we pass an Anthropic-compatible
    hint for token COUNTING only. This does NOT change which model litellm
    actually calls -- the hint is used by Headroom internally for sizing."""
    m = model or ""
    if "application-inference-profile" in m:
        return "anthropic/claude-sonnet-4-5-20250929"
    return m


_STATS_LOCK = Lock()
_STATS: list[dict] = []


def record_stats(stats: dict) -> None:
    if not stats:
        return
    with _STATS_LOCK:
        _STATS.append(stats)


def drain_stats() -> list[dict]:
    with _STATS_LOCK:
        out = list(_STATS)
        _STATS.clear()
    return out


def aggregate_stats(items: list[dict] | None = None) -> dict:
    if items is None:
        items = drain_stats()
    if not items:
        return {"enabled": headroom_enabled(), "calls": 0, "tokens_saved_total": 0}
    per_kind: dict[str, dict] = {}
    before_total = 0
    after_total = 0
    saved_total = 0
    for s in items:
        kind = str(s.get("kind", "unknown"))
        bucket = per_kind.setdefault(
            kind, {"calls": 0, "tokens_before": 0, "tokens_after": 0, "tokens_saved": 0}
        )
        bucket["calls"] += 1
        b = int(s.get("tokens_before", 0) or 0)
        a = int(s.get("tokens_after", 0) or 0)
        sv = int(s.get("tokens_saved", 0) or 0)
        bucket["tokens_before"] += b
        bucket["tokens_after"] += a
        bucket["tokens_saved"] += sv
        before_total += b
        after_total += a
        saved_total += sv
    return {
        "enabled": headroom_enabled(),
        "calls": len(items),
        "tokens_before_total": before_total,
        "tokens_after_total": after_total,
        "tokens_saved_total": saved_total,
        "per_kind": per_kind,
    }


def _build_config() -> Any:
    """Lazily import Headroom's ``CompressConfig`` so importing this module
    does not pull in the dep when the feature is disabled."""
    from headroom import CompressConfig  # type: ignore

    return CompressConfig(
        compress_user_messages=True,
        compress_system_messages=False,  # Aider system prompt is load-bearing
        protect_analysis_context=False,  # without this Headroom routes every
        # user message to ``router:protected:user_message`` and never compresses;
        # Aider's user turns ARE the workload we want shrunk.
        target_ratio=_target_ratio(),
        protect_recent=_protect_recent(),
        min_tokens_to_compress=_min_tokens(),
    )


def _result_to_stats(kind: str, result: Any) -> dict:
    """Extract the 5-key telemetry dict from a Headroom ``CompressResult``.
    Defensive: tolerates attribute differences across Headroom versions."""
    def _g(name: str, default: int = 0) -> int:
        try:
            return int(getattr(result, name, default) or default)
        except Exception:
            return default

    before = _g("tokens_before")
    after = _g("tokens_after")
    saved = max(0, before - after)
    ratio = (after / before) if before > 0 else 1.0
    return {
        "kind": kind,
        "tokens_before": before,
        "tokens_after": after,
        "tokens_saved": saved,
        "compression_ratio": round(ratio, 4),
    }


def maybe_compress_text(text: str, *, model: str, kind: str) -> tuple[str, dict]:
    """Compress a single flat string blob. Returns ``(text, stats)``.

    Returns the original text with ``stats={}`` when:
      * ``KAIJU_HEADROOM_ENABLED`` is false,
      * ``text`` is empty,
      * ``text`` is below the heuristic min-size floor
        (``min_tokens * 4`` chars),
      * Headroom is not installed,
      * any internal Headroom error occurs.
    """
    if not headroom_enabled() or not text:
        return text, {}
    if len(text) < _min_tokens() * 4:
        return text, {}
    try:
        from headroom import compress  # type: ignore
    except Exception:
        return text, {}
    try:
        cfg = _build_config()
        messages = [{"role": "user", "content": text}]
        result = compress(messages, model=_tokenizer_model_hint(model), config=cfg)
        new_msgs = getattr(result, "messages", messages) or messages
        new_text = new_msgs[0].get("content", text) if new_msgs else text
        return new_text, _result_to_stats(kind, result)
    except Exception as exc:
        logger.warning("headroom %s compress failed: %s", kind, str(exc)[:200])
        return text, {}


def maybe_compress_messages(
    messages: list[dict], *, model: str, kind: str = "aider_call"
) -> tuple[list[dict], dict]:
    """Compress a litellm-style messages list. Returns ``(messages, stats)``.

    Headroom is configured with ``compress_system_messages=False``, so Aider's
    system prompt (SEARCH/REPLACE format directives) is preserved verbatim.
    Only user/assistant turns are eligible for compression.

    Returns the original list with ``stats={}`` on any skip/failure path."""
    if not headroom_enabled() or not messages:
        return messages, {}
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if total_chars < _min_tokens() * 4:
        return messages, {}
    try:
        from headroom import compress  # type: ignore
    except Exception:
        return messages, {}
    try:
        cfg = _build_config()
        result = compress(list(messages), model=_tokenizer_model_hint(model), config=cfg)
        new_msgs = getattr(result, "messages", messages) or messages
        return new_msgs, _result_to_stats(kind, result)
    except Exception as exc:
        logger.warning("headroom %s compress failed: %s", kind, str(exc)[:200])
        return messages, {}
