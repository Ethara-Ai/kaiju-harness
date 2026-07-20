#!/usr/bin/env bash
# ============================================================
# 3-Stage Go Pipeline for Commit0
# ============================================================
#
# Go-adapted version of run_pipeline.sh.
# Uses Go-specific CLI entry points, agent config, and constants.
#
# Usage:
#     bash run_pipeline_go.sh --model <preset|model_id> --dataset <name>
#
# Examples:
#     bash run_pipeline_go.sh --model opus --dataset ./conc_go_dataset.json
#     bash run_pipeline_go.sh --model nova-lite --dataset ./conc_go_dataset.json
#     bash run_pipeline_go.sh --model kimi --dataset conc_go --branch my-branch
#
# Requirements: jq, bc
# ============================================================

set -euo pipefail

# Refuse to run with xtrace on. The whitelisted `.env` loader still exports
# AWS_BEARER_TOKEN_BEDROCK / ANTHROPIC_API_KEY / OPENAI_API_KEY into every child;
# with `set -x` those (and the full agent command line) get echoed to logs.
case "$-" in
    *x*) echo "FATAL: refusing to run with 'set -x' (xtrace) — it would leak secrets from .env into logs." >&2; exit 2 ;;
esac

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

# QC-C2-009: whitelisted .env export via the shared helper, NOT the blanket
# `set -a; source .env` which exported every .env var (incl. unrelated secrets)
# into every child process. See scripts/_load_env_whitelist.sh.
# shellcheck source=scripts/_load_env_whitelist.sh
source "${BASE_DIR}/scripts/_load_env_whitelist.sh"
source "${BASE_DIR}/scripts/_outputs_layout.sh"
"${BASE_DIR}/scripts/generate_aider_config.sh"
REPO_BASE="${BASE_DIR}/repos"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
BACKEND="local"
MAX_ITERATION=3
export LANGUAGE="go"  # LOW: parity with rust driver (LANGUAGE="rust") for one env-var contract.

MODEL_ARG=""
USE_CLAUDE_CODE="false"
DATASET_ARG=""
BRANCH_OVERRIDE=""
REPO_SPLIT_OVERRIDE=""
STAGE_TIMEOUT=0
EVAL_TIMEOUT=3600
NO_STAGE3_LINT="false"
GO_CRAZY="false"
# B5 audit fix: tunable model-preflight probe timeout (default 120s). Bump to
# 300+ for slow-start bridges (multi-account pool, cold Docker network) so
# preflight doesn't abort the whole run on transient upstream slowness.
PROBE_TIMEOUT="${PROBE_TIMEOUT:-120}"
# A4/A8 audit fix: runtime toggle for spec.pdf/README injection into agent prompt.
# Previously hardcoded to true; now controllable via --use-spec-info true/false for
# ablation studies (mirrors Rust/CPP/JS/TS/Java pipelines).
USE_SPEC_INFO="true"
STRICT_INVENTORY="true"
INACTIVITY_TIMEOUT=900
MAX_WALL_TIME=86400
SKIP_TO_STAGE=""
RESUME="false"
NUM_SAMPLES=1
MAX_TEST_OUTPUT_LENGTH=15000
MAX_PARALLEL_REPOS=1
# Ablation knobs (parity with run_pipeline_c.sh). Emitted into the agent config
# YAML; default to the non-ablated production values.
BLIND_LINT="false"
BLIND_TESTS="false"
NAMES_ONLY_TESTS="false"
STRIP_NON_STUBS="false"
# Test-SOURCE files are NOT injected into the agent prompt by default (QC): reading
# the exact test bodies is answer-leakage (the model reverse-engineers expected
# values) AND on test-heavy repos it ballooned the prompt to ~315k tokens -> empty
# completions. The agent still RUNS the tests and sees SUMMARIZED results
# (max_test_output_length), and it can NEVER edit test files (GuardedInputOutput
# protected_paths is independent of this flag). Opt back in with --test-files-readonly.
INJECT_TEST_FILES_READONLY="false"

print_usage() {
    cat <<'USAGE'
Usage: run_pipeline_go.sh --model <preset|model_id> --dataset <name> [OPTIONS]

Required:
  --model    <preset|id>   Model preset or full model ID
  --dataset  <name|path>   Dataset name or path to JSON file

Model presets:
  opus     Bedrock Claude Opus 4.6
  kimi     Bedrock Kimi K2.5
  glm5     Bedrock GLM 5
  minimax  Bedrock MiniMax M2.5
  gpt54    OpenAI GPT-5.4
  gpt55    OpenAI GPT-5.5 (reasoning_effort=high)

Options:
  --branch         <name>    Override auto-generated branch name
  --repo-split     <name>    Override repo_split
  --max-iteration  <n>       Max agent iterations per stage (default: 3)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --backend        <name>    Backend: local or modal (default: local)
  --no-stage3-lint           Disable lint in Stage 3
  --no-strict-inventory      Warn (do not FATAL) when a repo's frozen test-id inventory is missing
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap (default: 86400)
  --num-samples    <n>       Number of independent samples (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages)
  -h, --help                 Show this help
  --use-claude-code        Route anthropic/* models through the local Claude Code OAuth bridge
USAGE
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       [[ $# -lt 2 ]] && { echo "Error: --model requires a value"; exit 1; }; MODEL_ARG="$2";          shift 2 ;;
        --dataset)     [[ $# -lt 2 ]] && { echo "Error: --dataset requires a value"; exit 1; }; DATASET_ARG="$2";       shift 2 ;;
        --branch)      [[ $# -lt 2 ]] && { echo "Error: --branch requires a value"; exit 1; }; BRANCH_OVERRIDE="$2";   shift 2 ;;
        --repo-split)  [[ $# -lt 2 ]] && { echo "Error: --repo-split requires a value"; exit 1; }; REPO_SPLIT_OVERRIDE="$2"; shift 2 ;;
        --max-iteration) [[ $# -lt 2 ]] && { echo "Error: --max-iteration requires a value"; exit 1; }; MAX_ITERATION="$2"; shift 2 ;;
        --stage-timeout) [[ $# -lt 2 ]] && { echo "Error: --stage-timeout requires a value"; exit 1; }; STAGE_TIMEOUT="$2"; shift 2 ;;
        --eval-timeout)  [[ $# -lt 2 ]] && { echo "Error: --eval-timeout requires a value"; exit 1; }; EVAL_TIMEOUT="$2";  shift 2 ;;
        --backend)     [[ $# -lt 2 ]] && { echo "Error: --backend requires a value"; exit 1; }; BACKEND="$2";           shift 2 ;;
        --no-stage3-lint) NO_STAGE3_LINT="true"; shift ;;
        --go-crazy) GO_CRAZY="true"; shift ;;
        --preflight-timeout) [[ $# -lt 2 ]] && { echo "Error: --preflight-timeout requires a value (seconds)"; exit 1; }; PROBE_TIMEOUT="$2"; shift 2 ;;
        --use-spec-info) [[ $# -lt 2 ]] && { echo "Error: --use-spec-info requires true|false"; exit 1; }; USE_SPEC_INFO="$2"; shift 2 ;;
        --no-strict-inventory) STRICT_INVENTORY="false"; shift ;;
        --inactivity-timeout) [[ $# -lt 2 ]] && { echo "Error: --inactivity-timeout requires a value"; exit 1; }; INACTIVITY_TIMEOUT="$2"; shift 2 ;;
        --max-wall-time) [[ $# -lt 2 ]] && { echo "Error: --max-wall-time requires a value"; exit 1; }; MAX_WALL_TIME="$2"; shift 2 ;;
        --num-samples) [[ $# -lt 2 ]] && { echo "Error: --num-samples requires a value"; exit 1; }; NUM_SAMPLES="$2"; shift 2 ;;
        --skip-to-stage) [[ $# -lt 2 ]] && { echo "Error: --skip-to-stage requires a value"; exit 1; }; SKIP_TO_STAGE="$2"; shift 2 ;;
        --max-test-output-length) [[ $# -lt 2 ]] && { echo "Error: --max-test-output-length requires a value"; exit 1; }; MAX_TEST_OUTPUT_LENGTH="$2"; shift 2 ;;
        --max-parallel-repos) [[ $# -lt 2 ]] && { echo "Error: --max-parallel-repos requires a value"; exit 1; }; MAX_PARALLEL_REPOS="$2"; shift 2 ;;
        --blind-lint) BLIND_LINT="true"; shift ;;
        --blind-tests) BLIND_TESTS="true"; shift ;;
        --names-only-tests) NAMES_ONLY_TESTS="true"; shift ;;
        --strip-non-stubs) STRIP_NON_STUBS="true"; shift ;;
        --no-test-files-readonly) INJECT_TEST_FILES_READONLY="false"; shift ;;
        --test-files-readonly) INJECT_TEST_FILES_READONLY="true"; shift ;;
        -h|--help)     print_usage ;;
        --use-claude-code) USE_CLAUDE_CODE="true"; shift ;;
        --resume)      RESUME="true"; shift ;;
        *)             echo "Error: Unknown argument '$1'"; echo ""; print_usage ;;
    esac
done

# Propagate strict-blocking toggle (--go-crazy) to all subprocesses.
export KAIJU_GO_CRAZY="$GO_CRAZY"
export PROBE_TIMEOUT

[[ -z "$MODEL_ARG" ]] && { echo "Error: --model is required"; echo ""; print_usage; }
[[ -z "$DATASET_ARG" ]] && { echo "Error: --dataset is required"; echo ""; print_usage; }

if ! [[ "$NUM_SAMPLES" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: --num-samples must be a positive integer (got: $NUM_SAMPLES)"
    exit 1
fi

# Validate the remaining numeric args up front so a non-numeric value fails with
# a clear message instead of a confusing `[[: integer expression expected` or a
# jq parse error deep inside a stage.
for _nv in \
    "--max-iteration:$MAX_ITERATION" \
    "--stage-timeout:$STAGE_TIMEOUT" \
    "--eval-timeout:$EVAL_TIMEOUT" \
    "--inactivity-timeout:$INACTIVITY_TIMEOUT" \
    "--max-wall-time:$MAX_WALL_TIME"; do
    _flag="${_nv%%:*}"; _val="${_nv#*:}"
    if ! [[ "$_val" =~ ^[0-9]+$ ]]; then
        echo "Error: ${_flag} must be a non-negative integer (got: '${_val}')"
        exit 1
    fi
done

if [[ "$NUM_SAMPLES" -gt 1 ]] && [[ -n "$SKIP_TO_STAGE" ]]; then
    echo "Error: --skip-to-stage and --num-samples > 1 cannot be used together."
    exit 1
fi

# Preserve the EXPLICIT --skip-to-stage value (usually empty). SKIP_TO_STAGE is a
# global that --resume RE-COMPUTES per sample; without resetting it to this
# baseline at the top of each sample, sample 1's computed value (e.g. "3") leaks
# into sample 2 and either aborts ("no prior results") or evaluates wrong stages.
# (Issue 10)
_SKIP_TO_STAGE_CLI="$SKIP_TO_STAGE"

# ============================================================
# Model resolution and preflight (shared across all pipelines)
# ============================================================
source "${BASE_DIR}/commit0/harness/resolve_model.sh"

resolve_model "$MODEL_ARG"

# resolve_model is expected to export CACHE_PROMPTS; guard with a default so a
# future change there can't abort the run with an unbound-variable error under
# `set -u` far from the cause.
: "${CACHE_PROMPTS:=true}"

# ============================================================
# Claude Code OAuth bridge (optional --use-claude-code)
# ============================================================
source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
claude_code_maybe_start_bridge "$MODEL_NAME"

if [[ "$MODEL_NAME" == bedrock/* ]] && [[ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]]; then
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE 2>/dev/null || true
    export AWS_SHARED_CREDENTIALS_FILE="/dev/null"
fi

# ============================================================
# Resolve Dataset (Go-specific: uses GO_SPLIT, not SPLIT)
# ============================================================

resolve_dataset() {
    local arg="$1"

    if [[ "$arg" == *.json ]] || [[ "$arg" == */* ]]; then
        if [[ ! -f "$arg" ]]; then
            if [[ -f "${BASE_DIR}/${arg}" ]]; then
                arg="${BASE_DIR}/${arg}"
            else
                echo "Error: Dataset file not found: $arg"
                exit 1
            fi
        fi
        DATASET_FILE="$arg"
        if [[ -n "$REPO_SPLIT_OVERRIDE" ]]; then
            REPO_SPLIT="$REPO_SPLIT_OVERRIDE"
        else
            local basename
            basename=$(basename "$arg" .json)
            basename="${basename%_dataset}"
            basename="${basename%_go}"
            REPO_SPLIT="$basename"
        fi
        DATASET_SHORT=$(basename "$arg" .json)
        return
    fi

    local candidate="${BASE_DIR}/${arg}_go_dataset.json"
    [[ ! -f "$candidate" ]] && candidate="${BASE_DIR}/${arg}_dataset.json"
    if [[ -f "$candidate" ]]; then
        DATASET_FILE="$candidate"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="${arg}"
        return
    fi

    local known_splits
    known_splits=$("$VENV_PYTHON" -c "
from commit0.harness.constants_go import GO_SPLIT
for k in sorted(GO_SPLIT.keys()):
    print(k)
" 2>/dev/null || true)

    if echo "$known_splits" | grep -qx "$arg"; then
        DATASET_FILE="wentingzhao/commit0_go"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="$arg"
        DATASET_SPLIT="test"
        return
    fi

    echo "Error: Cannot resolve dataset '$arg'"
    echo ""
    echo "Provide one of:"
    echo "  - A path to a .json dataset file"
    echo "  - A known name with a local <name>_go_dataset.json file"
    echo "  - A GO_SPLIT key ($(echo "$known_splits" | tr '\n' ',' | sed 's/,$//'))"
    exit 1
}

DATASET_FILE=""
REPO_SPLIT=""
DATASET_SHORT=""
DATASET_SPLIT="train"
resolve_dataset "$DATASET_ARG"

DATASET_UUID=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[0].get('id','') if d else '')" "$DATASET_FILE" 2>/dev/null || true)
DATASET_N=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$DATASET_FILE" 2>/dev/null || echo 1)
if [[ "$DATASET_N" -gt 1 ]]; then
    echo "[WARNING] dataset has $DATASET_N entries; using entries[0].id ($DATASET_UUID) as folder key. Dataset-level UUIDs deferred (§11 Q1)."
fi
if [[ -z "$DATASET_UUID" ]]; then
    DATASET_UUID="$DATASET_SHORT"
fi
export KAIJU_EXPERIMENT_UUID="$DATASET_UUID"


BASE_BRANCH_NAME="${BRANCH_OVERRIDE:-aider-go-${MODEL_SHORT}-${DATASET_SHORT}}"
if [[ -z "$BRANCH_OVERRIDE" ]] && [[ "$NO_STAGE3_LINT" == "true" ]]; then
    BASE_BRANCH_NAME="${BASE_BRANCH_NAME}-nolint-s3"
fi

BASE_RUN_ID_FLAT=$(echo "${MODEL_SHORT}_${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
DATASET_DIR_NAME=$(echo "${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
MODEL_DIR_NAME=$(echo "${MODEL_SHORT}" | tr -dc 'a-zA-Z0-9._-')
if [[ "$NO_STAGE3_LINT" == "true" ]]; then
    MODEL_DIR_NAME="${MODEL_DIR_NAME}_nolint-s3"
    BASE_RUN_ID_FLAT="${BASE_RUN_ID_FLAT}_nolint-s3"
fi

set_sample_vars() {
    local sample_idx="$1"
    if [[ "$NUM_SAMPLES" -eq 1 ]]; then
        BRANCH_NAME="${BASE_BRANCH_NAME}"
        RUN_ID="${BASE_RUN_ID_FLAT}"
    else
        BRANCH_NAME="${BASE_BRANCH_NAME}-run_${sample_idx}"
        RUN_ID="${BASE_RUN_ID_FLAT}_run_${sample_idx}"
    fi
    if is_consolidated; then
        LOG_BASE="$(runs_dir "$DATASET_UUID")/${MODEL_DIR_NAME}/agent/run_${sample_idx}"
        PIPELINE_LOG="${LOG_BASE}/pipeline_results.json"
    else
        LOG_BASE="${BASE_DIR}/logs/agent/${DATASET_DIR_NAME}/${MODEL_DIR_NAME}/run_${sample_idx}"
        PIPELINE_LOG="${BASE_DIR}/logs/pipeline_${RUN_ID}_results.json"
    fi
    COMMIT0_CONFIG="${BASE_DIR}/.commit0_${RUN_ID}.yaml"
    AGENT_CONFIG="${BASE_DIR}/.agent_${RUN_ID}.yaml"
    if is_consolidated; then
        COMMIT0_CONFIG="$(configs_dir "$DATASET_UUID")/commit0_${RUN_ID}.yaml"
        AGENT_CONFIG="$(configs_dir "$DATASET_UUID")/agent_${RUN_ID}.yaml"
    fi
}

set_sample_vars 1

mkdir -p "$LOG_BASE"
exec > >(tee -a "$LOG_BASE/pipeline.log") 2>&1

# ============================================================
# Preflight Checks
# ============================================================

preflight() {
    local errors=0

    # N16: Go preflight parity with rust driver's preflight() (checks docker CLI,
    # docker daemon, and go toolchain BEFORE we spend LLM budget on runs that
    # would fail mid-eval). In-container skips docker (eval uses local_inplace).
    if ! command -v docker &>/dev/null; then
        local _docker_app_bin="/Applications/Docker.app/Contents/Resources/bin"
        if [[ -x "$_docker_app_bin/docker" ]]; then
            export PATH="$_docker_app_bin:$PATH"
            echo "  Docker CLI not on PATH; auto-resolved to $_docker_app_bin/docker"
        fi
    fi
    local _required_cmds=(jq bc timeout go docker)
    if [[ "${KAIJU_IN_CONTAINER:-0}" == "1" ]]; then
        _required_cmds=(jq bc timeout go)
    fi
    for cmd in "${_required_cmds[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            echo "Error: Required command '$cmd' not found"
            errors=$((errors + 1))
        fi
    done
    if [[ "${KAIJU_IN_CONTAINER:-0}" != "1" ]] && command -v docker &>/dev/null; then
        if ! docker info &>/dev/null; then
            echo "Error: Docker daemon not reachable. Start Docker Desktop and retry."
            echo "       (docker info returned non-zero; socket likely missing)"
            errors=$((errors + 1))
        fi
    fi
    if command -v go &>/dev/null; then
        local go_v
        go_v=$(go version 2>/dev/null | awk '{print $3}' | sed 's/^go//')
        if [[ -z "$go_v" ]]; then
            echo "Error: go found but 'go version' probe failed"
            errors=$((errors + 1))
        else
            echo "  Toolchain: go $go_v"
            if [[ -n "${GO_VERSION:-}" ]] && [[ "$GO_VERSION" != "stable" ]]; then
                if [[ "$go_v" != "$GO_VERSION"* ]]; then
                    echo "Warning: go version '$go_v' does not match pinned GO_VERSION='$GO_VERSION'."
                fi
            fi
        fi
    fi
    if [[ "${KAIJU_IN_CONTAINER:-0}" != "1" ]]; then
        # goimports / staticcheck are only needed on the HOST for lint refine
        # (in-container the tools are baked into the image).
        for opt_tool in goimports staticcheck; do
            if ! command -v "$opt_tool" &>/dev/null; then
                echo "Warning: '$opt_tool' not found on PATH — lint refine may skip; install with 'go install ...'"
            fi
        done
    fi

    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "Error: Python venv not found at $VENV_PYTHON"
        errors=$((errors + 1))
    fi

    if [[ ! -d "$REPO_BASE" ]]; then
        echo "Error: Repo base directory not found at $REPO_BASE"
        errors=$((errors + 1))
    fi

    if [[ "$MODEL_NAME" == bedrock/* ]]; then
        if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]] && [[ -z "${AWS_BEARER_TOKEN_BEDROCK:-}" ]] && [[ -z "${AWS_PROFILE:-}" ]]; then
            echo "Warning: No AWS credentials detected"
        fi
    elif [[ "$MODEL_NAME" == openai/* ]] || [[ "$MODEL_NAME" == gpt* ]]; then
        if [[ -z "${OPENAI_API_KEY:-}" ]]; then
            echo "Error: OPENAI_API_KEY not set (required for model: $MODEL_NAME)"
            errors=$((errors + 1))
        fi
    elif [[ "$MODEL_NAME" == gemini/* ]]; then
        if [[ -z "${GOOGLE_API_KEY:-}" ]]; then
            echo "Error: GOOGLE_API_KEY not set (required for model: $MODEL_NAME)"
            errors=$((errors + 1))
        fi
    elif [[ "$MODEL_NAME" == vertex_ai/*claude* ]] || [[ "$MODEL_NAME" == vertex_ai_beta/*claude* ]]; then
        if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "Error: GOOGLE_APPLICATION_CREDENTIALS not set (required for Vertex AI Claude model: $MODEL_NAME)"
            errors=$((errors + 1))
        fi
    elif [[ "$MODEL_NAME" == vertex_ai/* ]]; then
        if [[ -z "${VERTEX_AI_API_KEY:-}" ]] && [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "Error: VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS required for model: $MODEL_NAME"
            errors=$((errors + 1))
        fi
        if [[ -z "${VERTEXAI_LOCATION:-}" ]]; then
            echo "Error: VERTEXAI_LOCATION not set for model: $MODEL_NAME (regional endpoints return HTTP 404; set VERTEXAI_LOCATION=global)"
            errors=$((errors + 1))
        fi
    fi

    if [[ "$DATASET_FILE" != wentingzhao/* ]] && [[ ! -f "$DATASET_FILE" ]]; then
        echo "Error: Dataset file not found: $DATASET_FILE"
        errors=$((errors + 1))
    fi

    if [[ "$DATASET_FILE" != wentingzhao/* ]] && [[ -f "$DATASET_FILE" ]]; then
        local repos_in_dataset
        repos_in_dataset=$(_PIPELINE_DATASET_FILE="$DATASET_FILE" "$VENV_PYTHON" -c "
import json, os
with open(os.environ['_PIPELINE_DATASET_FILE']) as f:
    data = json.load(f)
for item in data:
    repo = item['repo'].split('/')[-1]
    print(repo)
" 2>/dev/null || true)

        if [[ -n "$repos_in_dataset" ]]; then
            while IFS= read -r repo; do
                if [[ ! -d "${REPO_BASE}/${repo}" ]]; then
                    echo "Error: Repo directory not found: ${REPO_BASE}/${repo}"
                    echo "  Run: python commit0/cli_go.py setup all --dataset-name $DATASET_FILE"
                    errors=$((errors + 1))
                fi
            done <<< "$repos_in_dataset"
        fi
    fi

    if [[ "$errors" -gt 0 ]]; then
        echo ""
        echo "Preflight failed with $errors error(s). Fix the above and retry."
        exit 1
    fi

    preflight_model_api
}


# ============================================================
# Helpers
# ============================================================

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] [${RUN_ID}] $1"; }

get_mtime() {
    stat -c '%Y' "$1" 2>/dev/null \
        || stat -f '%m' "$1" 2>/dev/null \
        || "$VENV_PYTHON" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1" 2>/dev/null \
        || echo "0"
}

# B15 (parity with run_pipeline_rust.sh): an INTENTIONAL rate-limit pause is not
# a hang. agent/claude_code/recovery.py drops a `.rate_limit_paused` marker and
# re-touches it each heartbeat while it waits on the subscription cap to reset
# (run_agent_go.py passes `_kaiju_log_dir` to run_with_recovery in every stage
# loop). The watchdog treats a FRESH marker as a legit pause and suppresses the
# inactivity kill; the absolute wall-time cap (checked unconditionally earlier
# each loop) still bounds an unbounded pause. Without this, a rate-limit pause
# with no live socket + no local CPU would be misread as a hang and killed.
# Returns 0 (true) iff a `.rate_limit_paused` exists under $1 with mtime within
# $2 seconds of now.
_pause_marker_fresh() {
    local search_dir="$1"
    local fresh_within="$2"
    local now mt newest_mt=0
    now=$(date +%s)
    while IFS= read -r marker; do
        mt=$(get_mtime "$marker")
        if [[ "$mt" -gt "$newest_mt" ]]; then newest_mt="$mt"; fi
    done < <(find "$search_dir" -name ".rate_limit_paused" 2>/dev/null)
    [[ "$newest_mt" -gt 0 ]] || return 1
    local age=$(( now - newest_mt ))
    [[ "$age" -lt "$fresh_within" ]]
}

get_newest_aider_log() {
    local search_dir="$1"
    local newest=""
    local newest_mtime=0
    while IFS= read -r logfile; do
        local mt
        mt=$(get_mtime "$logfile")
        if [[ "$mt" -gt "$newest_mtime" ]]; then
            newest_mtime="$mt"
            newest="$logfile"
        fi
    done < <(find "$search_dir" -name "aider.log" 2>/dev/null)
    echo "$newest"
}

# ============================================================
# Config Writers (Go-specific)
# ============================================================

write_commit0_config() {
    local ds_value
    ds_value="$(cd "$(dirname "$DATASET_FILE")" && pwd)/$(basename "$DATASET_FILE")"

    cat > "$COMMIT0_CONFIG" <<EOF
base_dir: ${REPO_BASE}
dataset_name: ${ds_value}
dataset_split: ${DATASET_SPLIT}
repo_split: ${REPO_SPLIT}
EOF
    log "  Wrote commit0 Go config: ${COMMIT0_CONFIG}"
}

yaml_escape() {
    local val="$1"
    val="${val//\'/\'\'}"
    echo "'${val}'"
}

write_agent_config() {
    local run_tests="$1"
    local use_lint_info="$2"
    local run_entire_dir_lint="$3"
    local add_import_module_to_context="$4"
    # QC-C2-008: parameterizable per-stage (defaults to the prior hardcoded
    # false) so the emitter signature matches cpp/js/rust/ts and the value can
    # no longer silently drift between hardcoded and per-stage across languages.
    local use_unit_tests_info="${5:-false}"

    cat > "$AGENT_CONFIG" <<'YAMLEOF'
agent_name: aider
YAMLEOF
    cat >> "$AGENT_CONFIG" <<EOF
model_name: $(yaml_escape "${MODEL_NAME}")
model_short: $(yaml_escape "${MODEL_SHORT}")
use_user_prompt: false
user_prompt: 'You need to complete the implementations for all stubbed functions
  (those containing the marker string "STUB: not implemented") and pass the unit tests.

  Do not change the names or signatures of existing functions.

  IMPORTANT: You must NEVER modify, edit, or delete any test files
  (files matching *_test.go). Test files are read-only and define
  the expected behavior.'
use_topo_sort_dependencies: false
add_import_module_to_context: ${add_import_module_to_context}
use_repo_info: false
max_repo_info_length: 10000
use_unit_tests_info: ${use_unit_tests_info}
max_unit_tests_info_length: 10000
use_spec_info: ${USE_SPEC_INFO}
max_spec_info_length: 10000
spec_summary_max_tokens: 4000
use_lint_info: ${use_lint_info}
max_lint_info_length: 10000
run_entire_dir_lint: ${run_entire_dir_lint}
pre_commit_config_path: ''
run_tests: ${run_tests}
max_iteration: ${MAX_ITERATION}
record_test_for_each_commit: false
cache_prompts: ${CACHE_PROMPTS}
max_test_output_length: ${MAX_TEST_OUTPUT_LENGTH}
capture_thinking: true
trajectory_md: true
output_jsonl: true
blind_lint: ${BLIND_LINT}
blind_tests: ${BLIND_TESTS}
names_only_tests: ${NAMES_ONLY_TESTS}
inject_test_files_readonly: ${INJECT_TEST_FILES_READONLY}
strip_non_stubs: ${STRIP_NON_STUBS}
language: go
EOF
    log "  Wrote agent Go config: ${AGENT_CONFIG}"
}

# ============================================================
# Run Agent (Go-specific: uses agent/config_go.py run)
# ============================================================

AGENT_PID=""
AGENT_ELAPSED=0
AGENT_RC=0

# Signal a whole process group, falling back to the single PID. The agent is
# launched under `set -m` (monitor mode) so it leads its own process group;
# signalling the group (negative PID) reaps the go/docker/aider children it
# forked, instead of orphaning them to keep burning CPU/API budget after a kill.
_kill_tree() {
    local pid="$1" sig="${2:-TERM}"
    [[ -z "$pid" ]] && return 0
    kill "-${sig}" "-${pid}" 2>/dev/null \
        || kill "-${sig}" "${pid}" 2>/dev/null \
        || true
}

# Cumulative CPU seconds for every process in the group led by $1 (the agent +
# its go/python children). Used as a "still computing locally" liveness gate.
_pgroup_cpu_secs() {
    ps -o time= -g "$1" 2>/dev/null | awk '
        { gsub(/ /,""); n=split($0,a,":"); s=0; for(i=1;i<=n;i++) s=s*60+a[i]; t+=s }
        END { printf "%d", t+0 }'
}

# True if any process in group $1 has an ESTABLISHED outbound TCP connection —
# i.e. an LLM request is in flight. During SERVER-SIDE extended thinking the
# local process is blocked on the socket at ~0% CPU and writes no logs, so
# neither mtime nor CPU shows life; a live connection is the real signal that
# the agent is healthily waiting on the model, not hung. Best-effort: if lsof is
# unavailable we return non-zero so the caller falls back to the CPU/mtime gate.
_pgroup_has_live_conn() {
    command -v lsof >/dev/null 2>&1 || return 2
    local pids
    pids=$(pgrep -g "$1" 2>/dev/null | paste -sd, -)
    [[ -z "$pids" ]] && return 1
    lsof -nP -a -p "$pids" -iTCP -sTCP:ESTABLISHED >/dev/null 2>&1
}

# Evaluate a bc expression and emit a JSON-safe number. bc drops the leading
# zero on values < 1 (".3000", "-.08"), which jq <= 1.6 rejects via --argjson.
# Re-add it so the result is always valid JSON. Propagates bc's exit status.
bc_json() {
    local _out
    _out=$(echo "$1" | bc) || return 1
    # bc exits 0 even on a SYNTAX error (writing the diagnostic to stderr and
    # nothing / a partial value to stdout), so the caller's `|| return 1` guard
    # never fires and an empty/garbage value flows into a jq argjson binding or
    # shell arithmetic. Validate the result is a plain number before returning it.
    if ! [[ "$_out" =~ ^-?[0-9]*\.?[0-9]+$ ]]; then
        return 1
    fi
    printf '%s\n' "$_out" | sed -E 's/^(-?)\./\10./'
}


# ============================================================
# Spec Doc Provisioning (Go)
# ============================================================

ensure_spec_docs_go() {

    log "Ensuring spec docs are available for all Go repos..."

    "$VENV_PYTHON" - "$DATASET_FILE" "$REPO_BASE" "$BASE_DIR" <<'PYEOF'
import json, os, sys, shutil, subprocess, bz2
from pathlib import Path

dataset_file = sys.argv[1]
repo_base    = sys.argv[2]
base_dir     = sys.argv[3]

# Load dataset
if dataset_file.endswith(".json") or os.path.isfile(dataset_file):
    with open(dataset_file) as f:
        data = json.load(f)
    if isinstance(data, dict) and "data" in data:
        entries = data["data"]
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
else:
    entries = []

if not entries:
    print("  No dataset entries found — skipping spec provisioning.")
    sys.exit(0)

specs_dir = os.path.join(base_dir, "specs")
os.makedirs(specs_dir, exist_ok=True)

for entry in entries:
    repo = entry.get("repo", "")
    repo_name = repo.split("/")[-1]
    repo_dir = os.path.join(repo_base, repo_name)

    if not os.path.isdir(repo_dir):
        print(f"  SKIP {repo_name}: repo dir not found at {repo_dir}")
        continue

    bz2_in_repo = os.path.join(repo_dir, "spec.pdf.bz2")
    pdf_in_repo = os.path.join(repo_dir, "spec.pdf")

    if os.path.exists(bz2_in_repo) or os.path.exists(pdf_in_repo):
        print(f"  OK   {repo_name}: spec already present")
        continue

    spec_url = None
    setup = entry.get("setup", {})
    if isinstance(setup, dict):
        spec_url = setup.get("specification")
    if not spec_url:
        print(f"  SKIP {repo_name}: no specification URL in dataset entry")
        continue

    cached_bz2 = os.path.join(specs_dir, f"{repo_name}.pdf.bz2")
    cached_pdf = os.path.join(specs_dir, f"{repo_name}.pdf")

    if os.path.exists(cached_bz2):
        shutil.copy2(cached_bz2, bz2_in_repo)
        print(f"  OK   {repo_name}: copied cached spec from {cached_bz2}")
        continue
    if os.path.exists(cached_pdf):
        shutil.copy2(cached_pdf, pdf_in_repo)
        print(f"  OK   {repo_name}: copied cached spec from {cached_pdf}")
        continue

    print(f"  SCRAPE {repo_name}: {spec_url}")
    try:
        from tools.scrape_pdf import scrape_spec
        result = scrape_spec(
            base_url=spec_url,
            name=repo_name,
            output_dir=specs_dir,
            compress=True,
        )
        if result and os.path.exists(result):
            shutil.copy2(result, bz2_in_repo)
            print(f"  OK   {repo_name}: scraped and placed spec.pdf.bz2")
        else:
            print(f"  WARN {repo_name}: scrape returned no output")
    except Exception as e:
        print(f"  WARN {repo_name}: scrape failed: {e}")

PYEOF
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        log "  WARNING: Spec doc provisioning had errors (rc=$rc) — continuing anyway."
    fi
}

verify_spec_docs_go() {

    log "Verifying all Go repos have spec docs..."

    local missing=0
    local missing_repos=""

    local repo_list
    if [[ "$DATASET_FILE" != wentingzhao/* ]] && [[ -f "$DATASET_FILE" ]]; then
        repo_list=$(_PIPELINE_DATASET_FILE="$DATASET_FILE" "$VENV_PYTHON" -c "
import json, os
with open(os.environ['_PIPELINE_DATASET_FILE']) as f:
    data = json.load(f)
if isinstance(data, dict) and 'data' in data:
    data = data['data']
for item in data:
    print(item['repo'].split('/')[-1])
" 2>/dev/null || true)
    else
        # N9 injection close: mirror rust driver — pass REPO_SPLIT via env var,
        # never interpolate into the Python -c body.
        repo_list=$(_PIPELINE_REPO_SPLIT="$REPO_SPLIT" "$VENV_PYTHON" -c "
import os
from commit0.harness.constants_go import GO_SPLIT
for r in sorted(GO_SPLIT.get(os.environ['_PIPELINE_REPO_SPLIT'], [])):
    print(r)
" 2>/dev/null || true)
    fi

    if [[ -z "$repo_list" ]]; then
        log "  WARNING: Could not enumerate repos for spec verification."
        return 0
    fi

    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        local repo_dir="${REPO_BASE}/${repo}"
        if [[ ! -d "$repo_dir" ]]; then
            continue
        fi
        if [[ ! -f "${repo_dir}/spec.pdf" ]] && [[ ! -f "${repo_dir}/spec.pdf.bz2" ]]; then
            log "  MISSING spec: ${repo}"
            missing=$((missing + 1))
            missing_repos="${missing_repos}  - ${repo}\n"
        else
            log "  OK spec: ${repo}"
        fi
    done <<< "$repo_list"

    if [[ "$missing" -gt 0 ]]; then
        log ""
        log "======================================================================"
        log "FATAL: ${missing} Go repo(s) missing spec docs."
        log "  The pipeline requires spec docs for all repos. Missing repos:"
        echo -e "$missing_repos" | while IFS= read -r line; do [[ -n "$line" ]] && log "$line"; done
        log ""
        log "  Options:"
        log "    1. Place spec.pdf or spec.pdf.bz2 in each repo directory"
        log "    2. Add 'specification' URLs to the dataset JSON and re-run"
        log "======================================================================"
        return 1
    fi

    log "  All Go repos have spec docs. ✓"
}

# Frozen test-id inventory gate (mirrors verify_spec_docs_go). A missing
# inventory makes the eval SILENTLY score against ALL discovered tests — a
# wrong, non-reproducible denominator. Resolved with the SAME function the eval
# uses (kaiju.verify_inventory -> find_test_ids_file). FATAL by default;
# --no-strict-inventory (or KAIJU_REQUIRE_INVENTORY=0) downgrades to warn-only.
verify_inventory_go() {
    log "Verifying all Go repos have a frozen test-id inventory..."
    local strict_flag="--strict"
    [[ "$STRICT_INVENTORY" != "true" ]] && strict_flag="--no-strict"
    local ds_arg=()
    [[ -n "${DATASET_FILE:-}" ]] && ds_arg=(--dataset "$DATASET_FILE")
    local split_arg=()
    [[ -n "${REPO_SPLIT:-}" ]] && split_arg=(--repo-split "$REPO_SPLIT")
    "$VENV_PYTHON" -m kaiju.verify_inventory --language go \
        "${ds_arg[@]}" "${split_arg[@]}" "$strict_flag"
}

# Return code contract for watchdog_run:
#   0    = agent exited successfully
#   124  = watchdog killed agent (inactivity / hard / wall-time)
#   other= agent error (non-zero exit)
#   NOTE: wait returns 127 when PID is already reaped; treated as 0 (success)
watchdog_run() {
    local agent_pid="$1"
    local log_dir="$2"
    local inactivity_limit="$3"
    local hard_timeout="$4"
    local absolute_max="${5:-86400}"
    local start_time
    start_time=$(date +%s)
    local hard_timeout_warned="false"
    local _veto_start=0  # when the live-conn/CPU gate started suppressing the inactivity kill

    # How long a live-connection / advancing-CPU agent may run WITHOUT log
    # progress before we kill it anyway. A long extended-thinking turn writes
    # NOTHING to the log until it completes (buffered SSE stream), so log
    # inactivity alone is not a hang. The absolute wall-time cap still backstops
    # a true hang. Default 90 min; override via WATCHDOG_LIVECONN_VETO_SECS.
    local _liveconn_veto_secs="${WATCHDOG_LIVECONN_VETO_SECS:-}"
    if ! [[ "$_liveconn_veto_secs" =~ ^[0-9]+$ ]] || [[ "$_liveconn_veto_secs" -lt 1 ]]; then
        _liveconn_veto_secs=$(( inactivity_limit * 6 ))
        [[ "$_liveconn_veto_secs" -lt 5400 ]] && _liveconn_veto_secs=5400
    fi

    while kill -0 "$agent_pid" 2>/dev/null; do
        sleep 5

        local now_epoch
        now_epoch=$(date +%s)
        local latest_mtime=0

        local latest_log
        latest_log=$(get_newest_aider_log "$log_dir")
        if [[ -n "$latest_log" ]] && [[ -f "$latest_log" ]]; then
            local aider_mtime
            aider_mtime=$(get_mtime "$latest_log")
            [[ "$aider_mtime" -gt "$latest_mtime" ]] && latest_mtime="$aider_mtime"
        fi

        local agent_run_log="${log_dir}/agent_run.log"
        if [[ -f "$agent_run_log" ]]; then
            local run_log_mtime
            run_log_mtime=$(get_mtime "$agent_run_log")
            [[ "$run_log_mtime" -gt "$latest_mtime" ]] && latest_mtime="$run_log_mtime"
        fi

        local idle=0
        local agent_active="false"
        if [[ "$latest_mtime" -gt 0 ]]; then
            idle=$(( now_epoch - latest_mtime ))
            if [[ $idle -lt $inactivity_limit ]]; then
                agent_active="true"
                _veto_start=0  # real log progress — clear the alive-but-silent veto timer
            fi
        else
            agent_active="true"
            _veto_start=0
        fi

        # Absolute wall-time cap — unconditional, prevents unbounded spend.
        if [[ "$absolute_max" -gt 0 ]]; then
            local wall_elapsed=$(( now_epoch - start_time ))
            if [[ $wall_elapsed -ge $absolute_max ]]; then
                log "  WATCHDOG: Absolute wall-time cap ${absolute_max}s reached. Force-killing agent."
                _kill_tree "$agent_pid" TERM; sleep 2; _kill_tree "$agent_pid" KILL
                wait "$agent_pid" 2>/dev/null || true
                return 124
            fi
        fi

        # Hard timeout: only kill if the agent is also inactive.
        if [[ "$hard_timeout" -gt 0 ]]; then
            local elapsed=$(( now_epoch - start_time ))
            if [[ $elapsed -ge $hard_timeout ]]; then
                if [[ "$agent_active" == "true" ]]; then
                    if [[ "$hard_timeout_warned" == "false" ]]; then
                        log "  WATCHDOG: Hard timeout ${hard_timeout}s reached but agent still active."
                        hard_timeout_warned="true"
                    fi
                else
                    log "  WATCHDOG: Hard timeout ${hard_timeout}s reached and agent inactive (${idle}s). Killing."
                    _kill_tree "$agent_pid" TERM; sleep 2; _kill_tree "$agent_pid" KILL
                    wait "$agent_pid" 2>/dev/null || true
                    return 124
                fi
            fi
        fi

        # Inactivity timeout: kill if no log writes within the limit — but NOT if
        # the agent is healthily waiting on the model or still computing locally.
        if [[ "$latest_mtime" -gt 0 ]] && [[ "$agent_active" == "false" ]]; then
            # B15: an intentional rate-limit pause is not a hang. If recovery has
            # a fresh `.rate_limit_paused` marker (re-touched each heartbeat),
            # suppress the inactivity kill. The absolute wall-time cap (checked
            # above) still bounds an unbounded pause, so this can't hang forever.
            if _pause_marker_fresh "$log_dir" "$(( inactivity_limit * 2 ))"; then
                log "  WATCHDOG: log idle ${idle}s but a fresh rate-limit pause marker is present — intentionally paused, not stuck. Continuing."
                _veto_start=0
                continue
            fi
            # Log-inactivity ALONE is not "stuck". A long server-side extended-
            # thinking turn writes no logs and burns ~0 local CPU (blocked on the
            # socket). Before killing — and wasting a paid turn — require BOTH: no
            # live LLM connection AND no local CPU progress over a short window.
            local _alive="false"
            if _pgroup_has_live_conn "$agent_pid"; then
                _alive="true"
                if [[ $(( idle % 60 )) -lt 5 ]]; then
                    log "  WATCHDOG: log idle ${idle}s but a live LLM connection is open — thinking, not stuck. Continuing."
                fi
            else
                local _cpu1 _cpu2
                _cpu1=$(_pgroup_cpu_secs "$agent_pid")
                sleep 3
                _cpu2=$(_pgroup_cpu_secs "$agent_pid")
                if [[ "${_cpu2:-0}" -gt "${_cpu1:-0}" ]]; then
                    _alive="true"
                    log "  WATCHDOG: log idle ${idle}s but agent CPU advancing (${_cpu1}->${_cpu2}s) — working, not stuck. Continuing."
                fi
            fi
            if [[ "$_alive" == "true" ]]; then
                # A live connection/CPU DELAYS the inactivity kill, it must not VETO
                # it forever — bound the veto at _liveconn_veto_secs; the absolute
                # wall-time cap still backstops a genuine infinite hang.
                if [[ "$_veto_start" -eq 0 ]]; then _veto_start="$now_epoch"; fi
                local _veto_for=$(( now_epoch - _veto_start ))
                if [[ "$_veto_for" -lt "$_liveconn_veto_secs" ]]; then
                    continue
                fi
                log "  WATCHDOG: agent alive-but-silent for ${_veto_for}s (> ${_liveconn_veto_secs}s live-conn veto cap) — killing despite live signal."
            else
                _veto_start=0
            fi
            log "  WATCHDOG: No log activity for ${idle}s AND no live connection / CPU idle. Agent appears stuck."
            log "  WATCHDOG: Killing agent (PID ${agent_pid})."
            _kill_tree "$agent_pid" TERM; sleep 2; _kill_tree "$agent_pid" KILL
            wait "$agent_pid" 2>/dev/null || true
            return 124
        fi
    done

    wait "$agent_pid" 2>/dev/null
    local rc=$?
    [[ $rc -eq 127 ]] && rc=0
    return $rc
}

# AUTO-RESUME helper (shared shape across languages). Re-run any module left
# .needs_retry (a transient LLM error that persisted through the in-line recovery)
# IN-PLACE, up to K rounds with a pause, so a batch NEVER needs a manual --resume.
# KAIJU_RESUME=1 rebuilds the branch from per-module patches and the agent skips
# modules that already have a .done marker, so each round re-attempts only the
# failed ones; a module that succeeds clears its .needs_retry and gains .done
# (_mark_module_done), so the count converges to 0 unless GENUINELY persistent.
# Args: <log_dir> <agent_log> -- <base agent command...>  (command WITHOUT
# --override-previous-changes, which would reset the branch and discard progress).
# Limbo sweep: a module dir with aider.log or turns.jsonl but NO .done AND NO
# .needs_retry means the agent was killed mid-post-processing (typically by the
# inactivity watchdog after aider finished a turn but before _mark_module_done
# ran). Auto-resume detection uses `.needs_retry` files, so limbo modules would
# be silently skipped without this sweep. Convert them so auto-resume re-runs them.
_sweep_limbo_modules() {
    local _ld="$1"
    [[ -d "$_ld" ]] || return 0
    local _swept=0 _aider _moddir
    while IFS= read -r _aider; do
        _moddir=$(dirname "$_aider")
        if [[ ! -f "$_moddir/.done" && ! -f "$_moddir/.needs_retry" ]]; then
            echo "limbo (agent killed mid-postprocessing, no .done marker)" > "$_moddir/.needs_retry"
            _swept=$((_swept + 1))
        fi
    done < <(find "$_ld" -type f -name aider.log 2>/dev/null)
    if [[ "$_swept" -gt 0 ]]; then
        log "  SWEEP: converted ${_swept} limbo module(s) to .needs_retry (had aider.log but neither .done nor .needs_retry)"
    fi
}

_auto_resume_agent() {
    local _ld="$1" _alog="$2"; shift 2
    [[ "${1:-}" == "--" ]] && shift
    local _amax="${KAIJU_AUTO_RESUME_ROUNDS:-3}" _auto=0 _nr
    _sweep_limbo_modules \"$_ld\"
    _nr=$(find \"$_ld\" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
    while [[ "${_nr:-0}" -gt 0 && "$_auto" -lt "$_amax" ]]; do
        _auto=$((_auto + 1))
        log "  AUTO-RESUME ${_auto}/${_amax}: ${_nr} module(s) left .needs_retry — waiting ${KAIJU_AUTO_RESUME_PAUSE:-60}s then re-running in-place (no manual --resume)."
        sleep "${KAIJU_AUTO_RESUME_PAUSE:-60}"
        local _rs _re _pid
        _rs=$(date +%s)
        set +e
        set -m
        KAIJU_RESUME=1 "$@" >>"$_alog" 2>&1 &
        _pid=$!
        set +m
        AGENT_PID=$_pid
        watchdog_run "$_pid" "$_ld" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
        AGENT_RC=$?
        AGENT_PID=""
        set -e
        _re=$(date +%s)
        AGENT_ELAPSED=$(( AGENT_ELAPSED + (_re - _rs) ))
        _sweep_limbo_modules \"$_ld\"
    _nr=$(find \"$_ld\" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
        log "  AUTO-RESUME ${_auto}/${_amax} finished (rc=${AGENT_RC}); ${_nr} module(s) still .needs_retry."
    done
    if [[ "${_nr:-0}" -gt 0 ]]; then
        log "  WARNING: ${_nr} module(s) STILL .needs_retry after ${_amax} auto-resume round(s) — genuinely persistent (not a passing transient); run INCOMPLETE."
        # STRICT-BLOCKING: fail loudly unless --go-crazy was passed. Enforces the
        # "no proceeding past .needs_retry orphans" contract so batch scores stay
        # meaningful (a silent skip lets unimplementable modules dilute the result).
        if [[ "${GO_CRAZY:-false}" != "true" ]]; then
            log "  FATAL (strict-blocking): halting stage. Pass --go-crazy to bypass and continue anyway."
            exit 1
        fi
    elif [[ "$_auto" -gt 0 ]]; then
        log "  AUTO-RESUME succeeded: all modules completed after ${_auto} round(s); run COMPLETE (no manual --resume needed)."
    fi
}

run_agent() {
    local branch="$1"
    local override="$2"
    local log_dir="$3"

    # Base command. Resume rounds reuse this WITHOUT --override-previous-changes:
    # that flag resets the branch and would discard every module already completed.
    local cmd=(
        "$VENV_PYTHON" -m agent.config_go run "$branch"  # N15: module invocation for parity with rust driver's `-m agent.cli_rust`
        --backend "$BACKEND"
        --agent-config-file "$AGENT_CONFIG"
        --commit0-config-file "$COMMIT0_CONFIG"
        --log-dir "$log_dir"
        --max-parallel-repos "$MAX_PARALLEL_REPOS"
    )

    local first_cmd=( "${cmd[@]}" )
    if [[ "$override" == "true" ]]; then
        first_cmd+=(--override-previous-changes)
    fi

    local agent_log="${log_dir}/agent_run.log"
    log "  Running Go agent (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s)"
    log "  Command: ${first_cmd[*]}"
    log "  Output → ${agent_log}"

    local start_time
    start_time=$(date +%s)

    set +e
    # Force unbuffered Python so streamed thinking/progress reliably advances the
    # log mtime the inactivity watchdog reads. Block-buffered stdout (the default
    # when stdout is a file) can withhold writes for minutes, making a healthy
    # streaming agent look idle.
    export PYTHONUNBUFFERED=1
    # Launch under monitor mode so the agent leads its own process group; this
    # lets the watchdog/cleanup signal the whole group and reap forked
    # go/docker/aider children instead of orphaning them.
    set -m
    "${first_cmd[@]}" >>"$agent_log" 2>&1 &
    local agent_pid=$!
    set +m
    AGENT_PID=$agent_pid

    watchdog_run "$agent_pid" "$log_dir" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
    AGENT_RC=$?
    AGENT_PID=""
    set -e

    local end_time
    end_time=$(date +%s)
    AGENT_ELAPSED=$(( end_time - start_time ))

    if [[ $AGENT_RC -eq 124 ]]; then
        log "  Agent killed by watchdog after ${AGENT_ELAPSED}s"
    elif [[ $AGENT_RC -ne 0 ]]; then
        log "  Agent FAILED (rc=${AGENT_RC}) in ${AGENT_ELAPSED}s — last 20 lines:"
        tail -20 "$agent_log" 2>/dev/null | while IFS= read -r line; do log "    | $line"; done
    else
        log "  Agent finished in ${AGENT_ELAPSED}s, returncode=${AGENT_RC}"
    fi

    _auto_resume_agent "$log_dir" "$agent_log" -- "${cmd[@]}"
}

# ============================================================
# Run Evaluate (Go-specific: uses cli_go.py evaluate)
# ============================================================

EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_RUNTIME="0.0"
EVAL_ELAPSED=0
# Distinguishes a real "0 of N passed" from "eval did not run". Values:
# OK | NO_RESULTS | EVAL_FAILED | EVAL_TIMEOUT. Stages record this so a broken
# eval (timeout / missing image / hung container) never masquerades as 0%.
EVAL_STATUS="OK"

run_evaluate() {
    local branch="$1"
    local stage_label="${2:-eval}"

    local cmd=(
        "$VENV_PYTHON" commit0/cli_go.py evaluate
        --branch "$branch"
        --backend "$BACKEND"
        # Outer per-repo harness bound. Must EXCEED the inner `go test` timeout
        # (spec_go: timeout 600 + go test -timeout 600s, ~610s worst case) so the
        # inner fires first and yields clean partial output + a 124/137 exit the
        # evaluator classifies as TEST_SUITE_TIMEOUT — instead of the outer killpg
        # pre-empting it at 300s. Env-overridable; stays under EVAL_TIMEOUT (3600).
        --timeout "${KAIJU_EVAL_HARNESS_TIMEOUT:-1800}"
        --num-cpus 1
        --num-workers 1
        --commit0-config-file "$COMMIT0_CONFIG"
    )

    local eval_log="${LOG_BASE}/${stage_label}_eval.log"
    log "  Running Go evaluation: ${cmd[*]}"
    log "  Output → ${eval_log}"

    local start_time
    start_time=$(date +%s)

    set +e
    timeout "$EVAL_TIMEOUT" "${cmd[@]}" >"$eval_log" 2>&1
    local eval_rc=$?
    set -e

    local end_time
    end_time=$(date +%s)
    EVAL_ELAPSED=$(( end_time - start_time ))

    log "  Evaluation finished in ${EVAL_ELAPSED}s (rc=${eval_rc})"

    collect_eval_artifacts "${stage_label:-eval}"

    local combined_output
    combined_output=$(cat "$eval_log")
    parse_eval_output "$combined_output"   # sets EVAL_STATUS=OK|NO_RESULTS

    # Refine status from the eval process exit code. A non-zero rc with no
    # parseable results is a broken eval, NOT a 0% score — flag it so the stage
    # records EVAL_FAILED/EVAL_TIMEOUT instead of a misleading 0/N.
    if [[ "$EVAL_STATUS" != "OK" ]]; then
        if [[ $eval_rc -eq 124 ]]; then
            EVAL_STATUS="EVAL_TIMEOUT"
        elif [[ $eval_rc -ne 0 ]]; then
            EVAL_STATUS="EVAL_FAILED"
        fi
    fi

    if [[ $eval_rc -ne 0 || "$EVAL_STATUS" != "OK" ]]; then
        log "  Evaluation issue (rc=${eval_rc}, status=${EVAL_STATUS}) — last 10 lines:"
        tail -10 "$eval_log" 2>/dev/null | while IFS= read -r line; do log "    | $line"; done
    fi
}

parse_eval_output() {
    local output="$1"

    EVAL_NUM_PASSED=0
    EVAL_NUM_TESTS=0
    EVAL_PASS_RATE="0.0"
    EVAL_RUNTIME="0.0"

    local total_passed=0
    local total_tests=0
    local total_runtime="0.0"
    local found_any="false"
    local row_status=""

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        [[ "$line" == repo,* ]] && continue
        if [[ "$line" == *","*"/"* ]]; then
            local runtime passed_total passed total _st
            runtime=$(echo "$line" | cut -d',' -f2 | tr -d ' ')
            passed_total=$(echo "$line" | cut -d',' -f3 | tr -d ' ')
            # 4th column = per-repo outcome (TESTS_RAN on success; COMPILE_FAILED /
            # PATCH_APPLY_FAILED / TEST_SUITE_TIMEOUT / NO_TESTS_DEFINED /
            # OUTPUT_MISSING / GO_TEST_CRASH otherwise). Any non-TESTS_RAN status
            # means the 0/N is a build/patch/infra failure, NOT a genuine 0% model
            # score — surface it so eval_status isn't a bogus OK (mirrors C/rust).
            _st=$(echo "$line" | cut -d',' -f4 | tr -d ' ')
            if [[ -n "$_st" && "$_st" != "TESTS_RAN" ]]; then
                row_status="$_st"
            fi

            if [[ "$passed_total" == *"/"* ]]; then
                passed=$(echo "$passed_total" | cut -d'/' -f1)
                total=$(echo "$passed_total" | cut -d'/' -f2)

                if [[ "$passed" =~ ^[0-9]+$ ]] && [[ "$total" =~ ^[0-9]+$ ]]; then
                    total_passed=$((total_passed + passed))
                    total_tests=$((total_tests + total))
                    if [[ "$runtime" =~ ^[0-9]*\.?[0-9]+$ ]]; then
                        total_runtime=$(bc_json "scale=4; $total_runtime + $runtime")
                    fi
                    found_any="true"
                fi
            fi
        fi
    done <<< "$output"

    if [[ "$found_any" == "true" ]]; then
        # Non-TESTS_RAN row -> the 0/N is a build/patch/infra failure; propagate it
        # so downstream never reads it as a legit result. TESTS_RAN (or an old
        # statusless row) -> OK.
        if [[ -n "$row_status" ]]; then
            EVAL_STATUS="$row_status"
        else
            EVAL_STATUS="OK"
        fi
        EVAL_NUM_PASSED="$total_passed"
        EVAL_NUM_TESTS="$total_tests"
        EVAL_RUNTIME="$total_runtime"
        if [[ "$total_tests" -gt 0 ]]; then
            EVAL_PASS_RATE=$(bc_json "scale=6; $total_passed / $total_tests")
        fi
    else
        EVAL_STATUS="NO_RESULTS"
    fi

    if [[ "$EVAL_PASS_RATE" == "0.0" ]] || [[ "$EVAL_PASS_RATE" == "0" ]]; then
        local avg_line
        avg_line=$(echo "$output" | grep -i "average pass rate:" || true)
        if [[ -n "$avg_line" ]]; then
            local rate
            rate=$(echo "$avg_line" | awk -F':' '{print $NF}' | tr -d ' ')
            if [[ -n "$rate" ]] && [[ "$rate" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then  # strict number (reject "1.2.3"/".")
                EVAL_PASS_RATE="$rate"
            fi
        fi
    fi
}

# ============================================================
# Cost Extraction (identical to Python pipeline)
# ============================================================

# A $0.0000 result is ambiguous — it could be a genuinely free stage OR a silent
# extraction failure (no output.json, unparseable cost). The Python prints
# "<cost> <source>" where source ∈ {output_json:N, aider_fallback:N, none}.
# The caller runs this in a command substitution `$(...)` (a SUBSHELL), so any
# assignment to a global here would be lost — returning "<cost> <source>" on
# stdout lets the caller recover the real source. A "none" source with $0 is
# logged LOUD so a broken-cost run is never mistaken for a free one.
extract_all_stage_costs() {
    local log_dir="$1"
    if [[ ! -d "$log_dir" ]]; then
        echo "0.0000 missing_dir"
        return
    fi
    local err_file="${log_dir}/cost_extract.err"
    [[ -w "$log_dir" ]] || err_file="/dev/null"
    local result
    result=$("$VENV_PYTHON" - "$log_dir" <<'PYEOF' 2>>"$err_file"
import json, os, re, sys
log_dir = sys.argv[1]

# Truth source: output.json.metrics.total_cost (written by llm_cost_capture).
# Includes aider main loop + summarizer + commit_msg + cache writes.
oj_total = 0.0
oj_count = 0
for root, _d, files in os.walk(log_dir):
    if "output.json" not in files:
        continue
    fpath = os.path.join(root, "output.json")
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        c = (data.get("metrics") or {}).get("total_cost")
        if c is not None:
            oj_total += float(c)
            oj_count += 1
    except (OSError, ValueError, json.JSONDecodeError):
        pass

if oj_count > 0:
    print(f"{oj_total:.4f} output_json:{oj_count}")
    sys.exit(0)

# Fallback (no output.json present): aider.log session regex.
# Only counts aider's main edit loop; misses summarizer + commit_msg + cache.
COST_RE = re.compile(r"Cost:\s+\$\d+\.\d+\s+(?:message|request),\s+\$(\d+\.\d+)\s+session")
fallback_total = 0.0
fallback_count = 0
for root, _d, files in os.walk(log_dir):
    if "aider.log" not in files:
        continue
    fpath = os.path.join(root, "aider.log")
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            last_match = None
            for line in f:
                m = COST_RE.search(line)
                if m:
                    last_match = m
            if last_match:
                fallback_total += float(last_match.group(1))
                fallback_count += 1
    except (OSError, ValueError):
        pass
if fallback_count > 0:
    print(f"{fallback_total:.4f} aider_fallback:{fallback_count}")
else:
    # No cost source at all — distinguish this from a real free run.
    print("0.0000 none")
PYEOF
) || true
    # result is "<cost> <source>"; split it.
    local cost_part source_part
    cost_part="${result%% *}"
    source_part="${result#* }"
    if [[ "$cost_part" =~ ^[0-9]+\.[0-9]+$ ]]; then
        if [[ "${source_part:-none}" == "none" ]]; then
            # Redirect to stderr: callers capture this function's stdout via
            # $(...); a log line on stdout would contaminate the "<cost> <source>"
            # payload and break the downstream jq --argjson.
            log "  WARNING: cost extraction found NO output.json/aider.log cost in ${log_dir} — reporting \$0.0000 but this is an EXTRACTION FAILURE, not a free run." >&2
        fi
        echo "$cost_part ${source_part:-none}"
    else
        log "  WARNING: cost extraction returned unparseable result [${result}] for ${log_dir}; defaulting to \$0.0000." >&2
        echo "0.0000 parse_error"
    fi
}

format_pct() {
    local val="$1"
    # Guard empty / non-numeric input: a 0/0 eval (e.g. COMPILE_FAILED) leaves
    # the pass rate unset, which would make `bc` print
    # "(standard_in) 1: syntax error" and render a blank %.
    if ! [[ "$val" =~ ^-?[0-9]*\.?[0-9]+$ ]]; then
        val=0
    fi
    printf "%.1f%%" "$(echo "$val * 100" | bc)"
}

# Preserve per-repo eval artifacts into the run's output tree. The eval writes
# test_output.txt / exit codes / eval.sh / patch.diff / apply+revert stderr /
# reports under logs/<lang>_test(s)/<repo>/<BRANCH_NAME>/<hash>/, which is NOT
# under outputs/<uuid>/ and is lost on container teardown — leaving a
# COMPILE_FAILED undebuggable. Copied per stage so each keeps its own snapshot.
collect_eval_artifacts() {
    local stage_label="${1:-eval}"
    [[ -n "${LOG_BASE:-}" && -n "${BRANCH_NAME:-}" ]] || return 0
    local dest="${LOG_BASE}/${stage_label}_eval_artifacts"
    local bdir hdir repo out found=0
    shopt -s nullglob
    for bdir in logs/*_test*/*/"${BRANCH_NAME}" logs/pytest/*/"${BRANCH_NAME}"; do
        [[ -d "$bdir" ]] || continue
        repo=$(basename "$(dirname "$bdir")")
        for hdir in "$bdir"/*/; do
            [[ -d "$hdir" ]] || continue
            out="${dest}/${repo}"
            mkdir -p "$out"
            find "$hdir" -maxdepth 1 -type f \( \
                -name 'test_output.txt' -o -name 'test_output.json' \
                -o -name '*_exit_code.txt' -o -name 'eval.sh' \
                -o -name 'patch.diff' -o -name '*stderr.log' \
                -o -name 'report.*' -o -name 'test_results.json' \
                -o -name 'run_*_tests.log' -o -name '*.log' \
                \) -exec cp -f {} "$out/" \; 2>/dev/null || true
            found=1
        done
    done
    shopt -u nullglob
    # Also copy the pipeline-level eval run log (eval command stdout/stderr) so a
    # failed eval is debuggable from the run tree next to the collected artifacts.
    if [[ -n "${LOG_BASE:-}" && -f "${LOG_BASE}/${stage_label}_eval.log" ]]; then
        mkdir -p "$dest"
        cp -f "${LOG_BASE}/${stage_label}_eval.log" "$dest/" 2>/dev/null || true
        found=1
    fi
    [[ "$found" == "1" ]] && log "  Eval artifacts -> ${dest}" || true
    return 0
}

# ============================================================
# JSON Results
# ============================================================

RESULTS_JSON=""

init_results() {
    RESULTS_JSON=$(jq -n \
        --arg model "$MODEL_SHORT" \
        --arg model_short "$MODEL_SHORT" \
        --arg model_name "$MODEL_NAME" \
        --arg branch "$BRANCH_NAME" \
        --arg backend "$BACKEND" \
        --arg repo_split "$REPO_SPLIT" \
        --arg dataset "$DATASET_FILE" \
        --arg dataset_short "$DATASET_SHORT" \
        --argjson max_iter "$MAX_ITERATION" \
        --arg cache_prompts "$CACHE_PROMPTS" \
        --arg start_time "$(ts)" \
        --arg language "go" \
        '{
            language: $language,
            model: $model,
            model_short: $model_short,
            model_name: $model_name,
            branch: $branch,
            backend: $backend,
            repo_split: $repo_split,
            dataset: $dataset,
            dataset_short: $dataset_short,
            max_iteration: $max_iter,
            cache_prompts: $cache_prompts,
            start_time: $start_time
        }')
}

save_results() {
    # G8 audit fix: best-effort warnings aggregation. Scans LOG_BASE for grep-able
    # warning markers (F2/F4/INSTALL_VERIFICATION_FAILED/PREP_WARN:*) and merges
    # a histogram into RESULTS_JSON.warnings_by_type so operators can spot silent
    # failures in a large batch without per-repo log grep. Non-fatal: any failure
    # (missing venv, jq error, timeout) leaves RESULTS_JSON untouched.
    if [[ -n "${LOG_BASE:-}" ]] && [[ -x "${VENV_PYTHON:-python3}" ]]; then
        local _wagg
        _wagg=$("${VENV_PYTHON:-python3}" -m agent.warnings_aggregator "$LOG_BASE" 2>/dev/null || echo '')
        if [[ -n "$_wagg" ]]; then
            local _merged
            _merged=$(echo "$RESULTS_JSON" | jq --argjson w "$_wagg" \
                '. + {warnings_by_type: ($w.counts // {}), warning_scan_stats: {files_scanned: ($w.files_scanned // 0), bytes_scanned: ($w.bytes_scanned // 0)}}' 2>/dev/null || echo '')
            if [[ -n "$_merged" ]]; then
                RESULTS_JSON="$_merged"
            fi
        fi
    fi
    mkdir -p "$(dirname "$PIPELINE_LOG")"
    # Atomic write. `> "$PIPELINE_LOG"` truncates the file BEFORE jq produces
    # output, so a killed/failed jq (or invalid RESULTS_JSON) leaves a 0-byte or
    # partial results file — destroying a prior good run (esp. under --skip-to-stage
    # which overwrites in place). Write to a temp file, validate it's non-empty
    # valid JSON, then atomically rename. Keep one .bak of the previous good file.
    local _tmp="${PIPELINE_LOG}.tmp.$$"
    if echo "$RESULTS_JSON" | jq '.' > "$_tmp" 2>/dev/null && [[ -s "$_tmp" ]]; then
        [[ -f "$PIPELINE_LOG" ]] && cp -f "$PIPELINE_LOG" "${PIPELINE_LOG}.bak" 2>/dev/null || true
        mv -f "$_tmp" "$PIPELINE_LOG"
    else
        rm -f "$_tmp" 2>/dev/null || true
        log "  WARNING: save_results produced invalid/empty JSON — kept previous ${PIPELINE_LOG} intact"
        return 1
    fi
}

# ============================================================
# Pipeline Stages (Go-specific agent config)
# ============================================================

stage_1_draft() {
    log "======================================================================"
    log "STAGE 1: Draft Initial Go Implementations"
    log "======================================================================"

    write_agent_config "false" "false" "false" "false"

    local stage_log_dir="${LOG_BASE}/stage1_draft"
    mkdir -p "$stage_log_dir"

    run_agent "$BRANCH_NAME" "true" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local cost cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 1 cost extraction failed"; return 1; }
    cost="${_co%% *}"; cost_source="${_co#* }"
    log "  Stage 1 cost: \$${cost} (source: ${cost_source})"

    run_evaluate "$BRANCH_NAME" "stage1"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 1 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Draft (no feedback)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost "$cost" \
        --arg cost_source "$cost_source" \
        --argjson rc "$rc" \
        --argjson runtime "${EVAL_RUNTIME:-0.0}" \
        --argjson num_passed "$EVAL_NUM_PASSED" \
        --argjson num_tests "$EVAL_NUM_TESTS" \
        --argjson pass_rate "$EVAL_PASS_RATE" \
        --arg eval_status "$EVAL_STATUS" \
        '.stage1 = {
            name: $name,
            elapsed_s: $elapsed,
            eval_time_s: $eval_time,
            cost_usd: $cost,
            cost_source: $cost_source,
            returncode: $rc,
            runtime: $runtime,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate,
            eval_status: $eval_status
        }')

    save_results
}

stage_2_lint_refine() {
    log "======================================================================"
    log "STAGE 2: Refine with Go Static Analysis (goimports/staticcheck/govet)"
    log "======================================================================"

    write_agent_config "false" "true" "true" "false"

    local stage_log_dir="${LOG_BASE}/stage2_lint"
    mkdir -p "$stage_log_dir"

    run_agent "$BRANCH_NAME" "false" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s1_cost
    s1_cost=$(echo "$RESULTS_JSON" | jq -r '.stage1.cost_usd // 0') || { log "ERROR: Stage 2 failed to read stage1 cost"; return 1; }
    local s2_incremental cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 2 cost extraction failed"; return 1; }
    s2_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(bc_json "scale=4; $s1_cost + $s2_incremental") || { log "ERROR: Stage 2 cost calculation failed"; return 1; }

    log "  Stage 2 incremental cost: \$${s2_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    run_evaluate "$BRANCH_NAME" "stage2"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 2 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Lint refine (goimports+staticcheck+govet)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost_inc "$s2_incremental" \
        --argjson cost_cum "$total_cost" \
        --arg cost_source "$cost_source" \
        --argjson rc "$rc" \
        --argjson runtime "${EVAL_RUNTIME:-0.0}" \
        --argjson num_passed "$EVAL_NUM_PASSED" \
        --argjson num_tests "$EVAL_NUM_TESTS" \
        --argjson pass_rate "$EVAL_PASS_RATE" \
        --arg eval_status "$EVAL_STATUS" \
        '.stage2 = {
            name: $name,
            elapsed_s: $elapsed,
            eval_time_s: $eval_time,
            cost_usd_incremental: $cost_inc,
            cost_usd_cumulative: $cost_cum,
            cost_source: $cost_source,
            returncode: $rc,
            runtime: $runtime,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate,
            eval_status: $eval_status
        }')

    save_results
}

stage_3_test_refine() {
    log "======================================================================"
    log "STAGE 3: Refine with Go Test Feedback (go test -json)"
    log "======================================================================"

    local s3_lint="true"
    if [[ "$NO_STAGE3_LINT" == "true" ]]; then
        s3_lint="false"
        log "  Stage 3 lint DISABLED (--no-stage3-lint)"
    fi

    write_agent_config "true" "$s3_lint" "false" "false"

    local stage_log_dir="${LOG_BASE}/stage3_tests"
    mkdir -p "$stage_log_dir"

    run_agent "$BRANCH_NAME" "false" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s2_cumulative
    s2_cumulative=$(echo "$RESULTS_JSON" | jq -r '.stage2.cost_usd_cumulative // 0') || { log "ERROR: Stage 3 failed to read stage2 cost"; return 1; }
    local s3_incremental cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 3 cost extraction failed"; return 1; }
    s3_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(bc_json "scale=4; $s2_cumulative + $s3_incremental") || { log "ERROR: Stage 3 cost calculation failed"; return 1; }

    log "  Stage 3 incremental cost: \$${s3_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    run_evaluate "$BRANCH_NAME" "stage3"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 3 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Test refine (go test -json)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost_inc "$s3_incremental" \
        --argjson cost_cum "$total_cost" \
        --arg cost_source "$cost_source" \
        --argjson rc "$rc" \
        --argjson runtime "${EVAL_RUNTIME:-0.0}" \
        --argjson num_passed "$EVAL_NUM_PASSED" \
        --argjson num_tests "$EVAL_NUM_TESTS" \
        --argjson pass_rate "$EVAL_PASS_RATE" \
        --arg eval_status "$EVAL_STATUS" \
        '.stage3 = {
            name: $name,
            elapsed_s: $elapsed,
            eval_time_s: $eval_time,
            cost_usd_incremental: $cost_inc,
            cost_usd_cumulative: $cost_cum,
            cost_source: $cost_source,
            returncode: $rc,
            runtime: $runtime,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate,
            eval_status: $eval_status
        }')

    save_results
}

# ============================================================
# Summary Table
# ============================================================

print_summary_table() {
    log ""
    log "=========================================================================================="
    log "RESULTS SUMMARY — Go 3-Stage Pipeline"
    log "Model: ${MODEL_SHORT} (${MODEL_NAME})"
    log "Dataset: ${DATASET_SHORT} | Repo Split: ${REPO_SPLIT} | Branch: ${BRANCH_NAME}"
    log "Cache Prompts: ${CACHE_PROMPTS} | Max Iteration: ${MAX_ITERATION} | Backend: ${BACKEND}"
    log "=========================================================================================="
    log ""

    printf -v header "%-40s %12s %14s %12s %14s %10s" "Stage" "Pass Rate" "Passed/Total" "Stage Cost" "Cumul. Cost" "Time (s)"
    log "$header"
    log "--------------------------------------------------------------------------------------------------------------"

    for stage_key in stage1 stage2 stage3; do
        local name passed total pass_rate stage_cost cumul_cost elapsed

        name=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.name // \"—\"")
        [[ "$name" == "—" ]] && continue

        passed=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.num_passed // 0")
        total=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.num_tests // 0")
        pass_rate=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.pass_rate // 0")
        elapsed=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.elapsed_s // 0")
        local eval_status
        eval_status=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.eval_status // \"OK\"")

        if [[ "$stage_key" == "stage1" ]]; then
            stage_cost=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.cost_usd // 0")
            cumul_cost="$stage_cost"
        else
            stage_cost=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.cost_usd_incremental // 0")
            cumul_cost=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.cost_usd_cumulative // 0")
        fi

        local rate_str stage_cost_str cumul_cost_str passed_str elapsed_str
        # A 0% from a build/patch/infra failure is NOT a genuine score — show the
        # status in the Pass Rate column instead so it isn't misread (parity w/ rust).
        if [[ "$eval_status" == "OK" || -z "$eval_status" || "$eval_status" == "null" ]]; then
            rate_str=$(format_pct "$pass_rate")
        else
            rate_str="$eval_status"
        fi
        stage_cost_str=$(printf "\$%.2f" "$stage_cost")
        cumul_cost_str=$(printf "\$%.2f" "$cumul_cost")
        passed_str="${passed}/${total}"
        elapsed_str=$(printf "%.0f" "$elapsed")

        printf -v row "%-40s %12s %14s %12s %14s %10s" "$name" "$rate_str" "$passed_str" "$stage_cost_str" "$cumul_cost_str" "$elapsed_str"
        log "$row"
    done

    log "--------------------------------------------------------------------------------------------------------------"
    log ""
}

# ============================================================
# Cleanup
# ============================================================

PIPELINE_SUCCESS="false"

cleanup() {
    # Make cleanup idempotent/re-entrant. Reset traps immediately so a second
    # Ctrl-C (or a SIGTERM arriving during our own kill/sleep) doesn't re-enter
    # cleanup or interrupt the escalation mid-way and leave children alive.
    trap - INT TERM EXIT
    if [[ -n "${AGENT_PID:-}" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
        _kill_tree "$AGENT_PID" TERM
        sleep 2
        _kill_tree "$AGENT_PID" KILL
    fi

    if [[ "$PIPELINE_SUCCESS" == "true" ]]; then
        for _si in $(seq 1 "$NUM_SAMPLES"); do
            set_sample_vars "$_si"
            rm -f "$COMMIT0_CONFIG" "$AGENT_CONFIG" 2>/dev/null || true
        done
        log "Cleaned up per-run config files"
    else
        for _si in $(seq 1 "$NUM_SAMPLES"); do
            set_sample_vars "$_si"
            if [[ -f "$COMMIT0_CONFIG" ]] || [[ -f "$AGENT_CONFIG" ]]; then
                log "Pipeline did not complete successfully. Config files preserved for debugging:"
                [[ -f "$COMMIT0_CONFIG" ]] && log "  ${COMMIT0_CONFIG}"
                [[ -f "$AGENT_CONFIG" ]] && log "  ${AGENT_CONFIG}"
            fi
        done
    fi
}
trap cleanup EXIT
# Preserve interrupt semantics (don't mask with a bare `exit`, which returns the
# last command's status). 130=SIGINT, 143=SIGTERM. cleanup runs via EXIT.
trap 'exit 130' INT
trap 'exit 143' TERM

# ============================================================
# Main
# ============================================================

declare -a SAMPLE_RESULT_FILES=()

run_single_sample() {
    local sample_idx="$1"

    # Reset the (global) resume stage to the explicit CLI baseline so a value
    # computed for a PRIOR sample's resume can't leak into this one. (Issue 10)
    SKIP_TO_STAGE="$_SKIP_TO_STAGE_CLI"

    set_sample_vars "$sample_idx"

    # Resume: continue a prior run stopped by a subscription limit / kill, WITHOUT
    # redoing finished modules. Derive the resume stage from the prior results,
    # and flag the agent (KAIJU_RESUME) to rebuild the branch from host-persisted
    # per-module patches; finished modules' .done markers then skip them.
    if [[ "$RESUME" == "true" ]]; then
        export KAIJU_RESUME=1
        local _rs
        _rs="$("$VENV_PYTHON" -m agent.resume_state which-stage --results "$PIPELINE_LOG" 2>/dev/null || echo "")"
        if [[ "$_rs" == "2" || "$_rs" == "3" ]]; then
            SKIP_TO_STAGE="$_rs"
            log "RESUME: prior progress found -> skipping to stage ${SKIP_TO_STAGE}; finished modules will be skipped."
        elif [[ -z "$_rs" ]]; then
            log "RESUME: prior run already completed all stages (nothing to skip); modules will be restored + re-verified."
        else
            log "RESUME: re-entering stage 1; finished modules will be skipped."
        fi
    fi

    if [[ "$NUM_SAMPLES" -gt 1 ]]; then
        log ""
        log "############################################################"
        log "# RUN ${sample_idx} of ${NUM_SAMPLES}  (pass@${NUM_SAMPLES})"
        log "############################################################"
    fi

    log "======================================================================"
    log "Commit0 Go 3-Stage Pipeline"
    log "Model:        ${MODEL_NAME} (${MODEL_SHORT})"
    log "Dataset:      ${DATASET_FILE} (${DATASET_SHORT})"
    log "Repo Split:   ${REPO_SPLIT}"
    log "Branch:       ${BRANCH_NAME}"
    log "Backend:      ${BACKEND}"
    log "Cache:        ${CACHE_PROMPTS}"
    log "Max Iter:     ${MAX_ITERATION}"
    log "Num Samples:  ${NUM_SAMPLES} (run_${sample_idx})"
    log "Stage Timeout: ${STAGE_TIMEOUT}s | Eval Timeout: ${EVAL_TIMEOUT}s"
    log "Inactivity:   ${INACTIVITY_TIMEOUT}s"
    log "Wall-time cap: ${MAX_WALL_TIME}s"
    log "Logs:         ${LOG_BASE}"
    log "Results:      ${PIPELINE_LOG}"
    log "Start time:   $(ts)"
    log "======================================================================"

    if [[ "$sample_idx" -eq 1 ]]; then
        preflight
    fi

    mkdir -p "$LOG_BASE"
    write_commit0_config

    if [[ "$sample_idx" -eq 1 ]]; then
        ensure_spec_docs_go
        if ! verify_spec_docs_go; then
            return 1
        fi
        if ! verify_inventory_go; then
            return 1
        fi
    fi

    if [[ -n "$SKIP_TO_STAGE" ]]; then
        if [[ ! -f "$PIPELINE_LOG" ]]; then
            log "ERROR: Cannot skip to stage ${SKIP_TO_STAGE}: no prior results at ${PIPELINE_LOG}"
            return 1
        fi
        RESULTS_JSON=$(cat "$PIPELINE_LOG")
        local loaded_ok="true"
        if [[ "$SKIP_TO_STAGE" == "2" ]]; then
            echo "$RESULTS_JSON" | jq -e '.stage1' >/dev/null 2>&1 || loaded_ok="false"
            [[ "$loaded_ok" == "false" ]] && { log "ERROR: Prior results missing stage1 data."; return 1; }
        elif [[ "$SKIP_TO_STAGE" == "3" ]]; then
            echo "$RESULTS_JSON" | jq -e '.stage1' >/dev/null 2>&1 || loaded_ok="false"
            echo "$RESULTS_JSON" | jq -e '.stage2' >/dev/null 2>&1 || loaded_ok="false"
            [[ "$loaded_ok" == "false" ]] && { log "ERROR: Prior results missing stage1/stage2 data."; return 1; }
        fi
        log "  Loaded prior results from: ${PIPELINE_LOG}"
    else
        init_results
    fi

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --argjson sample_idx "$sample_idx" \
        --argjson num_samples "$NUM_SAMPLES" \
        '.sample_index = $sample_idx | .num_samples = $num_samples')

    local pipeline_error=""

    local skip_stage_1="false"
    local skip_stage_2="false"
    if [[ "$SKIP_TO_STAGE" == "2" ]]; then
        skip_stage_1="true"
        log "Skipping Stage 1 (--skip-to-stage 2)"
    elif [[ "$SKIP_TO_STAGE" == "3" ]]; then
        skip_stage_1="true"
        skip_stage_2="true"
        log "Skipping Stage 1 and 2 (--skip-to-stage 3)"
    fi

    if [[ "$skip_stage_1" == "false" ]]; then
        if ! stage_1_draft; then
            pipeline_error="Stage 1 failed"
            log "PIPELINE ERROR: ${pipeline_error}"
        fi
    else
        log "Stage 1: SKIPPED"
    fi

    if [[ -z "$pipeline_error" ]] && [[ "$skip_stage_2" == "false" ]]; then
        if ! stage_2_lint_refine; then
            pipeline_error="Stage 2 failed"
            log "PIPELINE ERROR: ${pipeline_error}"
        fi
    elif [[ "$skip_stage_2" == "true" ]]; then
        log "Stage 2: SKIPPED"
    fi

    if [[ -z "$pipeline_error" ]]; then
        if ! stage_3_test_refine; then
            pipeline_error="Stage 3 failed"
            log "PIPELINE ERROR: ${pipeline_error}"
        fi
    fi

    if [[ -n "$pipeline_error" ]]; then
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq --arg err "$pipeline_error" '.error = $err')
    fi

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq --arg end_ts "$(ts)" '.end_time = $end_ts')

    print_summary_table
    save_results
    log "run_${sample_idx} results saved to: ${PIPELINE_LOG}"

    SAMPLE_RESULT_FILES+=("$PIPELINE_LOG")
    if [[ -x "${BASE_DIR}/.venv/bin/python" ]]; then
        "${BASE_DIR}/.venv/bin/python" "${BASE_DIR}/scripts/commit0_to_atif_v2.py" \
            "$LOG_BASE" \
            "${BASE_DIR}/Harbor_Data/Trajectory" \
            --kaiju-mode \
            --pipeline "$PIPELINE_LOG" \
            --task-name "$DATASET_DIR_NAME" \
            && log "ATIF conversion complete for run_${sample_idx}" \
            || log "[WARN] ATIF conversion failed for run_${sample_idx}"
    fi

    # Signal sample failure to the caller. Without this the function returns the
    # status of the last command (the always-succeeding ATIF block), so a sample
    # where every stage errored still counts as "completed" -> PIPELINE_SUCCESS
    # flips true -> cleanup deletes the per-run configs needed to debug it.
    if [[ -n "$pipeline_error" ]]; then
        return 1
    fi
    return 0
}

SAMPLES_COMPLETED=0

main() {
    for sample_idx in $(seq 1 "$NUM_SAMPLES"); do
        if run_single_sample "$sample_idx"; then
            SAMPLES_COMPLETED=$((SAMPLES_COMPLETED + 1))
        else
            log "WARNING: run_${sample_idx} failed — continuing with remaining samples."
        fi
    done

    RUN_ID="${BASE_RUN_ID_FLAT}"

    if [[ "$SAMPLES_COMPLETED" -eq "$NUM_SAMPLES" ]]; then
        log "Go pipeline complete. All ${NUM_SAMPLES} sample(s) succeeded."
        PIPELINE_SUCCESS="true"
    elif [[ "$SAMPLES_COMPLETED" -gt 0 ]]; then
        log "Go pipeline complete. ${SAMPLES_COMPLETED}/${NUM_SAMPLES} sample(s) succeeded."
        PIPELINE_SUCCESS="true"
    else
        log "Go pipeline FAILED. No samples completed successfully."
    fi
}

cd "$BASE_DIR"
main
