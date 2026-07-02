#!/usr/bin/env bash
# TF14 fix: whitelisted env-var export from .env.
#
# Previous pattern (`set -a; source .env; set +a`) auto-exported every
# variable in .env to every child process — including secrets like API
# tokens leaking to `npm install` postinstall scripts, `tsc` subprocesses,
# `git` invocations, etc. This helper sources .env into shell-local scope
# then explicitly exports ONLY the vars downstream tools genuinely need.
#
# Requires: caller has already set BASE_DIR before sourcing this file.
# Sourcing (not executing) is required so exports affect the caller shell.

if [[ -z "${BASE_DIR:-}" ]]; then
    echo "ERROR: _load_env_whitelist.sh requires BASE_DIR to be set by caller" >&2
    return 1 2>/dev/null || exit 1
fi

if [[ -f "${BASE_DIR}/.env" ]]; then
    # shellcheck disable=SC1091
    source "${BASE_DIR}/.env"

    for _kaiju_env_var in \
        AWS_REGION AWS_DEFAULT_REGION AWS_SHARED_CREDENTIALS_FILE \
        AWS_BEARER_TOKEN_BEDROCK \
        BEDROCK_OPUS_ARN BEDROCK_OPUS47_ARN BEDROCK_OPUS48_ARN \
        BEDROCK_KIMI_ARN BEDROCK_GLM5_ARN BEDROCK_MINIMAX_ARN \
        BEDROCK_NOVA_PREMIER_ARN BEDROCK_NOVA_LITE_ARN \
        OPENAI_API_KEY OPENAI_ORG_ID OPENAI_PROJECT_ID \
        ANTHROPIC_API_KEY \
        VERTEXAI_PROJECT VERTEXAI_LOCATION \
        GOOGLE_APPLICATION_CREDENTIALS \
        VERTEX_AI_API_KEY GEMINI_API_KEY GOOGLE_API_KEY \
        GITHUB_TOKEN GH_TOKEN GH_PAT \
        AIDER_MODEL_METADATA_FILE AIDER_MODEL_SETTINGS_FILE \
        KAIJU_CC_MAX_PAUSE_SEC KAIJU_CC_WRITE_BACK_KEYCHAIN \
        KAIJU_CC_BRIDGE_PORT KAIJU_CC_BRIDGE_HOST \
        KAIJU_TS_WORKER_TIMEOUT_SEC \
        KAIJU_TS_COMPILE_GATE KAIJU_TS_COMPILE_GATE_MAX_RETRIES \
        HTTP_PROXY HTTPS_PROXY NO_PROXY \
        http_proxy https_proxy no_proxy \
        HF_TOKEN HUGGINGFACE_TOKEN \
        LITELLM_LOG; do
        if [[ -n "${!_kaiju_env_var+x}" ]]; then
            export "$_kaiju_env_var"
        fi
    done
    unset _kaiju_env_var
fi
