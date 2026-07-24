"""Anthropic-compatible FastAPI proxy backed by Claude Code OAuth.

Listens locally and forwards every request to ``api.anthropic.com`` with:

  Authorization: Bearer <oauth-token>     (replaces incoming x-api-key)
  anthropic-beta: oauth-2025-04-20[,...]  (merged with caller-supplied betas)
  anthropic-version: 2023-06-01           (only added if caller didn't set one)

On ``POST /v1/messages`` the bridge also injects the required
"You are Claude Code, Anthropic's official CLI for Claude." system prefix.
Anthropic rejects OAuth-scoped messages without it. Injection is idempotent
and preserves any caller-supplied system content.

Point Anthropic SDK / litellm at the bridge with::

    export ANTHROPIC_API_BASE=http://localhost:8765
    export ANTHROPIC_API_KEY=any-non-empty-stub

The stub key is required because litellm/aider refuse to start without one;
the bridge strips it before forwarding.

Resilience: the bridge retries transient 429/529 inline (short waits), and
if a multi-account pool is configured (``KAIJU_CC_ACCOUNT_POOL``) failover
to a different account on subscription-cap exhaustion. See ``errors.py``
for the classification heuristics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Iterable, Optional, Tuple, Union

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from agent.claude_code.credentials import (
    CredentialProvider,
    CredentialsError,
    MultiAccountCredentialProvider,
    load_account_pool,
)
from agent.claude_code.errors import (
    ClassifiedError,
    ErrorKind,
    classify_anthropic_error,
)

_LOG = logging.getLogger(__name__)

UPSTREAM_DEFAULT = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"
SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

# Resilience tuning knobs (overridable via env).
DEFAULT_MAX_INLINE_RETRIES = 3
DEFAULT_MAX_INLINE_WAIT_SECONDS = 30
DEFAULT_REQUEST_TIMEOUT = 600.0
# Per-chunk read timeout: how long httpx waits for the NEXT byte of an in-flight
# response. With Opus 4.8 + extended thinking + ~96k context, single-turn
# reasoning can pause for 90-150s between streamed chunks. The previous httpx
# default (5s on read) caused MidStreamFallbackError storms. 180s gives the
# model headroom while still flagging genuine stalls. Configurable via
# KAIJU_BRIDGE_READ_TIMEOUT (seconds, integer or float).
DEFAULT_READ_TIMEOUT = 180.0
DEFAULT_CONNECT_TIMEOUT = 30.0


# Streaming read timeout: a single Opus 4.8 extended-thinking turn can run 10+
# MINUTES and pause far longer than 180s between visible chunks, so the
# non-streaming 600s total + 180s read caps will KILL a perfectly healthy turn
# mid-stream (observed). For streaming we disable the overall/total cap and use a
# very generous per-chunk read timeout so only a genuinely dead connection trips.
DEFAULT_STREAM_READ_TIMEOUT = 600.0


def _bridge_timeout(streaming: bool = False) -> "httpx.Timeout":
    """Build the httpx.Timeout for upstream calls, honoring env overrides.

    Env vars (all optional, all in seconds):
      - KAIJU_BRIDGE_REQUEST_TIMEOUT     (non-stream overall, default 600)
      - KAIJU_BRIDGE_READ_TIMEOUT        (non-stream per-chunk read, default 180)
      - KAIJU_BRIDGE_STREAM_READ_TIMEOUT (stream per-chunk read, default 600)
      - KAIJU_BRIDGE_CONNECT_TIMEOUT     (TCP connect, default 30)"""
    def _f(env, default):
        try:
            return float(os.environ.get(env, "").strip() or default)
        except (ValueError, TypeError):
            return default
    connect = _f("KAIJU_BRIDGE_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)
    if streaming:
        # No total cap (None) — long thinking turns must not be killed by wall
        # time; the harness watchdog is the backstop. Generous per-chunk read.
        read = _f("KAIJU_BRIDGE_STREAM_READ_TIMEOUT", DEFAULT_STREAM_READ_TIMEOUT)
        return httpx.Timeout(None, connect=connect, read=read, write=None, pool=None)
    total = _f("KAIJU_BRIDGE_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
    read = _f("KAIJU_BRIDGE_READ_TIMEOUT", DEFAULT_READ_TIMEOUT)
    return httpx.Timeout(total, connect=connect, read=read)

# Headers that must never propagate inbound -> upstream.
STRIP_HEADERS_IN = frozenset(
    {
        "host",
        "authorization",
        "x-api-key",
        "content-length",
        "connection",
        "accept-encoding",
        "proxy-authorization",
    }
)
# Headers we must never copy upstream -> client (chunking, encoding artifacts).
STRIP_HEADERS_OUT = frozenset(
    {
        "content-encoding",
        "transfer-encoding",
        "connection",
        "content-length",
    }
)


# Either a single-account or multi-account provider is acceptable -- both
# expose ``get_access_token() -> str`` and ``force_reload() -> None``.
ProviderLike = Union[CredentialProvider, MultiAccountCredentialProvider]


def _upstream_base() -> str:
    return os.environ.get("KAIJU_CC_UPSTREAM", UPSTREAM_DEFAULT).rstrip("/")


def _max_inline_retries() -> int:
    try:
        return max(0, int(os.environ.get("KAIJU_CC_MAX_INLINE_RETRIES", "")))
    except ValueError:
        return DEFAULT_MAX_INLINE_RETRIES


def _max_inline_wait_seconds() -> int:
    try:
        return max(0, int(os.environ.get("KAIJU_CC_MAX_INLINE_WAIT", "")))
    except ValueError:
        return DEFAULT_MAX_INLINE_WAIT_SECONDS


# Option D — buffer-and-retry: buffer the whole upstream SSE stream and re-issue
# on a mid-stream drop so the client only ever sees a COMPLETE response.
#
# DEFAULT OFF (opt-in). Buffering the whole stream means the client (aider) gets
# NO incremental output until a turn completes, so the harness inactivity
# watchdog sees a frozen log for the entire turn and — on a long extended-thinking
# module (observed on gj's promise_node) — false-killed a healthy agent. The
# mid-stream drop is already handled transparently at the call level by litellm
# `num_retries` (Option A) plus recovery.py's transient retry (Option B), so D is
# only needed as a last resort. Enable with KAIJU_CC_BUFFER_AND_RETRY=1 when A/B
# prove insufficient for a persistent drop.
def _buffer_and_retry_enabled() -> bool:
    return os.environ.get("KAIJU_CC_BUFFER_AND_RETRY", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _max_stream_buffer_retries() -> int:
    try:
        return max(0, int(os.environ.get("KAIJU_CC_STREAM_BUFFER_RETRIES", "3")))
    except ValueError:
        return 3


# Seconds between SSE keepalive pings emitted to the client while the bridge is
# buffering/retrying upstream (keeps the client<->bridge connection from timing out).
_STREAM_KEEPALIVE_SECS = 15
# The exact ping event Anthropic itself emits — every client already ignores it
# for content, so it's the safest keepalive to synthesize.
_SSE_PING = b'event: ping\ndata: {"type": "ping"}\n\n'


def _sse_error_bytes(err_type: str, message: str) -> bytes:
    return (
        b"\nevent: error\ndata: "
        + json.dumps({"type": "error", "error": {"type": err_type, "message": message}}).encode("utf-8")
        + b"\n\n"
    )


def inject_system_prefix(body: dict[str, Any]) -> dict[str, Any]:
    """Ensure the Claude Code system prefix is present in ``body['system']``.

    Handles the three shapes the Messages API accepts:
        - ``system`` absent
        - ``system`` is a plain string
        - ``system`` is a list of content blocks

    Idempotent: if WE already injected the prefix (it sits at the very start of
    the system content) the body is returned unchanged.

    B16: the idempotency test is ANCHORED at the start, not a substring search.
    A substring `SYSTEM_PREFIX in system` false-suppresses injection whenever a
    user prompt merely quotes the prefix text mid-content — we'd then forward a
    request with no real leading prefix and the upstream rejects it as not a
    Claude Code request. Our own injection always lands at position 0, so an
    anchored check is both correct and quote-proof.
    """
    system = body.get("system")

    if isinstance(system, str):
        if system.startswith(SYSTEM_PREFIX):
            return body
        body["system"] = f"{SYSTEM_PREFIX}\n\n{system}"
        return body

    if isinstance(system, list):
        # Only the FIRST text block matters — that's where we inject.
        first_text = next(
            (blk.get("text", "") for blk in system
             if isinstance(blk, dict) and blk.get("type") == "text"),
            "",
        )
        if first_text.startswith(SYSTEM_PREFIX):
            return body
        body["system"] = [{"type": "text", "text": SYSTEM_PREFIX}, *system]
        return body

    body["system"] = [{"type": "text", "text": SYSTEM_PREFIX}]
    return body


def _normalize_path(path: str) -> str:
    return path.lstrip("/")


def _is_streaming_payload(raw_body: bytes) -> bool:
    # B12: parse the JSON and read the real boolean. A substring probe both
    # false-positives (the literal "stream":true inside prompt content) and
    # false-negatives ("stream" : true with odd spacing), and mis-routing a
    # streaming response to the non-streaming path buffers a multi-MB body and
    # breaks SSE timing. Fall back to the substring probe only on parse failure.
    if not raw_body:
        return False
    try:
        obj = json.loads(raw_body)
        if isinstance(obj, dict):
            return bool(obj.get("stream") is True)
    except (ValueError, TypeError):
        pass
    return b'"stream":true' in raw_body or b'"stream": true' in raw_body


def _build_forward_headers(
    request_headers: Any, access_token: str
) -> dict[str, str]:
    fwd: dict[str, str] = {}
    for k, v in request_headers.items():
        if k.lower() in STRIP_HEADERS_IN:
            continue
        fwd[k] = v
    fwd["Authorization"] = f"Bearer {access_token}"

    # Merge anthropic-beta values, case-insensitive.
    incoming_beta = ""
    for hdr_key in list(fwd.keys()):
        if hdr_key.lower() == "anthropic-beta":
            incoming_beta = fwd.pop(hdr_key)
    betas = [b.strip() for b in incoming_beta.split(",") if b.strip()]
    if OAUTH_BETA not in betas:
        betas.insert(0, OAUTH_BETA)
    fwd["anthropic-beta"] = ",".join(betas)

    if not any(k.lower() == "anthropic-version" for k in fwd):
        fwd["anthropic-version"] = DEFAULT_ANTHROPIC_VERSION
    return fwd


def _token_prefix(token: str) -> str:
    return token[:20] if token else ""


def _apply_classification_to_provider(
    provider: ProviderLike,
    token_used: str,
    classified: ClassifiedError,
) -> None:
    """Mark account state on the provider based on an upstream classification.

    Only the multi-account provider tracks per-account state; for the single
    provider we just ``force_reload`` on a token-invalid signal so the next
    request re-fetches from Keychain (in case the ``claude`` CLI rotated it).
    """
    # Pass the FULL token so the provider can attribute the error to the exact
    # slot that produced it (it matches on slot.last_token); a 20-char prefix is
    # ambiguous because all OAuth tokens share the `sk-ant-oat01-` prefix.
    # B5: stash the cap reset on the provider so /quota can surface it even for a
    # SINGLE account (whose /quota otherwise always reports next_reset_at=None,
    # forcing recovery to a 300s guess and premature give-up against a 5h cap).
    if classified.kind == ErrorKind.SUBSCRIPTION_CAP:
        try:
            provider.last_cap_reset_at = classified.reset_at_unix or (  # type: ignore[attr-defined]
                time.time() + (classified.retry_after_seconds or 300)
            )
        except Exception:  # noqa: BLE001
            pass
    if isinstance(provider, MultiAccountCredentialProvider):
        if classified.kind == ErrorKind.SUBSCRIPTION_CAP:
            reset_at = classified.reset_at_unix or (
                time.time() + (classified.retry_after_seconds or 300)
            )
            provider.mark_account_exhausted(token_used, reset_at)
        elif classified.kind in (
            ErrorKind.OAUTH_TOKEN_INVALID,
            ErrorKind.ACCOUNT_RESTRICTED,
            ErrorKind.BILLING_ERROR,
        ):
            provider.mark_account_invalid(token_used)
    else:
        if classified.kind == ErrorKind.OAUTH_TOKEN_INVALID:
            provider.force_reload()


def _build_error_response(
    classified: ClassifiedError, upstream_headers: Any = None
) -> JSONResponse:
    """Return a structured error response to the client."""
    headers: dict[str, str] = {"X-Kaiju-Bridge-Error": classified.kind.value}
    # B10: forward the genuine upstream rate-limit/request-id headers so the
    # client's own back-off logic (which keys on anthropic-ratelimit-*) and
    # debugging (request-id) keep working through the bridge.
    if upstream_headers is not None:
        for k, v in upstream_headers.items():
            kl = k.lower()
            if kl.startswith("anthropic-ratelimit-") or kl in ("request-id", "anthropic-request-id", "retry-after"):
                headers[k] = v
    if classified.retry_after_seconds is not None:
        headers["Retry-After"] = str(max(1, classified.retry_after_seconds))
    if classified.reset_at_unix is not None:
        headers["X-Kaiju-Reset-At"] = f"{classified.reset_at_unix:.0f}"
    body = {
        "type": "error",
        "error": {
            "type": classified.raw_error_type or "rate_limit_error",
            "message": classified.message,
        },
        "kaiju_bridge": {
            "kind": classified.kind.value,
            "retry_after_seconds": classified.retry_after_seconds,
            "reset_at_unix": classified.reset_at_unix,
            "request_id": classified.request_id,
        },
    }
    return JSONResponse(body, status_code=classified.status_code, headers=headers)


async def _forward_non_streaming(
    provider: ProviderLike,
    request_method: str,
    url: str,
    raw_body: bytes,
    headers_in: Any,
    params: dict[str, str],
) -> Response:
    """Send a non-streaming request with retry + failover."""
    max_retries = _max_inline_retries()
    max_wait = _max_inline_wait_seconds()
    attempt = 0
    last_response: Union[httpx.Response, None] = None
    # B9: track tokens already tried this call so we never spin re-selecting a
    # slot whose exhaustion/invalid marking didn't stick (attribution miss).
    _tried_tokens: set[str] = set()

    while True:
        try:
            # get_access_token() may block: Keychain subprocess, sync httpx
            # refresh with time.sleep backoff, and a blocking flock. Run it off
            # the event loop so one refresh can't freeze every concurrent request.
            access_token = await asyncio.to_thread(provider.get_access_token)
        except CredentialsError as e:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": str(e)},
                    "kaiju_bridge": {"kind": "credentials_unavailable"},
                },
                status_code=401,
            )

        # B9: if failover handed us a slot we already burned this call (its
        # exhausted/invalid marking didn't stick), stop rather than tight-spin
        # against a dead account. Mirrors the streaming path's guard.
        if access_token in _tried_tokens and last_response is not None:
            _LOG.warning(
                "failover re-selected an already-failed account; stopping to "
                "avoid a spin (tried %d)", len(_tried_tokens),
            )
            break

        fwd_headers = _build_forward_headers(headers_in, access_token)
        if request_method in ("POST", "PUT", "PATCH"):
            fwd_headers.setdefault("content-type", "application/json")

        async with httpx.AsyncClient(
            timeout=_bridge_timeout()
        ) as client:
            try:
                upstream = await client.request(
                    request_method,
                    url,
                    content=raw_body,
                    headers=fwd_headers,
                    params=params,
                )
            except httpx.HTTPError as e:
                _LOG.warning("upstream network error: %s", e)
                if attempt >= max_retries:
                    return JSONResponse(
                        {
                            "type": "error",
                            "error": {"type": "api_error", "message": str(e)},
                            "kaiju_bridge": {"kind": "network_error"},
                        },
                        status_code=502,
                    )
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, max_wait))
                continue

        last_response = upstream
        if 200 <= upstream.status_code < 300:
            # B5/H2: a success means we're no longer capped — clear any stale
            # cap-reset so /quota doesn't keep reporting a phantom cap after a
            # brief throttle recovered.
            try:
                if getattr(provider, "last_cap_reset_at", None) is not None:
                    provider.last_cap_reset_at = None  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            resp_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in STRIP_HEADERS_OUT
            }
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=upstream.headers.get("content-type"),
            )

        classified = classify_anthropic_error(
            upstream.status_code, upstream.content, upstream.headers
        )
        _LOG.info(
            "upstream error: status=%d kind=%s retry_after=%s request_id=%s",
            upstream.status_code,
            classified.kind.value,
            classified.retry_after_seconds,
            classified.request_id,
        )
        _apply_classification_to_provider(provider, access_token, classified)

        # Failover path: account problem + multi-account pool has another slot.
        if classified.kind.is_account_problem and isinstance(
            provider, MultiAccountCredentialProvider
        ):
            _tried_tokens.add(access_token)
            if provider.next_reset_at() is None:
                attempt += 1
                if attempt > max_retries:
                    break
                # B9: floor the failover retry so a marking-miss can't tight-spin;
                # the top-of-loop guard breaks if we get a burned token back.
                await asyncio.sleep(0.5)
                continue  # retry with next account

        # Inline retry on transient throttle or upstream 5xx within budget.
        if classified.kind.is_retryable:
            wait = classified.retry_after_seconds or (2 ** attempt)
            if attempt < max_retries and wait <= max_wait:
                attempt += 1
                _LOG.info(
                    "transient %s, sleeping %ds (attempt %d/%d)",
                    classified.kind.value, wait, attempt, max_retries,
                )
                await asyncio.sleep(wait)
                continue

        return _build_error_response(classified, upstream.headers)

    # Loop fell through (all retries exhausted on account-problem path).
    if last_response is not None:
        classified = classify_anthropic_error(
            last_response.status_code, last_response.content, last_response.headers
        )
        return _build_error_response(classified, last_response.headers)
    return JSONResponse(
        {
            "type": "error",
            "error": {"type": "api_error", "message": "max retries exceeded"},
            "kaiju_bridge": {"kind": "max_retries_exceeded"},
        },
        status_code=502,
    )


async def _stream_with_failover(
    provider: ProviderLike,
    request_method: str,
    url: str,
    raw_body: bytes,
    headers_in: Any,
    params: dict[str, str],
) -> Response:
    """Streaming variant: probe upstream first to classify failures cleanly.

    We open the stream and peek at the status; only on 2xx do we hand off
    to a chunk-passthrough generator. On 4xx/5xx we drain the body for the
    classifier and return a structured error / failover-retry just like
    the non-streaming path.
    """
    max_retries = _max_inline_retries()
    max_wait = _max_inline_wait_seconds()
    attempt = 0
    _tried_tokens: set[str] = set()  # B9: burned slots this call
    _last_classified: Optional[ClassifiedError] = None
    _last_headers: Any = None

    while True:
        try:
            # get_access_token() may block: Keychain subprocess, sync httpx
            # refresh with time.sleep backoff, and a blocking flock. Run it off
            # the event loop so one refresh can't freeze every concurrent request.
            access_token = await asyncio.to_thread(provider.get_access_token)
        except CredentialsError as e:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": "authentication_error", "message": str(e)},
                    "kaiju_bridge": {"kind": "credentials_unavailable"},
                },
                status_code=401,
            )

        # B9: stop if failover handed us an already-failed slot (marking miss).
        if access_token in _tried_tokens and _last_classified is not None:
            _LOG.warning(
                "stream failover re-selected an already-failed account; stopping "
                "to avoid a spin (tried %d)", len(_tried_tokens),
            )
            return _build_error_response(_last_classified, _last_headers)

        fwd_headers = _build_forward_headers(headers_in, access_token)
        fwd_headers.setdefault("content-type", "application/json")

        client = httpx.AsyncClient(
            timeout=_bridge_timeout(streaming=True)
        )
        try:
            upstream_cm = client.stream(
                request_method,
                url,
                content=raw_body,
                headers=fwd_headers,
                params=params,
            )
            upstream = await upstream_cm.__aenter__()
        except httpx.HTTPError as e:
            await client.aclose()
            _LOG.warning("upstream stream open error: %s", e)
            if attempt >= max_retries:
                return JSONResponse(
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": str(e)},
                        "kaiju_bridge": {"kind": "network_error"},
                    },
                    status_code=502,
                )
            attempt += 1
            await asyncio.sleep(min(2 ** attempt, max_wait))
            continue

        if 200 <= upstream.status_code < 300:
            async def event_stream():
                # B2: a 200 only means the stream OPENED. Anthropic can still drop
                # the connection mid-stream or emit an `event: error` frame AFTER
                # the 200. If we relay bytes blindly and the stream ends without a
                # terminal `message_stop`, the client records a TRUNCATED turn as a
                # completed assistant message — silent garbage. So we watch for the
                # terminal event / an error frame, and on premature close inject a
                # synthetic SSE error event so the client raises and retries.
                saw_stop = False
                saw_error = False
                # Track only SSE *event lines* (`event: message_stop` / `event: error`)
                # — matching arbitrary body bytes false-latches when the model's own
                # output contains the literal `message_stop` / `"type":"error"`.
                # Scan carry+chunk IN FULL, then keep a 64B carry (>= marker length)
                # so a marker split across two chunks still matches. Never truncate
                # BEFORE scanning: a marker sitting >256B before the end of a large
                # chunk would scroll out unseen (the codex-bridge bug that flagged
                # every long completed stream as truncated). The carry is seeded
                # with a newline so an event line at stream start is line-anchored.
                tail = b"\n"
                try:
                    async for chunk in upstream.aiter_bytes():
                        window = tail + chunk
                        if b"\nevent: message_stop" in window:
                            saw_stop = True
                        if b"\nevent: error" in window:
                            saw_error = True
                        tail = window[-64:]
                        yield chunk
                except Exception as e:  # noqa: BLE001 - any read failure mid-stream (not BaseException)
                    _LOG.warning("mid-stream read error after status 200: %s", e)
                    yield (
                        b"\nevent: error\n"
                        b'data: {"type":"error","error":{"type":"api_error",'
                        b'"message":"kaiju-bridge: upstream stream aborted mid-response"}}\n\n'
                    )
                    saw_error = True
                finally:
                    await upstream_cm.__aexit__(None, None, None)
                    await client.aclose()
                if not saw_stop and not saw_error:
                    # Stream ended cleanly at the socket but without a terminal
                    # message_stop -> truncation. Force the client to treat it as
                    # an error rather than a complete (short) turn.
                    _LOG.warning("stream ended without message_stop -> signalling truncation")
                    yield (
                        b"\nevent: error\n"
                        b'data: {"type":"error","error":{"type":"api_error",'
                        b'"message":"kaiju-bridge: upstream stream ended without message_stop (truncated)"}}\n\n'
                    )

            # Forward the upstream status and headers (request-id,
            # anthropic-ratelimit-*) instead of hardcoding 200 / dropping them,
            # so clients keep rate-limit visibility and debugging IDs.
            passthrough_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in STRIP_HEADERS_OUT
            }
            return StreamingResponse(
                event_stream(),
                status_code=upstream.status_code,
                headers=passthrough_headers,
                media_type=upstream.headers.get("content-type", "text/event-stream"),
            )

        # Non-2xx: drain body for classification and unwind the stream.
        body_bytes = b""
        try:
            async for chunk in upstream.aiter_bytes():
                body_bytes += chunk
                if len(body_bytes) > 65536:
                    break
        finally:
            await upstream_cm.__aexit__(None, None, None)
            await client.aclose()

        classified = classify_anthropic_error(
            upstream.status_code, body_bytes, upstream.headers
        )
        _last_classified = classified
        _last_headers = upstream.headers
        _LOG.info(
            "upstream stream error: status=%d kind=%s retry_after=%s",
            upstream.status_code, classified.kind.value, classified.retry_after_seconds,
        )
        _apply_classification_to_provider(provider, access_token, classified)

        if classified.kind.is_account_problem and isinstance(
            provider, MultiAccountCredentialProvider
        ):
            _tried_tokens.add(access_token)
            if provider.next_reset_at() is None:
                attempt += 1
                if attempt > max_retries:
                    return _build_error_response(classified, upstream.headers)
                # B9: floor the failover retry so a marking-miss can't tight-spin.
                await asyncio.sleep(0.5)
                continue

        if classified.kind.is_retryable:
            wait = classified.retry_after_seconds or (2 ** attempt)
            if attempt < max_retries and wait <= max_wait:
                attempt += 1
                await asyncio.sleep(wait)
                continue

        return _build_error_response(classified, upstream.headers)


async def _stream_buffered_with_retry(
    provider: ProviderLike,
    request_method: str,
    url: str,
    raw_body: bytes,
    headers_in: Any,
    params: dict[str, str],
) -> Response:
    """Option D — buffer the ENTIRE upstream SSE stream and re-issue on a
    mid-stream drop, so the client only ever receives a COMPLETE response (or a
    clean error), never a truncated one.

    A ``peer closed connection without sending complete message body (incomplete
    chunked read)`` on a long turn is invisible to the client here: the bridge
    swallows it and re-issues the request to Anthropic itself, emitting SSE ping
    keepalives to the client meanwhile so its connection can't time out.

    Trade vs ``_stream_with_failover``: no incremental token delivery (the whole
    response is replayed at once) and upstream ratelimit headers aren't forwarded
    on the success path. Gated by ``KAIJU_CC_BUFFER_AND_RETRY`` (default on).
    """
    max_retries = _max_stream_buffer_retries()
    max_wait = _max_inline_wait_seconds()

    async def _capture() -> Tuple[str, bytes]:
        """Return (kind, body) where kind ∈ {'ok','error','creds','incomplete'}.
        'ok' body is a complete SSE stream (or a terminal error frame) ready to
        replay verbatim."""
        attempt = 0
        tried_tokens: set[str] = set()
        while True:
            try:
                access_token = await asyncio.to_thread(provider.get_access_token)
            except CredentialsError as e:
                return ("creds", str(e).encode("utf-8"))
            if access_token in tried_tokens:
                return ("error", b"")  # failover looped to a burned slot

            fwd = _build_forward_headers(headers_in, access_token)
            fwd.setdefault("content-type", "application/json")
            buf = bytearray()
            tail = b"\n"  # line-anchor seed for the event-line scan below
            saw_stop = False
            saw_error = False
            client = httpx.AsyncClient(timeout=_bridge_timeout(streaming=True))
            try:
                cm = client.stream(request_method, url, content=raw_body, headers=fwd, params=params)
                upstream = await cm.__aenter__()
                try:
                    if not (200 <= upstream.status_code < 300):
                        body = b""
                        async for c in upstream.aiter_bytes():
                            body += c
                            if len(body) > 65536:
                                break
                        classified = classify_anthropic_error(upstream.status_code, body, upstream.headers)
                        _apply_classification_to_provider(provider, access_token, classified)
                        if classified.kind.is_account_problem and isinstance(provider, MultiAccountCredentialProvider):
                            tried_tokens.add(access_token)
                            if provider.next_reset_at() is None and attempt < max_retries:
                                attempt += 1
                                await asyncio.sleep(0.5)
                                continue
                        if classified.kind.is_retryable and attempt < max_retries:
                            attempt += 1
                            wait = classified.retry_after_seconds or (2 ** attempt)
                            await asyncio.sleep(min(wait, max_wait))
                            continue
                        return ("error", body)
                    # 2xx — a success means we're not capped anymore (clear phantom).
                    try:
                        if getattr(provider, "last_cap_reset_at", None) is not None:
                            provider.last_cap_reset_at = None  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
                    async for chunk in upstream.aiter_bytes():
                        buf += chunk
                        # Scan carry+chunk in full, THEN truncate the carry —
                        # see the event_stream twin above for why order matters.
                        window = tail + chunk
                        if b"\nevent: message_stop" in window:
                            saw_stop = True
                        if b"\nevent: error" in window:
                            saw_error = True
                        tail = window[-64:]
                finally:
                    await cm.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001 — mid-stream read/connect drop
                _LOG.warning("buffered stream: upstream drop (attempt %d/%d): %s",
                             attempt + 1, max_retries, e)
            finally:
                await client.aclose()

            if saw_stop:
                return ("ok", bytes(buf))  # complete stream captured
            # Incomplete: mid-stream drop OR ended without message_stop.
            attempt += 1
            if attempt > max_retries:
                if saw_error:
                    return ("ok", bytes(buf))  # a terminal error frame is complete enough to relay
                _LOG.error("buffered stream: still incomplete after %d retries", max_retries)
                return ("incomplete", b"")
            await asyncio.sleep(min(2 ** attempt, max_wait))
            _LOG.info("buffered stream: re-issuing upstream (attempt %d/%d)", attempt, max_retries)

    async def event_stream():
        task = asyncio.create_task(_capture())
        try:
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=_STREAM_KEEPALIVE_SECS)
                except asyncio.TimeoutError:
                    yield _SSE_PING  # keep the client<->bridge connection warm
            kind, body = task.result()
            if kind == "ok":
                yield body
            elif kind == "creds":
                yield _sse_error_bytes("authentication_error", body.decode("utf-8", "replace") or "credentials unavailable")
            elif kind == "error":
                yield _sse_error_bytes("api_error", "kaiju-bridge: upstream error (buffered)")
            else:  # incomplete
                yield _sse_error_bytes("api_error", "kaiju-bridge: upstream stream incomplete after retries")
        finally:
            # If the client disconnected mid-buffer, don't leak the capture task.
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Kaiju-Bridge-Mode": "buffer-and-retry"},
    )


def _resolve_provider() -> ProviderLike:
    """Pick single-account or multi-account provider based on env."""
    pool_spec = os.environ.get("KAIJU_CC_ACCOUNT_POOL", "").strip()
    if pool_spec:
        pool = load_account_pool(pool_spec)
        if pool is not None:
            _LOG.info("Using multi-account pool with %d slots", len(pool.snapshot()))
            return pool
    return CredentialProvider()


def build_app(provider: ProviderLike | None = None) -> FastAPI:
    app = FastAPI(title="Claude Code OAuth Bridge", version="1.1.0")
    prov: ProviderLike = provider if provider is not None else _resolve_provider()
    inject = os.environ.get("KAIJU_CC_SKIP_SYSTEM_PREFIX") != "1"

    # B1: optional shared secret. Without it, ANY local process can spend the
    # user's subscription by POSTing to the bridge. When KAIJU_CC_BRIDGE_SECRET is
    # set, every proxied request must present it (x-api-key OR Authorization
    # bearer OR x-kaiju-bridge-secret). Bind to 127.0.0.1 regardless.
    bridge_secret = os.environ.get("KAIJU_CC_BRIDGE_SECRET", "").strip()
    if not bridge_secret:
        _LOG.warning(
            "KAIJU_CC_BRIDGE_SECRET is not set — the bridge is UNAUTHENTICATED; any "
            "local process can spend this subscription. Set it (and point clients' "
            "ANTHROPIC_API_KEY at the same value) to lock it down."
        )

    def _authorized(request: Request) -> bool:
        if not bridge_secret:
            return True
        presented = (
            request.headers.get("x-kaiju-bridge-secret")
            or request.headers.get("x-api-key")
            or ""
        )
        if not presented:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                presented = auth[7:].strip()
        # constant-time compare
        import hmac
        return hmac.compare_digest(presented, bridge_secret)

    @app.get("/healthz")
    async def healthz(request: Request):
        # Liveness must work WITHOUT the secret (the launcher/monitor poll it),
        # but M1: don't leak the token prefix / account state to unauthenticated
        # callers when a secret is configured — redact instead of 401.
        _auth = _authorized(request)
        try:
            # B11: get_access_token can block (Keychain subprocess / refresh /
            # flock); run it off the loop so /healthz can't stall and trigger a
            # spurious monitor restart that wipes account state.
            token = await asyncio.to_thread(prov.get_access_token)
        except CredentialsError as e:
            return JSONResponse(
                {"ok": False, "error": str(e)},
                status_code=503,
            )
        info: dict[str, Any] = {"ok": True}
        if _auth:
            info["token_prefix"] = token[:15] + "..."
            if isinstance(prov, MultiAccountCredentialProvider):
                info["accounts"] = prov.snapshot()
        return info

    # C1 audit fix: cache /quota responses briefly so 100+ modules simultaneously
    # hitting a cap don't stampede the provider snapshot lock. TTL is short (2s)
    # so recovery.py's next_reset polling still gets fresh data on the next tick.
    # State is per-app (per-process), never persisted — safe to lose on restart.
    _quota_cache: dict[str, tuple[float, dict]] = {}
    _quota_cache_ttl_sec = float(os.environ.get("KAIJU_CC_QUOTA_CACHE_TTL_SEC", "2.0"))

    @app.get("/quota")
    async def quota(request: Request):
        """Pipeline introspection: per-account exhaustion + soonest reset.

        recovery.py needs the reset time without coordinating a secret, so this
        stays reachable; but the per-account token_prefix is redacted unless the
        caller is authorized (M1). Responses are cached briefly (KAIJU_CC_QUOTA_CACHE_TTL_SEC,
        default 2s) so parallel modules querying during a cap don't stampede the
        provider snapshot lock."""
        _auth = _authorized(request)
        cache_key = "auth" if _auth else "noauth"
        _now = time.time()
        _entry = _quota_cache.get(cache_key)
        if _entry is not None and (_now - _entry[0]) < _quota_cache_ttl_sec:
            return _entry[1]
        if isinstance(prov, MultiAccountCredentialProvider):
            snap = prov.snapshot()
            if not _auth:
                for s in snap:
                    s.pop("token_prefix", None)
            payload = {
                "multi_account": True,
                "accounts": snap,
                "next_reset_at_unix": prov.next_reset_at(),
            }
        else:
            # B5: surface the most recent observed cap reset for the single account
            # so recovery can wait the real duration instead of a 300s fallback.
            _reset = getattr(prov, "last_cap_reset_at", None)
            if _reset is not None and _reset <= _now:
                _reset = None  # already reset
            payload = {"multi_account": False, "accounts": [], "next_reset_at_unix": _reset}
        _quota_cache[cache_key] = (_now, payload)
        return payload

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def proxy(path: str, request: Request) -> Response:
        if not _authorized(request):
            return JSONResponse(
                {"type": "error", "error": {"type": "authentication_error",
                 "message": "kaiju-bridge: missing/invalid bridge secret"}},
                status_code=401,
            )
        raw_body = await request.body()
        norm_path = _normalize_path(path)

        # Inject "You are Claude Code" prefix on /v1/messages POSTs.
        if (
            inject
            and norm_path == "v1/messages"
            and request.method == "POST"
            and raw_body
        ):
            try:
                body_json = json.loads(raw_body)
                if isinstance(body_json, dict):
                    body_json = inject_system_prefix(body_json)
                    raw_body = json.dumps(body_json).encode("utf-8")
            except ValueError as e:
                _LOG.warning("Skipping system-prefix injection (bad JSON): %s", e)

        url = f"{_upstream_base()}/{norm_path}"
        params = dict(request.query_params)

        if _is_streaming_payload(raw_body):
            # Option D: buffer-and-retry recovers a mid-stream drop transparently
            # (default on); the incremental path is the fallback when disabled.
            if _buffer_and_retry_enabled():
                return await _stream_buffered_with_retry(
                    prov, request.method, url, raw_body, request.headers, params
                )
            return await _stream_with_failover(
                prov, request.method, url, raw_body, request.headers, params
            )
        return await _forward_non_streaming(
            prov, request.method, url, raw_body, request.headers, params
        )

    return app


app = build_app()
