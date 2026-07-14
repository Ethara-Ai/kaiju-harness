#!/usr/bin/env bash
# ============================================================
# 3-Stage SDE-I Trajectory Pipeline for Commit0 — Java
# ============================================================
#
# Usage:
#     bash run_pipeline_java.sh --model <preset|model_id> --dataset <dataset>
#
# Examples:
#     bash run_pipeline_java.sh --model nova-lite --dataset lite
#     bash run_pipeline_java.sh --model opus --dataset all
#     bash run_pipeline_java.sh --model nova-lite --dataset ./commons-lang_dataset.json
#     bash run_pipeline_java.sh --model nova-lite --dataset commons-lang
#     bash run_pipeline_java.sh --model gpt54 --dataset lite --num-samples 3
#     nohup bash run_pipeline_java.sh --model nova-lite --dataset lite > logs/java_nova_lite.log 2>&1 &
#
# Requirements: jq, bc
# ============================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "${BASE_DIR}/.env" ]]; then
    set -a
    source "${BASE_DIR}/.env"
    set +a
fi
source "${BASE_DIR}/scripts/_outputs_layout.sh"
"${BASE_DIR}/scripts/generate_aider_config.sh"
REPO_BASE="${BASE_DIR}/repos/java"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
COMMIT0_JAVA="${BASE_DIR}/.venv/bin/commit0-java"
MAX_ITERATION=3
export LANGUAGE="java"  # H8: parity with other drivers so child processes can rely on $LANGUAGE

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
INACTIVITY_TIMEOUT=1800
MAX_WALL_TIME=86400
SKIP_TO_STAGE=""
RESUME="false"
NUM_SAMPLES=1
MAX_PARALLEL_REPOS=1
MAX_TEST_OUTPUT_LENGTH=15000
INJECT_TEST_FILES_READONLY="true"
BLIND_LINT="false"
BLIND_TESTS="false"
NAMES_ONLY_TESTS="false"
STRIP_NON_STUBS="false"

print_usage() {
    cat <<'USAGE'
Usage: run_pipeline_java.sh --model <preset|model_id> --dataset <dataset> [OPTIONS]

Required:
  --model    <preset|id>   Model preset or full model ID
  --dataset  <dataset>     Dataset: split name (lite, all), named dataset, or JSON path

Dataset resolution (same as Python pipeline):
  lite                     Built-in split: 7 curated repos
  all                      Built-in split: all 20 repos
  commons-lang             Named dataset: looks for ./commons-lang_dataset.json
  ./my_dataset.json        Explicit path to a JSON dataset file

Model presets:
  opus     Bedrock Claude Opus 4.6
  kimi     Bedrock Kimi K2.5
  glm5     Bedrock GLM 5
  minimax  Bedrock MiniMax M2.5
  gpt54    OpenAI GPT-5.4
  gpt55    OpenAI GPT-5.5 (reasoning_effort=high)
  nova-premier  Bedrock Nova Premier
  nova-lite     Bedrock Nova 2 Lite

Options:
  --branch         <name>    Override auto-generated branch name
  --repo-split     <split>   Override repo split (filter repos from dataset)
  --max-iteration  <n>       Max agent iterations per stage (default: 3)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled)
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap in seconds (default: 86400)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --no-stage3-lint           Disable compile-check in Stage 3
  --no-spec-info             Disable spec doc context for agents
  --no-strict-inventory      Warn (do not FATAL) when a repo's frozen test-id inventory is missing
  --num-samples    <n>       Number of independent samples (pass@k, default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages)
  --max-test-output-length <n>  Max test output length (default: 15000)
  --use-claude-code        Route anthropic/* models through the local Claude Code OAuth bridge
  --max-parallel-repos <n>     Max repos to run in parallel (default: 1, >1 enables batch mode)
  --no-test-files-readonly        Disable test file injection as read-only context
  --blind-lint                    Stage 2: show only lint count, not details
  --blind-tests                   Stage 3: show only test summary line, not tracebacks
  --names-only-tests              Stage 3: show only failed test names, not assertion text
  --strip-non-stubs               Limit edits to files that contain stub markers
  -h, --help                 Show this help

Examples:
  bash run_pipeline_java.sh --model nova-lite --dataset lite
  bash run_pipeline_java.sh --model opus --dataset all
  bash run_pipeline_java.sh --model nova-lite --dataset ./commons-lang_dataset.json
  bash run_pipeline_java.sh --model nova-lite --dataset commons-lang
  bash run_pipeline_java.sh --model nova-lite --dataset lite --repo-split lite
USAGE
    exit 1
}

# In-container the only backend is local_inplace (git worktree, no nested docker).
# The containerized runner passes --backend to EVERY language's pipeline for
# parity, so accept it here (java always evaluates in-place) rather than dying
# with "Unknown argument '--backend'".
BACKEND="local_inplace"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       [[ $# -lt 2 ]] && { echo "Error: --model requires a value"; exit 1; }; MODEL_ARG="$2";          shift 2 ;;
        --dataset)     [[ $# -lt 2 ]] && { echo "Error: --dataset requires a value"; exit 1; }; DATASET_ARG="$2";       shift 2 ;;
        --backend)     [[ $# -lt 2 ]] && { echo "Error: --backend requires a value"; exit 1; }; BACKEND="$2";           shift 2 ;;
        --branch)      [[ $# -lt 2 ]] && { echo "Error: --branch requires a value"; exit 1; }; BRANCH_OVERRIDE="$2";   shift 2 ;;
        --repo-split)  [[ $# -lt 2 ]] && { echo "Error: --repo-split requires a value"; exit 1; }; REPO_SPLIT_OVERRIDE="$2"; shift 2 ;;
        --max-iteration) [[ $# -lt 2 ]] && { echo "Error: --max-iteration requires a value"; exit 1; }; MAX_ITERATION="$2"; shift 2 ;;
        --stage-timeout) [[ $# -lt 2 ]] && { echo "Error: --stage-timeout requires a value"; exit 1; }; STAGE_TIMEOUT="$2"; shift 2 ;;
        --eval-timeout)  [[ $# -lt 2 ]] && { echo "Error: --eval-timeout requires a value"; exit 1; }; EVAL_TIMEOUT="$2";  shift 2 ;;
        --no-stage3-lint) NO_STAGE3_LINT="true"; shift ;;
        --no-spec-info) USE_SPEC_INFO="false"; shift ;;
        --no-strict-inventory) STRICT_INVENTORY="false"; shift ;;
        --inactivity-timeout) [[ $# -lt 2 ]] && { echo "Error: --inactivity-timeout requires a value"; exit 1; }; INACTIVITY_TIMEOUT="$2"; shift 2 ;;
        --max-wall-time) [[ $# -lt 2 ]] && { echo "Error: --max-wall-time requires a value"; exit 1; }; MAX_WALL_TIME="$2"; shift 2 ;;
        --num-samples) [[ $# -lt 2 ]] && { echo "Error: --num-samples requires a value"; exit 1; }; NUM_SAMPLES="$2"; shift 2 ;;
        --skip-to-stage) [[ $# -lt 2 ]] && { echo "Error: --skip-to-stage requires a value"; exit 1; }; SKIP_TO_STAGE="$2"; shift 2 ;;
        --max-test-output-length) [[ $# -lt 2 ]] && { echo "Error: --max-test-output-length requires a value"; exit 1; }; MAX_TEST_OUTPUT_LENGTH="$2"; shift 2 ;;
        --max-parallel-repos) [[ $# -lt 2 ]] && { echo "Error: --max-parallel-repos requires a value"; exit 1; }; MAX_PARALLEL_REPOS="$2"; shift 2 ;;
        --no-test-files-readonly) INJECT_TEST_FILES_READONLY="false"; shift ;;
        --blind-lint)             BLIND_LINT="true"; shift ;;
        --blind-tests)            BLIND_TESTS="true"; shift ;;
        --names-only-tests)       NAMES_ONLY_TESTS="true"; shift ;;
        --strip-non-stubs)        STRIP_NON_STUBS="true"; shift ;;
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
# Resolve Dataset (Java — supports JSON paths, named datasets, and splits)
# ============================================================
#
# Sets: DATASET_SPLIT, DATASET_SHORT, DATASET_FILE, REPO_SPLIT
#
# Three resolution modes (matching Python run_pipeline.sh):
#   1. Explicit JSON path:  ./my_dataset.json or path/to/dataset.json
#   2. Named dataset:       commons-lang → looks for ./commons-lang_dataset.json
#   3. Built-in split name: lite, all → uses java_dataset.json with JAVA_SPLIT filtering

DATASET_FILE=""
REPO_SPLIT=""

resolve_dataset_java() {
    local arg="$1"

    # Case 1: explicit path to a JSON file
    if [[ "$arg" == *.json ]] || [[ "$arg" == */* ]]; then
        if [[ ! -f "$arg" ]]; then
            # Try relative to BASE_DIR
            if [[ -f "${BASE_DIR}/${arg}" ]]; then
                arg="${BASE_DIR}/${arg}"
            else
                echo "Error: Dataset file not found: $arg"
                exit 1
            fi
        fi
        DATASET_FILE="$(cd "$(dirname "$arg")" && pwd)/$(basename "$arg")"
        # Repo split: use override, or extract from filename
        if [[ -n "$REPO_SPLIT_OVERRIDE" ]]; then
            REPO_SPLIT="$REPO_SPLIT_OVERRIDE"
        else
            local basename
            basename=$(basename "$arg" .json)
            basename="${basename%_dataset}"
            REPO_SPLIT="$basename"
        fi
        DATASET_SHORT=$(basename "$arg" .json)
        DATASET_SPLIT="custom"
        return
    fi

    # Case 2: named dataset — look for <name>_dataset.json locally
    local candidate="${BASE_DIR}/${arg}_dataset.json"
    if [[ -f "$candidate" ]]; then
        DATASET_FILE="$candidate"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="${arg}"
        DATASET_SPLIT="custom"
        return
    fi

    # Case 3: built-in split name (lite, all)
    case "$arg" in
        lite|all)
            DATASET_SPLIT="$arg"
            DATASET_SHORT="java_dataset_${arg}"
            REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
            DATASET_FILE=""  # uses default java_dataset.json via list-repos
            ;;
        *)
            echo "Error: Cannot resolve dataset '$arg'"
            echo ""
            echo "Provide one of:"
            echo "  - A path to a .json dataset file (e.g., ./commons-lang_dataset.json)"
            echo "  - A named dataset with a local <name>_dataset.json file"
            echo "  - A built-in split name: lite, all"
            echo ""
            echo "Available local datasets:"
            for f in "${BASE_DIR}"/*_dataset.json; do
                [[ -f "$f" ]] && echo "  $(basename "${f}" _dataset.json)"
            done
            exit 1
            ;;
    esac
}

resolve_dataset_java "$DATASET_ARG"

# Write the in-container commit0-java config. Both agent.config_java and
# `commit0-java evaluate` read the DEFAULT .commit0.java.yaml from CWD (evaluate
# takes no --commit0-config-file flag). prepare wrote it on the HOST with the host
# dataset filename, but it is NOT present in the container's /opt/kaiju — so the
# eval died with "Invalid value: .commit0.java.yaml not found". Regenerate it here
# with the CONTAINER-resolved dataset so both the agent and the eval find it.
if [[ -n "${DATASET_FILE:-}" ]]; then
    cat > "${BASE_DIR}/.commit0.java.yaml" <<EOF
# commit0 Java config (generated in-container by run_pipeline_java.sh)
dataset_name: ${DATASET_FILE}
dataset_split: ${DATASET_SPLIT}
repo_split: ${REPO_SPLIT}
base_dir: repos
EOF
    log "  Wrote in-container config: ${BASE_DIR}/.commit0.java.yaml (dataset=${DATASET_FILE})" 2>/dev/null || \
      echo "  Wrote in-container config: ${BASE_DIR}/.commit0.java.yaml"
fi

DATASET_UUID=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[0].get('id','') if d else '')" "$DATASET_FILE" 2>/dev/null || true)
DATASET_N=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$DATASET_FILE" 2>/dev/null || echo 1)
if [[ "$DATASET_N" -gt 1 ]]; then
    echo "[WARNING] dataset has $DATASET_N entries; using entries[0].id ($DATASET_UUID) as folder key. Dataset-level UUIDs deferred (§11 Q1)."
fi
if [[ -z "$DATASET_UUID" ]]; then
    DATASET_UUID="$DATASET_SHORT"
fi
export KAIJU_EXPERIMENT_UUID="$DATASET_UUID"

# Mirror the commit0-java config into the canonical outputs/<uuid>/configs/ folder
# for provenance + parity with the other languages (which write their configs
# there). The LIVE copy MUST stay at BASE_DIR because `commit0-java evaluate`
# reads the default .commit0.java.yaml from CWD (it has no --commit0-config-file
# flag) — this is an ADDITIONAL copy for the run's audit trail, never a move, so
# it can't affect config resolution. Best-effort; never aborts the run.
if [[ -f "${BASE_DIR}/.commit0.java.yaml" ]]; then
    _java_cfg_dir="$(configs_dir "$DATASET_UUID" 2>/dev/null || true)"
    if [[ -n "${_java_cfg_dir:-}" ]]; then
        cp "${BASE_DIR}/.commit0.java.yaml" "${_java_cfg_dir}/commit0_java.yaml" 2>/dev/null || true
        log "  Mirrored config -> ${_java_cfg_dir}/commit0_java.yaml" 2>/dev/null || true
    fi
fi

# Build branch name: aider-java-<model_short>-<dataset_short>
BASE_BRANCH_NAME="${BRANCH_OVERRIDE:-aider-java-${MODEL_SHORT}-${DATASET_SHORT}}"
if [[ -z "$BRANCH_OVERRIDE" ]] && [[ "$NO_STAGE3_LINT" == "true" ]]; then
    BASE_BRANCH_NAME="${BASE_BRANCH_NAME}-nolint-s3"
fi

# Base RUN_ID with java_ prefix
BASE_RUN_ID_FLAT=$(echo "java_${MODEL_SHORT}_${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
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
        LOG_BASE="${BASE_DIR}/logs/agent/java/${DATASET_DIR_NAME}/${MODEL_DIR_NAME}/run_${sample_idx}"
        PIPELINE_LOG="${BASE_DIR}/logs/pipeline_java_${RUN_ID}_results.json"
    fi
}

set_sample_vars 1

mkdir -p "$LOG_BASE"
exec > >(tee -a "$LOG_BASE/pipeline.log") 2>&1

# ============================================================
# Enumerate Repos
# ============================================================

enumerate_repos() {
    if [[ -n "$DATASET_FILE" ]]; then
        jq -r '.[].repo' "$DATASET_FILE"
    else
        "$COMMIT0_JAVA" list-repos --split "$DATASET_SPLIT"
    fi
}

REPOS=""

load_repos() {
    REPOS=$(enumerate_repos)
    if [[ -z "$REPOS" ]]; then
        echo "Error: No repos found for dataset '${DATASET_FILE:-$DATASET_SPLIT}'"
        exit 1
    fi
    local count
    count=$(echo "$REPOS" | wc -l | tr -d ' ')
    log "  Loaded $count repos for '${DATASET_FILE:-$DATASET_SPLIT}'"
}

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

    if [[ ! -x "$COMMIT0_JAVA" ]]; then
        echo "Error: 'commit0-java' not found at $COMMIT0_JAVA. Install the commit0 package."
        errors=$((errors + 1))
    fi

    if [[ ! -x "$VENV_PYTHON" ]]; then
        echo "Error: Python venv not found at $VENV_PYTHON"
        errors=$((errors + 1))
    fi

    if [[ ! -d "$REPO_BASE" ]]; then
        echo "Error: Java repo base directory not found at $REPO_BASE"
        echo "  Run: $COMMIT0_JAVA setup --dataset-split $DATASET_SPLIT"
        errors=$((errors + 1))
    fi

    # Check API keys based on model provider
    if [[ "$MODEL_NAME" == bedrock/* ]]; then
        if [[ -z "${AWS_ACCESS_KEY_ID:-}" ]] && [[ -z "${AWS_BEARER_TOKEN_BEDROCK:-}" ]] && [[ -z "${AWS_PROFILE:-}" ]]; then
            echo "Warning: No AWS credentials detected (AWS_ACCESS_KEY_ID, AWS_BEARER_TOKEN_BEDROCK, or AWS_PROFILE)"
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

    # Verify repos exist
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        local repo_short="${repo##*/}"
        if [[ ! -d "${REPO_BASE}/${repo_short}" ]]; then
            echo "Error: Repo directory not found: ${REPO_BASE}/${repo_short}"
        echo "  Run: $COMMIT0_JAVA setup --dataset-split $DATASET_SPLIT"
            errors=$((errors + 1))
        fi
    done <<< "$REPOS"

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
# Verify Spec Docs (Java — specs are in repo dirs)
# ============================================================

# H7: Provision spec docs from a shared cache into repo dirs before the verify
# step runs. Without ensure, verify_spec_docs_java would FATAL on the first
# repo without a spec.pdf even if a cached copy sits in specs/<repo>.pdf(.bz2).
# Mirrors ensure_spec_docs_{js,ts,rust,c,cpp,go} — verification-without-
# provisioning was the last driver-level asymmetry.
ensure_spec_docs_java() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        log "  Spec docs disabled — skipping."
        return 0
    fi
    local specs_dir="${SPECS_DIR:-specs}"
    if [[ ! -d "$specs_dir" ]]; then
        log "  No shared specs dir at $specs_dir — skipping provisioning (verify may still succeed if repos ship their own spec)."
        return 0
    fi
    log "Ensuring spec docs are available for all Java repos (source: $specs_dir)..."
    local provisioned=0
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        local repo_short="${repo##*/}"
        local repo_dir="${REPO_BASE}/${repo_short}"
        [[ ! -d "$repo_dir" ]] && continue
        # Skip if the repo already has a spec.
        if [[ -f "${repo_dir}/spec.pdf" ]] || [[ -f "${repo_dir}/spec.pdf.bz2" ]]; then
            continue
        fi
        # Try cached copies from the shared specs dir.
        if [[ -f "${specs_dir}/${repo_short}.pdf.bz2" ]]; then
            cp "${specs_dir}/${repo_short}.pdf.bz2" "${repo_dir}/spec.pdf.bz2" 2>/dev/null && provisioned=$((provisioned + 1))
        elif [[ -f "${specs_dir}/${repo_short}.pdf" ]]; then
            cp "${specs_dir}/${repo_short}.pdf" "${repo_dir}/spec.pdf" 2>/dev/null && provisioned=$((provisioned + 1))
        fi
    done <<< "$REPOS"
    log "  Provisioned $provisioned spec doc(s) from $specs_dir."
}

verify_spec_docs_java() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        return 0
    fi

    log "Verifying all Java repos have spec docs..."

    local missing=0
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        local repo_short="${repo##*/}"
        local repo_dir="${REPO_BASE}/${repo_short}"
        if [[ ! -d "$repo_dir" ]]; then
            continue
        fi
        if [[ ! -f "${repo_dir}/spec.pdf" ]] && [[ ! -f "${repo_dir}/spec.pdf.bz2" ]]; then
            log "  MISSING spec: ${repo_short}"
            missing=$((missing + 1))
        else
            log "  OK spec: ${repo_short}"
        fi
    done <<< "$REPOS"

    if [[ "$missing" -gt 0 ]]; then
        log ""
        log "======================================================================"
        log "FATAL: ${missing} Java repo(s) missing spec docs (use_spec_info=true)."
        log "  Options:"
        log "    1. Place spec.pdf or spec.pdf.bz2 in each repo directory"
        log "    2. Use --no-spec-info to run without spec context"
        log "======================================================================"
        return 1
    fi

    log "  All Java repos have spec docs."
}

# Frozen test-id inventory gate (mirrors verify_spec_docs_java). A missing
# inventory means there is no canonical scoring denominator. NOTE: evaluate_java
# currently scores against len(results) (discovered) unconditionally, so this
# gate is the ONLY line of defense that a frozen java_test_ids/<repo>.bz2 exists.
# FATAL by default; --no-strict-inventory (or KAIJU_REQUIRE_INVENTORY=0) warns.
verify_inventory_java() {
    log "Verifying all Java repos have a frozen test-id inventory..."
    local strict_flag="--strict"
    [[ "$STRICT_INVENTORY" != "true" ]] && strict_flag="--no-strict"
    local ds_arg=()
    [[ -n "${DATASET_FILE:-}" ]] && ds_arg=(--dataset "$DATASET_FILE")
    local split_arg=()
    [[ -n "${REPO_SPLIT:-}" ]] && split_arg=(--repo-split "$REPO_SPLIT")
    "$VENV_PYTHON" -m kaiju.verify_inventory --language java \
        "${ds_arg[@]}" "${split_arg[@]}" "$strict_flag"
}

# ============================================================
# Run Agent (Java — per-repo loop)
# ============================================================

AGENT_PID=""
AGENT_ELAPSED=0
AGENT_RC=0

# kill_tree: recursively kill a process and all its descendants (macOS-compatible).
# setsid is not available on macOS, so we walk the tree with pkill -P recursively.
kill_tree() {
    local pid="$1"
    local sig="${2:-TERM}"
    local child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        kill_tree "$child" "$sig"
    done
    kill -"$sig" "$pid" 2>/dev/null || true
}

# Watchdog: identical to Python pipeline
watchdog_run() {
    local agent_pid="$1"
    local log_dir="$2"
    local inactivity_limit="$3"
    local hard_timeout="$4"
    local absolute_max="${5:-86400}"
    local start_time
    start_time=$(date +%s)

    local hard_timeout_warned="false"

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

        # Absolute wall-time cap
        if [[ "$absolute_max" -gt 0 ]]; then
            local wall_elapsed=$(( now_epoch - start_time ))
            if [[ $wall_elapsed -ge $absolute_max ]]; then
                log "  WATCHDOG: Absolute wall-time cap ${absolute_max}s reached. Force-killing agent."
                kill_tree "$agent_pid" TERM
                sleep 2
                kill_tree "$agent_pid" 9
                wait "$agent_pid" 2>/dev/null || true
                return 124
            fi
        fi

        # Hard timeout: only kill if agent is also inactive
        if [[ "$hard_timeout" -gt 0 ]]; then
            local elapsed=$(( now_epoch - start_time ))
            if [[ $elapsed -ge $hard_timeout ]]; then
                if [[ "$agent_active" == "true" ]]; then
                    if [[ "$hard_timeout_warned" == "false" ]]; then
                        log "  WATCHDOG: Hard timeout ${hard_timeout}s reached but agent still active (last write ${idle}s ago). Letting it continue."
                        hard_timeout_warned="true"
                    fi
                else
                    log "  WATCHDOG: Hard timeout ${hard_timeout}s reached and agent inactive (${idle}s). Killing agent."
                    kill_tree "$agent_pid" TERM
                    sleep 2
                    kill_tree "$agent_pid" 9
                    wait "$agent_pid" 2>/dev/null || true
                    return 124
                fi
            fi
        fi

        # Inactivity timeout
        if [[ "$latest_mtime" -gt 0 ]] && [[ "$agent_active" == "false" ]]; then
            log "  WATCHDOG: No log activity for ${idle}s (limit: ${inactivity_limit}s). Agent appears stuck."
            log "  WATCHDOG: Killing agent (PID ${agent_pid})."
            kill_tree "$agent_pid" TERM
            sleep 2
            kill_tree "$agent_pid" 9
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

# run_java_agent_loop: runs commit0-java agent for each repo in $REPOS
# Args: run_tests use_unit_tests_info use_spec_info compile_check override log_dir
run_java_agent_loop() {
    local run_tests="$1"
    local use_unit_tests_info="$2"
    local use_spec_info="$3"
    local compile_check="$4"
    local override="$5"
    local log_dir="$6"
    local run_entire_dir_lint="${7:-false}"

    local agent_log="${log_dir}/agent_run.log"

    # Build flags shared across every agent invocation
    local common_flags=(
        --branch "$BRANCH_NAME"
        --model "$MODEL_NAME"
        --max-iteration "$MAX_ITERATION"
        --log-dir "$log_dir"
        --max-test-output-length "$MAX_TEST_OUTPUT_LENGTH"
        --capture-thinking --trajectory-md --output-jsonl
        --model-short "$MODEL_SHORT"
        --record-test-for-each-commit
    )

    if [[ "$run_tests" == "true" ]]; then
        common_flags+=(--run-tests)
    else
        common_flags+=(--no-run-tests)
    fi

    if [[ "$run_entire_dir_lint" == "true" ]]; then
        common_flags+=(--run-entire-dir-lint)
    else
        common_flags+=(--no-run-entire-dir-lint)
    fi

    if [[ "$use_unit_tests_info" == "true" ]]; then
        common_flags+=(--use-unit-tests-info)
    else
        common_flags+=(--no-use-unit-tests-info)
    fi

    if [[ "$use_spec_info" == "true" ]]; then
        common_flags+=(--use-spec-info)
    else
        common_flags+=(--no-use-spec-info)
    fi

    if [[ "$compile_check" == "true" ]]; then
        common_flags+=(--compile-check)
    else
        common_flags+=(--no-compile-check)
    fi

    if [[ "$CACHE_PROMPTS" == "true" ]]; then
        common_flags+=(--cache-prompts)
    else
        common_flags+=(--no-cache-prompts)
    fi

    if [[ "$override" == "true" ]]; then
        common_flags+=(--override-previous)
    else
        common_flags+=(--no-override-previous)
    fi

    if [[ "$BLIND_LINT" == "true" ]]; then
        common_flags+=(--blind-lint)
    fi
    if [[ "$BLIND_TESTS" == "true" ]]; then
        common_flags+=(--blind-tests)
    fi
    if [[ "$NAMES_ONLY_TESTS" == "true" ]]; then
        common_flags+=(--names-only-tests)
    fi
    if [[ "$STRIP_NON_STUBS" == "true" ]]; then
        common_flags+=(--strip-non-stubs)
    fi
    if [[ "$INJECT_TEST_FILES_READONLY" == "false" ]]; then
        common_flags+=(--no-inject-test-files-readonly)
    fi

    if [[ "$MAX_PARALLEL_REPOS" -gt 1 ]]; then
        local repos_tmpfile
        repos_tmpfile=$(mktemp)
        echo "$REPOS" > "$repos_tmpfile"
        log "  Running Java agent in batch mode (parallel=${MAX_PARALLEL_REPOS}) via repos-file"
        local cmd=("$COMMIT0_JAVA" agent
            --repos-file "$repos_tmpfile"
            --max-parallel-repos "$MAX_PARALLEL_REPOS"
            "${common_flags[@]}"
        )
        log "  Command: ${cmd[*]}"
        "${cmd[@]}" >>"$agent_log" 2>&1
        local rc=$?
        rm -f "$repos_tmpfile"
        return $rc
    fi

    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        log "  Running agent for repo: ${repo}"
        local cmd=("$COMMIT0_JAVA" agent --repo "$repo" "${common_flags[@]}")
        log "  Command: ${cmd[*]}"
        "${cmd[@]}" >>"$agent_log" 2>&1
        local repo_rc=$?
        if [[ $repo_rc -ne 0 ]]; then
            log "  Agent for ${repo} exited with rc=${repo_rc}"
        fi
    done <<< "$REPOS"
}

# run_agent_java: wraps run_java_agent_loop with watchdog
# Args: run_tests use_unit_tests_info use_spec_info compile_check override log_dir [run_entire_dir_lint]
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

run_agent_java() {
    local run_tests="$1"
    local use_unit_tests_info="$2"
    local use_spec_info="$3"
    local compile_check="$4"
    local override="$5"
    local log_dir="$6"
    local run_entire_dir_lint="${7:-false}"

    mkdir -p "$log_dir"

    log "  Running Java agent loop (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s)"

    local start_time
    start_time=$(date +%s)

    set +e
    run_java_agent_loop "$run_tests" "$use_unit_tests_info" "$use_spec_info" "$compile_check" "$override" "$log_dir" "$run_entire_dir_lint" &
    local agent_pid=$!
    AGENT_PID=$agent_pid

    watchdog_run "$agent_pid" "$log_dir" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
    AGENT_RC=$?
    AGENT_PID=""
    set -e

    local end_time
    end_time=$(date +%s)
    AGENT_ELAPSED=$(( end_time - start_time ))

    local agent_log="${log_dir}/agent_run.log"
    if [[ $AGENT_RC -eq 124 ]]; then
        log "  Agent killed by watchdog after ${AGENT_ELAPSED}s"
    elif [[ $AGENT_RC -ne 0 ]]; then
        log "  Agent FAILED (rc=${AGENT_RC}) in ${AGENT_ELAPSED}s — last 20 lines:"
        tail -20 "$agent_log" 2>/dev/null | while IFS= read -r line; do log "    | $line"; done
    else
        log "  Agent finished in ${AGENT_ELAPSED}s, returncode=${AGENT_RC}"
    fi

    # AUTO-RESUME (Java): re-run any module left .needs_retry (a transient error
    # that persisted through the in-line recovery) IN-PLACE, up to K rounds with a
    # pause, so a batch NEVER needs a manual --resume. Resume passes override=false
    # (-> --no-override-previous) so completed modules are KEPT; KAIJU_RESUME=1
    # rebuilds the branch from per-module patches and .done modules are skipped, so
    # only the failed ones re-run and (on success) clear .needs_retry + gain .done.
    local _amax="${KAIJU_AUTO_RESUME_ROUNDS:-3}" _auto=0 _nr
    _sweep_limbo_modules "$log_dir"
    _nr=$(find "$log_dir" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
    while [[ "${_nr:-0}" -gt 0 && "$_auto" -lt "$_amax" ]]; do
        _auto=$((_auto + 1))
        log "  AUTO-RESUME ${_auto}/${_amax}: ${_nr} module(s) left .needs_retry — waiting ${KAIJU_AUTO_RESUME_PAUSE:-60}s then re-running in-place (no manual --resume)."
        sleep "${KAIJU_AUTO_RESUME_PAUSE:-60}"
        local _rs _re
        _rs=$(date +%s)
        set +e
        KAIJU_RESUME=1 run_java_agent_loop "$run_tests" "$use_unit_tests_info" "$use_spec_info" "$compile_check" "false" "$log_dir" &
        agent_pid=$!
        AGENT_PID=$agent_pid
        watchdog_run "$agent_pid" "$log_dir" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
        AGENT_RC=$?
        AGENT_PID=""
        set -e
        _re=$(date +%s)
        AGENT_ELAPSED=$(( AGENT_ELAPSED + (_re - _rs) ))
        _sweep_limbo_modules "$log_dir"
    _nr=$(find "$log_dir" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
        log "  AUTO-RESUME ${_auto}/${_amax} finished (rc=${AGENT_RC}); ${_nr} module(s) still .needs_retry."
    done
    if [[ "${_nr:-0}" -gt 0 ]]; then
        log "  WARNING: ${_nr} module(s) STILL .needs_retry after ${_amax} auto-resume round(s) — genuinely persistent (not a passing transient); run INCOMPLETE."
    elif [[ "$_auto" -gt 0 ]]; then
        log "  AUTO-RESUME succeeded: all modules completed after ${_auto} round(s); run COMPLETE (no manual --resume needed)."
    fi
}

# ============================================================
# Run Evaluate (Java — per-repo, branch-based)
# ============================================================

EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_RUNTIME="0.0"
EVAL_ELAPSED=0

run_evaluate_java() {
    local branch="$1"
    local stage_label="${2:-eval}"

    local eval_log="${LOG_BASE}/${stage_label}_eval.log"
    log "  Running Java evaluation for branch: ${branch}"
    log "  Output -> ${eval_log}"

    local start_time
    start_time=$(date +%s)

    EVAL_NUM_PASSED=0
    EVAL_NUM_TESTS=0
    EVAL_PASS_RATE="0.0"
    EVAL_RUNTIME="0.0"

    set +e
    while IFS= read -r repo; do
        [[ -z "$repo" ]] && continue
        log "  Evaluating repo: ${repo}"
        local repo_short repo_eval_dir
        repo_short=$(basename "$repo")
        repo_eval_dir="${LOG_BASE}/${stage_label:-eval}_eval_artifacts/${repo_short}"
        mkdir -p "$repo_eval_dir"
        timeout "$EVAL_TIMEOUT" "$COMMIT0_JAVA" evaluate \
            --repo "$repo" \
            --branch "$branch" \
            --timeout "$EVAL_TIMEOUT" \
            --backend "$BACKEND" \
            --log-dir "$repo_eval_dir" \
            >>"$eval_log" 2>&1
        local eval_rc=$?
        if [[ $eval_rc -ne 0 ]]; then
            log "  Evaluation for ${repo} returned rc=${eval_rc}"
        fi
    done <<< "$REPOS"
    set -e

    local end_time
    end_time=$(date +%s)
    EVAL_ELAPSED=$(( end_time - start_time ))

    log "  Evaluation finished in ${EVAL_ELAPSED}s"

    collect_eval_artifacts "${stage_label:-eval}"

    # Parse eval output
    local combined_output
    combined_output=$(cat "$eval_log" 2>/dev/null || echo "")

    parse_eval_output "$combined_output"

    log "  Eval results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"
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

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        [[ "$line" == repo,* ]] && continue
        if [[ "$line" == *","*"/"* ]]; then
            local runtime passed_total passed total
            runtime=$(echo "$line" | cut -d',' -f2 | tr -d ' ')
            passed_total=$(echo "$line" | cut -d',' -f3 | tr -d ' ')

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
        EVAL_NUM_PASSED="$total_passed"
        EVAL_NUM_TESTS="$total_tests"
        EVAL_RUNTIME="$total_runtime"
        if [[ "$total_tests" -gt 0 ]]; then
            EVAL_PASS_RATE=$(echo "scale=6; $total_passed / $total_tests" | bc)
        fi
    fi

    # Fallback: look for "average pass rate:" line
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
# Cost Extraction (identical to Python pipeline)
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
            local artifact_count done_count
            artifact_count=$(find "$log_dir" \( -name aider.log -o -name output.json -o -name turns.jsonl \) 2>/dev/null | wc -l | tr -d ' ')
            done_count=$(find "$log_dir" -name .done 2>/dev/null | wc -l | tr -d ' ')
            if [[ "${artifact_count:-0}" == "0" && "${done_count:-0}" == "0" ]]; then
                log "  WARNING: no agent artifacts in ${log_dir} \$0.0000 reported — AGENT SKIPPED (no target files, module init failure, or crash before first LLM call). Check agent_run.log for the failure reason." >&2
            elif [[ "${artifact_count:-0}" == "0" && "${done_count:-0}" != "0" ]]; then
                log "  INFO: ${done_count} .done marker(s) but no aider.log/output.json in ${log_dir} — \$0.0000 reported (all modules resumed from prior cache; not an extraction failure)." >&2
            else
                log "  WARNING: ${artifact_count} agent artifact(s) present in ${log_dir} but cost extraction found NO output.json/aider.log cost — \$0.0000 reported, this is an EXTRACTION FAILURE." >&2
            fi
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
        --arg language "java" \
        --arg model "$MODEL_SHORT" \
        --arg model_short "$MODEL_SHORT" \
        --arg branch "$BRANCH_NAME" \
        --arg repo_split "$DATASET_SPLIT" \
        --arg dataset "$DATASET_SHORT" \
        --arg dataset_short "$DATASET_SHORT" \
        --argjson max_iter "$MAX_ITERATION" \
        --arg cache_prompts "$CACHE_PROMPTS" \
        --arg start_time "$(ts)" \
        '{
            language: $language,
            model: $model,
            model_short: $model_short,
            branch: $branch,
            backend: "local",
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

# ============================================================
# Pipeline Stages
# ============================================================

stage_1_draft() {
    log "======================================================================"
    log "STAGE 1: Draft Initial Implementations"
    log "======================================================================"

    local stage_log_dir="${LOG_BASE}/stage1_draft"
    mkdir -p "$stage_log_dir"

    # Stage 1 (draft): run_tests=false, use_unit_tests_info=true, use_spec_info=$USE_SPEC_INFO, compile_check=true, override=true, run_entire_dir_lint=false
    run_agent_java "false" "true" "$USE_SPEC_INFO" "true" "true" "$stage_log_dir" "false"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local cost cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir" "$MODEL_NAME") || { log "ERROR: Stage 1 cost extraction failed"; return 1; }
    cost="${_co%% *}"; cost_source="${_co#* }"
    log "  Stage 1 cost: \$${cost} (source: ${cost_source})"

    run_evaluate_java "$BRANCH_NAME" "stage1"
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
            pass_rate: $pass_rate
        }')

    save_results

    if [[ $rc -eq 124 ]]; then
        log "  Stage 1 INCOMPLETE: agent killed by watchdog. Aborting further stages."
        return 1
    fi
}

stage_2_lint_refine() {
    log "======================================================================"
    log "STAGE 2: Refine with Compile Check (Lint)"
    log "======================================================================"

    local stage_log_dir="${LOG_BASE}/stage2_lint"
    mkdir -p "$stage_log_dir"

    # Stage 2 (lint): run_tests=false, use_unit_tests_info=false, use_spec_info=$USE_SPEC_INFO, compile_check=true, override=false, run_entire_dir_lint=TRUE
    # run_entire_dir_lint=true drives the model from compile/lint errors (lint_first),
    # matching python/go/rust — NOT a re-send of the draft "implement stubs" prompt.
    run_agent_java "false" "false" "$USE_SPEC_INFO" "true" "false" "$stage_log_dir" "true"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s1_cost
    s1_cost=$(echo "$RESULTS_JSON" | jq -r '.stage1.cost_usd // 0') || { log "ERROR: Stage 2 failed to read stage1 cost"; return 1; }
    local s2_incremental cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir" "$MODEL_NAME") || { log "ERROR: Stage 2 cost extraction failed"; return 1; }
    s2_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(echo "scale=4; $s1_cost + $s2_incremental" | bc) || { log "ERROR: Stage 2 cost calculation failed"; return 1; }

    log "  Stage 2 incremental cost: \$${s2_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    run_evaluate_java "$BRANCH_NAME" "stage2"
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
            pass_rate: $pass_rate
        }')

    save_results

    if [[ $rc -eq 124 ]]; then
        log "  Stage 2 INCOMPLETE: agent killed by watchdog. Aborting further stages."
        return 1
    fi
}

stage_3_test_refine() {
    log "======================================================================"
    log "STAGE 3: Refine with Unit Test Feedback"
    log "======================================================================"

    local s3_compile_check="true"
    if [[ "$NO_STAGE3_LINT" == "true" ]]; then
        s3_compile_check="false"
        log "  Stage 3 compile-check DISABLED (--no-stage3-lint)"
    fi

    local stage_log_dir="${LOG_BASE}/stage3_tests"
    mkdir -p "$stage_log_dir"

    # Stage 3 (tests): run_tests=true, use_unit_tests_info=false, use_spec_info=$USE_SPEC_INFO, compile_check=$s3_compile_check, override=false, run_entire_dir_lint=false
    run_agent_java "true" "false" "$USE_SPEC_INFO" "$s3_compile_check" "false" "$stage_log_dir" "false"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s2_cumulative
    s2_cumulative=$(echo "$RESULTS_JSON" | jq -r '.stage2.cost_usd_cumulative // 0') || { log "ERROR: Stage 3 failed to read stage2 cost"; return 1; }
    local s3_incremental cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir" "$MODEL_NAME") || { log "ERROR: Stage 3 cost extraction failed"; return 1; }
    s3_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(echo "scale=4; $s2_cumulative + $s3_incremental" | bc) || { log "ERROR: Stage 3 cost calculation failed"; return 1; }

    log "  Stage 3 incremental cost: \$${s3_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    run_evaluate_java "$BRANCH_NAME" "stage3"
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
            pass_rate: $pass_rate
        }')

    save_results

    if [[ $rc -eq 124 ]]; then
        log "  Stage 3 INCOMPLETE: agent killed by watchdog."
        return 1
    fi
}

# ============================================================
# Summary Table
# ============================================================

print_summary_table() {
    log ""
    log "=========================================================================================="
    log "RESULTS SUMMARY — Java SDE-I 3-Stage Pipeline"
    log "Model: ${MODEL_SHORT} (${MODEL_NAME})"
    log "Dataset: ${DATASET_SHORT} | Split: ${DATASET_SPLIT} | Branch: ${BRANCH_NAME}"
    log "Cache Prompts: ${CACHE_PROMPTS} | Max Iteration: ${MAX_ITERATION}"
    log "=========================================================================================="
    log ""

    printf -v header "%-30s %12s %14s %12s %14s %10s" "Stage" "Pass Rate" "Passed/Total" "Stage Cost" "Cumul. Cost" "Time (s)"
    log "$header"
    log "--------------------------------------------------------------------------------------------------------------"

    for stage_key in stage1 stage2 stage3; do
        local name passed total pass_rate stage_cost cumul_cost elapsed

        name=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.name // \"---\"")
        [[ "$name" == "---" ]] && continue

        passed=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.num_passed // 0")
        total=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.num_tests // 0")
        pass_rate=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.pass_rate // 0")
        elapsed=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.elapsed_s // 0")

        if [[ "$stage_key" == "stage1" ]]; then
            stage_cost=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.cost_usd // 0")
            cumul_cost="$stage_cost"
        else
            stage_cost=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.cost_usd_incremental // 0")
            cumul_cost=$(echo "$RESULTS_JSON" | jq -r ".${stage_key}.cost_usd_cumulative // 0")
        fi

        local rate_str stage_cost_str cumul_cost_str passed_str elapsed_str
        rate_str=$(format_pct "$pass_rate")
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
# Cleanup
# ============================================================

PIPELINE_SUCCESS="false"

cleanup() {
    if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
        kill_tree "$AGENT_PID" TERM
        sleep 2
        kill_tree "$AGENT_PID" 9
    fi
    _claude_code_bridge_cleanup
}
trap cleanup EXIT
trap 'exit' INT TERM

# ============================================================
# Main
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
    log "Commit0 Java SDE-I 3-Stage Pipeline"
    log "Model:        ${MODEL_NAME} (${MODEL_SHORT})"
    if [[ -n "$DATASET_FILE" ]]; then
        log "Dataset:      ${DATASET_FILE} (custom)"
    else
        log "Dataset:      java_dataset.json (${DATASET_SHORT})"
    fi
    log "Split:        ${DATASET_SPLIT}"
    log "Repo Split:   ${REPO_SPLIT}"
    log "Branch:       ${BRANCH_NAME}"
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
        load_repos
        preflight
        ensure_spec_docs_java
        if ! verify_spec_docs_java; then
            return 1
        fi
        if ! verify_inventory_java; then
            return 1
        fi
    fi

    if [[ -n "$SKIP_TO_STAGE" ]]; then
        if [[ ! -f "$PIPELINE_LOG" ]]; then
            log "ERROR: Cannot skip to stage ${SKIP_TO_STAGE}: no prior results found at ${PIPELINE_LOG}"
            log "  Run a full pipeline first, then use --skip-to-stage."
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
}

print_pass_at_k_summary() {
    local k="$NUM_SAMPLES"
    log ""
    log "=========================================================================================="
    log "PASS@${k} SUMMARY — Java ${MODEL_SHORT} / ${DATASET_SHORT}"
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

        local sidx s1r s2r s3r s3c
        sidx=$(echo "$rj" | jq -r '.sample_index // "?"')
        s1r=$(echo "$rj" | jq -r '.stage1.pass_rate // 0')
        s2r=$(echo "$rj" | jq -r '.stage2.pass_rate // 0')
        s3r=$(echo "$rj" | jq -r '.stage3.pass_rate // 0')
        s3c=$(echo "$rj" | jq -r '.stage3.cost_usd_cumulative // .stage1.cost_usd // 0')

        local s1_pct s2_pct s3_pct cost_str
        s1_pct=$(format_pct "$s1r")
        s2_pct=$(format_pct "$s2r")
        s3_pct=$(format_pct "$s3r")
        cost_str=$(printf "\$%.2f" "$s3c")

        printf -v row "%-12s %14s %14s %14s %12s" "run_${sidx}" "$s1_pct" "$s2_pct" "$s3_pct" "$cost_str"
        log "$row"

        local is_better
        is_better=$(echo "$s3r > $best_s3_rate" | bc -l)
        if [[ "$is_better" -eq 1 ]]; then
            best_s3_rate="$s3r"
            best_s3_sample="$sidx"
        fi
    done

    log "--------------------------------------------------------------------------------------------"
    local best_pct
    best_pct=$(format_pct "$best_s3_rate")
    log "Best-of-${k} (pass@${k}):  run_${best_s3_sample}  ->  ${best_pct}"
    log "=========================================================================================="
    log ""
}

SAMPLES_COMPLETED=0

main() {
    # Load repos on first call (in case not already loaded)
    if [[ -z "$REPOS" ]]; then
        load_repos
    fi

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
main
