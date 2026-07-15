"""Shared inline-retry pattern for language agent runners.

When a per-module TransientLLMError fires, retry the module IN PLACE up to
KAIJU_MODULE_INLINE_MAX_RETRIES times (default 3) with escalating backoff
(KAIJU_MODULE_INLINE_WAIT_SEC * attempt) before falling through to strict
outer blocking OR `.needs_retry` handling (depending on KAIJU_GO_CRAZY).

## Strict module-blocking (default)

When the inline retries exhaust, the module enters an OUTER strict-blocking
loop that BLOCKS the current module (does not skip to the next) for up to
``KAIJU_MAX_BLOCKING_ROUNDS`` (default 3) additional rounds separated by
``KAIJU_BLOCKING_WAIT_SEC`` (default 600s = 10 min) waits. This gives long-
lived transient conditions (rate-limit soaks, upstream outages, quota resets)
time to clear before the module is declared genuinely failed.

- If a round succeeds: mark module .done, move on.
- If all rounds fail: raise ``StrictBlockingFailed`` (fatal — halts stage,
  so the operator sees the failure loudly instead of silently accumulating
  ``.needs_retry`` orphans that dilute batch scores).

## `--go-crazy` escape hatch

Setting ``KAIJU_GO_CRAZY=1`` (from any pipeline's ``--go-crazy`` flag) reverts
to the legacy behavior: on inline-retry exhaustion, write ``.needs_retry`` and
CONTINUE to the next module. AUTO-RESUME will re-run the failed modules at
end-of-stage. Use this ONLY when a batch has a KNOWN-flaky module you want to
soak-skip past instead of blocking everything.

## False-positive detection

``looks_like_real_transient(err)`` performs a conservative sanity check on a
raised TransientLLMError BEFORE we treat it as "genuinely transient":

  1. The exception's `str(err)` must be non-empty and contain at least one
     substring that could plausibly come from a real API/network error (not
     from source-code text like "class InternalServerError"). The recovery
     modules (agent.claude_code.recovery + agent.openai_codex.recovery) already
     apply context-guarded regex to raise TransientLLMError only on real
     signals; this is a defense-in-depth secondary check.
  2. A too-short error message (< 20 chars) or one that lacks ANY of the
     known transient tokens is treated as SUSPICIOUS and demoted to a normal
     exception (so the module fails fast instead of blocking the pipeline).

Cross-language parity: CPP/Java/Go/JS/TS/Rust/C all import from this module.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

INLINE_MODULE_MAX_RETRIES = int(os.environ.get("KAIJU_MODULE_INLINE_MAX_RETRIES", "3"))
INLINE_MODULE_WAIT_SEC = int(os.environ.get("KAIJU_MODULE_INLINE_WAIT_SEC", "60"))

STRICT_MAX_BLOCKING_ROUNDS = int(os.environ.get("KAIJU_MAX_BLOCKING_ROUNDS", "3"))
STRICT_BLOCKING_WAIT_SEC = int(os.environ.get("KAIJU_BLOCKING_WAIT_SEC", "600"))


def _is_go_crazy() -> bool:
    """Return True if KAIJU_GO_CRAZY is set (bypass strict blocking).

    Read at call time (not import time) so a mid-pipeline env change works
    for tests and for CLI-flag-driven runtime toggles.
    """
    return os.environ.get("KAIJU_GO_CRAZY", "0").lower() in ("1", "true", "yes")


# Substrings that indicate a REAL transient failure. Deliberately narrow: only
# tokens that describe a runtime/API condition, not tokens that could match a
# code-comment or class definition. Case-insensitive substring match after
# lowercasing the exception text. Kept in sync with the raise-sites in
# agent.claude_code.recovery, agent.openai_codex.recovery, and agent.agents.
_REAL_TRANSIENT_TOKENS: tuple[str, ...] = (
    # Network / connection
    "connection reset", "connection aborted", "peer closed", "server disconnected",
    "read timeout", "write timeout", "timed out", "incomplete chunked read",
    "remote end closed", "econnreset",
    # HTTP status contexts (recovery regex has already validated prefix)
    "500 : internal server error", "500 internal server error",
    "502 bad gateway", "503 service unavailable", "504 gateway",
    # NOTE: bare "internalservererror" / "serviceunavailableerror" removed to
    # avoid matching source-code class definitions like `class InternalServerError`.
    # Real API errors typically include "internal server error" (with spaces) which
    # is covered above via "500 : internal server error" and "500 internal server error".
    # LLM API structured errors
    '"overloaded_error"', "error type: overloaded", "anthropic api overloaded",
    "rate_limit_error", "ratelimiterror", "too many requests",
    "resource_exhausted", "midstreamfallbackerror", "apiconnectionerror",
    "apitimeouterror",
    # Recovery-loop breadcrumb
    "max retries exceeded",
)


def looks_like_real_transient(err: BaseException) -> bool:
    """Defense-in-depth: verify a TransientLLMError has REAL-shaped signal text.

    Returns True if the exception message contains at least one substring from
    the vetted transient-token list. Returns False for suspicious matches (too
    short, no known token) — the caller should treat the module as a normal
    hard failure instead of blocking the pipeline waiting for a phantom
    transient condition to clear.

    Rationale: the recovery module regexes are context-guarded, but a raise-site
    upstream could still misclassify. If we're going to BLOCK the entire stage
    for 30+ minutes on a single module, we want extra confidence that the
    error is a real API/network condition and not a source-code text match.
    """
    msg = str(err) if err is not None else ""
    if len(msg) < 20:
        # A message this short can't carry both a token and the context prefix
        # the recovery regex checked. Treat as suspicious (upstream defect).
        logger.warning(
            "false-positive check: TransientLLMError message too short "
            "(%d chars) — treating as hard failure. Message: %r",
            len(msg), msg,
        )
        return False
    low = msg.lower()
    if not any(tok in low for tok in _REAL_TRANSIENT_TOKENS):
        logger.warning(
            "false-positive check: TransientLLMError message contains no known "
            "transient token — treating as hard failure. Message: %r",
            msg[:200],
        )
        return False
    return True


class StrictBlockingFailed(Exception):
    """Raised by strict-mode helpers when a module can't be recovered after
    all strict-blocking rounds. Fatal at the stage level: the runner should
    let this propagate so the pipeline sees the failure loudly instead of
    silently accumulating ``.needs_retry`` orphans.
    """

    def __init__(self, module_name: str, rounds: int, last_error: Exception) -> None:
        super().__init__(
            f"Module {module_name!r} failed after {rounds} strict-blocking rounds "
            f"(inline retries + {rounds} outer rounds). Pass --go-crazy to bypass "
            f"strict blocking and continue with other modules. Last error: {last_error}"
        )
        self.module_name = module_name
        self.rounds = rounds
        self.last_error = last_error


T = TypeVar("T")


def run_module_with_inline_retry(
    fn: Callable[[], T],
    log_dir: Path,
    module_name: str,
    stage_label: str,
    skip_callback: Callable[[Path, str, Exception], None],
    live_path_setter: Optional[Callable[[Path], None]] = None,
) -> tuple[Optional[T], bool]:
    """Legacy inline-retry helper (LOW-BLOCKING behavior).

    Returns ``(result, ok)`` where ``ok=True`` means the module succeeded and
    ``result`` is ``fn()``'s return value; ``ok=False`` means all retries were
    exhausted and ``skip_callback`` was invoked (caller should ``continue``
    its outer loop). Non-transient exceptions propagate unchanged so the
    caller's own ``except Exception`` handler still fires.
    """
    from agent.agents import TransientLLMError  # deferred (avoid circular import)

    last_tle: Optional[TransientLLMError] = None
    for attempt in range(INLINE_MODULE_MAX_RETRIES):
        try:
            return fn(), True
        except TransientLLMError as tle:
            last_tle = tle
            if attempt >= INLINE_MODULE_MAX_RETRIES - 1:
                skip_callback(log_dir, module_name, tle)
                return None, False
            wait_s = INLINE_MODULE_WAIT_SEC * (attempt + 1)
            logger.warning(
                "Module %s (%s) TransientLLMError attempt %d/%d — inline-retrying after %ds",
                module_name, stage_label, attempt + 1, INLINE_MODULE_MAX_RETRIES, wait_s,
            )
            if live_path_setter is not None:
                try:
                    live_path_setter(log_dir / "turns.jsonl")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(wait_s)
    if last_tle is not None:
        skip_callback(log_dir, module_name, last_tle)
    return None, False


def strict_block_or_skip(
    err: Exception,
    log_dir: Path,
    module_name: str,
    stage_label: str,
    fn: Callable[[], T],
    skip_callback: Callable[[Path, str, Exception], None],
    live_path_setter: Optional[Callable[[Path], None]] = None,
) -> tuple[Optional[T], bool]:
    """Called by a runner AFTER inline retries exhaust with ``err``.

    Behavior depends on ``KAIJU_GO_CRAZY``:

    - GO_CRAZY set: mirror legacy behavior — call ``skip_callback`` (writes
      .needs_retry + error.log) and return ``(None, False)``. Caller should
      ``continue`` its outer for-loop. AUTO-RESUME picks up the failure at
      end-of-stage.

    - Default (strict blocking): run outer blocking rounds up to
      ``STRICT_MAX_BLOCKING_ROUNDS``, each separated by
      ``STRICT_BLOCKING_WAIT_SEC`` seconds. Between rounds, re-attempt ``fn()``.
      On success: return ``(result, True)`` (caller should mark .done).
      On exhaustion: call skip_callback, then RAISE ``StrictBlockingFailed`` so
      the stage halts loudly instead of silently skipping.

    Applies false-positive protection: if the last error doesn't look like a
    real transient (checked via ``looks_like_real_transient``), treat as
    non-transient (skip + return False) regardless of strict mode. This
    prevents a source-code text false-positive from blocking the pipeline for
    30+ minutes.
    """
    from agent.agents import TransientLLMError  # deferred

    # Defense-in-depth false-positive check
    if not looks_like_real_transient(err):
        logger.warning(
            "Module %s (%s): TransientLLMError failed false-positive check — "
            "treating as hard failure (skip + .needs_retry, no strict block).",
            module_name, stage_label,
        )
        skip_callback(log_dir, module_name, err)
        return None, False

    if _is_go_crazy():
        logger.info(
            "Module %s (%s): --go-crazy set, skipping without strict-block.",
            module_name, stage_label,
        )
        skip_callback(log_dir, module_name, err)
        return None, False

    # Strict mode: OUTER blocking rounds
    last_err: Exception = err
    for outer_round in range(STRICT_MAX_BLOCKING_ROUNDS):
        logger.warning(
            "STRICT-BLOCK: Module %s (%s) round %d/%d — waiting %ds before re-attempt "
            "(pass --go-crazy to skip instead of blocking).",
            module_name, stage_label, outer_round + 1, STRICT_MAX_BLOCKING_ROUNDS,
            STRICT_BLOCKING_WAIT_SEC,
        )
        if live_path_setter is not None:
            try:
                live_path_setter(log_dir / "turns.jsonl")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(STRICT_BLOCKING_WAIT_SEC)
        try:
            result = fn()
            logger.info(
                "STRICT-BLOCK: Module %s (%s) RECOVERED after outer round %d/%d.",
                module_name, stage_label, outer_round + 1, STRICT_MAX_BLOCKING_ROUNDS,
            )
            return result, True
        except TransientLLMError as tle:
            last_err = tle
            if not looks_like_real_transient(tle):
                logger.warning(
                    "STRICT-BLOCK: Module %s (%s) got non-real transient during round %d — "
                    "aborting strict block.",
                    module_name, stage_label, outer_round + 1,
                )
                break
            # continue to next outer round
        except Exception:
            # Non-transient exception during outer retry → let it propagate so
            # the caller's outer handler fires. Strict block only handles
            # transient conditions.
            raise

    # All outer rounds exhausted
    skip_callback(log_dir, module_name, last_err)
    raise StrictBlockingFailed(module_name, STRICT_MAX_BLOCKING_ROUNDS, last_err)
