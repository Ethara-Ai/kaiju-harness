#!/usr/bin/env bash
# QC-C2-009 / TF14: whitelisted env-var export from .env, shared by ALL 8
# language pipelines (was TS-only).
#
# The old blanket pattern (`set -a; source .env; set +a`) auto-exported EVERY
# variable in .env into every child process — so an unrelated secret in .env
# (a personal token, a DB URL, ...) leaked into `npm install` postinstall
# scripts, `tsc`/`javac`/`cargo` subprocesses, `git`, the eval harness, etc.
# This helper sources .env into shell-local scope, then exports ONLY:
#   1. an explicit allow-list of provider/auth/proxy vars children genuinely
#      need (model ARNs, API keys, GitHub/HF tokens, proxy config), and
#   2. every variable in the harness's OWN namespaces — KAIJU_*, COMMIT0_*,
#      BEDROCK_*, AIDER_*, plus EVAL_TIMEOUT — via prefix match, so each
#      language's tuning knobs (KAIJU_<LANG>_*, KAIJU_EVAL_HARNESS_TIMEOUT,
#      COMMIT0_BUILD_PLATFORMS, ...) propagate WITHOUT a per-language list that
#      would silently drift as languages are added.
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

    # (1) Explicit provider/auth/proxy allow-list (secrets children need).
    for _kaiju_env_var in \
        AWS_REGION AWS_DEFAULT_REGION AWS_SHARED_CREDENTIALS_FILE \
        AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE \
        AWS_BEARER_TOKEN_BEDROCK \
        OPENAI_API_KEY OPENAI_ORG_ID OPENAI_PROJECT_ID OPENAI_GPT55_MODEL \
        ANTHROPIC_API_KEY \
        VERTEXAI_PROJECT VERTEXAI_LOCATION \
        GOOGLE_APPLICATION_CREDENTIALS \
        VERTEX_AI_API_KEY GEMINI_API_KEY GOOGLE_API_KEY \
        GITHUB_TOKEN GH_TOKEN GH_PAT \
        AIDER_MODEL_METADATA_FILE AIDER_MODEL_SETTINGS_FILE \
        HTTP_PROXY HTTPS_PROXY NO_PROXY \
        http_proxy https_proxy no_proxy \
        HF_TOKEN HUGGINGFACE_TOKEN \
        EVAL_TIMEOUT \
        LITELLM_LOG; do
        if [[ -n "${!_kaiju_env_var+x}" ]]; then
            export "$_kaiju_env_var"
        fi
    done
    unset _kaiju_env_var

    # (2) Prefix-export the harness's own namespaces so every language's tuning
    # vars propagate to children without enumerating each one (no drift).
    for _kaiju_env_var in $(compgen -v); do
        case "$_kaiju_env_var" in
            KAIJU_*|COMMIT0_*|BEDROCK_*|AIDER_*) export "$_kaiju_env_var" ;;
        esac
    done
    unset _kaiju_env_var
fi
