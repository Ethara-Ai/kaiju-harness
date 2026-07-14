"""Pause-and-resume helper for the per-module aider loop.

When ``run_pipeline.sh`` is using the Claude Code OAuth bridge and the
upstream returns a `SUBSCRIPTION_CAP` 429 (5-hour or weekly limit hit), the
bridge can't retry inline -- the wait would exceed the inactivity watchdog.
Instead it bubbles a structured ``X-Kaiju-Bridge-Error: subscription_cap``
response, which litellm raises as ``litellm.exceptions.RateLimitError``.

This module wraps ``agent.run(...)`` so we catch that, query the bridge's
``/quota`` endpoint for the soonest reset time, sleep with periodic
heartbeats (so ``INACTIVITY_TIMEOUT`` doesn't fire), and retry the same call
once. Bedrock / Vertex paths bypass this entirely (the wrapper is a no-op
when ``ANTHROPIC_API_BASE`` is not pointing at the bridge).
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import urlparse

import httpx

# Best-effort: import real exception classes so isinstance() works against the
# canonical types. Fall back to MRO-walk + string match if litellm/openai aren't
# present (e.g. in pure-unit tests).
_RATE_LIMIT_EXC_CLASSES: tuple[type, ...] = ()
try:
    from litellm.exceptions import RateLimitError as _LitellmRateLimitError  # type: ignore
    _RATE_LIMIT_EXC_CLASSES = _RATE_LIMIT_EXC_CLASSES + (_LitellmRateLimitError,)
except ImportError:
    pass
try:
    from openai import RateLimitError as _OpenAIRateLimitError  # type: ignore
    _RATE_LIMIT_EXC_CLASSES = _RATE_LIMIT_EXC_CLASSES + (_OpenAIRateLimitError,)
except ImportError:
    pass
_LOG = logging.getLogger(__name__)

_RATE_LIMIT_PHRASE_RE = re.compile(r"\brate[\s_-]?limit(?:ed|ing)?\b", re.IGNORECASE)

T = TypeVar("T")

DEFAULT_MAX_PAUSE_SECONDS = 6 * 3600  # 6h: just above the 5h cap, well below weekly
DEFAULT_HEARTBEAT_SECONDS = 60        # < INACTIVITY_TIMEOUT (900s in run_pipeline.sh)
QUOTA_TIMEOUT = 5.0


def _bridge_base_url() -> Optional[str]:
    """Return the bridge base URL only when ``ANTHROPIC_API_BASE`` is set to a
    local-looking value. We don't want to call /quota on api.anthropic.com.
    """
    base = os.environ.get("ANTHROPIC_API_BASE", "").strip()
    if not base:
        return None
    try:
        host = urlparse(base).hostname or ""
    except Exception:  # noqa: BLE001
        return None
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal", "gateway.docker.internal") or host.endswith(".local") or host.endswith(".internal"):
        return base.rstrip("/")
    return None


def _fetch_quota(base_url: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=QUOTA_TIMEOUT) as c:
            r = c.get(f"{base_url}/quota")
            if r.status_code != 200:
                return {}
            return r.json()
    except (httpx.HTTPError, ValueError) as e:
        _LOG.warning("could not query bridge /quota: %s", e)
        return {}


def _next_reset_seconds(base_url: str, fallback_seconds: int) -> int:
    """Seconds until the soonest cap reset, or ``fallback_seconds`` if unknown."""
    quota = _fetch_quota(base_url)
    reset_at = quota.get("next_reset_at_unix")
    if reset_at:
        delta = int(reset_at - time.time())
        if delta > 0:
            return delta
    return fallback_seconds


def _effective_max_retries(base_url: str, user_max_retries: int) -> int:
    """Auto-scale ``max_retries`` to the multi-account pool size.

    Single-account: user's value (default 1) is fine -- one pause cycle covers
    the 5h cap reset for a single subscription.

    Multi-account: we must be able to traverse the entire pool before giving
    up. If accountA hits a cap, recovery pauses, retries, lands on accountB.
    If accountB is ALSO capped, we need ANOTHER retry to reach accountC, etc.
    So effective_max_retries >= pool_size.
    """
    quota = _fetch_quota(base_url)
    accounts = quota.get("accounts") or []
    pool_size = len(accounts) if quota.get("multi_account") else 0
    if pool_size <= 1:
        return user_max_retries
    return max(user_max_retries, pool_size)


def _max_pause_seconds() -> int:
    raw = os.environ.get("KAIJU_CC_MAX_PAUSE_SEC", "").strip()
    if not raw:
        return DEFAULT_MAX_PAUSE_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return DEFAULT_MAX_PAUSE_SECONDS


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect litellm/openai-flavored rate-limit errors."""
    if _RATE_LIMIT_EXC_CLASSES and isinstance(exc, _RATE_LIMIT_EXC_CLASSES):
        return True
    for cls in type(exc).__mro__:
        # Only the concrete RateLimitError — NOT litellm's base `APIError`, which
        # is the parent of auth/bad-request/etc. Treating APIError as rate-limit
        # would pause-and-resume (up to KAIJU_CC_MAX_PAUSE_SEC, default 6h) on
        # errors a retry can never fix.
        if cls.__name__ == "RateLimitError" and "litellm" in cls.__module__:
            return True
        if cls.__name__ == "RateLimitError" and cls.__module__.startswith("openai"):
            return True
    msg = str(exc).lower()
    # Slug-shape / exception-name / HTTP-status matches: unambiguous, no negation risk.
    if ("rate_limit_error" in msg
            or "ratelimiterror" in msg
            or "too many requests" in msg
            or "resource_exhausted" in msg
            or "subscription_cap" in msg):
        return True
    # Human-message form: "rate limit(ed)" phrase. Guard against negations
    # like "not a rate limit" and "no rate limit" so a test/comment/log line
    # referencing the concept doesn't false-positive-match. Anthropic frequently
    # returns a rate limit wrapped in a 429/500/529 whose TYPE is
    # InternalServerError/MidStreamFallbackError but whose MESSAGE is
    # "Rate limited" (a SPACE, not the rate_limit_error slug) — that legitimate
    # form still matches because no negation precedes it.
    for m in _RATE_LIMIT_PHRASE_RE.finditer(msg):
        prefix = msg[max(0, m.start() - 12):m.start()]
        # Add a leading space so negations at message start (e.g. "not a rate...")
        # still match the pattern " not a " / " not " with word boundaries.
        prefix_padded = " " + prefix
        if any(neg in prefix_padded for neg in (" not a ", " not ", " no ", " isn't a ", " isn't ")):
            continue
        return True
    # Bridge "all accounts exhausted" signal: when every account in a
    # multi-account pool is capped, the bridge short-circuits with a 401
    # authentication_error whose body carries kind="credentials_unavailable"
    # and a message "all N accounts exhausted; soonest reset in Xs". That is a
    # CAP condition a pause-and-resume WILL fix (wait for the soonest reset via
    # /quota), NOT a real auth failure. Without this, parallel requests that
    # arrive while the pool is already fully capped get this 401 and terminate
    # instead of waiting (only the request that TRIGGERS the last cap sees a
    # real subscription_cap and pauses). Kept narrow to the bridge's distinctive
    # phrasing so a genuine bad-token 401 still fails fast rather than pausing.
    return ("accounts exhausted" in msg
            or "credentials_unavailable" in msg
            or "credentials unavailable" in msg)


# --------------------------------------------------------------------------
# Transient network-error detection (Fix #5: connectivity hardening)
#
# Distinct from rate-limit errors: these are timeouts, connection resets,
# and mid-stream aborts where Anthropic's edge layer dropped or stalled the
# response. They retry FAST with bounded exponential backoff (5s/10s/20s)
# rather than waiting for a rate-limit reset.
#
# We catch by name (not isinstance) so we don't take a hard dependency on
# every possible httpx/httpcore version installed alongside litellm.
# --------------------------------------------------------------------------
_TRANSIENT_EXC_NAMES = (
    "ReadTimeout", "WriteTimeout", "ConnectTimeout", "PoolTimeout",
    "ReadError", "WriteError", "RemoteProtocolError", "ConnectError",
    "NetworkError", "MidStreamFallbackError", "APIConnectionError",
    "APITimeoutError",
    # LLM API server-side errors (openai/anthropic/litellm) — class-name check
    # is safe (zero false-positive risk) because it matches type(exc).__name__,
    # NOT source code text. Substring match on 'InternalServerError' was
    # removed from _LLM_TRANSIENT_SIGNALS to avoid false positives on repos
    # like BlackSheep whose source references InternalServerError as an HTTP
    # exception class. Class-name detection catches the RAISED form here.
    "InternalServerError", "ServiceUnavailableError",
    # Raised by agent.agents.raise_if_transient_llm_error when aider SWALLOWED a
    # transient LLM/network error (printed but did not re-raise). Retrying it
    # re-runs the whole module so no litellm error is left unhandled.
    "TransientLLMError",
)

_TRANSIENT_MSG_SIGNALS = (
    "timed out", "timeout", "connection reset", "connection aborted",
    "remote end closed", "server disconnected", "midstream",
    "read timeout", "connection error",
    # Mid-stream chunked-transfer drop (Anthropic load-shedding a long stream):
    # "peer closed connection without sending complete message body
    #  (incomplete chunked read)". Match by message too, not just class name,
    # so a litellm/httpx rename can't silently drop it off the transient track.
    "peer closed connection", "incomplete chunked read", "chunked read",
    "without sending complete message body",
)

# Default backoff schedule (seconds). Configurable via
# KAIJU_CC_TRANSIENT_BACKOFF="5,10,20" env var.
# 5 attempts (was 3): a giant turn that drops mid-stream is re-sent verbatim, so
# it often needs several tries to land — especially while the server is
# load-shedding (529s). The extra waits are cheap vs. losing the module's work.
_DEFAULT_TRANSIENT_BACKOFF = (5, 10, 20, 40, 60)


def _transient_backoff_schedule() -> tuple[int, ...]:
    raw = os.environ.get("KAIJU_CC_TRANSIENT_BACKOFF", "").strip()
    if not raw:
        return _DEFAULT_TRANSIENT_BACKOFF
    try:
        out = tuple(int(x.strip()) for x in raw.split(",") if x.strip())
        return out or _DEFAULT_TRANSIENT_BACKOFF
    except ValueError:
        return _DEFAULT_TRANSIENT_BACKOFF


# Total in-line wait budget for a SUSTAINED transient outage (seconds). Rather
# than give up after the short escalating schedule above and defer the module to a
# manual --resume (bad for large batches: wasted setup, split data, human in the
# loop), we RIDE OUT the outage in-line — escalate to the cap, then keep retrying
# at the cap until this budget is spent. A 502/InternalServerError/mid-stream drop
# almost always clears within a few minutes, so the module completes in the SAME
# pass and marks .done. Only a genuinely dead endpoint (budget exhausted) gives up.
# Configurable via KAIJU_CC_TRANSIENT_MAX_SEC (default 900s = 15 min).
_DEFAULT_TRANSIENT_MAX_SECONDS = 900


def _transient_max_seconds() -> int:
    raw = os.environ.get("KAIJU_CC_TRANSIENT_MAX_SEC", "").strip()
    if not raw:
        return _DEFAULT_TRANSIENT_MAX_SECONDS
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_TRANSIENT_MAX_SECONDS


def _is_transient_network_error(exc: BaseException) -> bool:
    """Detect transient network errors (timeouts, connection drops, mid-stream aborts).

    NOT the same as rate-limit errors (those use the dedicated rate-limit
    detection path and pause-and-resume on /quota reset)."""
    # Don't double-classify: a rate-limit error is also a network event but
    # belongs on its own slower retry track.
    if _is_rate_limit_error(exc):
        return False
    for cls in type(exc).__mro__:
        if cls.__name__ in _TRANSIENT_EXC_NAMES:
            return True
    msg = str(exc).lower()
    if any(sig in msg for sig in _TRANSIENT_MSG_SIGNALS):
        return True
    # Stay in lock-step with the backstop's comprehensive signal list (agents.py
    # raise_if_transient_llm_error). Historically the two drifted: agents.py knew
    # 'internalservererror'/'overloaded'/'service unavailable'/'502-504'/
    # 'connection refused' but recovery.py did not, so a RAISED form of those
    # (as opposed to one aider swallowed) was NOT retried -> the module crashed
    # with no retry. Reuse the SAME list here so a raised error is always retried
    # whenever a swallowed one would be. Lazy import avoids the agents<->recovery
    # import cycle. (Rate limits are handled above on their own track first.)
    try:
        from agent.agents import _LLM_TRANSIENT_SIGNALS
        return any(sig in msg for sig in _LLM_TRANSIENT_SIGNALS)
    except Exception:  # noqa: BLE001 - never let a diagnostic import break recovery
        return False


def _extract_retry_after_from_error(exc: BaseException) -> Optional[int]:
    """Best-effort: pull a Retry-After hint from a litellm RateLimitError."""
    resp = getattr(exc, "response", None) or getattr(exc, "_response", None)
    if resp is not None:
        headers = getattr(resp, "headers", None)
        if headers is not None:
            try:
                val = headers.get("Retry-After") or headers.get("retry-after")
                if val is not None:
                    return int(val)
            except (TypeError, ValueError):
                pass
    msg = str(exc)
    m = re.search(r"retry[-_ ]after[:\s]+(\d+)", msg, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _heartbeat(log_dir: Optional[Path]) -> None:
    """Re-touch the dedicated rate-limit pause marker.

    B15: we deliberately do NOT forge activity on ``agent_run.log`` / ``aider.log``
    anymore. Touching the very files the watchdog uses to detect a hang made a
    genuine wedge (e.g. the recovery loop itself stalling) indistinguishable from
    a healthy pause. Instead we drop/refresh an EXPLICIT ``.rate_limit_paused``
    marker that the watchdog now understands (see ``run_pipeline_rust.sh:
    _pause_marker_fresh``): while the marker is fresh the inactivity kill is
    suppressed, but the absolute wall-time cap still bounds the pause.
    """
    if log_dir is None:
        return
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / ".rate_limit_paused").touch()
    except OSError:
        pass


def _sleep_with_heartbeat(
    total_seconds: int,
    log_dir: Optional[Path],
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
) -> None:
    """Sleep up to ``total_seconds``, touching ``log_dir/.rate_limit_paused`` periodically."""
    end = time.time() + max(0, total_seconds)
    while time.time() < end:
        slice_s = min(heartbeat_seconds, end - time.time())
        if slice_s <= 0:
            break
        time.sleep(slice_s)
        _heartbeat(log_dir)


def _raise_as_transient(exc: BaseException) -> "NoReturn":
    """Re-raise an EXHAUSTED but retryable error (transient network / rate-limit)
    as ``TransientLLMError`` so the agent runners' ``except TransientLLMError``
    routes it to a ``.needs_retry`` breadcrumb -> AUTO-RESUME, instead of letting
    the raw exception crash the module/repo with no retry.

    Without this, only errors aider SWALLOWED (which the agents.py backstop already
    wraps as TransientLLMError) got auto-resumed; a raw litellm/network error that
    propagated un-swallowed, or a rate-limit that outran the pause budget, would
    fall through the runners' `except TransientLLMError` and fail the repo. This
    makes recovery-exhaustion uniformly retryable. Idempotent (an already-
    TransientLLMError is re-raised as-is); falls back to the original on import
    failure so it can never mask an error by crashing here.
    """
    try:
        from agent.agents import TransientLLMError  # lazy: avoid circular import
    except Exception:  # noqa: BLE001
        raise exc
    if isinstance(exc, TransientLLMError):
        raise exc
    raise TransientLLMError(str(exc)) from exc


def run_with_recovery(
    fn: Callable[..., T],
    *args: Any,
    _kaiju_log_dir: Optional[Path] = None,
    max_retries: int = 1,
    **kwargs: Any,
) -> T:
    """Call ``fn(*args, **kwargs)``, pause-and-resume on rate-limit errors.

    Only active when the harness is talking to the local Claude Code bridge
    (i.e. ``ANTHROPIC_API_BASE`` points at localhost). Bedrock/Vertex/direct-API
    callers get the unwrapped exception, preserving existing behavior.
    """
    base_url = _bridge_base_url()
    if base_url is None:
        # Non-bridge (direct API / Vertex / Bedrock): there's no bridge quota
        # endpoint to pause against for the in-line ride-out, but we STILL
        # guarantee auto-resume — classify a retryable failure and re-cast it as
        # TransientLLMError, which every agent runner's `except TransientLLMError`
        # routes to a .needs_retry breadcrumb -> AUTO-RESUME (that loop supplies
        # the retry cadence + pause). Non-retryable errors propagate unchanged, and
        # KeyboardInterrupt/SystemExit propagate because we catch Exception only.
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _is_transient_network_error(exc) or _is_rate_limit_error(exc):
                _raise_as_transient(exc)  # -> .needs_retry -> auto-resume
            raise

    effective_max_retries = _effective_max_retries(base_url, max_retries)
    max_pause = _max_pause_seconds()
    transient_backoff = _transient_backoff_schedule()
    transient_budget = _transient_max_seconds()
    attempt = 0          # rate-limit retry counter
    transient_attempt = 0  # transient-network retry counter
    transient_waited = 0   # cumulative in-line transient wait (seconds)
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            # Catch Exception (NOT BaseException) so KeyboardInterrupt/SystemExit
            # propagate immediately instead of being swallowed/retried.
            # Branch 1: transient network error (timeout / connection drop / mid-stream)
            if _is_transient_network_error(exc):
                # Escalate through the schedule, then hold at its cap and KEEP
                # retrying until the total in-line budget is spent — ride out a
                # sustained outage here instead of dropping to a manual --resume.
                wait_s = transient_backoff[min(transient_attempt, len(transient_backoff) - 1)]
                if transient_waited + wait_s > transient_budget:
                    _LOG.error(
                        "transient-error recovery exhausted after %ds of in-line "
                        "waiting across %d retries (budget=%ds): %s",
                        transient_waited, transient_attempt, transient_budget, exc,
                    )
                    _raise_as_transient(exc)  # -> .needs_retry -> auto-resume
                _LOG.warning(
                    "transient network error (%s); retrying in %ds (attempt %d, "
                    "%ds/%ds of in-line budget spent). Heartbeating dir=%s to keep "
                    "the inactivity watchdog quiet.",
                    type(exc).__name__, wait_s, transient_attempt + 1,
                    transient_waited, transient_budget, _kaiju_log_dir,
                )
                _sleep_with_heartbeat(wait_s, _kaiju_log_dir)
                transient_waited += wait_s
                transient_attempt += 1
                continue

            # Branch 2: rate-limit error
            if not _is_rate_limit_error(exc):
                raise
            if attempt >= effective_max_retries:
                _LOG.error(
                    "rate-limit recovery exhausted after %d retries: %s", attempt, exc
                )
                _raise_as_transient(exc)  # -> .needs_retry -> auto-resume

            retry_after_hint = _extract_retry_after_from_error(exc) or 300
            wait_seconds = _next_reset_seconds(base_url, retry_after_hint)
            if wait_seconds > max_pause:
                _LOG.warning(
                    "rate-limit reset in %ds exceeds KAIJU_CC_MAX_PAUSE_SEC=%ds; giving up",
                    wait_seconds, max_pause,
                )
                _raise_as_transient(exc)  # -> .needs_retry -> auto-resume

            _LOG.warning(
                "rate-limit hit (%s); pausing %ds (attempt %d/%d). "
                "Heartbeating dir=%s every %ds to keep the inactivity watchdog quiet.",
                type(exc).__name__, wait_seconds, attempt + 1, effective_max_retries + 1,
                _kaiju_log_dir, DEFAULT_HEARTBEAT_SECONDS,
            )
            _sleep_with_heartbeat(wait_seconds, _kaiju_log_dir)
            attempt += 1
