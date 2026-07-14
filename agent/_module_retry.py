"""Shared inline-retry pattern for language agent runners.

When a per-module TransientLLMError fires, retry the module IN PLACE up to
KAIJU_MODULE_INLINE_MAX_RETRIES times (default 3) with escalating backoff
(KAIJU_MODULE_INLINE_WAIT_SEC * attempt) before falling through to
`.needs_retry` handling. AUTO-RESUME would restart from the same failed
module anyway, so retrying inline is cheaper (no cold container restart,
no dataset reload). Cross-language parity: CPP/Java/Go/JS/TS/Rust/C.
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

T = TypeVar("T")


def run_module_with_inline_retry(
    fn: Callable[[], T],
    log_dir: Path,
    module_name: str,
    stage_label: str,
    skip_callback: Callable[[Path, str, Exception], None],
    live_path_setter: Optional[Callable[[Path], None]] = None,
) -> tuple[Optional[T], bool]:
    """Run ``fn()`` with inline retry on TransientLLMError.

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
