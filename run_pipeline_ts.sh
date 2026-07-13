#!/bin/bash
# ============================================================
# 3-Stage SDE-I Trajectory Pipeline for Commit0 — TypeScript
# ============================================================
#
# Usage:
#     bash run_pipeline_ts.sh --model <preset|model_id> --dataset <name>
#
# Examples:
#     bash run_pipeline_ts.sh --model opus --dataset my_ts_lib
#     bash run_pipeline_ts.sh --model kimi --dataset ts_custom_dataset.json
#     bash run_pipeline_ts.sh --model opus --dataset my_ts_lib --skip-to-stage 3
#
# Requirements: jq, bc
# ============================================================

set -euo pipefail
set -m

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=scripts/_load_env_whitelist.sh
source "${BASE_DIR}/scripts/_load_env_whitelist.sh"
# shellcheck source=scripts/_outputs_layout.sh
source "${BASE_DIR}/scripts/_outputs_layout.sh"
"${BASE_DIR}/scripts/generate_aider_config.sh"
REPO_BASE_TS="${BASE_DIR}/repos_ts"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
BACKEND="local"
MAX_ITERATION=3

# ============================================================
# Argument Parsing
# ============================================================

MODEL_ARG=""
USE_CLAUDE_CODE="false"
DATASET_ARG=""
BRANCH_OVERRIDE=""
REPO_SPLIT_OVERRIDE=""
STAGE_TIMEOUT=0
EVAL_TIMEOUT=3600
NO_STAGE3_LINT="false"
USE_SPEC_INFO="true"
STRICT_INVENTORY="true"
INACTIVITY_TIMEOUT=900
MAX_WALL_TIME=86400
SKIP_TO_STAGE=""
RESUME="false"
NUM_SAMPLES=1
MAX_TEST_OUTPUT_LENGTH=15000
MAX_PARALLEL_REPOS=1
BLIND_LINT="false"
BLIND_TESTS="false"
NAMES_ONLY_TESTS="false"
INJECT_TEST_FILES_READONLY="true"
STRIP_NON_STUBS="false"

print_usage() {
    cat <<'USAGE'
Usage: run_pipeline_ts.sh --model <preset|model_id> --dataset <name> [OPTIONS]

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

Dataset examples:
  my_ts_lib                  Uses my_ts_lib_ts_dataset.json or my_ts_lib_dataset.json
  ./custom_ts_dataset.json   Uses custom JSON, requires --repo-split

Options:
  --branch         <name>    Override auto-generated branch name
  --repo-split     <name>    Override repo_split (required for custom dataset paths)
  --max-iteration  <n>       Max agent iterations per stage (default: 3)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled)
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap in seconds (default: 86400)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --backend        <name>    Backend: local or modal (default: local)
  --no-stage3-lint           Disable lint in Stage 3
  --no-spec-info             Disable spec doc provisioning
  --no-strict-inventory      Warn (do not FATAL) when a repo's frozen test-id inventory is missing
  --num-samples    <n>       Number of independent samples to run (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages)
  --blind-lint               Stage 2 sees only "lint failed: N issues" (default: full output)
  --blind-tests              Stage 3 sees only summary line, no per-test failures (default: full output)
  --names-only-tests         Stage 3 sees only failed test node IDs + count (default: full output)
  --no-test-files-readonly   Remove test source files from read-only agent context (default: injected)
  -h, --help                 Show this help
  --use-claude-code        Route anthropic/* models through the local Claude Code OAuth bridge
USAGE
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       [[ $# -lt 2 ]] && { echo "Error: --model requires a value"; exit 1; }; MODEL_ARG="$2";          shift 2 ;;
        --dataset)     [[ $# -lt 2 ]] && { echo "Error: --dataset requires a value"; exit 1; }; DATASET_ARG="$2";         shift 2 ;;
        --branch)      [[ $# -lt 2 ]] && { echo "Error: --branch requires a value"; exit 1; }; BRANCH_OVERRIDE="$2";     shift 2 ;;
        --repo-split)  [[ $# -lt 2 ]] && { echo "Error: --repo-split requires a value"; exit 1; }; REPO_SPLIT_OVERRIDE="$2"; shift 2 ;;
        --max-iteration) [[ $# -lt 2 ]] && { echo "Error: --max-iteration requires a value"; exit 1; }; MAX_ITERATION="$2";     shift 2 ;;
        --stage-timeout) [[ $# -lt 2 ]] && { echo "Error: --stage-timeout requires a value"; exit 1; }; STAGE_TIMEOUT="$2";     shift 2 ;;
        --eval-timeout)  [[ $# -lt 2 ]] && { echo "Error: --eval-timeout requires a value"; exit 1; }; EVAL_TIMEOUT="$2";      shift 2 ;;
        --backend)     [[ $# -lt 2 ]] && { echo "Error: --backend requires a value"; exit 1; }; BACKEND="$2";             shift 2 ;;
        --no-stage3-lint) NO_STAGE3_LINT="true"; shift ;;
        --no-spec-info) USE_SPEC_INFO="false"; shift ;;
        --no-strict-inventory) STRICT_INVENTORY="false"; shift ;;
        --inactivity-timeout) [[ $# -lt 2 ]] && { echo "Error: --inactivity-timeout requires a value"; exit 1; }; INACTIVITY_TIMEOUT="$2"; shift 2 ;;
        --max-wall-time) [[ $# -lt 2 ]] && { echo "Error: --max-wall-time requires a value"; exit 1; }; MAX_WALL_TIME="$2"; shift 2 ;;
        --num-samples) [[ $# -lt 2 ]] && { echo "Error: --num-samples requires a value"; exit 1; }; NUM_SAMPLES="$2"; shift 2 ;;
        --skip-to-stage) [[ $# -lt 2 ]] && { echo "Error: --skip-to-stage requires a value"; exit 1; }; SKIP_TO_STAGE="$2"; shift 2 ;;
        --blind-lint) BLIND_LINT="true"; shift ;;
        --blind-tests) BLIND_TESTS="true"; shift ;;
        --names-only-tests) NAMES_ONLY_TESTS="true"; shift ;;
        --no-test-files-readonly) INJECT_TEST_FILES_READONLY="false"; shift ;;
        --strip-non-stubs) STRIP_NON_STUBS="true"; shift ;;
        --max-test-output-length) [[ $# -lt 2 ]] && { echo "Error: --max-test-output-length requires a value"; exit 1; }; MAX_TEST_OUTPUT_LENGTH="$2"; shift 2 ;;
        --max-parallel-repos) [[ $# -lt 2 ]] && { echo "Error: --max-parallel-repos requires a value"; exit 1; }; MAX_PARALLEL_REPOS="$2"; shift 2 ;;
        --resume)      RESUME="true"; shift ;;
        -h|--help)     print_usage ;;
        --use-claude-code) USE_CLAUDE_CODE="true"; shift ;;
        *)
            echo "Error: Unknown argument '$1'"
            echo ""
            print_usage
            ;;
    esac
done

if [[ -z "$MODEL_ARG" ]]; then
    echo "Error: --model is required"
    echo ""
    print_usage
fi

if [[ -z "$DATASET_ARG" ]]; then
    echo "Error: --dataset is required"
    echo ""
    print_usage
fi

if ! [[ "$NUM_SAMPLES" =~ ^[1-9][0-9]*$ ]]; then
    echo "Error: --num-samples must be a positive integer (got: $NUM_SAMPLES)"
    exit 1
fi

if [[ "$NUM_SAMPLES" -gt 1 ]] && [[ -n "$SKIP_TO_STAGE" ]]; then
    echo "Error: --skip-to-stage and --num-samples > 1 cannot be used together."
    exit 1
fi

# ============================================================
# Model resolution and preflight (shared across all pipelines)
# ============================================================
source "${BASE_DIR}/commit0/harness/resolve_model.sh"

resolve_model "$MODEL_ARG"

# ============================================================
# Claude Code OAuth bridge (optional --use-claude-code)
# ============================================================
source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
claude_code_maybe_start_bridge "$MODEL_NAME"

# ============================================================
# Bedrock Bearer Token Priority
# ============================================================

if [[ "$MODEL_NAME" == bedrock/* ]] && [[ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]]; then
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE 2>/dev/null || true
    export AWS_SHARED_CREDENTIALS_FILE="/dev/null"
fi

# ============================================================
# Resolve Dataset (TS-specific: look for *_ts_dataset.json first)
# ============================================================

resolve_dataset_ts() {
    local arg="$1"

    # Case 1: explicit path to a JSON file
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
            basename="${basename%_ts_dataset}"
            basename="${basename%_dataset}"
            REPO_SPLIT="$basename"
        fi
        DATASET_SHORT=$(basename "$arg" .json)
        return
    fi

    # Case 2: named dataset — look for <name>_ts_dataset.json first, then <name>_dataset.json
    local ts_candidate="${BASE_DIR}/${arg}_ts_dataset.json"
    if [[ -f "$ts_candidate" ]]; then
        DATASET_FILE="$ts_candidate"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="${arg}"
        return
    fi

    local candidate="${BASE_DIR}/${arg}_dataset.json"
    if [[ -f "$candidate" ]]; then
        DATASET_FILE="$candidate"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="${arg}"
        return
    fi

    # Case 3: named split from TS_SPLIT constants
    local known_splits
    known_splits=$("$VENV_PYTHON" -c "
from commit0.harness.constants_ts import TS_SPLIT
for k in sorted(TS_SPLIT.keys()):
    print(k)
" 2>/dev/null || true)

    if echo "$known_splits" | grep -qx "$arg"; then
        DATASET_FILE="wentingzhao/commit0_combined"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="$arg"
        DATASET_SPLIT="test"
        return
    fi

    echo "Error: Cannot resolve TS dataset '$arg'"
    echo ""
    echo "Provide one of:"
    echo "  - A known name with a local <name>_ts_dataset.json or <name>_dataset.json file"
    echo "  - A path to a .json dataset file"
    echo "  - A commit0 TS split name (all_ts, etc.)"
    echo ""
    echo "Available local TS datasets:"
    for f in "${BASE_DIR}"/*_ts_dataset.json "${BASE_DIR}"/*_dataset.json; do
        [[ -f "$f" ]] && echo "  $(basename "$f" .json)"
    done
    exit 1
}

DATASET_FILE=""
REPO_SPLIT=""
DATASET_SHORT=""
DATASET_SPLIT="train"
resolve_dataset_ts "$DATASET_ARG"

DATASET_UUID=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[0].get('id','') if d else '')" "$DATASET_FILE" 2>/dev/null || true)
DATASET_N=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$DATASET_FILE" 2>/dev/null || echo 1)
if [[ "$DATASET_N" -gt 1 ]]; then
    echo "[WARNING] dataset has $DATASET_N entries; using entries[0].id ($DATASET_UUID) as folder key. Dataset-level UUIDs deferred (\u00a711 Q1)."
fi
if [[ -z "$DATASET_UUID" ]]; then
    DATASET_UUID="$DATASET_SHORT"
fi
export KAIJU_EXPERIMENT_UUID="$DATASET_UUID"

# TS branch naming: aider-ts-<model_short>-<dataset_short>
BASE_BRANCH_NAME="${BRANCH_OVERRIDE:-aider-ts-${MODEL_SHORT}-${DATASET_SHORT}}"
if [[ -z "$BRANCH_OVERRIDE" ]] && [[ "$NO_STAGE3_LINT" == "true" ]]; then
    BASE_BRANCH_NAME="${BASE_BRANCH_NAME}-nolint-s3"
fi

BASE_RUN_ID_FLAT=$(echo "ts_${MODEL_SHORT}_${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
DATASET_DIR_NAME=$(echo "${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
MODEL_DIR_NAME=$(echo "${MODEL_SHORT}" | tr -dc 'a-zA-Z0-9._-')
if [[ "$NO_STAGE3_LINT" == "true" ]]; then
    MODEL_DIR_NAME="${MODEL_DIR_NAME}_nolint-s3"
    BASE_RUN_ID_FLAT="${BASE_RUN_ID_FLAT}_nolint-s3"
fi

COMMIT0_TS_CONFIG=""
AGENT_CONFIG=""

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
        LOG_BASE="${BASE_DIR}/logs/agent_ts/${DATASET_DIR_NAME}/${MODEL_DIR_NAME}/run_${sample_idx}"
        PIPELINE_LOG="${BASE_DIR}/logs/pipeline_ts_${RUN_ID}_results.json"
    fi
    COMMIT0_TS_CONFIG="${BASE_DIR}/.commit0.ts_${RUN_ID}.yaml"
    AGENT_CONFIG="${BASE_DIR}/.agent_ts_${RUN_ID}.yaml"
    if is_consolidated; then
        COMMIT0_TS_CONFIG="$(configs_dir "$DATASET_UUID")/commit0.ts_${RUN_ID}.yaml"
        AGENT_CONFIG="$(configs_dir "$DATASET_UUID")/agent_ts_${RUN_ID}.yaml"
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

    for cmd in jq bc timeout; do
        if ! command -v "$cmd" &>/dev/null; then
            echo "Error: Required command '$cmd' not found"
            errors=$((errors + 1))
        fi
    done

    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "Error: Python venv not found at $VENV_PYTHON"
        errors=$((errors + 1))
    fi

    if [[ ! -d "$REPO_BASE_TS" ]]; then
        echo "Error: TS repo base directory not found at $REPO_BASE_TS"
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
    elif [[ "$MODEL_NAME" == vertex_ai/*claude* ]] || [[ "$MODEL_NAME" == vertex_ai_beta/*claude* ]]; then
        if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "Error: GOOGLE_APPLICATION_CREDENTIALS not set (required for Vertex AI Claude model: $MODEL_NAME)"
            errors=$((errors + 1))
        fi
    elif [[ "$MODEL_NAME" == *claude* ]] && [[ "$MODEL_NAME" != bedrock/* ]]; then
        if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
            echo "Error: ANTHROPIC_API_KEY not set (required for model: $MODEL_NAME)"
            errors=$((errors + 1))
        fi
    elif [[ "$MODEL_NAME" == gemini/* ]]; then
        if [[ -z "${GOOGLE_API_KEY:-}" ]]; then
            echo "Error: GOOGLE_API_KEY not set (required for model: $MODEL_NAME)"
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
                if [[ ! -d "${REPO_BASE_TS}/${repo}" ]]; then
                    echo "Error: TS repo directory not found: ${REPO_BASE_TS}/${repo}"
                    echo "  Run: python -m commit0.cli_ts setup $REPO_SPLIT"
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
# Helpers (verbatim from run_pipeline.sh)
# ============================================================

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] [${RUN_ID}] $1"; }

get_mtime() {
    stat -c '%Y' "$1" 2>/dev/null \
        || stat -f '%m' "$1" 2>/dev/null \
        || "$VENV_PYTHON" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1" 2>/dev/null \
        || echo "0"
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
# Watchdog (verbatim from run_pipeline.sh)
# ============================================================

watchdog_run() {
    local agent_pid="$1"
    local log_dir="$2"
    local inactivity_limit="$3"
    local hard_timeout="$4"
    local absolute_max="${5:-86400}"
    local start_time
    start_time=$(date +%s)

    local hard_timeout_warned="false"
    local mtime_functional="true"

    if [[ "$(uname -s)" == "Linux" ]]; then
        local _probe_mtime
        _probe_mtime=$(get_mtime "/proc/self/status")
        if [[ "$_probe_mtime" -eq 0 ]] 2>/dev/null; then
            log "  WATCHDOG: WARNING — get_mtime returned 0 for /proc/self/status."
            mtime_functional="false"
        fi
    fi

    while kill -0 "$agent_pid" 2>/dev/null; do
        sleep 15

        local now_epoch
        now_epoch=$(date +%s)

        local latest_mtime=0

        local latest_log
        latest_log=$(get_newest_aider_log "$log_dir")
        if [[ -n "$latest_log" ]] && [[ -f "$latest_log" ]]; then
            local aider_mtime
            aider_mtime=$(get_mtime "$latest_log")
            if [[ "$aider_mtime" -gt "$latest_mtime" ]]; then
                latest_mtime="$aider_mtime"
            fi
        fi

        local agent_run_log="${log_dir}/agent_run.log"
        if [[ -f "$agent_run_log" ]]; then
            local run_log_mtime
            run_log_mtime=$(get_mtime "$agent_run_log")
            if [[ "$run_log_mtime" -gt "$latest_mtime" ]]; then
                latest_mtime="$run_log_mtime"
            fi
        fi

        local idle=0
        local agent_active="false"
        if [[ "$latest_mtime" -gt 0 ]]; then
            idle=$(( now_epoch - latest_mtime ))
            if [[ $idle -lt $inactivity_limit ]]; then
                agent_active="true"
            fi
        else
            agent_active="true"
        fi

        if [[ "$absolute_max" -gt 0 ]]; then
            local wall_elapsed=$(( now_epoch - start_time ))
            if [[ $wall_elapsed -ge $absolute_max ]]; then
                log "  WATCHDOG: Absolute wall-time cap ${absolute_max}s reached. Force-killing agent."
                kill "$agent_pid" 2>/dev/null || true
                sleep 2
                kill -9 "$agent_pid" 2>/dev/null || true
                wait "$agent_pid" 2>/dev/null || true
                return 124
            fi
        fi

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
                    kill "$agent_pid" 2>/dev/null || true
                    sleep 2
                    kill -9 "$agent_pid" 2>/dev/null || true
                    wait "$agent_pid" 2>/dev/null || true
                    return 124
                fi
            fi
        fi

        if [[ "$latest_mtime" -gt 0 ]] && [[ "$agent_active" == "false" ]]; then
            log "  WATCHDOG: No log activity for ${idle}s (limit: ${inactivity_limit}s). Agent appears stuck."
            if [[ -n "$latest_log" ]] && [[ -f "$latest_log" ]]; then
                log "  WATCHDOG: Last aider log: $(basename "$(dirname "$latest_log")")"
            fi
            log "  WATCHDOG: Killing agent (PID ${agent_pid})."
            kill "$agent_pid" 2>/dev/null || true
            sleep 2
            kill -9 "$agent_pid" 2>/dev/null || true
            wait "$agent_pid" 2>/dev/null || true
            return 124
        fi
    done

    wait "$agent_pid" 2>/dev/null
    local rc=$?
    if [[ $rc -eq 127 ]]; then
        rc=0
    fi
    return $rc
}

# ============================================================
# Config Writers (TS-specific)
# ============================================================

write_commit0_ts_config() {
    local ds_value
    ds_value="$(cd "$(dirname "$DATASET_FILE")" && pwd)/$(basename "$DATASET_FILE")"

    cat > "$COMMIT0_TS_CONFIG" <<EOF
base_dir: ${REPO_BASE_TS}
dataset_name: ${ds_value}
dataset_split: ${DATASET_SPLIT}
repo_split: ${REPO_SPLIT}
EOF
    log "  Wrote TS commit0 config: ${COMMIT0_TS_CONFIG}"
}

yaml_escape() {
    local val="$1"
    val="${val//\'/\'\'}"
    echo "'${val}'"
}

# ============================================================
# Spec Doc Provisioning (TS-specific)
# ============================================================

ensure_spec_docs_ts() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        log "  Spec docs disabled — skipping."
        return 0
    fi

    log "Ensuring spec docs are available for all TS repos..."

    "$VENV_PYTHON" - "$DATASET_FILE" "$REPO_BASE_TS" "." <<'PYEOF'
import json, os, sys, shutil

dataset_file = sys.argv[1]
repo_base    = sys.argv[2]
base_dir     = sys.argv[3]

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

verify_spec_docs_ts() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        return 0
    fi

    log "Verifying all TS repos have spec docs..."

    local missing=0
    local missing_repos=""

    local repo_list
    repo_list=$(_PIPELINE_DATASET_FILE="$DATASET_FILE" "$VENV_PYTHON" -c "
import json, os
with open(os.environ['_PIPELINE_DATASET_FILE']) as f:
    data = json.load(f)
if isinstance(data, dict) and 'data' in data:
    data = data['data']
for item in data:
    print(item['repo'].split('/')[-1])
" 2>/dev/null || true)

    if [[ -z "$repo_list" ]]; then
        log "  WARNING: Could not enumerate repos for spec verification."
        return 0
    fi

    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        local repo_dir="${REPO_BASE_TS}/${repo}"
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
        log "FATAL: ${missing} TS repo(s) missing spec docs (use_spec_info=true)."
        log "  The pipeline requires spec docs for all repos when --no-spec-info"
        log "  is not set. Missing repos:"
        echo -e "$missing_repos" | while IFS= read -r line; do [[ -n "$line" ]] && log "$line"; done
        log ""
        log "  Options:"
        log "    1. Place spec.pdf or spec.pdf.bz2 in each repo directory"
        log "    2. Add 'specification' URLs to the dataset JSON and re-run"
        log "    3. Use --no-spec-info to run without spec context"
        log "======================================================================"
        return 1
    fi

    log "  All TS repos have spec docs."
}

# Frozen test-id inventory gate (mirrors verify_spec_docs_ts). A missing
# inventory makes the eval SILENTLY score against ALL discovered tests — a
# wrong, non-reproducible denominator. Resolved with the SAME function the eval
# uses (kaiju.verify_inventory -> find_test_ids_file). FATAL by default;
# --no-strict-inventory (or KAIJU_REQUIRE_INVENTORY=0) downgrades to warn-only.
verify_inventory_ts() {
    log "Verifying all TS repos have a frozen test-id inventory..."
    local strict_flag="--strict"
    [[ "$STRICT_INVENTORY" != "true" ]] && strict_flag="--no-strict"
    local ds_arg=()
    [[ -n "${DATASET_FILE:-}" ]] && ds_arg=(--dataset "$DATASET_FILE")
    local split_arg=()
    [[ -n "${REPO_SPLIT:-}" ]] && split_arg=(--repo-split "$REPO_SPLIT")
    "$VENV_PYTHON" -m kaiju.verify_inventory --language ts \
        "${ds_arg[@]}" "${split_arg[@]}" "$strict_flag"
}

# ============================================================
# Config Writers (TS-specific, continued)
# ============================================================

write_agent_config_ts() {
    local run_tests="$1"
    local use_lint_info="$2"
    local run_entire_dir_lint="$3"
    local use_unit_tests_info="$4"
    local add_import_module_to_context="$5"
    local use_spec_info="${6:-false}"

    cat > "$AGENT_CONFIG" <<'YAMLEOF'
agent_name: aider
YAMLEOF
    cat >> "$AGENT_CONFIG" <<EOF
model_name: $(yaml_escape "${MODEL_NAME}")
model_short: $(yaml_escape "${MODEL_SHORT}")
use_user_prompt: false
user_prompt: 'Here is your task:

  You need to complete the implementations for all functions (i.e., those with
  throw new Error("STUB") statements) and pass the unit tests.

  Do not change the names of existing functions or classes, as they may be referenced
  from other code like unit tests, etc.

  When you generate code, you must maintain the original formatting of the function
  stubs (such as whitespaces), otherwise we will not be able to search/replace blocks
  for code modifications, and therefore you will receive a score of 0 for your generated
  code.'
use_topo_sort_dependencies: false
add_import_module_to_context: ${add_import_module_to_context}
use_repo_info: false
max_repo_info_length: 10000
use_unit_tests_info: ${use_unit_tests_info}
max_unit_tests_info_length: 10000
use_spec_info: ${use_spec_info}
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
EOF
    log "  Wrote TS agent config: ${AGENT_CONFIG}"
}

# ============================================================
# Run Agent (TS-specific: invokes agent.run_agent_ts)
# ============================================================

AGENT_PID=""
AGENT_ELAPSED=0
AGENT_RC=0

# AUTO-RESUME helper (shared shape across languages). Re-run any module left
# .needs_retry (a transient LLM error that persisted through the in-line recovery)
# IN-PLACE, up to K rounds with a pause, so a batch NEVER needs a manual --resume.
# KAIJU_RESUME=1 rebuilds the branch from per-module patches and the agent skips
# modules that already have a .done marker, so each round re-attempts only the
# failed ones; a module that succeeds clears its .needs_retry and gains .done
# (_mark_module_done), so the count converges to 0 unless GENUINELY persistent.
# Args: <log_dir> <agent_log> -- <base agent command...>  (command WITHOUT
# --override-previous-changes, which would reset the branch and discard progress).
_auto_resume_agent() {
    local _ld="$1" _alog="$2"; shift 2
    [[ "${1:-}" == "--" ]] && shift
    local _amax="${KAIJU_AUTO_RESUME_ROUNDS:-3}" _auto=0 _nr
    _nr=$(find "$_ld" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
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
        _nr=$(find "$_ld" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
        log "  AUTO-RESUME ${_auto}/${_amax} finished (rc=${AGENT_RC}); ${_nr} module(s) still .needs_retry."
    done
    if [[ "${_nr:-0}" -gt 0 ]]; then
        log "  WARNING: ${_nr} module(s) STILL .needs_retry after ${_amax} auto-resume round(s) — genuinely persistent (not a passing transient); run INCOMPLETE."
    elif [[ "$_auto" -gt 0 ]]; then
        log "  AUTO-RESUME succeeded: all modules completed after ${_auto} round(s); run COMPLETE (no manual --resume needed)."
    fi
}

run_agent_ts() {
    local branch="$1"
    local override="$2"
    local log_dir="$3"

    local cmd=(
        "$VENV_PYTHON" -m agent.run_agent_ts "$branch"
        --backend "$BACKEND"
        --agent-config-file "$AGENT_CONFIG"
        --commit0-config-file "$COMMIT0_TS_CONFIG"
        --log-dir "$log_dir"
        --max-parallel-repos "$MAX_PARALLEL_REPOS"
    )

    local first_cmd=( "${cmd[@]}" )
    if [[ "$override" == "true" ]]; then
        first_cmd+=(--override-previous-changes)
    fi

    local agent_log="${log_dir}/agent_run.log"
    log "  Running TS agent (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s)"
    log "  Command: ${first_cmd[*]}"
    log "  Output → ${agent_log}"

    local start_time
    start_time=$(date +%s)

    set +e
    "${first_cmd[@]}" >>"$agent_log" 2>&1 &
    local agent_pid=$!
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
        tail -20 "$agent_log" | while IFS= read -r line; do log "    | $line"; done
    else
        log "  Agent finished in ${AGENT_ELAPSED}s, returncode=${AGENT_RC}"
    fi

    _auto_resume_agent "$log_dir" "$agent_log" -- "${cmd[@]}"
}

# ============================================================
# Run Evaluate (TS-specific: invokes commit0.cli_ts evaluate)
# ============================================================

EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_RUNTIME="0.0"
EVAL_ELAPSED=0

run_evaluate_ts() {
    local branch="$1"
    local stage_label="${2:-eval}"

    local cmd=(
        "$VENV_PYTHON" -m commit0.cli_ts evaluate
        --branch "$branch"
        --backend "$BACKEND"
        --timeout 300
        --num-cpus 1
        --num-workers 1
        --commit0-config-file "$COMMIT0_TS_CONFIG"
    )

    local eval_log="${LOG_BASE}/${stage_label}_eval.log"
    log "  Running TS evaluation: ${cmd[*]}"
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

    parse_eval_output "$combined_output"

    EVAL_INFRA_FAILED="false"
    if [[ $eval_rc -eq 124 ]]; then
        log "  Evaluation TIMED OUT after ${EVAL_TIMEOUT}s — marking infra failure (pass_rate=null)"
        EVAL_INFRA_FAILED="true"
        EVAL_PASS_RATE="null"
    elif [[ $eval_rc -ne 0 && "$EVAL_NUM_TESTS" -eq 0 ]]; then
        log "  Evaluation FAILED with no parseable results (rc=${eval_rc}) — marking infra failure (pass_rate=null)"
        EVAL_INFRA_FAILED="true"
        EVAL_PASS_RATE="null"
    fi

    if [[ $eval_rc -ne 0 ]]; then
        log "  Evaluation FAILED — last 10 lines:"
        tail -10 "$eval_log" | while IFS= read -r line; do log "    | $line"; done
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
            # PATCH_APPLY_FAILED / OUTPUT_MISSING / SUITE_CRASHED otherwise). Any
            # non-TESTS_RAN status means the 0/N is a build/patch/infra failure, NOT
            # a genuine 0% model score — surface it (mirrors go/rust/c/cpp).
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
                        total_runtime=$(echo "scale=4; $total_runtime + $runtime" | bc)
                    fi
                    found_any="true"
                fi
            fi
        fi
    done <<< "$output"

    if [[ "$found_any" == "true" ]]; then
        if [[ -n "$row_status" ]]; then
            EVAL_STATUS="$row_status"
        else
            EVAL_STATUS="OK"
        fi
        EVAL_NUM_PASSED="$total_passed"
        EVAL_NUM_TESTS="$total_tests"
        EVAL_RUNTIME="$total_runtime"
        if [[ "$total_tests" -gt 0 ]]; then
            EVAL_PASS_RATE=$(echo "scale=6; $total_passed / $total_tests" | bc)
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
            if [[ -n "$rate" ]] && [[ "$rate" =~ ^[0-9.]+$ ]]; then
                EVAL_PASS_RATE="$rate"
            fi
        fi
    fi
}

# ============================================================
# Cost Extraction (verbatim from run_pipeline.sh)
# ============================================================

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
# JSON Results (verbatim from run_pipeline.sh)
# ============================================================

RESULTS_JSON=""

init_results() {
    RESULTS_JSON=$(jq -n \
        --arg model "$MODEL_SHORT" \
        --arg model_short "$MODEL_SHORT" \
        --arg branch "$BRANCH_NAME" \
        --arg backend "$BACKEND" \
        --arg repo_split "$REPO_SPLIT" \
        --arg dataset "$DATASET_FILE" \
        --arg dataset_short "$DATASET_SHORT" \
        --argjson max_iter "$MAX_ITERATION" \
        --arg cache_prompts "$CACHE_PROMPTS" \
        --arg start_time "$(ts)" \
        --arg pipeline "typescript" \
        '{
            pipeline: $pipeline,
            model: $model,
            model_short: $model_short,
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
    mkdir -p "$(dirname "$PIPELINE_LOG")"
    echo "$RESULTS_JSON" | jq '.' > "$PIPELINE_LOG"
}

write_cache() {
    :
}

# ============================================================
# Pipeline Stages (TS-specific)
# ============================================================

stage_1_draft_ts() {
    log "======================================================================"
    log "STAGE 1: Draft Initial TS Implementations"
    log "======================================================================"

    write_agent_config_ts "false" "false" "false" "true" "true" "$USE_SPEC_INFO"

    local stage_log_dir="${LOG_BASE}/stage1_draft"
    mkdir -p "$stage_log_dir"

    run_agent_ts "$BRANCH_NAME" "true" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local cost cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 1 cost extraction failed"; return 1; }
    cost="${_co%% *}"; cost_source="${_co#* }"
    log "  Stage 1 cost: \$${cost} (source: ${cost_source})"

    run_evaluate_ts "$BRANCH_NAME" "stage1"
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
        --arg eval_status "${EVAL_STATUS:-OK}" \
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

stage_2_lint_ts() {
    log "======================================================================"
    log "STAGE 2: Refine with Static Analysis (Lint) — TS"
    log "======================================================================"

    write_agent_config_ts "false" "true" "true" "false" "false" "$USE_SPEC_INFO"

    local stage_log_dir="${LOG_BASE}/stage2_lint"
    mkdir -p "$stage_log_dir"

    run_agent_ts "$BRANCH_NAME" "false" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s1_cost
    s1_cost=$(echo "$RESULTS_JSON" | jq -r '.stage1.cost_usd // 0') || { log "ERROR: Stage 2 failed to read stage1 cost"; return 1; }
    local s2_incremental cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 2 cost extraction failed"; return 1; }
    s2_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(echo "scale=4; $s1_cost + $s2_incremental" | bc) || { log "ERROR: Stage 2 cost calculation failed"; return 1; }

    log "  Stage 2 incremental cost: \$${s2_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    run_evaluate_ts "$BRANCH_NAME" "stage2"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 2 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Lint refine" \
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
        --arg eval_status "${EVAL_STATUS:-OK}" \
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

stage_3_test_ts() {
    log "======================================================================"
    log "STAGE 3: Refine with Unit Test Feedback — TS"
    log "======================================================================"

    local s3_lint="true"
    if [[ "$NO_STAGE3_LINT" == "true" ]]; then
        s3_lint="false"
        log "  Stage 3 lint DISABLED (--no-stage3-lint)"
    fi

    write_agent_config_ts "true" "$s3_lint" "false" "false" "false" "$USE_SPEC_INFO"

    local stage_log_dir="${LOG_BASE}/stage3_tests"
    mkdir -p "$stage_log_dir"

    run_agent_ts "$BRANCH_NAME" "false" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s2_cumulative
    s2_cumulative=$(echo "$RESULTS_JSON" | jq -r '.stage2.cost_usd_cumulative // 0') || { log "ERROR: Stage 3 failed to read stage2 cost"; return 1; }
    local s3_incremental cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 3 cost extraction failed"; return 1; }
    s3_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(echo "scale=4; $s2_cumulative + $s3_incremental" | bc) || { log "ERROR: Stage 3 cost calculation failed"; return 1; }

    log "  Stage 3 incremental cost: \$${s3_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    run_evaluate_ts "$BRANCH_NAME" "stage3"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 3 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Test refine" \
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
        --arg eval_status "${EVAL_STATUS:-OK}" \
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
# Summary Table (verbatim from run_pipeline.sh)
# ============================================================

print_summary_table() {
    log ""
    log "=========================================================================================="
    log "RESULTS SUMMARY — SDE-I 3-Stage Pipeline (TypeScript)"
    log "Model: ${MODEL_SHORT} (${MODEL_NAME})"
    log "Dataset: ${DATASET_SHORT} | Repo Split: ${REPO_SPLIT} | Branch: ${BRANCH_NAME}"
    log "Cache Prompts: ${CACHE_PROMPTS} | Max Iteration: ${MAX_ITERATION} | Backend: ${BACKEND}"
    log "=========================================================================================="
    log ""

    printf -v header "%-30s %12s %14s %12s %14s %10s" "Stage" "Pass Rate" "Passed/Total" "Stage Cost" "Cumul. Cost" "Time (s)"
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

        printf -v row "%-30s %12s %14s %12s %14s %10s" "$name" "$rate_str" "$passed_str" "$stage_cost_str" "$cumul_cost_str" "$elapsed_str"
        log "$row"
    done

    log "--------------------------------------------------------------------------------------------------------------"
    log ""
}

# ============================================================
# Cleanup (verbatim from run_pipeline.sh)
# ============================================================

PIPELINE_SUCCESS="false"

cleanup() {
    if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
        kill -- -"$AGENT_PID" 2>/dev/null || true
        sleep 2
        kill -9 -- -"$AGENT_PID" 2>/dev/null || true
    fi

    if [[ "$PIPELINE_SUCCESS" == "true" ]]; then
        for _si in $(seq 1 "$NUM_SAMPLES"); do
            set_sample_vars "$_si"
            rm -f "$COMMIT0_TS_CONFIG" "$AGENT_CONFIG" 2>/dev/null || true
        done
        log "Cleaned up per-run config files"
    else
        for _si in $(seq 1 "$NUM_SAMPLES"); do
            set_sample_vars "$_si"
            if [[ -f "$COMMIT0_TS_CONFIG" ]] || [[ -f "$AGENT_CONFIG" ]]; then
                log "Pipeline did not complete successfully. Config files preserved for debugging:"
                [[ -f "$COMMIT0_TS_CONFIG" ]] && log "  ${COMMIT0_TS_CONFIG}"
                [[ -f "$AGENT_CONFIG" ]] && log "  ${AGENT_CONFIG}"
            fi
        done
    fi
}
trap cleanup EXIT
trap 'exit' INT TERM

# ============================================================
# Main (TS-specific)
# ============================================================

declare -a SAMPLE_RESULT_FILES=()

run_single_sample() {
    local sample_idx="$1"

    set_sample_vars "$sample_idx"

    # Resume: continue a prior run stopped by a subscription limit / kill WITHOUT
    # redoing finished modules. Derive the resume stage from prior results and flag
    # the agent (KAIJU_RESUME) to rebuild the branch from host-persisted per-module
    # patches; finished modules' .done markers then skip them.
    if [[ "$RESUME" == "true" ]]; then
        export KAIJU_RESUME=1
        _rs="$("$VENV_PYTHON" -m agent.resume_state which-stage --results "$PIPELINE_LOG" 2>/dev/null || echo "")"
        if [[ "$_rs" == "2" || "$_rs" == "3" ]]; then
            SKIP_TO_STAGE="$_rs"
            log "RESUME: prior progress found -> skipping to stage ${SKIP_TO_STAGE}; finished modules will be skipped."
        elif [[ -z "$_rs" ]]; then
            log "RESUME: prior run already completed all stages; modules restored + re-verified."
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
    log "Commit0 SDE-I 3-Stage Pipeline — TypeScript"
    log "Model:        ${MODEL_NAME} (${MODEL_SHORT})"
    log "Dataset:      ${DATASET_FILE} (${DATASET_SHORT})"
    log "Repo Split:   ${REPO_SPLIT}"
    log "Branch:       ${BRANCH_NAME}"
    log "Backend:      ${BACKEND}"
    log "Cache:        ${CACHE_PROMPTS}"
    log "Max Iter:     ${MAX_ITERATION}"
    log "Num Samples:  ${NUM_SAMPLES} (run_${sample_idx})"
    log "Stage Timeout: ${STAGE_TIMEOUT}s (0=disabled) | Eval Timeout: ${EVAL_TIMEOUT}s"
    log "Inactivity:   ${INACTIVITY_TIMEOUT}s (watchdog kills stuck agents)"
    log "Wall-time cap: ${MAX_WALL_TIME}s (unconditional, 0=disable)"
    log "Spec Info:    ${USE_SPEC_INFO}"
    if [[ "$NO_STAGE3_LINT" == "true" ]]; then
        log "Stage3 Lint:  DISABLED (--no-stage3-lint)"
    else
        log "Stage3 Lint:  enabled"
    fi
    if [[ -n "$SKIP_TO_STAGE" ]]; then
        log "Skip To:      Stage ${SKIP_TO_STAGE} (prior stages skipped)"
    fi
    log "Logs:         ${LOG_BASE}"
    log "Results:      ${PIPELINE_LOG}"
    log "Start time:   $(ts)"
    log "======================================================================"

    if [[ "$sample_idx" -eq 1 ]]; then
        preflight
    fi

    write_commit0_ts_config

    if [[ "$sample_idx" -eq 1 ]]; then
        ensure_spec_docs_ts
        if ! verify_spec_docs_ts; then
            return 1
        fi
        if ! verify_inventory_ts; then
            return 1
        fi
    fi

    if [[ -n "$SKIP_TO_STAGE" ]]; then
        if [[ ! -f "$PIPELINE_LOG" ]]; then
            log "ERROR: Cannot skip to stage ${SKIP_TO_STAGE}: no prior results found at ${PIPELINE_LOG}"
            return 1
        fi
        RESULTS_JSON=$(cat "$PIPELINE_LOG")
        local loaded_ok="true"
        if [[ "$SKIP_TO_STAGE" == "2" ]]; then
            echo "$RESULTS_JSON" | jq -e '.stage1' >/dev/null 2>&1 || loaded_ok="false"
            if [[ "$loaded_ok" == "false" ]]; then
                log "ERROR: Prior results missing stage1 data. Cannot skip to stage 2."
                return 1
            fi
        elif [[ "$SKIP_TO_STAGE" == "3" ]]; then
            echo "$RESULTS_JSON" | jq -e '.stage1' >/dev/null 2>&1 || loaded_ok="false"
            echo "$RESULTS_JSON" | jq -e '.stage2' >/dev/null 2>&1 || loaded_ok="false"
            if [[ "$loaded_ok" == "false" ]]; then
                log "ERROR: Prior results missing stage1/stage2 data. Cannot skip to stage 3."
                return 1
            fi
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
        if ! stage_1_draft_ts; then
            pipeline_error="Stage 1 failed"
            log "PIPELINE ERROR: ${pipeline_error}"
        fi
    else
        log "Stage 1: SKIPPED"
    fi

    if [[ -z "$pipeline_error" ]] && [[ "$skip_stage_2" == "false" ]]; then
        if ! stage_2_lint_ts; then
            pipeline_error="Stage 2 failed"
            log "PIPELINE ERROR: ${pipeline_error}"
        fi
    elif [[ "$skip_stage_2" == "true" ]]; then
        log "Stage 2: SKIPPED"
    fi

    if [[ -z "$pipeline_error" ]]; then
        if ! stage_3_test_ts; then
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
}

print_pass_at_k_summary() {
    local k="$NUM_SAMPLES"
    log ""
    log "=========================================================================================="
    log "PASS@${k} SUMMARY — ${MODEL_SHORT} / ${DATASET_SHORT} (TypeScript)"
    log "=========================================================================================="
    log ""

    printf -v header "%-12s %14s %14s %14s %12s" "Run" "S1 Pass Rate" "S2 Pass Rate" "S3 Pass Rate" "S3 Cost"
    log "$header"
    log "--------------------------------------------------------------------------------------------"

    local best_s3_rate="0"
    local best_s3_sample=1

    for result_file in "${SAMPLE_RESULT_FILES[@]}"; do
        if [[ ! -f "$result_file" ]]; then
            continue
        fi
        local rj
        rj=$(cat "$result_file")

        local sidx s1r s2r s3r s3c s3status
        sidx=$(echo "$rj" | jq -r '.sample_index // "?"')
        s1r=$(echo "$rj" | jq -r '.stage1.pass_rate // 0')
        s2r=$(echo "$rj" | jq -r '.stage2.pass_rate // 0')
        s3r=$(echo "$rj" | jq -r '.stage3.pass_rate // 0')
        s3c=$(echo "$rj" | jq -r '.stage3.cost_usd_cumulative // .stage1.cost_usd // 0')
        # A 0% from a build/patch/infra failure is NOT "solved nothing" — surface
        # the status and exclude such samples from the best-run pick (parity w/ rust).
        s3status=$(echo "$rj" | jq -r '.stage3.eval_status // "OK"')

        local s1_pct s2_pct s3_pct cost_str
        s1_pct=$(format_pct "$s1r")
        s2_pct=$(format_pct "$s2r")
        if [[ "$s3status" == "OK" || "$s3status" == "null" || -z "$s3status" ]]; then
            s3_pct=$(format_pct "$s3r")
        else
            s3_pct="$s3status"
        fi
        cost_str=$(printf "\$%.2f" "$s3c")

        printf -v row "%-12s %14s %14s %14s %12s" "run_${sidx}" "$s1_pct" "$s2_pct" "$s3_pct" "$cost_str"
        log "$row"

        if [[ "$s3status" == "OK" || "$s3status" == "null" || -z "$s3status" ]]; then
            local is_better
            is_better=$(echo "$s3r > $best_s3_rate" | bc -l)
            if [[ "$is_better" -eq 1 ]]; then
                best_s3_rate="$s3r"
                best_s3_sample="$sidx"
            fi
        fi
    done

    log "--------------------------------------------------------------------------------------------"
    local best_pct
    best_pct=$(format_pct "$best_s3_rate")
    log "Best-of-${k} (pass@${k}):  run_${best_s3_sample}  →  ${best_pct}"
    log "=========================================================================================="
    log ""
}

SAMPLES_COMPLETED=0

main_ts() {
    for sample_idx in $(seq 1 "$NUM_SAMPLES"); do
        if run_single_sample "$sample_idx"; then
            SAMPLES_COMPLETED=$((SAMPLES_COMPLETED + 1))
        else
            log "WARNING: run_${sample_idx} failed — continuing with remaining samples."
        fi
    done

    RUN_ID="${BASE_RUN_ID_FLAT}"

    if [[ "$NUM_SAMPLES" -gt 1 ]]; then
        if [[ "$SAMPLES_COMPLETED" -gt 0 ]]; then
            print_pass_at_k_summary
        else
            log "ERROR: All ${NUM_SAMPLES} samples failed. No pass@k summary."
        fi
    fi

    if [[ "$SAMPLES_COMPLETED" -eq "$NUM_SAMPLES" ]]; then
        log "Pipeline complete. All ${NUM_SAMPLES} sample(s) succeeded."
        PIPELINE_SUCCESS="true"
    elif [[ "$SAMPLES_COMPLETED" -gt 0 ]]; then
        log "Pipeline complete. ${SAMPLES_COMPLETED}/${NUM_SAMPLES} sample(s) succeeded."
        PIPELINE_SUCCESS="true"
    else
        log "Pipeline FAILED. No samples completed successfully."
    fi
}

cd "$BASE_DIR"
main_ts
