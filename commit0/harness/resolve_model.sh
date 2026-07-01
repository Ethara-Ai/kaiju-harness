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
        opus48cc|opus48claudecode|claude-opus-4-8-claudecode)
            MODEL_NAME="anthropic/claude-opus-4-8"
            MODEL_SHORT="claude-opus-4.8"
            CACHE_PROMPTS="true"
            return 0
            ;;
        sonnet46v|sonnet46vertex|claude-sonnet-4-6-vertex)
            MODEL_NAME="vertex_ai/claude-sonnet-4-6"
            MODEL_SHORT="claude-sonnet-4.6"
            CACHE_PROMPTS="true"
            return 0
            ;;
        *)
            # Pass-through: caller supplied a full model string (openai/..., bedrock/..., bedrock/converse/arn:...)
            MODEL_NAME="$arg"
            MODEL_SHORT=$(echo "$arg" | sed 's|.*/||' | tr -dc 'a-zA-Z0-9._-' | cut -c1-20)
            [[ -z "$MODEL_SHORT" ]] && MODEL_SHORT="custom"
            if [[ "$arg" == bedrock/*claude* || "$arg" == bedrock/*anthropic* || "$arg" == anthropic/*claude* || "$arg" == anthropic/* || "$arg" == vertex_ai/*claude* || "$arg" == vertex_ai_beta/*claude* ]]; then
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
                # D5: must match the generated config key (bedrock/converse/...);
                # the old `bedrock/global...` id had no settings/metadata entry, so
                # the run silently lost thinking/cache_control/pricing ($0 cost).
                _default_model="bedrock/converse/global.anthropic.claude-opus-4-6-v1"
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

    if [[ "$MODEL_NAME" == vertex_ai/* || "$MODEL_NAME" == vertex_ai_beta/* ]]; then
        if [[ "$MODEL_NAME" == vertex_ai/*gemini* || "$MODEL_NAME" == vertex_ai_beta/*gemini* ]]; then
            # Gemini models: VERTEX_AI_API_KEY (Gemini Studio key) or ADC are both valid
            if [[ -z "${VERTEX_AI_API_KEY:-}" ]] && [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
                echo "ERROR: VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS required for ${MODEL_NAME}" >&2
                echo "       Add one to .env (see .env.example)." >&2
                exit 2
            fi
        else
            # Non-Gemini Vertex AI models (e.g. claude-opus-4-7): litellm uses GCP ADC auth only.
            # VERTEX_AI_API_KEY is a Gemini Studio key and is NOT injected for these models.
            if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
                echo "ERROR: GOOGLE_APPLICATION_CREDENTIALS required for ${MODEL_NAME}" >&2
                echo "       VERTEX_AI_API_KEY is a Gemini Studio key and does not authenticate Vertex AI Claude models." >&2
                echo "       Set GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json in .env." >&2
                exit 2
            fi
        fi
        if [[ -z "${VERTEXAI_LOCATION:-}" ]]; then
            echo "ERROR: VERTEXAI_LOCATION not set for ${MODEL_NAME}" >&2
            echo "       Regional Vertex endpoints return HTTP 404; set VERTEXAI_LOCATION=global in .env." >&2
            exit 2
        fi
    fi

    local probe_output probe_rc probe_result
    # D1: give the probe a reliable repo root so it finds .aider.model.settings.yml
    # regardless of CWD (so it always exercises the REAL extra_params).
    export KAIJU_REPO_ROOT="${BASE_DIR:-$(pwd)}"
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

# D1: exercise the SAME params the real run will send (thinking / output_config
# / reasoning_effort / max_tokens) under STREAMING, not a trivial 64-token call.
# Both prior outages (thinking-enabled-budget 400, effort=max ValueError) passed
# a trivial probe and then failed every real call. Load the model's extra_params
# from the generated settings and merge them so the probe fails BEFORE a run.
extra_params = {}
_found_entry = False
_settings_seen = False
try:
    import yaml  # aider dependency
    for _cand in (os.path.join(os.environ.get("KAIJU_REPO_ROOT", "."), ".aider.model.settings.yml"),
                  ".aider.model.settings.yml"):
        if os.path.isfile(_cand):
            _settings_seen = True
            for _entry in (yaml.safe_load(open(_cand)) or []):
                if isinstance(_entry, dict) and _entry.get("name") == model_name:
                    _found_entry = True
                    extra_params = dict(_entry.get("extra_params") or {})
                    break
            if _found_entry:
                break
except Exception:
    extra_params = {}

# D9: an off-table model (no settings entry) runs with aider defaults — no
# thinking/effort/max_tokens/cache tuning, possibly a wrong context window. That
# silently degrades quality. Warn LOUD so it's a deliberate choice, not a
# surprise. (We still probe — defaults may be fine — but the operator is told.)
if _settings_seen and not _found_entry:
    print(f"PROBE_WARNING: model {model_name!r} has NO entry in "
          f".aider.model.settings.yml — running with aider DEFAULTS (no thinking/"
          f"effort/max_tokens/cache tuning). Add it to scripts/generate_aider_config.sh "
          f"if you want tuned params.")

completion_kwargs = {"model": m.name, "messages": messages, "timeout": 120}
completion_kwargs.update(extra_params)
# D10: this probe verifies the thinking/effort/max_tokens param set is ACCEPTED,
# but it does NOT exercise prompt caching — the probe prompt is far below the
# per-model cache minimum (~1024-4096 tokens), so `cache_control` is neither
# triggered nor billed here. Cache correctness through the bridge is therefore
# unverified by this preflight; a regression would only surface as missing cache
# discounts in the cost report, not a probe failure. (Verifying it would require
# a multi-KB probe that inflates preflight time/cost.)
# Cap output small (we only need to confirm the param set is accepted and yields
# usable content) but keep it above any thinking floor; stream like the real run.
# Keep the probe SMALL+FAST. We only need to confirm the param set is accepted
# (no 400/ValueError) and yields content — a streamed 512-token thinking probe
# could exceed PROBE_TIMEOUT and falsely fail a model that works.
completion_kwargs["max_tokens"] = min(int(extra_params.get("max_tokens", 256) or 256), 256)
completion_kwargs["stream"] = True
if m.name.startswith("vertex_ai/gemini"):
    _vk = os.environ.get("VERTEX_AI_API_KEY", "").strip()
    if _vk:
        completion_kwargs["gemini_api_key"] = _vk
try:
    _stream = aider_litellm.completion(**completion_kwargs)
    _chunks = list(_stream)
    try:
        _full = litellm.stream_chunk_builder(_chunks, messages=messages)
    except Exception:
        _full = None
    if _full is not None:
        content = (_full.choices[0].message.content or "").strip()
        finish = getattr(_full.choices[0], "finish_reason", None)
        cost = getattr(_full, "_hidden_params", {}).get("response_cost")
    else:
        content, finish, cost = "", None, None
    cost_str = f" cost={cost:.8f}" if cost else " cost=unresolved"
    if finish == "length" and not content:
        # All of the SMALL probe budget went to thinking before any visible text.
        # The param set was ACCEPTED (no 400) and the model was generating — that's
        # what we're verifying. Not a failure (the real run uses 128K, not 256).
        print(f"PROBE_OK: param set accepted; probe hit its small token cap before "
              f"visible text (finish=length).{cost_str}")
    elif not content:
        print(f"PROBE_FAIL_EMPTY: model returned NO content and did NOT hit the token "
              f"cap with the real param set (thinking/effort from settings). "
              f"finish_reason={finish}. params={ {k: extra_params[k] for k in extra_params if k != 'max_tokens'} }")
        sys.exit(1)
    else:
        print(f"PROBE_OK: model responded (streamed, real params): {content[:40]!r}{cost_str}")
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
