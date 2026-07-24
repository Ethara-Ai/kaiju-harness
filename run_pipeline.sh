#!/usr/bin/env bash
# ============================================================
# 3-Stage SDE-I Trajectory Pipeline for Commit0
# ============================================================
#
# Usage:
#     bash run_pipeline.sh --model <preset|model_id> --dataset <name>
#
# Examples:
#     bash run_pipeline.sh --model opus --dataset minitorch
#     bash run_pipeline.sh --model kimi --dataset starlette
#     bash run_pipeline.sh --model glm5 --dataset starlette
#     bash run_pipeline.sh --model minimax --dataset minitorch
#     bash run_pipeline.sh --model gpt54 --dataset minitorch --branch my-branch
#     bash run_pipeline.sh --model "bedrock/some-arn" --dataset ./custom_dataset.json --repo-split myrepo
#     bash run_pipeline.sh --model opus --dataset minitorch --num-samples 3
#     nohup bash run_pipeline.sh --model kimi --dataset minitorch > logs/kimi_minitorch.log 2>&1 &
#
# Requirements: jq, bc
# ============================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

# QC-C2-009: whitelisted .env export via the shared helper, NOT the blanket
# `set -a; source .env` which exported every .env var (incl. unrelated secrets)
# into every child process. See scripts/_load_env_whitelist.sh.
# shellcheck source=scripts/_load_env_whitelist.sh
source "${BASE_DIR}/scripts/_load_env_whitelist.sh"
source "${BASE_DIR}/scripts/_outputs_layout.sh"
source "${BASE_DIR}/scripts/_pipeline_failure.sh"
"${BASE_DIR}/scripts/generate_aider_config.sh"
REPO_BASE="${BASE_DIR}/repos"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
BACKEND="local"
MAX_ITERATION=3
export LANGUAGE="python"  # H8: parity with other drivers so child processes can rely on $LANGUAGE

# ============================================================
# Argument Parsing
# ============================================================

MODEL_ARG=""
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
USE_CLAUDE_CODE="false"

print_usage() {
    cat <<'USAGE'
Usage: run_pipeline.sh --model <preset|model_id> --dataset <name> [OPTIONS]

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
  minitorch            Uses minitorch_dataset.json, repo_split=minitorch
  starlette            Uses starlette_dataset.json, repo_split=starlette
  ./my_dataset.json    Uses custom JSON, requires --repo-split

Options:
  --branch         <name>    Override auto-generated branch name
  --repo-split     <name>    Override repo_split (required for custom dataset paths)
  --max-iteration  <n>       Max agent iterations per stage (default: 3)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled, skipped if agent active)
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap in seconds (default: 86400, 0=disable)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --backend        <name>    Backend: local or modal (default: local)
  --no-stage3-lint           Disable lint in Stage 3 (for ablation experiments)
  --no-strict-inventory      Warn (do not FATAL) when a repo's frozen test-id inventory is missing
  --num-samples    <n>       Number of independent samples to run, pass@k (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages from existing branch)
  --use-claude-code          Route Anthropic traffic through the local Claude Code OAuth bridge (uses your Claude subscription instead of an API key).
  -h, --help                 Show this help
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
        --use-claude-code) USE_CLAUDE_CODE="true"; shift ;;
        --resume)      RESUME="true"; shift ;;
        -h|--help)     print_usage ;;
        *)
            echo "Error: Unknown argument '$1'"
            echo ""
            print_usage
            ;;
    esac
done

# Propagate strict-blocking toggle (--go-crazy) to all subprocesses.
export KAIJU_GO_CRAZY="$GO_CRAZY"
export PROBE_TIMEOUT

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
    echo "  Each sample writes its own results file, so --skip-to-stage"
    echo "  cannot determine which sample's prior results to resume from."
    echo "  Run each sample individually with --skip-to-stage instead."
    exit 1
fi

# ============================================================
# Model resolution and preflight (shared across all pipelines)
# ============================================================
source "${BASE_DIR}/commit0/harness/resolve_model.sh"

resolve_model "$MODEL_ARG"

# ============================================================
# Bedrock Bearer Token Priority
# ============================================================
# When AWS_BEARER_TOKEN_BEDROCK is set for Bedrock models, unset IAM
# credentials so litellm/boto3 cannot fall back to SigV4 signing with
# an IAM user that may lack bedrock:InvokeModel permissions.

if [[ "$MODEL_NAME" == bedrock/* ]] && [[ -n "${AWS_BEARER_TOKEN_BEDROCK:-}" ]]; then
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE 2>/dev/null || true
    # Also prevent boto3 from reading ~/.aws/credentials
    export AWS_SHARED_CREDENTIALS_FILE="/dev/null"
fi

# ============================================================
# Claude Code OAuth bridge (optional --use-claude-code)
# ============================================================
source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
claude_code_maybe_start_bridge "$MODEL_NAME"

# ============================================================
# Resolve Dataset
# ============================================================

resolve_dataset() {
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
        DATASET_FILE="$arg"
        # Repo split: use override, or try to extract from filename
        if [[ -n "$REPO_SPLIT_OVERRIDE" ]]; then
            REPO_SPLIT="$REPO_SPLIT_OVERRIDE"
        else
            # Extract repo name from <name>_dataset.json pattern
            local basename
            basename=$(basename "$arg" .json)
            basename="${basename%_dataset}"
            REPO_SPLIT="$basename"
        fi
        DATASET_SHORT=$(basename "$arg" .json)
        return
    fi

    # Case 2: named dataset — look for <name>_dataset.json locally
    local candidate="${BASE_DIR}/${arg}_dataset.json"
    if [[ -f "$candidate" ]]; then
        DATASET_FILE="$candidate"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="${arg}"
        return
    fi

    # Case 3: named split from commit0 constants (e.g., "lite", "all", etc.)
    # Validate it exists in the SPLIT dict
    local known_splits
    known_splits=$("$VENV_PYTHON" -c "
from commit0.harness.constants import SPLIT
for k in sorted(SPLIT.keys()):
    print(k)
" 2>/dev/null || true)

    if echo "$known_splits" | grep -qx "$arg"; then
        DATASET_FILE="wentingzhao/commit0_combined"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="$arg"
        DATASET_SPLIT="test"
        return
    fi

    echo "Error: Cannot resolve dataset '$arg'"
    echo ""
    echo "Provide one of:"
    echo "  - A known name with a local <name>_dataset.json file"
    echo "  - A path to a .json dataset file"
    echo "  - A commit0 split name (all, lite, minitorch, etc.)"
    echo ""
    echo "Available local datasets:"
    for f in "${BASE_DIR}"/*_dataset.json; do
        [[ -f "$f" ]] && echo "  $(basename "${f}" _dataset.json)"
    done
    echo ""
    echo "Available commit0 splits:"
    echo "  $known_splits" | head -20
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


# Build the base branch name: aider-<model_short>-<dataset_short>
BASE_BRANCH_NAME="${BRANCH_OVERRIDE:-aider-${MODEL_SHORT}-${DATASET_SHORT}}"
if [[ -z "$BRANCH_OVERRIDE" ]] && [[ "$NO_STAGE3_LINT" == "true" ]]; then
    BASE_BRANCH_NAME="${BASE_BRANCH_NAME}-nolint-s3"
fi

# Base RUN_ID (without sample suffix)
# Folder structure: logs/agent/{dataset}/{model}/stage{N}/...
BASE_RUN_ID_FLAT=$(echo "${MODEL_SHORT}_${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
DATASET_DIR_NAME=$(echo "${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
MODEL_DIR_NAME=$(echo "${MODEL_SHORT}" | tr -dc 'a-zA-Z0-9._-')
if [[ "$NO_STAGE3_LINT" == "true" ]]; then
    MODEL_DIR_NAME="${MODEL_DIR_NAME}_nolint-s3"
    BASE_RUN_ID_FLAT="${BASE_RUN_ID_FLAT}_nolint-s3"
fi

# Set per-sample variables. When NUM_SAMPLES=1 no suffix is added.
# Called at the top of each sample iteration.
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

# Initialize with sample 1 defaults (used by preflight, etc.)
set_sample_vars 1

mkdir -p "$LOG_BASE"
exec > >(tee -a "$LOG_BASE/pipeline.log") 2>&1

# ============================================================
# Preflight Checks
# ============================================================

preflight() {
    local errors=0

    # timeout is used for eval and API probe (not for agent runs — watchdog handles those)
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

    if [[ ! -d "$REPO_BASE" ]]; then
        echo "Error: Repo base directory not found at $REPO_BASE"
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
    elif [[ "$MODEL_NAME" == vertex_ai/* ]]; then
        if [[ -z "${VERTEX_AI_API_KEY:-}" ]] && [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "Error: VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS required for model: $MODEL_NAME"
            errors=$((errors + 1))
        fi
        if [[ -z "${VERTEXAI_LOCATION:-}" ]]; then
            echo "Error: VERTEXAI_LOCATION not set for model: $MODEL_NAME (regional endpoints return HTTP 404; set VERTEXAI_LOCATION=global)"
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
    fi

    # Verify dataset is accessible
    if [[ "$DATASET_FILE" != wentingzhao/* ]] && [[ ! -f "$DATASET_FILE" ]]; then
        echo "Error: Dataset file not found: $DATASET_FILE"
        errors=$((errors + 1))
    fi

    # Verify the repo directory exists (for local dataset files)
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
                    echo "  Run: commit0 setup $REPO_SPLIT"
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

    # Live model API probe — send a trivial request to verify the model responds
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

# An INTENTIONAL rate-limit pause is not a hang (parity with go/rust/ts/java).
# recovery.py drops a `.rate_limit_paused` marker (re-touched each heartbeat) in
# the module log dir while it waits on the subscription cap; the raw inactivity
# watchdog would otherwise misread the pause as a stuck agent and kill it. Returns
# 0 (true) iff a FRESH such marker (mtime within $2s of now) exists under $1. The
# absolute wall-time cap still bounds an unbounded pause.
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
# Spec Doc Provisioning
# ============================================================

ensure_spec_docs() {

    log "Ensuring spec docs are available for all repos..."

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

# ============================================================
# Config Writers
# ============================================================

write_commit0_config() {
    # Use absolute path for dataset_name — relative paths break when
    # DirContext cd's into repo dirs (e.g., commit0 lint/test subprocess
    # resolves ./foo.json relative to repos/<repo>/ instead of project root).
    local ds_value
    ds_value="$(cd "$(dirname "$DATASET_FILE")" && pwd)/$(basename "$DATASET_FILE")"

    cat > "$COMMIT0_CONFIG" <<EOF
base_dir: ${REPO_BASE}
dataset_name: ${ds_value}
dataset_split: ${DATASET_SPLIT}
repo_split: ${REPO_SPLIT}
EOF
    log "  Wrote commit0 config: ${COMMIT0_CONFIG}"
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
    # QC-C2-008: use_unit_tests_info is a per-stage knob in cpp/js/rust/ts; make
    # it parameterizable here too (defaulting to the prior hardcoded false) so
    # the emitter signature is uniform and the value can no longer silently
    # drift between "hardcoded" and "per-stage" across languages.
    local use_unit_tests_info="${5:-false}"

    cat > "$AGENT_CONFIG" <<'YAMLEOF'
agent_name: aider
YAMLEOF
    cat >> "$AGENT_CONFIG" <<EOF
model_name: $(yaml_escape "${MODEL_NAME}")
model_short: $(yaml_escape "${MODEL_SHORT}")
use_user_prompt: false
user_prompt: 'Here is your task:

  You need to complete the implementations for all functions (i.e., those with pass
  statements) and pass the unit tests.

  Do not change the names of existing functions or classes, as they may be referenced
  from other code like unit tests, etc.

  When you generate code, you must maintain the original formatting of the function
  stubs (such as whitespaces), otherwise we will not able to search/replace blocks
  for code modifications, and therefore you will receive a score of 0 for your generated
  code.'
use_topo_sort_dependencies: true
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
pre_commit_config_path: .pre-commit-config.yaml
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
strip_non_stubs: ${STRIP_NON_STUBS}
inject_test_files_readonly: ${INJECT_TEST_FILES_READONLY}
EOF
    log "  Wrote agent config: ${AGENT_CONFIG}"
}

# ============================================================
# Run Agent
# ============================================================

AGENT_PID=""
AGENT_ELAPSED=0
AGENT_RC=0

# Return code contract for watchdog_run:
#   0       = agent exited successfully
#   124     = watchdog killed agent (inactivity or hard timeout)
#   128+N   = agent killed by signal N
#   other   = agent error (non-zero exit)
#   NOTE: wait returns 127 when PID is already reaped; treated as 0 (success)

# Signal a whole process group, falling back to the single PID. The agent is
# launched under `set -m` (monitor mode) so it leads its own process group;
# signalling the group (negative PID) reaps the aider/subprocess children it
# forked, instead of orphaning them to keep burning CPU/API budget after a kill.
_kill_tree() {
    local pid="$1" sig="${2:-TERM}"
    [[ -z "$pid" ]] && return 0
    kill "-${sig}" "-${pid}" 2>/dev/null \
        || kill "-${sig}" "${pid}" 2>/dev/null \
        || true
}

# Total CPU seconds consumed by the agent's process group (ported from
# run_pipeline_go.sh — the go/ts/rust pipelines gained these with the
# "stop false-killing healthy long-thinking agents" watchdog fix; the python
# pipeline lacked them, so a >inactivity-limit silent reasoning turn was
# exit-124-killed here while surviving on every other language).
_pgroup_cpu_secs() {
    # procps `ps -g <numeric>` selects by SESSION (the `set -m` agent leads a
    # process GROUP, not a session), so on Linux `ps -o time= -g $1` matches
    # nothing and the CPU veto was a silent no-op. Resolve the group's PIDs via
    # pgrep -g (process group on BOTH procps and BSD), then sum with ps -p.
    local _pids
    _pids=$(pgrep -g "$1" 2>/dev/null | paste -sd, -)
    [[ -z "$_pids" ]] && { printf "0"; return; }
    ps -o time= -p "$_pids" 2>/dev/null | awk '
        { gsub(/ /,""); n=split($0,a,":"); s=0; for(i=1;i<=n;i++) s=s*60+a[i]; t+=s }
        END { printf "%d", t+0 }'
}

# True (0) if any process in the agent's group holds an ESTABLISHED TCP
# connection — a long extended-thinking LLM turn writes no logs but keeps its
# model-API socket open. Returns 2 if lsof is unavailable (caller falls back
# to the CPU-progress check).
_pgroup_has_live_conn() {
    command -v lsof >/dev/null 2>&1 || return 2
    local pids
    pids=$(pgrep -g "$1" 2>/dev/null | paste -sd, -)
    [[ -z "$pids" ]] && return 1
    lsof -nP -a -p "$pids" -iTCP -sTCP:ESTABLISHED >/dev/null 2>&1
}

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
    local _veto_start=0  # when the live-conn/CPU gate started suppressing the inactivity kill

    # How long a live-connection / advancing-CPU agent may run WITHOUT log
    # progress before we kill it anyway. A long extended-thinking turn writes
    # NOTHING to the log until it completes (buffered SSE stream), so log
    # inactivity alone is not a hang. The absolute wall-time cap still backstops
    # a true hang. Default 90 min; override via WATCHDOG_LIVECONN_VETO_SECS.
    # (Ported from run_pipeline_go.sh for cross-language watchdog parity.)
    local _liveconn_veto_secs="${WATCHDOG_LIVECONN_VETO_SECS:-}"
    if ! [[ "$_liveconn_veto_secs" =~ ^[0-9]+$ ]] || [[ "$_liveconn_veto_secs" -lt 1 ]]; then
        _liveconn_veto_secs=$(( inactivity_limit * 6 ))
        [[ "$_liveconn_veto_secs" -lt 5400 ]] && _liveconn_veto_secs=5400
    fi

    # Validate get_mtime works before relying on it
    local _probe_mtime
    _probe_mtime=$(get_mtime "/proc/self/status")
    if [[ "$_probe_mtime" -eq 0 ]] 2>/dev/null; then
        log "  WATCHDOG: WARNING — get_mtime returned 0 for /proc/self/status. File-activity detection may be non-functional."
        mtime_functional="false"
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
                _veto_start=0  # real log progress — clear the alive-but-silent veto timer
            fi
        else
            # No logs yet (slow startup, model download, dep install) — benefit of the doubt
            agent_active="true"
            _veto_start=0
        fi

        # Absolute wall-time cap — unconditional, prevents unbounded spend
        if [[ "$absolute_max" -gt 0 ]]; then
            local wall_elapsed=$(( now_epoch - start_time ))
            if [[ $wall_elapsed -ge $absolute_max ]]; then
                log "  WATCHDOG: Absolute wall-time cap ${absolute_max}s reached. Force-killing agent."
                _kill_tree "$agent_pid" TERM
                sleep 2
                _kill_tree "$agent_pid" KILL
                wait "$agent_pid" 2>/dev/null || true
                return 124
            fi
        fi

        # Hard timeout: only kill if the agent is also inactive
        if [[ "$hard_timeout" -gt 0 ]]; then
            local elapsed=$(( now_epoch - start_time ))
            if [[ $elapsed -ge $hard_timeout ]]; then
                if [[ "$agent_active" == "true" ]]; then
                    if [[ "$hard_timeout_warned" == "false" ]]; then
                        log "  WATCHDOG: Hard timeout ${hard_timeout}s reached but agent is still active (last write ${idle}s ago). Letting it continue."
                        hard_timeout_warned="true"
                    fi
                else
                    log "  WATCHDOG: Hard timeout ${hard_timeout}s reached and agent inactive (${idle}s). Killing agent."
                    _kill_tree "$agent_pid" TERM
                    sleep 2
                    _kill_tree "$agent_pid" KILL
                    wait "$agent_pid" 2>/dev/null || true
                    return 124
                fi
            fi
        fi

        # Inactivity timeout: kill if no log writes within the limit — but NOT if
        # the agent is healthily waiting on the model or still computing locally.
        if [[ "$latest_mtime" -gt 0 ]] && [[ "$agent_active" == "false" ]]; then
            # An intentional rate-limit pause is NOT a hang. If recovery left a
            # fresh `.rate_limit_paused` marker, suppress the inactivity kill — the
            # agent is waiting on the provider, not stuck. The absolute wall-time
            # cap (checked above) still bounds an unbounded pause.
            if _pause_marker_fresh "$log_dir" "$(( inactivity_limit * 2 ))"; then
                log "  WATCHDOG: log idle ${idle}s but a fresh rate-limit pause marker is present — intentionally paused, not stuck. Continuing."
                _veto_start=0
                continue
            fi
            # Log-inactivity ALONE is not "stuck". A long server-side extended-
            # thinking turn writes no logs and burns ~0 local CPU (blocked on the
            # socket). Before killing — and wasting a paid turn — require BOTH: no
            # live LLM connection AND no local CPU progress over a short window.
            # (Ported from run_pipeline_go.sh; the python pipeline was the only
            # one still killing healthy long-thinking agents at exit 124.)
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
            log "  WATCHDOG: No log activity for ${idle}s (limit: ${inactivity_limit}s). Agent appears stuck."
            if [[ -n "$latest_log" ]] && [[ -f "$latest_log" ]]; then
                log "  WATCHDOG: Last aider log: $(basename "$(dirname "$latest_log")")"
            fi
            log "  WATCHDOG: Killing agent (PID ${agent_pid})."
            _kill_tree "$agent_pid" TERM
            sleep 2
            _kill_tree "$agent_pid" KILL
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

    local cmd=(
        "$VENV_PYTHON" -m agent run "$branch"
        --backend "$BACKEND"
        --agent-config-file "$AGENT_CONFIG"
        --commit0-config-file "$COMMIT0_CONFIG"
        --log-dir "$log_dir"
        --max-parallel-repos "$MAX_PARALLEL_REPOS"
    )

    # --no-show is not override-related, so it belongs in the BASE cmd (resume
    # rounds keep it). first_cmd adds --override-previous-changes for the FIRST
    # pass only — a resume must never re-override (it would reset the branch and
    # discard completed modules).
    cmd+=(--no-show-rich-progress)
    local first_cmd=( "${cmd[@]}" )
    if [[ "$override" == "true" ]]; then
        first_cmd+=(--override-previous-changes)
    fi

    local agent_log="${log_dir}/agent_run.log"
    log "  Running agent (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s)"
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
# Run Evaluate
# ============================================================

EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_RUNTIME="0.0"
EVAL_ELAPSED=0
EVAL_STATUS="ok"

# Preserve per-stage eval artifacts into the run's output tree — matching the
# go/rust pipelines. The eval writes test_output.txt / report.json / exit codes /
# eval.sh / patch.diff under logs/pytest/<repo>/<branch>/<hash>/, which is NOT
# under outputs/<uuid>/ and is lost on container teardown (so a crashed eval is
# undebuggable). Copied per stage so each keeps its own snapshot.
collect_eval_artifacts() {
    local stage_label="${1:-eval}"
    [[ -n "${LOG_BASE:-}" && -n "${BRANCH_NAME:-}" ]] || return 0
    local dest="${LOG_BASE}/${stage_label}_eval_artifacts"
    local bdir hdir repo out found=0
    shopt -s nullglob
    for bdir in logs/pytest/*/"${BRANCH_NAME}" logs/*_test*/*/"${BRANCH_NAME}"; do
        [[ -d "$bdir" ]] || continue
        repo=$(basename "$(dirname "$bdir")")
        for hdir in "$bdir"/*/; do
            [[ -d "$hdir" ]] || continue
            out="${dest}/${repo}"
            mkdir -p "$out"
            find "$hdir" -maxdepth 1 -type f \( \
                -name 'test_output.txt' -o -name 'test_output.json' \
                -o -name '*_exit_code.txt' -o -name 'eval.sh' \
                -o -name 'patch.diff' -o -name '*stderr*' \
                -o -name 'report.*' -o -name 'run_pytest.log' \
                \) -exec cp -f {} "$out/" \; 2>/dev/null || true
            found=1
        done
    done
    shopt -u nullglob
    [[ "$found" == "1" ]] && log "  Eval artifacts -> ${dest}" || true
    return 0
}

run_evaluate() {
    local branch="$1"
    local stage_label="${2:-eval}"

    # QC-C2-010: the outer eval --timeout is the WHOLE-eval container-exec cap
    # (build + the inner per-run test cap). All 8 pipelines derive it from ONE
    # shared var, KAIJU_EVAL_HARNESS_TIMEOUT, so an operator override applies
    # uniformly. The 1800s default is 2x the inner EVAL_TEST_TIMEOUT=900 cap
    # (spec*.py) so a slow-but-legit suite is never falsely timed out to 0.
    # Build-heavy langs (c/cpp/java compile inside the eval) keep a larger
    # default via ${KAIJU_EVAL_HARNESS_TIMEOUT:-$EVAL_TIMEOUT} — a documented,
    # test-whitelisted deviation, not silent drift.
    local cmd=(
        "$VENV_PYTHON" -m commit0 evaluate
        --branch "$branch"
        --backend "$BACKEND"
        --timeout "${KAIJU_EVAL_HARNESS_TIMEOUT:-1800}"
        --num-cpus 1
        --num-workers 1
        --commit0-config-file "$COMMIT0_CONFIG"
    )

    local eval_log="${LOG_BASE}/${stage_label}_eval.log"
    log "  Running evaluation: ${cmd[*]}"
    log "  Output → ${eval_log}"

    local start_time
    start_time=$(date +%s)

    set +e
    timeout "$EVAL_TIMEOUT" "${cmd[@]}" >"$eval_log" 2>&1
    local eval_rc=$?

    # Snapshot the eval's artifacts (patch.diff/test_output/report/eval.sh/...)
    # into the run tree before the transient logs/pytest dir is lost.
    collect_eval_artifacts "${stage_label:-eval}"
    set -e

    local end_time
    end_time=$(date +%s)
    EVAL_ELAPSED=$(( end_time - start_time ))

    log "  Evaluation finished in ${EVAL_ELAPSED}s (rc=${eval_rc})"

    local combined_output
    combined_output=$(cat "$eval_log")

    parse_eval_output "$combined_output"

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
    EVAL_STATUS="ok"

    # Aggregate across all "repo,runtime,passed/total" lines
    local total_passed=0
    local total_tests=0
    local total_runtime="0.0"
    local found_any="false"

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        # An eval infra crash (pytest produced no report.json) is NOT a real 0% —
        # flag it so the recorded score is not mistaken for a legitimate result.
        if [[ "$line" == PYTEST_INFRA_ERROR,* ]]; then
            EVAL_STATUS="$(echo "$line" | cut -d',' -f3 | tr -d ' ')"
            log "  WARNING: eval infra crash (${EVAL_STATUS}) — score is NOT a real 0% (see eval log)"
            continue
        fi
        # Frozen inventory matched ZERO collected nodeids though tests ran — a
        # nodeid/rootdir format mismatch (or stubbed-base ImportError). The 0%
        # is NOT real; flag it so a stale/mis-formatted inventory never records
        # a silent false 0/N. Format: INVENTORY_MISMATCH,<repo>,<inv>,<report>,<passed>
        if [[ "$line" == INVENTORY_MISMATCH,* ]]; then
            EVAL_STATUS="inventory_mismatch"
            log "  WARNING: inventory<->report nodeid mismatch ($line) — score is NOT a real 0%; regenerate the frozen inventory (delete commit0/data/<subdir>/<repo>.bz2)"
            continue
        fi
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
# Cost Extraction
# ============================================================

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
print(f"{fallback_total:.4f} aider_fallback:{fallback_count}" if fallback_count else f"{fallback_total:.4f} parse_error")
PYEOF
) || true
    # result is "<cost> <source>"; validate the cost token, keep the source.
    if [[ "$result" =~ ^[0-9]+\.[0-9]+[[:space:]] ]]; then
        echo "$result"
    else
        echo "0.0000 parse_error"
    fi
}

format_pct() {
    local val="$1"
    printf "%.1f%%" "$(echo "$val * 100" | bc)"
}

# ============================================================
# JSON Results
# ============================================================

RESULTS_JSON=""

init_results() {
    # B4 audit fix: record both MODEL_SHORT (dashboard alias) AND MODEL_NAME (full
    # provider model id). Prior schema only recorded model_short which loses fidelity
    # when the alias maps to different provider models across runs (e.g. opus47cc →
    # claude-opus-4-7-20241022 vs claude-opus-4-7-20250501). model_name preserves
    # the exact provider model id so mixed-model batches can be de-conflated in eval.
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
        '{
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
    # Write atomically via a temp file and ONLY promote it if it is valid,
    # non-empty JSON. A jq failure or an empty RESULTS_JSON must never truncate
    # an already-good pipeline_results.json to 0 bytes (that loses all stage
    # results and zeroes ATIF rewards).
    local _tmp="${PIPELINE_LOG}.tmp.$$"
    if echo "$RESULTS_JSON" | jq '.' > "$_tmp" 2>/dev/null && [[ -s "$_tmp" ]]; then
        mv -f "$_tmp" "$PIPELINE_LOG"
    else
        rm -f "$_tmp"
        log "ERROR: save_results refused to write empty/invalid JSON to ${PIPELINE_LOG} (kept prior file)"
        return 1
    fi
}

# ============================================================
# Pipeline Stages
# ============================================================

stage_1_draft() {
    log "======================================================================"
    log "STAGE 1: Draft Initial Implementations"
    log "======================================================================"

    write_agent_config "false" "false" "false" "true"

    local stage_log_dir="${LOG_BASE}/stage1_draft"
    mkdir -p "$stage_log_dir"

    run_agent "$BRANCH_NAME" "true" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local cost cost_source _co
    _co=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 1 cost extraction failed"; return 1; }
    cost="${_co%% *}"; cost_source="${_co#* }"
    log "  Stage 1 cost: \$${cost} (source: ${cost_source})"

    if _agent_crashed_pre_llm "$stage_log_dir" "$rc"; then
        log "  Stage 1 AGENT CRASHED PRE-LLM (rc=${rc}, no artifacts under ${stage_log_dir}). Skipping evaluate; see agent_run.log."
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
            --arg name "Draft (no feedback)" \
            --argjson elapsed "$elapsed" \
            --argjson cost "$cost" \
            --arg cost_source "$cost_source" \
            --argjson rc "$rc" \
            '.stage1 = {
                name: $name,
                elapsed_s: $elapsed,
                eval_time_s: 0,
                cost_usd: $cost,
                cost_source: $cost_source,
                returncode: $rc,
                runtime: 0,
                num_passed: 0,
                num_tests: 0,
                pass_rate: 0,
                eval_status: "not_run",
                sample_failed: true,
                failure_reason: "agent_crashed_pre_llm"
            }')
        save_results
        return 1
    fi

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
        --arg eval_status "${EVAL_STATUS:-ok}" \
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
    log "STAGE 2: Refine with Static Analysis (Lint)"
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

    if _agent_crashed_pre_llm "$stage_log_dir" "$rc"; then
        log "  Stage 2 AGENT CRASHED PRE-LLM (rc=${rc}, no artifacts under ${stage_log_dir}). Skipping evaluate; see agent_run.log."
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
            --arg name "Lint refine" \
            --argjson elapsed "$elapsed" \
            --argjson cost_inc "$s2_incremental" \
            --argjson cost_cum "$total_cost" \
            --arg cost_source "$cost_source" \
            --argjson rc "$rc" \
            '.stage2 = {
                name: $name,
                elapsed_s: $elapsed,
                eval_time_s: 0,
                cost_usd_incremental: $cost_inc,
                cost_usd_cumulative: $cost_cum,
                cost_source: $cost_source,
                returncode: $rc,
                runtime: 0,
                num_passed: 0,
                num_tests: 0,
                pass_rate: 0,
                eval_status: "not_run",
                sample_failed: true,
                failure_reason: "agent_crashed_pre_llm"
            }')
        save_results
        return 1
    fi

    run_evaluate "$BRANCH_NAME" "stage2"
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
        --arg eval_status "${EVAL_STATUS:-ok}" \
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
    log "STAGE 3: Refine with Unit Test Feedback"
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

    if _agent_crashed_pre_llm "$stage_log_dir" "$rc"; then
        log "  Stage 3 AGENT CRASHED PRE-LLM (rc=${rc}, no artifacts under ${stage_log_dir}). Skipping evaluate; see agent_run.log."
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
            --arg name "Test refine" \
            --argjson elapsed "$elapsed" \
            --argjson cost_inc "$s3_incremental" \
            --argjson cost_cum "$total_cost" \
            --arg cost_source "$cost_source" \
            --argjson rc "$rc" \
            '.stage3 = {
                name: $name,
                elapsed_s: $elapsed,
                eval_time_s: 0,
                cost_usd_incremental: $cost_inc,
                cost_usd_cumulative: $cost_cum,
                cost_source: $cost_source,
                returncode: $rc,
                runtime: 0,
                num_passed: 0,
                num_tests: 0,
                pass_rate: 0,
                eval_status: "not_run",
                sample_failed: true,
                failure_reason: "agent_crashed_pre_llm"
            }')
        save_results
        return 1
    fi

    run_evaluate "$BRANCH_NAME" "stage3"
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
        --arg eval_status "${EVAL_STATUS:-ok}" \
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
    log "RESULTS SUMMARY — SDE-I 3-Stage Pipeline"
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
        kill -- -"$AGENT_PID" 2>/dev/null || true
        sleep 2
        kill -9 -- -"$AGENT_PID" 2>/dev/null || true
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
trap 'exit' INT TERM

verify_spec_docs() {

    log "Verifying all repos have spec docs..."

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
        repo_list=$("$VENV_PYTHON" -c "
from commit0.harness.constants import SPLIT
for r in sorted(SPLIT.get('${REPO_SPLIT}', [])):
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
        log "FATAL: ${missing} repo(s) missing spec docs (use_spec_info=true)."
        log "  The pipeline requires spec docs for all repos."
        log "  Missing repos:"
        echo -e "$missing_repos" | while IFS= read -r line; do [[ -n "$line" ]] && log "$line"; done
        log ""
        log "  Options:"
        log "    1. Place spec.pdf or spec.pdf.bz2 in each repo directory"
        log "    2. Add 'specification' URLs to the dataset JSON and re-run"
        log "======================================================================"
        return 1
    fi

    log "  All repos have spec docs. ✓"
}

# Frozen test-id inventory gate (mirrors verify_spec_docs). A missing inventory
# makes the eval SILENTLY score against ALL discovered tests — a wrong, non-
# reproducible denominator. Resolved with the SAME function the eval uses
# (kaiju.verify_inventory -> find_test_ids_file). FATAL by default;
# --no-strict-inventory (or KAIJU_REQUIRE_INVENTORY=0) downgrades to warn-only.
verify_inventory_python() {
    log "Verifying all repos have a frozen test-id inventory..."
    local strict_flag="--strict"
    [[ "$STRICT_INVENTORY" != "true" ]] && strict_flag="--no-strict"
    local ds_arg=()
    [[ -n "${DATASET_FILE:-}" ]] && ds_arg=(--dataset "$DATASET_FILE")
    local split_arg=()
    [[ -n "${REPO_SPLIT:-}" ]] && split_arg=(--repo-split "$REPO_SPLIT")
    "$VENV_PYTHON" -m kaiju.verify_inventory --language python \
        "${ds_arg[@]}" "${split_arg[@]}" "$strict_flag"
}

# ============================================================
# Main
# ============================================================

# All per-sample pipeline result files, collected for pass@k aggregation
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
    log "Commit0 SDE-I 3-Stage Pipeline"
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

    write_commit0_config

    if [[ "$sample_idx" -eq 1 ]]; then
        ensure_spec_docs
        if ! verify_spec_docs; then
            return 1
        fi
        if ! verify_inventory_python; then
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

    # NB: 'end' is a reserved keyword in jq (the older jq in the container image
    # rejects '$end' as a syntax error, which silently empties RESULTS_JSON and
    # truncates pipeline_results.json to 0 bytes). Use a non-reserved var name.
    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq --arg endts "$(ts)" '.end_time = $endts')

    print_summary_table
    save_results
    log "run_${sample_idx} results saved to: ${PIPELINE_LOG}"

    SAMPLE_RESULT_FILES+=("$PIPELINE_LOG")
    if [[ -x "${BASE_DIR}/.venv/bin/python" ]]; then
        # ATIF output goes to a PER-EXPERIMENT Harbor_Data dir so each run's
        # converted trajectory is self-contained under outputs/<uuid>/ (next to
        # runs/, configs/, datasets/). Non-consolidated layouts have no per-uuid
        # experiment dir, so fall back to the shared top-level path.
        if is_consolidated; then
            _HARBOR_TRAJ_OUT="$(experiment_dir "$DATASET_UUID")/Harbor_Data/Trajectory"
        else
            _HARBOR_TRAJ_OUT="${BASE_DIR}/Harbor_Data/Trajectory"
        fi
        "${BASE_DIR}/.venv/bin/python" "${BASE_DIR}/scripts/commit0_to_atif_v2.py" \
            "$LOG_BASE" \
            "$_HARBOR_TRAJ_OUT" \
            --kaiju-mode \
            --pipeline "$PIPELINE_LOG" \
            --task-name "$DATASET_DIR_NAME" \
            && log "ATIF conversion complete for run_${sample_idx} -> ${_HARBOR_TRAJ_OUT}" \
            || log "[WARN] ATIF conversion failed for run_${sample_idx}"
        # Mirror into the shared top-level Harbor_Data/Trajectory (accumulates
        # across runs; kept for existing consumers — run_pipeline_containerized
        # reporting + fallback copy-back read this path).
        if is_consolidated && [[ -d "$_HARBOR_TRAJ_OUT" ]]; then
            mkdir -p "${BASE_DIR}/Harbor_Data/Trajectory"
            cp -R "$_HARBOR_TRAJ_OUT/." "${BASE_DIR}/Harbor_Data/Trajectory/" 2>/dev/null \
                && log "Mirrored ATIF trajectory -> ${BASE_DIR}/Harbor_Data/Trajectory" \
                || log "[WARN] could not mirror ATIF trajectory to top-level Harbor_Data"
        fi
    fi
    [[ -z "$pipeline_error" ]] || return 1
}

print_pass_at_k_summary() {
    local k="$NUM_SAMPLES"
    log ""
    log "=========================================================================================="
    log "PASS@${k} SUMMARY — ${MODEL_SHORT} / ${DATASET_SHORT}"
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
    log "Best-of-${k} (pass@${k}):  run_${best_s3_sample}  →  ${best_pct}"
    log "=========================================================================================="
    log ""
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

[[ "$PIPELINE_SUCCESS" == "true" ]] || exit 1