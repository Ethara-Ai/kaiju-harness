"""Quality-aware watchdog: kill the running agent if compile-error count is
trending upward across recent samples.

This is a complement to the pipeline's existing inactivity watchdog
(``run_pipeline_rust.sh:watchdog_run``), which only detects log silence.
The inactivity watchdog cannot distinguish:

  - "Agent is busy producing useful edits."  (good, keep going)
  - "Agent is busy producing more compile errors."  (bad, kill early)

This sidecar runs `cargo check --tests --message-format=short` periodically
on the repo working tree and tracks the number of `error[...]` diagnostics
over time. If the count is monotonically increasing across K consecutive
samples (default 3), we kill the agent PID. The pipeline's existing
``watchdog_run`` catches that kill via exit code 124 and proceeds.

Invocation (from run_pipeline_rust.sh):

    .venv/bin/python -m agent.claude_code.quality_watchdog \
        --agent-pid 12345 \
        --repo-dir /path/to/repo \
        --interval 90 \
        --consecutive-rising 3 \
        --min-delta 5 \
        --log /path/to/quality_watchdog.log

Exit codes:
    0  agent finished naturally (PID disappeared)
    1  killed agent due to rising error count
    2  invalid arguments / runtime failure
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_LOG = logging.getLogger("agent.claude_code.quality_watchdog")


def _cargo_error_count(repo_dir: str, timeout: int = 120) -> tuple[int, str]:
    """Return (n_errors, raw_output). n_errors=-1 on subprocess/IO failure."""
    try:
        r = subprocess.run(
            ["cargo", "check", "--tests", "--all-features", "--message-format=short"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, f"cargo check timed out after {timeout}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"cargo check failed to invoke: {exc}"
    combined = (r.stderr or "") + (r.stdout or "")
    # cargo --message-format=short emits `file:line:col: error[Exxxx]:` for
    # every diagnostic. Match that prefix and the more general `error[`.
    n = 0
    for line in combined.splitlines():
        s = line.lstrip()
        if (": error[" in s) or s.startswith("error[") or s.startswith("error:"):
            n += 1
    return n, combined


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return True
    return True


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def watch(
    agent_pid: int,
    repo_dir: str,
    *,
    interval: int = 90,
    consecutive_rising: int = 3,
    min_delta: int = 5,
    max_samples: int = 200,
) -> int:
    """Watch agent_pid, sampling compile-error count every `interval` seconds.

    Kill the agent if we see ``consecutive_rising`` consecutive samples where
    each sample's error count exceeds the previous by at least ``min_delta``.
    Stop watching when the agent exits naturally.

    Returns exit code: 0 = natural exit, 1 = killed by us.
    """
    if not Path(repo_dir).is_dir():
        _LOG.error("repo_dir not found: %s", repo_dir)
        return 2

    history: list[int] = []
    samples = 0
    _LOG.info(
        "quality watchdog starting: pid=%d repo=%s interval=%ds rising=%d delta=%d",
        agent_pid, repo_dir, interval, consecutive_rising, min_delta,
    )
    # Initial wait so the agent has time to make at least one edit.
    time.sleep(interval)

    while samples < max_samples:
        if not _pid_alive(agent_pid):
            _LOG.info("agent pid %d gone; watchdog exiting cleanly", agent_pid)
            return 0
        n, _out = _cargo_error_count(repo_dir)
        samples += 1
        if n < 0:
            _LOG.warning("sample %d: cargo check failed to run", samples)
            time.sleep(interval)
            continue
        history.append(n)
        _LOG.info("sample %d: %d compile errors (history=%s)", samples, n, history[-10:])

        # Look at the last `consecutive_rising + 1` samples. We need that many
        # samples before we can detect a trend. Compare each adjacent pair.
        if len(history) >= consecutive_rising + 1:
            tail = history[-(consecutive_rising + 1):]
            deltas = [tail[i + 1] - tail[i] for i in range(consecutive_rising)]
            if all(d >= min_delta for d in deltas):
                _LOG.error(
                    "QUALITY-REGRESSION: error count rising %s across last %d samples "
                    "(deltas=%s, min_delta=%d). Killing agent pid %d.",
                    tail, consecutive_rising, deltas, min_delta, agent_pid,
                )
                _kill_pid(agent_pid)
                return 1
        time.sleep(interval)

    _LOG.warning("watchdog hit max_samples=%d; exiting without action", max_samples)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--agent-pid", type=int, required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--interval", type=int, default=int(os.environ.get("KAIJU_QW_INTERVAL", "90")))
    p.add_argument("--consecutive-rising", type=int, default=int(os.environ.get("KAIJU_QW_RISING", "3")))
    p.add_argument("--min-delta", type=int, default=int(os.environ.get("KAIJU_QW_MIN_DELTA", "5")))
    p.add_argument("--max-samples", type=int, default=200)
    p.add_argument("--log", default=None, help="Optional log file path")
    args = p.parse_args(argv)

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=args.log, level=logging.INFO, format=fmt)
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)

    try:
        return watch(
            args.agent_pid,
            args.repo_dir,
            interval=args.interval,
            consecutive_rising=args.consecutive_rising,
            min_delta=args.min_delta,
            max_samples=args.max_samples,
        )
    except KeyboardInterrupt:
        _LOG.info("watchdog interrupted")
        return 0
    except Exception:  # noqa: BLE001
        _LOG.exception("watchdog crashed")
        return 2


if __name__ == "__main__":
    sys.exit(main())
