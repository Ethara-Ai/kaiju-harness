# shellcheck shell=bash
# Shared model-resolution and preflight helper for all 5 pipeline scripts.
#
# Sourced by run_pipeline.sh, run_pipeline_ts.sh, run_pipeline_go.sh,
# run_pipeline_rust.sh, run_pipeline_java.sh.
#
# Inputs (read from the environment, populated by each script's .env source):
#   BEDROCK_OPUS_ARN, BEDROCK_KIMI_ARN, BEDROCK_GLM5_ARN, BEDROCK_MINIMAX_ARN,
#   BEDROCK_NOVA_PREMIER_ARN, BEDROCK_NOVA2_LITE_ARN
#
# Optional:
#   PROBE_TIMEOUT (default 120)
#   VENV_PYTHON   (default "${BASE_DIR}/.venv/bin/python")
#
# Contract (unchanged from the previous per-script implementations):
#   resolve_model <arg>       -> sets MODEL_NAME, MODEL_SHORT, CACHE_PROMPTS
#   preflight_model_api       -> probes MODEL_NAME via litellm; prints PROBE_OK or exits 1

# ------------------------------------------------------------
# resolve_model <alias|raw-model-string>
# ------------------------------------------------------------
resolve_model() {
    local arg="$1"
    local arn=""

    case "$arg" in
        opus)
            arn="${BEDROCK_OPUS_ARN:-}"
            MODEL_SHORT="opus4.6"
            CACHE_PROMPTS="true"
            ;;
        opus47)
            arn="${BEDROCK_OPUS47_ARN:-}"
            MODEL_SHORT="opus4.7"
            CACHE_PROMPTS="true"
            ;;
        kimi)
            arn="${BEDROCK_KIMI_ARN:-}"
            MODEL_SHORT="kimi-k2.5"
            CACHE_PROMPTS="false"
            ;;
        glm5|glm-5)
            arn="${BEDROCK_GLM5_ARN:-}"
            MODEL_SHORT="glm-5"
            CACHE_PROMPTS="false"
            ;;
        minimax)
            arn="${BEDROCK_MINIMAX_ARN:-}"
            MODEL_SHORT="minimax-m2.5"
            CACHE_PROMPTS="false"
            ;;
        nova-premier|nova_premier)
            arn="${BEDROCK_NOVA_PREMIER_ARN:-}"
            MODEL_SHORT="nova-premier"
            CACHE_PROMPTS="false"
            ;;
        nova-lite|nova-2-lite|nova_2_lite)
            arn="${BEDROCK_NOVA2_LITE_ARN:-}"
            MODEL_SHORT="nova-2-lite"
            CACHE_PROMPTS="false"
            ;;
        gpt54)
            MODEL_NAME="openai/gpt-5.4"
            MODEL_SHORT="gpt-5.4"
            CACHE_PROMPTS="false"
            return 0
            ;;
        gpt55)
            MODEL_NAME="openai/gpt-5.5-2026-04-23"
            MODEL_SHORT="gpt-5.5"
            CACHE_PROMPTS="false"
            return 0
            ;;
        gemini|gemini31|gemini-3.1-pro)
            MODEL_NAME="vertex_ai/gemini-3.1-pro-preview"
            MODEL_SHORT="gemini-3.1-pro"
            CACHE_PROMPTS="true"
            if [[ -n "${VERTEX_AI_API_KEY:-}" ]]; then
                export GEMINI_API_KEY="${VERTEX_AI_API_KEY}"
                export GOOGLE_API_KEY="${VERTEX_AI_API_KEY}"
            fi
            return 0
            ;;
        gemini25pro|gemini-2.5-pro)
            MODEL_NAME="vertex_ai/gemini-2.5-pro"
            MODEL_SHORT="gemini-2.5-pro"
            CACHE_PROMPTS="true"
            if [[ -n "${VERTEX_AI_API_KEY:-}" ]]; then
                export GEMINI_API_KEY="${VERTEX_AI_API_KEY}"
                export GOOGLE_API_KEY="${VERTEX_AI_API_KEY}"
            fi
            return 0
            ;;
        gemini25flash|gemini-2.5-flash)
            MODEL_NAME="vertex_ai/gemini-2.5-flash"
            MODEL_SHORT="gemini-2.5-flash"
            CACHE_PROMPTS="true"
            if [[ -n "${VERTEX_AI_API_KEY:-}" ]]; then
                export GEMINI_API_KEY="${VERTEX_AI_API_KEY}"
                export GOOGLE_API_KEY="${VERTEX_AI_API_KEY}"
            fi
            return 0
            ;;
        opus47v|opus47vertex|claude-opus-4-7-vertex)
            MODEL_NAME="vertex_ai/claude-opus-4-7"
            MODEL_SHORT="claude-opus-4.7"
            CACHE_PROMPTS="true"
            return 0
            ;;
        opus48v|opus48vertex|claude-opus-4-8-vertex)
            MODEL_NAME="vertex_ai/claude-opus-4-8"
            MODEL_SHORT="claude-opus-4.8"
            CACHE_PROMPTS="true"
            return 0
            ;;
        *)
            # Pass-through: caller supplied a full model string (openai/..., bedrock/..., bedrock/converse/arn:...)
            MODEL_NAME="$arg"
            MODEL_SHORT=$(echo "$arg" | sed 's|.*/||' | tr -dc 'a-zA-Z0-9._-' | cut -c1-20)
            [[ -z "$MODEL_SHORT" ]] && MODEL_SHORT="custom"
            if [[ "$arg" == bedrock/*claude* || "$arg" == bedrock/*anthropic* || "$arg" == anthropic/*claude* || "$arg" == anthropic/* ]]; then
                CACHE_PROMPTS="true"
            else
                CACHE_PROMPTS="false"
            fi
            # Auto-prepend converse/ for raw Bedrock ARNs
            if [[ "$MODEL_NAME" == bedrock/* && "$MODEL_NAME" == *:aws:bedrock:* && "$MODEL_NAME" != bedrock/converse/* ]]; then
                MODEL_NAME="bedrock/converse/${MODEL_NAME#bedrock/}"
            fi
            return 0
            ;;
    esac

    if [[ -z "$arn" ]]; then
        local _default_model=""
        local _default_cache_prompts=""
        case "$arg" in
            opus)
                _default_model="bedrock/global.anthropic.claude-opus-4-6-v1"
                _default_cache_prompts="true"
                ;;
            opus47)
                _default_model="${BEDROCK_OPUS47_ARN}"
                _default_cache_prompts="true"
                ;;
        esac
        if [[ -n "$_default_model" ]]; then
            MODEL_NAME="$_default_model"
            CACHE_PROMPTS="$_default_cache_prompts"
            return 0
        fi
        echo "ERROR: alias '$arg' requires an inference-profile ARN in .env," >&2
        echo "       or a tokenless default for this alias." >&2
        echo "       Set the corresponding BEDROCK_*_ARN variable and retry." >&2
        echo "       See .env.example for the full list." >&2
        exit 2
    fi
    if [[ "$arn" == arn:aws:bedrock:* ]]; then
        MODEL_NAME="bedrock/converse/${arn}"
    elif [[ "$arn" == bedrock/* && "$arn" != bedrock/converse/* ]]; then
        MODEL_NAME="bedrock/converse/${arn#bedrock/}"
    else
        MODEL_NAME="$arn"
    fi
}

# ------------------------------------------------------------
# preflight_model_api  (reads MODEL_NAME, CACHE_PROMPTS; uses VENV_PYTHON, PROBE_TIMEOUT, log)
# ------------------------------------------------------------
preflight_model_api() {
    log "  Probing model API: ${MODEL_NAME} ..."

    if [[ "$MODEL_NAME" == vertex_ai/* ]]; then
        if [[ -z "${VERTEX_AI_API_KEY:-}" ]] && [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "ERROR: VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS required for ${MODEL_NAME}" >&2
            echo "       Add one to .env (see .env.example)." >&2
            exit 2
        fi
        if [[ -z "${VERTEXAI_LOCATION:-}" ]]; then
            echo "ERROR: VERTEXAI_LOCATION not set for ${MODEL_NAME}" >&2
            echo "       Regional Vertex endpoints return HTTP 404; set VERTEXAI_LOCATION=global in .env." >&2
            exit 2
        fi
    fi

    local probe_output probe_rc probe_result
    probe_output=$(mktemp)

    set +e
    timeout "${PROBE_TIMEOUT:-120}" "$VENV_PYTHON" - "$MODEL_NAME" "$CACHE_PROMPTS" >"$probe_output" 2>&1 <<'PYEOF'
import os
import sys

model_name = sys.argv[1]

os.environ.setdefault("LITELLM_LOG", "ERROR")

import litellm  # noqa: E402
litellm.drop_params = True

from aider.models import Model  # noqa: E402
from aider.llm import litellm as aider_litellm  # noqa: E402

try:
    from agent.agents import register_bedrock_arn_pricing
    if model_name.startswith("bedrock/"):
        register_bedrock_arn_pricing(model_name)
except Exception:
    # Pricing registration is best-effort; absence only affects cost reporting.
    pass

try:
    m = Model(model_name)
except Exception as e:
    print(f"PROBE_FAIL_MODEL: aider Model() init failed: {str(e)[:400]}")
    sys.exit(1)

messages = [{"role": "user", "content": "Reply with exactly: OK"}]
completion_kwargs = {
    "model": m.name,
    "messages": messages,
    "max_tokens": 64,
    "timeout": 60,
}
if m.name.startswith("vertex_ai/"):
    _vk = os.environ.get("VERTEX_AI_API_KEY", "").strip()
    if _vk:
        completion_kwargs["gemini_api_key"] = _vk
try:
    resp = aider_litellm.completion(**completion_kwargs)
    content = (resp.choices[0].message.content or "").strip() or "<empty>"
    cost = getattr(resp, "_hidden_params", {}).get("response_cost")
    cost_str = f" cost={cost:.8f}" if cost else " cost=unresolved"
    print(f"PROBE_OK: model responded: {content!r}{cost_str}")
except Exception as e:
    err = str(e)
    if "AuthenticationError" in err or "InvalidClientTokenId" in err:
        print(f"PROBE_FAIL_AUTH: {err[:500]}")
    elif "AccessDeniedException" in err or "not authorized" in err.lower():
        print(f"PROBE_FAIL_ACCESS: {err[:500]}")
    elif "ModelNotReady" in err or "not found" in err.lower() or "does not exist" in err.lower():
        print(f"PROBE_FAIL_MODEL: {err[:500]}")
    elif "RateLimitError" in err or "ThrottlingException" in err:
        print(f"PROBE_OK: model reachable (rate-limited): {err[:200]}")
    elif "cache" in err.lower():
        print(f"PROBE_FAIL_CACHE: {err[:500]}")
    else:
        print(f"PROBE_FAIL_UNKNOWN: {err[:500]}")
    sys.exit(1)
PYEOF
    probe_rc=$?
    set -e

    probe_result=$(cat "$probe_output")
    rm -f "$probe_output"

    if [[ $probe_rc -ne 0 ]]; then
        echo ""
        echo "========================================"
        echo "MODEL API PREFLIGHT FAILED"
        echo "========================================"
        echo "Model: ${MODEL_NAME}"
        echo ""
        echo "$probe_result"
        echo ""
        if [[ "$probe_result" == *PROBE_FAIL_AUTH* ]]; then
            echo "Fix: Check your API credentials."
        elif [[ "$probe_result" == *PROBE_FAIL_ACCESS* ]]; then
            echo "Fix: Your credentials lack permission for this model/ARN."
        elif [[ "$probe_result" == *PROBE_FAIL_MODEL* ]]; then
            echo "Fix: Model ID or ARN is invalid or not available in this region."
        elif [[ "$probe_result" == *PROBE_FAIL_CACHE* ]]; then
            echo "Fix: Prompt-caching error. CACHE_PROMPTS='${CACHE_PROMPTS}'."
        else
            echo "Fix: Review the error above."
        fi
        echo "========================================"
        echo ""
        exit 1
    fi

    log "  $probe_result"
}
