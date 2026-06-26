#!/usr/bin/env bash
# Sourceable helper: starts the Claude Code OAuth bridge when
# --use-claude-code is set or KAIJU_USE_CLAUDE_CODE=1 in env, exports
# ANTHROPIC_API_BASE/ANTHROPIC_API_KEY into the calling shell, and registers
# an EXIT trap that stops the bridge if THIS pipeline started it.
#
# Pre-existing bridges (ANTHROPIC_API_BASE already set to localhost) are
# REUSED -- ownership stays with whoever started them.
#
# Usage from each language pipeline shell:
#     # ... after argument parsing populates MODEL_NAME and USE_CLAUDE_CODE ...
#     source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
#     claude_code_maybe_start_bridge "$MODEL_NAME"

# shellcheck disable=SC2034   # exported for the trap function below
KAIJU_CC_BRIDGE_STARTED_BY_PIPELINE="false"

_claude_code_bridge_cleanup() {
    if [[ "${KAIJU_CC_BRIDGE_STARTED_BY_PIPELINE:-false}" == "true" ]]; then
        echo "[claude-code-bridge] Stopping bridge (auto-cleanup)..."
        "${BASE_DIR}/scripts/claude_code_bridge.sh" stop >/dev/null 2>&1 || true
    fi
}

# claude_code_maybe_start_bridge <model_name>
#   - Returns 0 if bridge was started, reused, or not requested (always non-fatal).
#   - Exits the calling shell with 1 ONLY if --use-claude-code was requested AND
#     the bridge failed to start.
claude_code_maybe_start_bridge() {
    local model_name="${1:-}"
    if [[ "${USE_CLAUDE_CODE:-false}" != "true" ]] && [[ "${KAIJU_USE_CLAUDE_CODE:-0}" != "1" ]]; then
        return 0
    fi
    if [[ -z "${BASE_DIR:-}" ]]; then
        echo "[claude-code-bridge] Error: BASE_DIR is not set; helper needs it to locate scripts/" >&2
        exit 1
    fi
    if [[ "$model_name" != anthropic/* ]]; then
        echo "Warning: --use-claude-code is set but model '$model_name' is not anthropic/*."
        echo "         The bridge will run but the pipeline will route via the model's normal provider."
    fi
    if [[ -n "${ANTHROPIC_API_BASE:-}" ]] && \
       [[ "${ANTHROPIC_API_BASE}" == http://127.0.0.1:* || "${ANTHROPIC_API_BASE}" == http://localhost:* ]]; then
        echo "[claude-code-bridge] Reusing pre-existing ANTHROPIC_API_BASE=${ANTHROPIC_API_BASE}"
        return 0
    fi
    echo "[claude-code-bridge] Starting bridge..."
    local bridge_exports
    bridge_exports=$("${BASE_DIR}/scripts/claude_code_bridge.sh" start | grep '^export') || {
        echo "Error: failed to start Claude Code bridge" >&2
        exit 1
    }
    eval "$bridge_exports"
    KAIJU_CC_BRIDGE_STARTED_BY_PIPELINE="true"
    trap '_claude_code_bridge_cleanup' EXIT
}
