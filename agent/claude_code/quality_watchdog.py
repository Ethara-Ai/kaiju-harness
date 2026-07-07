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
    # C12: `cargo check` spawns `rustc` grandchildren. `subprocess.run(timeout=)`
    # SIGKILLs only the direct `cargo` child, leaving rustc workers alive — they
    # keep holding the target/ build lock (`.cargo-lock`) and wedge the NEXT
    # stage's cargo invocation. Put cargo in its own session/process-group and
    # kill the WHOLE group on timeout so nothing survives.
    try:
        proc = subprocess.Popen(
            ["cargo", "check", "--tests", "--all-features", "--message-format=short"],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"cargo check failed to invoke: {exc}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return -1, f"cargo check timed out after {timeout}s (process group killed)"
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, f"cargo check failed: {exc}"
    combined = (stderr or "") + (stdout or "")
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


def _proc_start_signature(pid: int) -> str | None:
    """A stable per-process identity (boot-relative start time) for PID-reuse
    detection. Returns None if it can't be read (we then skip the guard)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sig = (out.stdout or "").strip()
    return sig or None


def _kill_pid(pid: int, expected_sig: str | None = None) -> None:
    # B20: a PID is reused once the original process exits. If we captured the
    # original's start-time signature, re-verify it immediately before signalling
    # so we never SIGTERM/SIGKILL an unrelated process that inherited the number.
    if expected_sig is not None:
        current_sig = _proc_start_signature(pid)
        if current_sig is not None and current_sig != expected_sig:
            _LOG.warning(
                "refusing to kill pid %d: start-time signature changed (%r != %r) "
                "— PID was reused by another process", pid, current_sig, expected_sig,
            )
            return
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
    min_floor: int = 20,
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
    # B20: snapshot the agent's start-time signature now, while we KNOW the PID
    # refers to the real agent, so a later kill can detect PID reuse.
    expected_sig = _proc_start_signature(agent_pid)
    _LOG.info(
        "quality watchdog starting: pid=%d repo=%s interval=%ds rising=%d delta=%d",
        agent_pid, repo_dir, interval, consecutive_rising, min_delta,
    )
    # Initial wait so the agent has time to make at least one edit.
    time.sleep(interval)

    consecutive_cant_run = 0
    while samples < max_samples:
        if not _pid_alive(agent_pid):
            _LOG.info("agent pid %d gone; watchdog exiting cleanly", agent_pid)
            return 0
        n, _out = _cargo_error_count(repo_dir)
        samples += 1
        if n < 0:
            # B19: cargo check itself can't run. Going blind here is exactly the
            # "agent wrecked the build" case the watchdog should catch. After K
            # consecutive can't-run samples, treat it as a regression and kill.
            consecutive_cant_run += 1
            _LOG.warning(
                "sample %d: cargo check failed to run (%d consecutive)",
                samples, consecutive_cant_run,
            )
            if consecutive_cant_run >= max(consecutive_rising, 3):
                _LOG.error(
                    "QUALITY-REGRESSION: cargo check could not run for %d consecutive "
                    "samples — the tree is badly broken. Killing agent pid %d.",
                    consecutive_cant_run, agent_pid,
                )
                _kill_pid(agent_pid, expected_sig)
                return 1
            time.sleep(interval)
            continue
        consecutive_cant_run = 0
        history.append(n)
        _LOG.info("sample %d: %d compile errors (history=%s)", samples, n, history[-10:])

        # Look at the last `consecutive_rising + 1` samples. We need that many
        # samples before we can detect a trend. Compare each adjacent pair.
        if len(history) >= consecutive_rising + 1:
            tail = history[-(consecutive_rising + 1):]
            deltas = [tail[i + 1] - tail[i] for i in range(consecutive_rising)]
            # B18: only kill if errors are rising AND the latest count exceeds an
            # absolute floor. A legitimate large refactor (rename a core type ->
            # 40 transient errors that the agent then fixes) rises monotonically
            # for a few samples while still below the floor; killing it discards a
            # recoverable run right before it goes green. The floor means we only
            # act on a genuinely runaway error explosion.
            if all(d >= min_delta for d in deltas) and tail[-1] >= min_floor:
                _LOG.error(
                    "QUALITY-REGRESSION: error count rising %s across last %d samples "
                    "(deltas=%s, min_delta=%d, floor=%d). Killing agent pid %d.",
                    tail, consecutive_rising, deltas, min_delta, min_floor, agent_pid,
                )
                _kill_pid(agent_pid, expected_sig)
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
    p.add_argument("--min-floor", type=int, default=int(os.environ.get("KAIJU_QW_MIN_FLOOR", "20")),
                   help="Only kill on a rising trend if the latest error count >= this floor")
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
            min_floor=args.min_floor,
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
