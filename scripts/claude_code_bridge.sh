#!/usr/bin/env bash
# Start/stop/check the Claude Code OAuth bridge.
#
# Usage:
#   scripts/claude_code_bridge.sh check     # validate credentials only
#   scripts/claude_code_bridge.sh start     # start in background, write PID
#   scripts/claude_code_bridge.sh stop      # stop background process (and monitor)
#   scripts/claude_code_bridge.sh status    # report bridge + monitor state
#   scripts/claude_code_bridge.sh monitor   # start watchdog only (foreground)
#   eval "$(scripts/claude_code_bridge.sh start | grep ^export)"
#                                           # one-liner: start + export env
#
# After `start`, point your tools at the bridge:
#   export ANTHROPIC_API_BASE=http://127.0.0.1:8765
#   export ANTHROPIC_API_KEY=kaiju-cc-stub
#
# Containerized inference (agent runs inside the repo image): the container must
# reach the host bridge, so bind it to all interfaces and let the container use
# the Docker host gateway name:
#   KAIJU_CC_BRIDGE_HOST=0.0.0.0 scripts/claude_code_bridge.sh start
#   export KAIJU_CC_BRIDGE_URL=http://host.docker.internal:8765
# (agent/container/orchestrate.py reads KAIJU_CC_BRIDGE_URL; on Linux it also
# maps host.docker.internal -> host-gateway automatically.)
#
# Multi-account pool (optional, see docs/CLAUDE_CODE_BRIDGE.md):
#   export KAIJU_CC_ACCOUNT_POOL="$HOME/.cache/kaiju-harness/acc1.json:$HOME/.cache/kaiju-harness/acc2.json:default"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${KAIJU_CC_PYTHON:-${REPO_ROOT}/.venv/bin/python}"
HOST="${KAIJU_CC_BRIDGE_HOST:-127.0.0.1}"
PORT="${KAIJU_CC_BRIDGE_PORT:-8765}"
PID_FILE="${REPO_ROOT}/.claude_code_bridge.pid"
LOG_FILE="${KAIJU_CC_BRIDGE_LOG:-${REPO_ROOT}/logs/claude_code_bridge.log}"
MONITOR_PID_FILE="${REPO_ROOT}/.claude_code_bridge_monitor.pid"
MONITOR_LOG_FILE="${KAIJU_CC_BRIDGE_MONITOR_LOG:-${REPO_ROOT}/logs/claude_code_bridge_monitor.log}"
MONITOR_POLL_SECONDS="${KAIJU_CC_MONITOR_POLL:-30}"
MONITOR_FAIL_THRESHOLD="${KAIJU_CC_MONITOR_FAILS:-3}"

_start_bridge_process() {
  mkdir -p "$(dirname "$LOG_FILE")"
  # shellcheck disable=SC2024
  nohup "$PY" -m agent.claude_code --host "$HOST" --port "$PORT" \
    >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
}

_wait_for_healthz() {
  local ok=0
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 10 "http://${HOST}:${PORT}/healthz" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ok" != "1" ]]; then
    echo "[bridge] FAILED to come up; tail of $LOG_FILE:" >&2
    tail -20 "$LOG_FILE" >&2 || true
    return 1
  fi
  return 0
}

_start_monitor_process() {
  if [[ -f "$MONITOR_PID_FILE" ]] && kill -0 "$(cat "$MONITOR_PID_FILE")" 2>/dev/null; then
    return 0
  fi
  mkdir -p "$(dirname "$MONITOR_LOG_FILE")"
  # shellcheck disable=SC2024
  nohup bash "${BASH_SOURCE[0]}" monitor \
    >>"$MONITOR_LOG_FILE" 2>&1 &
  echo $! >"$MONITOR_PID_FILE"
}


action="${1:-start}"

case "$action" in
  check)
    exec "$PY" -m agent.claude_code --check
    ;;
  start)
    # Serialize concurrent `start` invocations so two callers can't both pass
    # the liveness check and race to bind the port (the loser would crash and
    # leave PID_FILE pointing at a dead process). flock is best-effort: if it's
    # unavailable we still proceed (single-user dev tool).
    _lock_fd=""
    if command -v flock >/dev/null 2>&1; then
      exec 9>"${PID_FILE}.lock"
      flock 9 || true
      _lock_fd=9
    fi
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[bridge] already running (PID $(cat "$PID_FILE"))" >&2
    else
      _start_bridge_process
    fi
    if ! _wait_for_healthz; then
      [[ -n "$_lock_fd" ]] && flock -u 9 2>/dev/null || true
      exit 1
    fi
    [[ -n "$_lock_fd" ]] && flock -u 9 2>/dev/null || true
    if [[ "${KAIJU_CC_DISABLE_MONITOR:-0}" != "1" ]]; then
      _start_monitor_process
      echo "[bridge] monitor up (PID $(cat "$MONITOR_PID_FILE"))" >&2
    fi
    echo "[bridge] up on http://${HOST}:${PORT} (PID $(cat "$PID_FILE"))" >&2
    echo "export ANTHROPIC_API_BASE=http://${HOST}:${PORT}"
    echo "export ANTHROPIC_API_KEY=kaiju-cc-stub"
    ;;
  stop)
    # Stop monitor first so it doesn't restart the bridge mid-shutdown.
    if [[ -f "$MONITOR_PID_FILE" ]]; then
      mpid=$(cat "$MONITOR_PID_FILE")
      if kill -0 "$mpid" 2>/dev/null; then
        kill "$mpid" || true
        sleep 0.3
        kill -0 "$mpid" 2>/dev/null && kill -9 "$mpid" 2>/dev/null || true
        echo "[bridge] monitor stopped PID $mpid" >&2
      fi
      rm -f "$MONITOR_PID_FILE"
    fi
    if [[ -f "$PID_FILE" ]]; then
      pid=$(cat "$PID_FILE")
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" || true
        sleep 0.5
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        echo "[bridge] stopped PID $pid" >&2
      fi
      rm -f "$PID_FILE"
    else
      echo "[bridge] no PID file at $PID_FILE" >&2
    fi
    ;;
  status)
    if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "[bridge] running (PID $(cat "$PID_FILE")) on http://${HOST}:${PORT}"
      curl -fsS --max-time 10 "http://${HOST}:${PORT}/healthz" || true
      echo
      if [[ -f "$MONITOR_PID_FILE" ]] && kill -0 "$(cat "$MONITOR_PID_FILE")" 2>/dev/null; then
        echo "[monitor] running (PID $(cat "$MONITOR_PID_FILE"))"
      else
        echo "[monitor] not running"
      fi
    else
      echo "[bridge] not running"
      exit 1
    fi
    ;;
  monitor)
    # Watchdog loop: poll /healthz, restart bridge after N consecutive failures.
    # Run in foreground when invoked directly; backgrounded by `start`.
    echo "[monitor] started PID=$$ poll=${MONITOR_POLL_SECONDS}s fail_threshold=${MONITOR_FAIL_THRESHOLD}"
    consecutive_failures=0
    while true; do
      if curl -fsS --max-time 10 "http://${HOST}:${PORT}/healthz" >/dev/null 2>&1; then
        if [[ "$consecutive_failures" -gt 0 ]]; then
          echo "[monitor] $(date '+%Y-%m-%d %H:%M:%S') bridge recovered"
        fi
        consecutive_failures=0
      else
        consecutive_failures=$((consecutive_failures + 1))
        echo "[monitor] $(date '+%Y-%m-%d %H:%M:%S') healthz failed (${consecutive_failures}/${MONITOR_FAIL_THRESHOLD})"
        if [[ "$consecutive_failures" -ge "$MONITOR_FAIL_THRESHOLD" ]]; then
          echo "[monitor] $(date '+%Y-%m-%d %H:%M:%S') restarting bridge"
          if [[ -f "$PID_FILE" ]]; then
            kill "$(cat "$PID_FILE")" 2>/dev/null || true
            sleep 0.5
            kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
            rm -f "$PID_FILE"
          fi
          _start_bridge_process
          consecutive_failures=0
          # Give the new bridge a moment to bind before next probe.
          sleep 2
        fi
      fi
      sleep "$MONITOR_POLL_SECONDS"
    done
    ;;
  *)
    echo "usage: $0 {start|stop|check|status|monitor}" >&2
    exit 2
    ;;
esac
