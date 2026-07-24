#!/usr/bin/env bash
# ============================================================
# 3-Stage SDE-I Trajectory Pipeline for Commit0 — Rust
# ============================================================
#
# Usage:
#     bash run_pipeline_rust.sh --model <preset|model_id> --dataset <name>
#
# Examples:
#     bash run_pipeline_rust.sh --model opus --dataset uom_rust
#     bash run_pipeline_rust.sh --model kimi --dataset tide_rust
#     bash run_pipeline_rust.sh --model opus --dataset ./uom_rust_dataset.json
#     bash run_pipeline_rust.sh --model opus --dataset uom_rust --num-samples 3
#     nohup bash run_pipeline_rust.sh --model opus --dataset uom_rust > logs/opus_uom_rust.log 2>&1 &
#
# Requirements: jq, bc
# ============================================================

set -euo pipefail

# C13: refuse to run with xtrace on. The whitelisted `.env` loader still exports
# AWS_BEARER_TOKEN_BEDROCK / ANTHROPIC_API_KEY / OPENAI_API_KEY into every child;
# with `set -x` those (and the full agent command line) get echoed to logs. A
# 2000-line script is often debugged with `bash -x` — fail closed instead.
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
source "${BASE_DIR}/scripts/_pipeline_failure.sh"
"${BASE_DIR}/scripts/generate_aider_config.sh"
REPO_BASE="${BASE_DIR}/repos"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
BACKEND="local"
MAX_ITERATION=3  # H6: aligned with go/c/cpp/java/js/ts (was 1 → pass@1 vs pass@3 methodology divergence). Override via --max-iteration.

# Rust pipeline — spec info enabled by default (use --no-spec-info to disable)
export LANGUAGE="rust"
USE_SPEC_INFO="true"
USE_UNIT_TESTS_INFO="true"
REPO_MAP_TOKENS=1024
STRIP_AUX_DOCS="false"
BLIND_LINT="false"
BLIND_TESTS="false"
STRIP_NON_STUBS="false"
# Test-SOURCE files are NOT injected into the agent prompt by default (QC): reading
# the exact test bodies is answer-leakage (the model reverse-engineers expected
# values) AND on test-heavy repos it ballooned the prompt to ~315k tokens -> empty
# completions. The agent still RUNS the tests and sees SUMMARIZED results
# (max_test_output_length), and it can NEVER edit test files (GuardedInputOutput
# protected_paths is independent of this flag). Opt back in with --test-files-readonly.
INJECT_TEST_FILES_READONLY="false"
NAMES_ONLY_TESTS="false"
PER_EDIT_COMPILE_GATE="false"
COMPILE_GATE_MAX_RETRIES=2
# Fix #3: Stage 2 -> Stage 3 gate (default ON per user instruction).
# After Stage 2 ends, run `cargo check` on the agent's branch tree. If it does
# NOT compile, skip Stage 3 entirely (running `cargo test` on a broken tree is
# wasted budget) and record STAGE_2_BROKE_TREE in the result.
STAGE3_SKIP_IF_BROKEN="true"
# Fix #4: Quality-aware watchdog (kill if compile-error count is rising).
# Default OFF to preserve existing run semantics; enable via --quality-watchdog.
QUALITY_WATCHDOG="false"
QUALITY_WATCHDOG_INTERVAL=90       # seconds between cargo check samples
QUALITY_WATCHDOG_RISING=3          # consecutive rising samples before kill
QUALITY_WATCHDOG_MIN_DELTA=5       # minimum error increase per sample to count as 'rising'

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
GO_CRAZY="false"
# B5 audit fix: tunable model-preflight probe timeout (default 120s). Bump to
# 300+ for slow-start bridges (multi-account pool, cold Docker network) so
# preflight doesn't abort the whole run on transient upstream slowness.
PROBE_TIMEOUT="${PROBE_TIMEOUT:-120}"
STRICT_INVENTORY="true"
INACTIVITY_TIMEOUT=900
MAX_WALL_TIME=86400
SKIP_TO_STAGE=""
RESUME="false"
NUM_SAMPLES=1
MAX_TEST_OUTPUT_LENGTH=15000
MAX_PARALLEL_REPOS=1
INFRA_BROKEN="false"   # C16: set true if the docker image build fails

print_usage() {
    cat <<'USAGE'
Usage: run_pipeline_rust.sh --model <preset|model_id> --dataset <name> [OPTIONS]

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
  uom_rust             Uses uom_rust_dataset.json, repo_split=uom_rust
  tide_rust            Uses tide_rust_dataset.json, repo_split=tide_rust
  ./my_dataset.json    Uses custom JSON, requires --repo-split

Options:
  --branch         <name>    Override auto-generated branch name
  --repo-split     <name>    Override repo_split (required for custom dataset paths)
  --max-iteration  <n>       Max agent iterations per stage (default: 1)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled, skipped if agent active)
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap in seconds (default: 86400, 0=disable)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --backend        <name>    Backend: local or modal (default: local)
  --no-spec-info             Disable spec/paper injection (default: enabled)
  --no-strict-inventory      Warn (do not FATAL) when a repo's frozen test-id inventory is missing
  --no-unit-tests-info       Disable inline-test injection into prompt (default: enabled; Stage 1 only)
  --no-repo-map              Disable aider's internal repo-map (default: enabled, map_tokens=1024)
  --strip-aux-docs           Hide README/CHANGELOG/HISTORY/etc. from agent's view (default: keep)
  --blind-lint               Stage 2 sees only "build failed: N errors" (default: full clippy output)
  --blind-tests              Stage 3 sees only summary line, no per-test failures (default: full output)
  --strip-non-stubs          Hide non-stubbed source files from agent context (default: visible)
  --names-only-tests         Stage 3 shows only failed test names, not tracebacks (default: full output)
  --no-test-files-readonly   Disable test-source read-only context (now the DEFAULT)
  --test-files-readonly           Inject test SOURCE as read-only context (opt-in; leaky)
  --no-stage3-lint           Disable lint in Stage 3 (for ablation experiments)
  --per-edit-compile-gate    Run `cargo check` after each aider edit; revert + retry on regression (default: off)
  --compile-gate-max-retries <n>  Retries before reverting a module (default: 2)
  --no-stage3-skip-if-broken Always run Stage 3 even if tree doesn't compile (default: skip Stage 3 if broken)
  --quality-watchdog         Kill agent if `cargo check` errors are trending up (default: off)
  --quality-watchdog-interval <s>  Seconds between samples (default: 90)
  --quality-watchdog-rising <n>    Consecutive rising samples to trigger kill (default: 3)
  --quality-watchdog-min-delta <n> Minimum error increase per sample to count as rising (default: 5)
  --num-samples    <n>       Number of independent samples to run, pass@k (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages from existing branch)
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
        --go-crazy) GO_CRAZY="true"; shift ;;
        --preflight-timeout) [[ $# -lt 2 ]] && { echo "Error: --preflight-timeout requires a value (seconds)"; exit 1; }; PROBE_TIMEOUT="$2"; shift 2 ;;
        --no-spec-info) USE_SPEC_INFO="false"; shift ;;
        --no-strict-inventory) STRICT_INVENTORY="false"; shift ;;
        --no-unit-tests-info) USE_UNIT_TESTS_INFO="false"; shift ;;
        --no-repo-map) REPO_MAP_TOKENS=0; shift ;;
        --strip-aux-docs) STRIP_AUX_DOCS="true"; shift ;;
        --blind-lint) BLIND_LINT="true"; shift ;;
        --blind-tests) BLIND_TESTS="true"; shift ;;
        --strip-non-stubs) STRIP_NON_STUBS="true"; shift ;;
        --names-only-tests) NAMES_ONLY_TESTS="true"; shift ;;
        --no-test-files-readonly) INJECT_TEST_FILES_READONLY="false"; shift ;;
        --test-files-readonly) INJECT_TEST_FILES_READONLY="true"; shift ;;
        --per-edit-compile-gate) PER_EDIT_COMPILE_GATE="true"; shift ;;
        --compile-gate-max-retries) [[ $# -lt 2 ]] && { echo "Error: --compile-gate-max-retries requires a value"; exit 1; }; COMPILE_GATE_MAX_RETRIES="$2"; shift 2 ;;
        --no-stage3-skip-if-broken) STAGE3_SKIP_IF_BROKEN="false"; shift ;;
        --quality-watchdog) QUALITY_WATCHDOG="true"; shift ;;
        --quality-watchdog-interval) [[ $# -lt 2 ]] && { echo "Error: --quality-watchdog-interval requires a value"; exit 1; }; QUALITY_WATCHDOG_INTERVAL="$2"; shift 2 ;;
        --quality-watchdog-rising) [[ $# -lt 2 ]] && { echo "Error: --quality-watchdog-rising requires a value"; exit 1; }; QUALITY_WATCHDOG_RISING="$2"; shift 2 ;;
        --quality-watchdog-min-delta) [[ $# -lt 2 ]] && { echo "Error: --quality-watchdog-min-delta requires a value"; exit 1; }; QUALITY_WATCHDOG_MIN_DELTA="$2"; shift 2 ;;
        --inactivity-timeout) [[ $# -lt 2 ]] && { echo "Error: --inactivity-timeout requires a value"; exit 1; }; INACTIVITY_TIMEOUT="$2"; shift 2 ;;
        --max-wall-time) [[ $# -lt 2 ]] && { echo "Error: --max-wall-time requires a value"; exit 1; }; MAX_WALL_TIME="$2"; shift 2 ;;
        --num-samples) [[ $# -lt 2 ]] && { echo "Error: --num-samples requires a value"; exit 1; }; NUM_SAMPLES="$2"; shift 2 ;;
        --skip-to-stage) [[ $# -lt 2 ]] && { echo "Error: --skip-to-stage requires a value"; exit 1; }; SKIP_TO_STAGE="$2"; shift 2 ;;
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

# Validate the remaining numeric args up front so a non-numeric value fails with
# a clear message instead of a confusing `[[: integer expression expected` or a
# jq parse error deep inside a stage.
for _nv in \
    "--max-iteration:$MAX_ITERATION" \
    "--stage-timeout:$STAGE_TIMEOUT" \
    "--eval-timeout:$EVAL_TIMEOUT" \
    "--inactivity-timeout:$INACTIVITY_TIMEOUT" \
    "--max-wall-time:$MAX_WALL_TIME" \
    "--compile-gate-max-retries:$COMPILE_GATE_MAX_RETRIES"; do
    _flag="${_nv%%:*}"; _val="${_nv#*:}"
    if ! [[ "$_val" =~ ^[0-9]+$ ]]; then
        echo "Error: ${_flag} must be a non-negative integer (got: '${_val}')"
        exit 1
    fi
done

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

# resolve_model is expected to export CACHE_PROMPTS; guard with a default so a
# future change there can't abort the run with an unbound-variable error under
# `set -u` far from the cause.
: "${CACHE_PROMPTS:=true}"

# ============================================================
# Claude Code OAuth bridge (optional --use-claude-code)
# ============================================================
source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
claude_code_maybe_start_bridge "$MODEL_NAME"

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
            # Extract repo name from <name>_rust_dataset.json pattern
            # Strip _dataset suffix, then strip _rust suffix so repo_split
            # matches the actual repo name (e.g. "grex" not "grex_rust").
            local basename
            basename=$(basename "$arg" .json)
            basename="${basename%_dataset}"
            basename="${basename%_rust}"
            REPO_SPLIT="$basename"
        fi
        DATASET_SHORT=$(basename "$arg" .json)
        return
    fi

    # Case 2: named dataset — look for <name>_dataset.json locally
    local candidate="${BASE_DIR}/${arg}_dataset.json"
    if [[ -f "$candidate" ]]; then
        DATASET_FILE="$candidate"
        local _rs="${REPO_SPLIT_OVERRIDE:-$arg}"
        _rs="${_rs%_rust}"
        REPO_SPLIT="$_rs"
        DATASET_SHORT="${arg}"
        return
    fi

    # Case 3: named split from commit0 Rust constants
    # NOTE: No Rust-specific HuggingFace dataset exists yet.
    # RUST_SPLIT keys are recognised so we can give a helpful error,
    # but the user must supply a local *_dataset.json file instead.
    local known_splits
    known_splits=$("$VENV_PYTHON" -c "
from commit0.harness.constants_rust import RUST_SPLIT
for k in sorted(RUST_SPLIT.keys()):
    print(k)
" 2>/dev/null || true)

    if echo "$known_splits" | grep -qx "$arg"; then
        echo "Error: Split '$arg' is a known Rust split, but no Rust HuggingFace dataset exists."
        echo "Please provide a local dataset JSON file instead."
        echo ""
        echo "Example: bash run_pipeline_rust.sh --model opus --dataset ./uom_rust_dataset.json --repo-split $arg"
        exit 1
    fi

    echo "Error: Cannot resolve dataset '$arg'"
    echo ""
    echo "Provide one of:"
    echo "  - A known name with a local <name>_dataset.json file"
    echo "  - A path to a .json dataset file"
    echo "  - A commit0 Rust split name"
    echo ""
    echo "Available local datasets:"
    for f in "${BASE_DIR}"/*_rust_dataset.json "${BASE_DIR}"/*_dataset.json; do
        [[ -f "$f" ]] && echo "  $(basename "${f}" _dataset.json)"
    done
    echo ""
    echo "Available Rust splits:"
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
BASE_BRANCH_NAME="${BRANCH_OVERRIDE:-aider-rust-${MODEL_SHORT}-${DATASET_SHORT}}"
if [[ -z "$BRANCH_OVERRIDE" ]] && [[ "$NO_STAGE3_LINT" == "true" ]]; then
    BASE_BRANCH_NAME="${BASE_BRANCH_NAME}-nolint-s3"
fi

# Base RUN_ID (without sample suffix)
# Folder structure: logs/agent/{dataset}/{model}/stage{N}/...
BASE_RUN_ID_FLAT=$(echo "rust_${MODEL_SHORT}_${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
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

    # Auto-detect Docker CLI on macOS where Docker Desktop ships the binary at
    # /Applications/Docker.app/Contents/Resources/bin/docker but the symlink
    # in /usr/local/bin can become stale (points to an unmounted DMG path).
    # Without this, eval + build steps fail with 'docker: command not found'
    # even though Docker Desktop is fully installed and running.
    if ! command -v docker &>/dev/null; then
        local _docker_app_bin="/Applications/Docker.app/Contents/Resources/bin"
        if [[ -x "$_docker_app_bin/docker" ]]; then
            export PATH="$_docker_app_bin:$PATH"
            echo "  Docker CLI not on PATH; auto-resolved to $_docker_app_bin/docker"
        fi
    fi

    # timeout is used for eval and API probe (not for agent runs — watchdog handles those)
    # Item 10: cargo+rustc on PATH so toolchain issues surface in preflight, not mid-stage.
    # In-container: docker itself is not present (and not needed — eval uses the
    # local_inplace worktree backend, the repo image is the sandbox).
    local _required_cmds=(jq bc timeout cargo rustc docker)
    if [[ "${KAIJU_IN_CONTAINER:-0}" == "1" ]]; then
        _required_cmds=(jq bc timeout cargo rustc)
    fi
    for cmd in "${_required_cmds[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            echo "Error: Required command '$cmd' not found"
            errors=$((errors + 1))
        fi
    done

    # Docker DAEMON liveness check. CLI presence alone is insufficient — on macOS the
    # Docker socket is missing until Docker Desktop is actively running, and silent
    # failure here causes pipelines to waste LLM budget on no-op eval cycles.
    # Skipped in-container (no docker daemon; eval runs via local_inplace).
    if [[ "${KAIJU_IN_CONTAINER:-0}" != "1" ]] && command -v docker &>/dev/null; then
        if ! docker info &>/dev/null; then
            echo "Error: Docker daemon not reachable. Start Docker Desktop and retry."
            echo "       (docker info returned non-zero; socket likely missing)"
            errors=$((errors + 1))
        fi
    fi

    # Item 10: probe toolchain versions; surface mismatch before agent runs.
    if command -v cargo &>/dev/null && command -v rustc &>/dev/null; then
        local cargo_v rustc_v
        cargo_v=$(cargo --version 2>/dev/null | awk '{print $2}')
        rustc_v=$(rustc --version 2>/dev/null | awk '{print $2}')
        if [[ -z "$cargo_v" || -z "$rustc_v" ]]; then
            echo "Error: cargo/rustc found but --version probe failed (cargo='$cargo_v' rustc='$rustc_v')"
            errors=$((errors + 1))
        else
            echo "  Toolchain: rustc $rustc_v, cargo $cargo_v"
            if [[ -n "${RUST_VERSION:-}" ]] && [[ "$RUST_VERSION" != "stable" ]]; then
                if [[ "$rustc_v" != "$RUST_VERSION"* ]]; then
                    echo "Warning: rustc version '$rustc_v' does not match pinned RUST_VERSION='$RUST_VERSION'."
                    echo "  Use 'rustup default $RUST_VERSION' for reproducible runs."
                fi
            fi
        fi
    fi

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

# B15: an INTENTIONAL rate-limit pause is not a hang. The recovery layer drops a
# `.rate_limit_paused` marker and re-touches it each heartbeat while it waits on
# the subscription cap to reset. The watchdog treats a FRESH marker as a legit
# pause and suppresses the inactivity kill (the absolute wall-time cap, checked
# earlier each loop, still bounds an unbounded pause). This replaces recovery's
# old approach of forging mtime on agent_run.log/aider.log — which masked REAL
# hangs because the watchdog couldn't distinguish a pause from a wedge.
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

# ============================================================
# Spec Doc Provisioning
# ============================================================

ensure_spec_docs_rust() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        log "  Spec docs disabled — skipping."
        return 0
    fi

    log "Ensuring spec docs are available for all Rust repos..."

    local ensure_log="${BASE_DIR}/logs/ensure_specs.log"
    mkdir -p "$(dirname "$ensure_log")"
    "$VENV_PYTHON" - "$DATASET_FILE" "$REPO_BASE" "$BASE_DIR" <<'PYEOF' | tee "$ensure_log"
import json, os, sys, shutil
from pathlib import Path

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

specs_dir = os.path.join(base_dir, "specs_rust")
os.makedirs(specs_dir, exist_ok=True)

failed_repos = []  # tracks per-repo spec provisioning failures (Item 6)

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

    spec_ref = None
    setup = entry.get("setup", {})
    if isinstance(setup, dict):
        spec_ref = setup.get("specification")
    if not spec_ref:
        print(f"  SKIP {repo_name}: no specification in dataset entry")
        continue

    cached_bz2 = os.path.join(specs_dir, f"{repo_name}.pdf.bz2")
    cached_pdf = os.path.join(specs_dir, f"{repo_name}.pdf")

    if spec_ref and not spec_ref.startswith("http"):
        local_path = os.path.join(specs_dir, spec_ref)
        if os.path.exists(local_path):
            shutil.copy2(local_path, bz2_in_repo if local_path.endswith(".bz2") else pdf_in_repo)
            print(f"  OK   {repo_name}: copied local spec from {local_path}")
            continue

    if os.path.exists(cached_bz2):
        shutil.copy2(cached_bz2, bz2_in_repo)
        print(f"  OK   {repo_name}: copied cached spec from {cached_bz2}")
        continue
    if os.path.exists(cached_pdf):
        shutil.copy2(cached_pdf, pdf_in_repo)
        print(f"  OK   {repo_name}: copied cached spec from {cached_pdf}")
        continue

    if spec_ref.startswith("http"):
        print(f"  SCRAPE {repo_name}: {spec_ref}")
        try:
            from tools.scrape_pdf import scrape_spec
            result = scrape_spec(
                base_url=spec_ref,
                name=repo_name,
                output_dir=specs_dir,
                compress=True,
            )
            if result and os.path.exists(result):
                shutil.copy2(result, bz2_in_repo)
                print(f"  OK   {repo_name}: scraped and placed spec.pdf.bz2")
            else:
                print(f"  WARN {repo_name}: scrape returned no output")
                failed_repos.append(repo_name)
        except Exception as e:
            print(f"  WARN {repo_name}: scrape failed: {e}")
            failed_repos.append(repo_name)
    else:
        print(f"  WARN {repo_name}: spec '{spec_ref}' not found in {specs_dir}")
        failed_repos.append(repo_name)

if failed_repos:
    print(f"[ENSURE_SPECS_SUMMARY] failures={len(failed_repos)} repos={','.join(failed_repos)}")
else:
    print("[ENSURE_SPECS_SUMMARY] failures=0")


PYEOF
    local rc=${PIPESTATUS[0]}
    if [[ $rc -ne 0 ]]; then
        log "  WARNING: Spec doc provisioning had errors (rc=$rc) — continuing anyway."
    fi
    # Surface per-repo failures from the Python summary line (Item 6)
    local summary_line
    summary_line=$(grep '\[ENSURE_SPECS_SUMMARY\]' "$ensure_log" 2>/dev/null | tail -n1 || true)
    if [[ -n "$summary_line" ]] && ! echo "$summary_line" | grep -q 'failures=0'; then
        log "  ⚠  Spec provisioning failures detected — see verify_spec_docs_rust for fatal gate."
        log "  ${summary_line}"
    fi
}

verify_spec_docs_rust() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        return 0
    fi

    log "Verifying all Rust repos have spec docs..."

    local missing=0
    local missing_repos=""

    local repo_list
    if [[ -f "$DATASET_FILE" ]]; then
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
        # N9 injection close: REPO_SPLIT was interpolated directly into the
        # Python literal (RUST_SPLIT.get('${REPO_SPLIT}', [])), letting a value
        # containing quotes / newlines / `);import os;os.system(...)` execute
        # arbitrary code in the harness venv. Pass via env var so Python reads
        # it as opaque data — same pattern used at line 757 for DATASET_FILE.
        repo_list=$(_PIPELINE_REPO_SPLIT="$REPO_SPLIT" "$VENV_PYTHON" -c "
import os
from commit0.harness.constants_rust import RUST_SPLIT
for r in sorted(RUST_SPLIT.get(os.environ['_PIPELINE_REPO_SPLIT'], [])):
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
        log "FATAL: ${missing} Rust repo(s) missing spec docs (use_spec_info=true)."
        log "  The pipeline requires spec docs for all repos when use_spec_info"
        log "  is true (default). Missing repos:"
        echo -e "$missing_repos" | while IFS= read -r line; do [[ -n "$line" ]] && log "$line"; done
        log ""
        log "  Options:"
        log "    1. Place spec.pdf or spec.pdf.bz2 in each repo directory"
        log "    2. Add 'specification' entries to the dataset JSON and re-run"
        log "    3. Pass --no-spec-info to run without spec context"
        log "======================================================================"
        return 1
    fi

    log "  All Rust repos have spec docs. ✓"
}

# Frozen test-id inventory gate (mirrors verify_spec_docs_rust). A missing
# inventory makes the eval SILENTLY score against ALL discovered tests — a
# wrong, non-reproducible denominator. Resolved with the SAME function the eval
# uses (kaiju.verify_inventory -> find_test_ids_file). FATAL by default;
# --no-strict-inventory (or KAIJU_REQUIRE_INVENTORY=0) downgrades to warn-only.
verify_inventory_rust() {
    log "Verifying all Rust repos have a frozen test-id inventory..."
    local strict_flag="--strict"
    [[ "$STRICT_INVENTORY" != "true" ]] && strict_flag="--no-strict"
    local ds_arg=()
    [[ -n "${DATASET_FILE:-}" ]] && ds_arg=(--dataset "$DATASET_FILE")
    local split_arg=()
    [[ -n "${REPO_SPLIT:-}" ]] && split_arg=(--repo-split "$REPO_SPLIT")
    "$VENV_PYTHON" -m kaiju.verify_inventory --language rust \
        "${ds_arg[@]}" "${split_arg[@]}" "$strict_flag"
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
language: rust
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
    local use_unit_tests_info="$4"
    local add_import_module_to_context="$5"
    local use_spec_info="${6:-false}"

    local user_prompt='Here is your task:

  You need to complete the implementations for all functions (i.e., those with
  panic!("STUB: not implemented") markers) and pass the unit tests.

  Do not change function signatures, struct definitions, or trait implementations,
  as they may be referenced from other code like unit tests.

  Do not modify Cargo.toml or any test files. Do not use unsafe blocks unless
  the existing code already uses them.

  When you generate code, you must maintain the original formatting of the function
  stubs (such as whitespaces), otherwise we will not be able to search/replace blocks
  for code modifications, and therefore you will receive a score of 0 for your generated
  code.'

    cat > "$AGENT_CONFIG" <<'YAMLEOF'
agent_name: aider
YAMLEOF
    cat >> "$AGENT_CONFIG" <<EOF
model_name: $(yaml_escape "${MODEL_NAME}")
model_short: $(yaml_escape "${MODEL_SHORT}")
use_user_prompt: false
user_prompt: '${user_prompt}'
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
repo_map_tokens: ${REPO_MAP_TOKENS}
strip_aux_docs: ${STRIP_AUX_DOCS}
blind_lint: ${BLIND_LINT}
blind_tests: ${BLIND_TESTS}
strip_non_stubs: ${STRIP_NON_STUBS}
names_only_tests: ${NAMES_ONLY_TESTS}
inject_test_files_readonly: ${INJECT_TEST_FILES_READONLY}
per_edit_compile_gate: ${PER_EDIT_COMPILE_GATE}
compile_gate_max_retries: ${COMPILE_GATE_MAX_RETRIES}
language: rust
EOF
    log "  Wrote agent config: ${AGENT_CONFIG}"
}

# ============================================================
# Run Agent
# ============================================================

AGENT_PID=""
QW_PID=""
AGENT_ELAPSED=0
AGENT_RC=0

# Signal a whole process group, falling back to the single PID. The agent is
# launched under `set -m` (monitor mode) so it leads its own process group;
# signalling the group (negative PID) reaps the cargo/docker/aider children it
# forked, instead of orphaning them to keep burning CPU/API budget after a kill.
_kill_tree() {
    local pid="$1" sig="${2:-TERM}"
    [[ -z "$pid" ]] && return 0
    kill "-${sig}" "-${pid}" 2>/dev/null \
        || kill "-${sig}" "${pid}" 2>/dev/null \
        || true
}

# C1: cumulative CPU seconds for every process in the group led by $1 (the agent
# + its cargo/python children). Used as a "still computing locally" liveness gate.
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

# C1/C2: true if any process in group $1 has an ESTABLISHED outbound TCP
# connection — i.e. an LLM request is in flight. During SERVER-SIDE extended
# thinking the local process is blocked on the socket at ~0% CPU and writes no
# logs, so neither mtime nor CPU shows life; a live connection is the real signal
# that the agent is healthily waiting on the model, not hung. Best-effort: if
# lsof is unavailable we return non-zero so the caller falls back to the CPU/mtime
# gate rather than wrongly assuming "alive".
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
    printf '%s\n' "$_out" | sed -E 's/^(-?)\./\10./'
}

# Return code contract for watchdog_run:
#   0       = agent exited successfully
#   124     = watchdog killed agent (inactivity or hard timeout)
#   128+N   = agent killed by signal N
#   other   = agent error (non-zero exit)
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
    local mtime_functional="true"
    local _veto_start=0  # C1: when the live-conn/CPU gate started suppressing the inactivity kill

    # C1 (gj fix): how long a live-connection / advancing-CPU agent may run WITHOUT
    # log progress before we kill it anyway. The old bound was a hard 2x inactivity
    # (30 min at defaults) — far too tight for a legitimate long extended-thinking
    # turn, ESPECIALLY now that the bridge buffers the whole SSE stream (Option D),
    # so aider writes NOTHING to the log until a turn completes. A live connection +
    # advancing work is strong health evidence, and the absolute wall-time cap
    # (MAX_WALL_TIME, default 24h) already backstops a true hang, so this can be
    # generous. Default 90 min; override with WATCHDOG_LIVECONN_VETO_SECS.
    local _liveconn_veto_secs="${WATCHDOG_LIVECONN_VETO_SECS:-}"
    if ! [[ "$_liveconn_veto_secs" =~ ^[0-9]+$ ]] || [[ "$_liveconn_veto_secs" -lt 1 ]]; then
        _liveconn_veto_secs=$(( INACTIVITY_TIMEOUT * 6 ))
        [[ "$_liveconn_veto_secs" -lt 5400 ]] && _liveconn_veto_secs=5400
    fi

    # Validate get_mtime works before relying on it. Probe a path that exists on
    # BOTH Linux and macOS — the agent's log_dir (created before launch) — not
    # /proc/self/status, which is absent on macOS and made the inactivity
    # watchdog falsely self-disable there even though `stat -f` works fine.
    local _probe_target="$log_dir"
    [[ -e "$_probe_target" ]] || _probe_target="${BASH_SOURCE[0]}"
    local _probe_mtime
    _probe_mtime=$(get_mtime "$_probe_target")
    if [[ "$_probe_mtime" -eq 0 ]] 2>/dev/null; then
        log "  WATCHDOG: WARNING — get_mtime returned 0 for ${_probe_target}. File-activity detection may be non-functional."
        mtime_functional="false"
        log "  WATCHDOG: Inactivity-timeout disabled; relying only on absolute wall-time cap."
    fi

    # Item 8 (hardened): when mtime probing is broken, tighten the absolute cap
    # so a stalled agent cannot spin indefinitely. Floor is env-configurable via
    # WATCHDOG_MTIME_FALLBACK_MIN_SECS (default 3600s = 1 hour).
    if [[ "$mtime_functional" == "false" ]] && [[ "$absolute_max" -gt 0 ]]; then
        local _orig_max="$absolute_max"
        local _floor="${WATCHDOG_MTIME_FALLBACK_MIN_SECS:-3600}"
        if ! [[ "$_floor" =~ ^[0-9]+$ ]] || [[ "$_floor" -lt 1 ]]; then
            log "  WATCHDOG: WARNING — WATCHDOG_MTIME_FALLBACK_MIN_SECS='$_floor' invalid; using 3600s."
            _floor=3600
        fi
        absolute_max=$(( absolute_max / 2 ))
        if [[ "$absolute_max" -lt "$_floor" ]]; then
            absolute_max="$_floor"
        fi
        log "  WATCHDOG: mtime non-functional — tightened absolute_max from ${_orig_max}s to ${absolute_max}s (floor=${_floor}s)."
    fi

    while kill -0 "$agent_pid" 2>/dev/null; do
        # Item 18: poll faster so short-lived failures surface promptly.
        sleep 5

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

        # Inactivity timeout: kill if no log writes within the limit
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
            # C1/C2: log-inactivity ALONE is not "stuck". A long server-side
            # extended-thinking turn writes no logs and burns ~0 local CPU (it is
            # blocked on the socket). Before killing — and wasting a paid turn —
            # require BOTH: no live LLM connection AND no local CPU progress over a
            # short window. If either shows life, the agent is healthy; continue.
            local _alive="false"
            if _pgroup_has_live_conn "$agent_pid"; then
                _alive="true"
                # Throttle this message: at a 5s poll it would print ~1080 identical
                # lines over a 90-min veto window. Log at most ~once/minute.
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
                # C1 (regression fix): a live connection/CPU DELAYS the inactivity
                # kill, it must not VETO it forever — otherwise a stale half-open
                # ESTABLISHED socket with --max-wall-time 0 hangs indefinitely.
                # Bound the veto at _liveconn_veto_secs (default 90 min, generous
                # for long buffered-stream turns); the absolute wall-time cap still
                # backstops a genuine infinite hang.
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
        "$VENV_PYTHON" -m agent.cli_rust run "$branch"
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
    log "  Running agent (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s)"
    log "  Command: ${first_cmd[*]}"
    log "  Output → ${agent_log}"

    local start_time
    start_time=$(date +%s)

    set +e
    # C1: force unbuffered Python so streamed thinking/progress reliably advances
    # the log mtime the inactivity watchdog reads. Block-buffered stdout (the
    # default when stdout is a file) can withhold writes for minutes, making a
    # healthy streaming agent look idle.
    export PYTHONUNBUFFERED=1
    # Launch under monitor mode so the agent leads its own process group; this
    # lets the watchdog/cleanup signal the whole group and reap forked
    # cargo/docker/aider children instead of orphaning them.
    set -m
    "${first_cmd[@]}" >>"$agent_log" 2>&1 &
    local agent_pid=$!
    set +m
    AGENT_PID=$agent_pid

    # ---- Fix #4: Quality-aware watchdog (opt-in) ----
    # Sidecar process that runs `cargo check` periodically and kills the agent
    # if compile-error count is monotonically rising. SIGTERMs the agent_pid;
    # the existing watchdog_run catches that as a normal kill (rc=124).
    local _qw_pid=""
    if [[ "$QUALITY_WATCHDOG" == "true" ]]; then
        local qw_log="${log_dir}/quality_watchdog.log"
        local repos_in_dataset
        repos_in_dataset=$("$VENV_PYTHON" -c "
import json,sys
with open(sys.argv[1]) as f: d = json.load(f)
rows = d if isinstance(d, list) else d.get('data', [])  # C18: handle wrapped datasets
for e in rows: print(e['repo'].split('/')[-1])" "$DATASET_FILE" 2>/dev/null | head -1)
        local qw_repo_dir="${REPO_BASE}/${repos_in_dataset}"
        if [[ -d "$qw_repo_dir" ]]; then
            "$VENV_PYTHON" -m agent.claude_code.quality_watchdog \
                --agent-pid "$agent_pid" \
                --repo-dir "$qw_repo_dir" \
                --interval "$QUALITY_WATCHDOG_INTERVAL" \
                --consecutive-rising "$QUALITY_WATCHDOG_RISING" \
                --min-delta "$QUALITY_WATCHDOG_MIN_DELTA" \
                --log "$qw_log" >/dev/null 2>&1 &
            _qw_pid=$!
            QW_PID=$_qw_pid  # expose to cleanup() so a SIGTERM to the script reaps it too
            log "  QUALITY-WATCHDOG: started (pid=${_qw_pid}, interval=${QUALITY_WATCHDOG_INTERVAL}s, rising=${QUALITY_WATCHDOG_RISING}, delta=${QUALITY_WATCHDOG_MIN_DELTA})"
        else
            log "  QUALITY-WATCHDOG: skipped — repo dir not found ($qw_repo_dir)"
        fi
    fi

    watchdog_run "$agent_pid" "$log_dir" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
    AGENT_RC=$?
    AGENT_PID=""

    # Stop the quality watchdog if it's still alive (it exits naturally when agent_pid disappears).
    if [[ -n "$_qw_pid" ]] && kill -0 "$_qw_pid" 2>/dev/null; then
        kill "$_qw_pid" 2>/dev/null || true
        wait "$_qw_pid" 2>/dev/null || true
    fi
    QW_PID=""
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
# Run Evaluate
# ============================================================

EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_RUNTIME="0.0"
EVAL_ELAPSED=0
# C4: distinguishes a real "0 of N passed" from "eval did not run". Values:
# OK | NO_RESULTS | EVAL_FAILED | EVAL_TIMEOUT. Stages record this so a broken
# eval (timeout / missing image / hung container) never masquerades as 0%.
EVAL_STATUS="OK"

# ============================================================
# Docker image build (idempotent, one-time per pipeline run)
# ============================================================
#
# Without this, the eval step fails with HTTP 404 'image not found on Docker
# Hub' because nothing else in the pipeline builds the repo image. cli_rust.py
# build is itself idempotent (skips existing images), so multiple calls are
# safe but we still gate on a flag to avoid double-logging.

_pipeline_build_done="false"

run_build_once() {
    if [[ "$_pipeline_build_done" == "true" ]]; then
        return 0
    fi
    # Containerized run: the pipeline is ALREADY executing inside the repo image
    # (the sandbox), and eval uses the local_inplace backend (a git worktree, no
    # nested container). There is no docker daemon to build with, so skip the
    # image build entirely. See scripts/run_pipeline_rust_container.sh.
    if [[ "${KAIJU_IN_CONTAINER:-0}" == "1" ]]; then
        log "  [in-container] Skipping docker image build (image is the sandbox)."
        _pipeline_build_done="true"
        return 0
    fi
    log "  Ensuring Docker images for eval are built (one-time per pipeline run)..."

    : "${LOG_BASE:?LOG_BASE must be set before run_build_once()}"
    mkdir -p "$LOG_BASE"
    local build_log="${LOG_BASE}/docker_build.log"
    mkdir -p "$(dirname "$build_log")"
    local cmd=(
        "$VENV_PYTHON" commit0/cli_rust.py build
        --commit0-config-file "$COMMIT0_CONFIG"
        --num-workers 2
    )

    local start_time
    start_time=$(date +%s)
    set +e
    "${cmd[@]}" >"$build_log" 2>&1
    local build_rc=$?
    set -e
    local elapsed=$(( $(date +%s) - start_time ))

    if [[ $build_rc -ne 0 ]]; then
        log "  Docker image build FAILED (rc=$build_rc) in ${elapsed}s — last 15 lines:"
        tail -15 "$build_log" 2>/dev/null | while IFS= read -r line; do log "    | $line"; done
        # C16: mark infra as broken so a downstream image-not-found eval is recorded
        # as INFRA_BROKEN, not a legitimate 0%. Without this, a broken build makes
        # every stage silently score 0/N and look like a model failure.
        INFRA_BROKEN="true"
        log "  Continuing pipeline; eval will be flagged INFRA_BROKEN (not a real 0%)."
    else
        log "  Docker image build OK in ${elapsed}s (log: ${build_log})"
        _pipeline_build_done="true"
    fi
}


run_evaluate() {
    local branch="$1"
    local stage_label="${2:-eval}"

    local cmd=(
        "$VENV_PYTHON" commit0/cli_rust.py evaluate \
            --branch "$branch" \
            --backend "$BACKEND" \
            --timeout "${KAIJU_EVAL_HARNESS_TIMEOUT:-1800}" \
            --num-cpus 1 \
            --num-workers 1 \
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
    set -e

    local end_time
    end_time=$(date +%s)
    EVAL_ELAPSED=$(( end_time - start_time ))

    log "  Evaluation finished in ${EVAL_ELAPSED}s (rc=${eval_rc})"

    collect_eval_artifacts "${stage_label:-eval}"

    # C6: if `timeout` killed the eval (124) the python process is gone but the
    # docker containers it started are children of dockerd and keep running,
    # holding the build lock and CPU. Reap any harness-labelled containers so the
    # next stage starts clean. (On a clean exit the harness removes its own
    # containers; this is a belt-and-braces sweep for the kill path.)
    if [[ $eval_rc -eq 124 ]] && command -v docker &>/dev/null; then
        local _orphans
        _orphans=$(docker ps -aq --filter "label=kaiju.harness=1" 2>/dev/null)
        if [[ -n "$_orphans" ]]; then
            log "  WATCHDOG: eval timed out — reaping $(echo "$_orphans" | wc -l | tr -d ' ') orphaned harness container(s)."
            # shellcheck disable=SC2086
            docker rm -f $_orphans >/dev/null 2>&1 || true
        fi
    fi

    local combined_output
    combined_output=$(cat "$eval_log")

    parse_eval_output "$combined_output"   # sets EVAL_STATUS=OK|NO_RESULTS

    # C4: refine status from the eval process exit code. A non-zero rc with no
    # parseable results is a broken eval, NOT a 0% score — flag it so the stage
    # records EVAL_FAILED/EVAL_TIMEOUT instead of a misleading 0/N.
    if [[ "$EVAL_STATUS" != "OK" ]]; then
        # C16: a known-broken build takes precedence — the eval couldn't have run.
        if [[ "$INFRA_BROKEN" == "true" ]]; then
            EVAL_STATUS="INFRA_BROKEN"
        elif [[ $eval_rc -eq 124 ]]; then
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

    # Aggregate across all "repo,runtime,passed/total,status,detail" lines
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
            # PATCH_APPLY_FAILED / OUTPUT_MISSING / *_TIMEOUT / INFRA_FETCH_FAILED
            # when the build broke, the patch didn't apply, or a module never
            # completed). Any non-TESTS_RAN status means the 0/N is NOT a genuine
            # model score — surface it so eval_status isn't a bogus OK (mirrors the
            # C pipeline; previously rust silently reported OK for COMPILE_FAILED).
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
        # A parsed non-TESTS_RAN row means the 0/N is a build/patch/infra failure
        # (not a real 0% model score); propagate it so downstream never reads it as
        # a legit result. TESTS_RAN (or an old statusless row) -> OK.
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

    # Fallback: look for "average pass rate:" line
    if [[ "$EVAL_PASS_RATE" == "0.0" ]] || [[ "$EVAL_PASS_RATE" == "0" ]]; then
        local avg_line
        avg_line=$(echo "$output" | grep -i "average pass rate:" || true)
        if [[ -n "$avg_line" ]]; then
            local rate
            rate=$(echo "$avg_line" | awk -F':' '{print $NF}' | tr -d ' ')
            if [[ -n "$rate" ]] && [[ "$rate" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then  # C11: strict number (reject "1.2.3"/".")
                EVAL_PASS_RATE="$rate"
            fi
        fi
    fi
}

# ============================================================
# Cost Extraction
# ============================================================

# C3: a $0.0000 result is ambiguous — it could be a genuinely free stage OR a
# silent extraction failure (no output.json, unparseable cost). The Python now
# prints "<cost> <source>" where source ∈ {output_json:N, aider_fallback, none}.
# This function sets the global LAST_COST_SOURCE and echoes only the cost (so
# existing callers stay compatible); a "none" source with $0 is logged LOUD so a
# broken-cost run is never mistaken for a free one.
LAST_COST_SOURCE="unknown"
extract_all_stage_costs() {
    local log_dir="$1"
    LAST_COST_SOURCE="none"
    if [[ ! -d "$log_dir" ]]; then
        LAST_COST_SOURCE="missing_dir"
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
        LAST_COST_SOURCE="${source_part:-none}"
        if [[ "$LAST_COST_SOURCE" == "none" ]]; then
            log "  WARNING: cost extraction found NO output.json/aider.log cost in ${log_dir} — reporting \$0.0000 but this is an EXTRACTION FAILURE, not a free run." >&2
        fi
        # Echo BOTH cost AND source: the caller runs this in a command
        # substitution `$(...)`, i.e. a SUBSHELL, so any assignment to the global
        # LAST_COST_SOURCE here is lost when the subshell exits — the parent would
        # always read the stale initial "unknown". Returning "<cost> <source>" on
        # stdout lets the caller recover the real source.
        echo "$cost_part ${source_part:-none}"
    else
        LAST_COST_SOURCE="parse_error"
        # NB: use plain brackets, NOT ${result@Q} — the @Q transform is bash 4.4+
        # and throws "bad substitution" on the macOS default bash 3.2.
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
        --arg language "rust" \
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
            language: $language,
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
    # C8: atomic write. `> "$PIPELINE_LOG"` truncates the file BEFORE jq produces
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
# Pipeline Stages
# ============================================================

# ----------------------------------------------------------------------------
# Fix #3: Stage 2 -> Stage 3 gate.
# Returns 0 if the tree compiles (cargo check --tests succeeds), nonzero otherwise.
# Side effects: writes count of compile errors to ${LOG_BASE}/.stage_gate_errors
# Used by run_single_sample to decide whether to enter Stage 3.
# ----------------------------------------------------------------------------
check_tree_compiles() {
    local repo_dir="$1"
    local out_file="$2"  # path to write cargo output for debugging
    if [[ ! -d "$repo_dir" ]]; then
        # C17: this is an INFRA condition (missing repo), not "N compile errors".
        # Write 0 (so the summed-count message isn't polluted with a fake 999) and
        # let the return code carry the "broken" signal.
        log "  GATE: repo dir not found: $repo_dir; treating as broken (infra, not a real error count)"
        echo 0 > "${LOG_BASE}/.stage_gate_errors"
        return 1
    fi
    log "  GATE: running cargo check --tests on $(basename "$repo_dir")"
    local rc=0
    ( cd "$repo_dir" && cargo check --tests --all-features --message-format=short ) > "$out_file" 2>&1 || rc=$?
    if [[ $rc -eq 0 ]]; then
        echo 0 > "${LOG_BASE}/.stage_gate_errors"
        log "  GATE: tree compiles cleanly"
        return 0
    fi
    # C17: count ALL error diagnostics, not just `path:line:col: error[...]`.
    # The old regex missed crate-level errors that carry no file:line (e.g.
    # `error[E0463]: can't find crate`, `error: linking with cc failed`), so a
    # tree that fails ONLY on those reported "0 errors" while rc!=0 — a
    # self-contradiction. Count file-level AND bare `error[`/`error:` lines, while
    # excluding cargo's trailing summary ("could not compile", "aborting due to")
    # so the count isn't inflated by 1-2 boilerplate lines.
    local n_errors
    n_errors=$(awk '
        /could not compile/ { next }
        /aborting due to/   { next }
        /: error\[/ || /: error:/ || /^error\[/ || /^error:/ { c++ }
        END { print c+0 }
    ' "$out_file" 2>/dev/null) || n_errors=0
    [[ "$n_errors" =~ ^[0-9]+$ ]] || n_errors=0
    # A failing build must report at least one error even if none were
    # categorized — never let rc!=0 claim "0 errors".
    if [[ "$n_errors" -eq 0 ]]; then n_errors=1; fi
    echo "$n_errors" > "${LOG_BASE}/.stage_gate_errors"
    log "  GATE: tree FAILS to compile ($n_errors error(s), rc=$rc); see $out_file"
    return 1
}

stage_1_draft() {
    log "======================================================================"
    log "STAGE 1: Draft Initial Implementations"
    log "======================================================================"

    # Build Docker image FIRST — before the agent burns API credits on a
    # trajectory we can't evaluate. Idempotent: subsequent stages no-op via
    # _pipeline_build_done flag. Failure here doesn't abort the pipeline; the
    # eval step will surface a clearer image-not-found error if build broke.
    run_build_once

    write_agent_config "false" "false" "false" "$USE_UNIT_TESTS_INFO" "false" "$USE_SPEC_INFO"

    local stage_log_dir="${LOG_BASE}/stage1_draft"
    mkdir -p "$stage_log_dir"

    run_agent "$BRANCH_NAME" "false" "$stage_log_dir"
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
    log "STAGE 2: Refine with Static Analysis (Lint)"
    log "======================================================================"

    write_agent_config "false" "true" "true" "false" "false" "$USE_SPEC_INFO"

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
    log "STAGE 3: Refine with Unit Test Feedback"
    log "======================================================================"

    local s3_lint="true"
    if [[ "$NO_STAGE3_LINT" == "true" ]]; then
        s3_lint="false"
        log "  Stage 3 lint DISABLED (--no-stage3-lint)"
    fi

    write_agent_config "true" "$s3_lint" "false" "false" "false" "$USE_SPEC_INFO"

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
    log "RESULTS SUMMARY — SDE-I 3-Stage Rust Pipeline"
    log "Model: ${MODEL_SHORT} (${MODEL_NAME})"
    log "Dataset: ${DATASET_SHORT} | Repo Split: ${REPO_SPLIT} | Branch: ${BRANCH_NAME}"
    log "Cache Prompts: ${CACHE_PROMPTS} | Max Iteration: ${MAX_ITERATION} | Backend: ${BACKEND}"
    log "Language: Rust"
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

strip_aux_docs_in_repos() {
    [[ "$STRIP_AUX_DOCS" != "true" ]] && return 0
    log "Stripping auxiliary docs from agent's view (strip_aux_docs=true)..."
    local strip_count=0
    local _slist
    if [[ -f "$DATASET_FILE" ]]; then
        _slist=$(_PIPELINE_DATASET_FILE="$DATASET_FILE" "$VENV_PYTHON" -c "
import json, os
with open(os.environ['_PIPELINE_DATASET_FILE']) as f:
    data = json.load(f)
if isinstance(data, dict) and 'data' in data:
    data = data['data']
for item in data:
    print(item['repo'].split('/')[-1])
" 2>/dev/null || true)
    fi
    while IFS= read -r _repo; do
        [[ -z "$_repo" ]] && continue
        local _rdir="${REPO_BASE}/${_repo}"
        [[ ! -d "$_rdir" ]] && continue
        for fname in README.md README.rst README.txt README \
                     CHANGELOG.md CHANGELOG.rst CHANGELOG CHANGES.md CHANGES \
                     HISTORY.md HISTORY.rst HISTORY \
                     AUTHORS AUTHORS.md CONTRIBUTORS CONTRIBUTORS.md \
                     NOTICE MAINTAINERS CODEOWNERS RELEASES.md RELEASE_NOTES.md; do
            if [[ -f "${_rdir}/${fname}" ]]; then
                rm -f "${_rdir}/${fname}" 2>/dev/null && strip_count=$((strip_count + 1))
            fi
        done
    done <<< "$_slist"
    log "  Stripped ${strip_count} aux-doc file(s) across repos."
}

restore_aux_docs_in_repos() {
    [[ "$STRIP_AUX_DOCS" != "true" ]] && return 0
    local _slist
    if [[ -f "$DATASET_FILE" ]]; then
        _slist=$(_PIPELINE_DATASET_FILE="$DATASET_FILE" "$VENV_PYTHON" -c "
import json, os
with open(os.environ['_PIPELINE_DATASET_FILE']) as f:
    data = json.load(f)
if isinstance(data, dict) and 'data' in data:
    data = data['data']
for item in data:
    print(item['repo'].split('/')[-1])
" 2>/dev/null || true)
    fi
    while IFS= read -r _repo; do
        [[ -z "$_repo" ]] && continue
        local _rdir="${REPO_BASE}/${_repo}"
        [[ ! -d "$_rdir" ]] && continue
        (cd "$_rdir" && git checkout HEAD -- README.md README.rst README.txt README \
            CHANGELOG.md CHANGELOG.rst CHANGELOG CHANGES.md CHANGES \
            HISTORY.md HISTORY.rst HISTORY \
            AUTHORS AUTHORS.md CONTRIBUTORS CONTRIBUTORS.md \
            NOTICE MAINTAINERS CODEOWNERS RELEASES.md RELEASE_NOTES.md \
            2>/dev/null) || true
    done <<< "$_slist"
    log "  Restored aux-doc files via git checkout."
}

cleanup() {
    # C7: make cleanup idempotent/re-entrant. Reset traps immediately so a second
    # Ctrl-C (or a SIGTERM arriving during our own kill/sleep) doesn't re-enter
    # cleanup or interrupt the escalation mid-way and leave children alive.
    trap - INT TERM EXIT
    if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
        _kill_tree "$AGENT_PID" TERM
        sleep 2
        _kill_tree "$AGENT_PID" KILL
    fi
    # Reap the quality-watchdog sidecar too — a SIGTERM to the script bypasses
    # the local stop in run_agent(), so without this it would be orphaned.
    if [[ -n "$QW_PID" ]] && kill -0 "$QW_PID" 2>/dev/null; then
        kill "$QW_PID" 2>/dev/null || true
    fi

    # C6: reap any harness-labelled docker containers (children of dockerd, not
    # of this script) so an interrupted run doesn't leave eval containers behind.
    if command -v docker &>/dev/null; then
        local _co
        _co=$(docker ps -aq --filter "label=kaiju.harness=1" 2>/dev/null)
        if [[ -n "$_co" ]]; then
            # shellcheck disable=SC2086
            docker rm -f $_co >/dev/null 2>&1 || true
        fi
    fi

    restore_aux_docs_in_repos

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
# C19: preserve interrupt semantics (don't mask with a bare `exit`, which returns
# the last command's status). 130=SIGINT, 143=SIGTERM. cleanup runs via EXIT.
trap 'exit 130' INT
trap 'exit 143' TERM

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
    log "Commit0 SDE-I 3-Stage Rust Pipeline"
    log "Model:        ${MODEL_NAME} (${MODEL_SHORT})"
    log "Dataset:      ${DATASET_FILE} (${DATASET_SHORT})"
    log "Language:     Rust"
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
        ensure_spec_docs_rust
        if ! verify_spec_docs_rust; then
            return 1
        fi
        if ! verify_inventory_rust; then
            return 1
        fi
    fi

    strip_aux_docs_in_repos

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
        # C9: the prior-results JSON is necessary but not sufficient — skipping to
        # a later stage reuses the git BRANCH built by earlier stages. If that
        # branch is missing (different machine, branch deleted) or the working
        # tree is dirty (uncommitted edits that would be silently included), the
        # skipped run produces garbage. Validate the actual git state per repo.
        local _vslist _vbad=0 _vchecked=0
        _vslist=$(_PIPELINE_DATASET_FILE="$DATASET_FILE" "$VENV_PYTHON" -c "
import json, os
with open(os.environ['_PIPELINE_DATASET_FILE']) as f:
    data = json.load(f)
if isinstance(data, dict) and 'data' in data:
    data = data['data']
for item in data:
    print(item['repo'].split('/')[-1])
" 2>/dev/null || true)
        while IFS= read -r _vrepo; do
            [[ -z "$_vrepo" ]] && continue
            local _vdir="${REPO_BASE}/${_vrepo}"
            [[ ! -d "$_vdir" ]] && continue
            _vchecked=$((_vchecked + 1))
            if ! git -C "$_vdir" show-ref --verify --quiet "refs/heads/${BRANCH_NAME}"; then
                log "ERROR: --skip-to-stage: branch '${BRANCH_NAME}' not found in repo ${_vrepo}. Prior stages were never built here."
                _vbad=$((_vbad + 1))
                continue
            fi
            # Warn (don't fail) on a dirty tree — uncommitted changes on the
            # branch would be silently folded into the skipped run.
            if [[ -n "$(git -C "$_vdir" status --porcelain 2>/dev/null)" ]]; then
                log "  WARNING: --skip-to-stage: repo ${_vrepo} has a DIRTY working tree on '${BRANCH_NAME}'; uncommitted changes will be included."
            fi
        done <<< "$_vslist"
        if [[ "$_vbad" -gt 0 ]]; then
            log "ERROR: --skip-to-stage aborted: ${_vbad} repo(s) are missing branch '${BRANCH_NAME}'. Run the earlier stages first."
            return 1
        fi
        log "  Validated git branch '${BRANCH_NAME}' across ${_vchecked} repo(s) for skip-to-stage."
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

    # ---- Fix #3: Stage 2 -> Stage 3 gate ----
    # Skip Stage 3 if the tree doesn't compile after Stage 2. Running cargo test
    # against a broken tree just produces the same compile errors at much higher
    # cost. Default ON; bypass with --no-stage3-skip-if-broken.
    if [[ -z "$pipeline_error" ]] && [[ "$STAGE3_SKIP_IF_BROKEN" == "true" ]]; then
        local gate_log="${LOG_BASE}/stage_gate_cargo_check.log"
        # C5: check ALL repos in the dataset, not just the first (`head -1`).
        # Previously the gate compiled only repo #1: if it compiled but repos
        # #2..N were broken, Stage 3 ran cargo test on broken trees (the wasted
        # budget the gate exists to prevent); if repo #1 was broken but the rest
        # compiled, Stage 3 was skipped for everyone (good work thrown away).
        # We skip Stage 3 only when NO repo compiles (definitely broken); if any
        # repo compiles we proceed so its work isn't discarded. Total compile
        # errors across repos are summed for reporting.
        local _all_repos _n_compile=0 _n_total=0 _total_errs=0
        # `|| true`: a dataset-read failure here must not abort under set -e (this
        # gate runs with set -e suppressed inside run_single_sample, but keep the
        # guard explicit and consistent with every other dataset enumeration). An
        # empty result skips the gate (_n_total=0) and falls through to Stage 3.
        _all_repos=$("$VENV_PYTHON" -c "
import json,sys
with open(sys.argv[1]) as f: d = json.load(f)
rows = d if isinstance(d, list) else d.get('data', [])
for e in rows: print(e['repo'].split('/')[-1])" "$DATASET_FILE" 2>/dev/null || true)
        : > "$gate_log"
        while IFS= read -r _r; do
            [[ -z "$_r" ]] && continue
            _n_total=$((_n_total + 1))
            if check_tree_compiles "${REPO_BASE}/${_r}" "$gate_log"; then
                _n_compile=$((_n_compile + 1))
            else
                local _e; _e=$(cat "${LOG_BASE}/.stage_gate_errors" 2>/dev/null) || _e=0
                [[ "$_e" =~ ^[0-9]+$ ]] || _e=0
                _total_errs=$((_total_errs + _e))
                log "  GATE: ${_r} does not compile (${_e} error(s))"
            fi
        done <<< "$_all_repos"
        log "  GATE: ${_n_compile}/${_n_total} repos compile after Stage 2"
        if [[ "$_n_total" -gt 0 && "$_n_compile" -eq 0 ]]; then
            local n_errs="$_total_errs"
            [[ "$n_errs" =~ ^[0-9]+$ ]] || n_errs=0
            # DO NOT skip Stage 3. Test-refine is precisely the stage that feeds
            # cargo build/test errors back to the agent so it can REPAIR the build;
            # skipping locks in a 0/N incomplete run and drops a whole trajectory
            # stage. Record that the tree was broken on entry (so the score is not
            # mistaken for a clean measurement) and run Stage 3 anyway.
            log "  GATE: tree broken after Stage 2 ($n_errs error(s)) — running Stage 3 so the agent can repair via test feedback (NOT skipping). See: $gate_log"
        fi
        if ! stage_3_test_refine; then
            pipeline_error="Stage 3 failed"
            log "PIPELINE ERROR: ${pipeline_error}"
        fi
    elif [[ -z "$pipeline_error" ]]; then
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

    # C15: signal sample failure to the caller. Previously this function returned
    # the status of the last command (always-succeeding ATIF block), so a sample
    # where every stage errored still counted as "completed" -> PIPELINE_SUCCESS
    # flipped true -> cleanup deleted the per-run configs needed to debug it.
    if [[ -n "$pipeline_error" ]]; then
        return 1
    fi
    return 0
}

print_pass_at_k_summary() {
    local k="$NUM_SAMPLES"
    log ""
    log "=========================================================================================="
    log "PASS@${k} SUMMARY — ${MODEL_SHORT} / ${DATASET_SHORT} (Rust)"
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
        # C14: a 0% from a SKIPPED/EVAL_FAILED/INFRA_BROKEN/TIMEOUT stage is NOT a
        # legitimate "solved nothing" — surface the status so it isn't read as a
        # real score, and exclude such samples from the best-run selection.
        s3status=$(echo "$rj" | jq -r '.stage3.eval_status // "?"')

        local s1_pct s2_pct s3_pct cost_str
        s1_pct=$(format_pct "$s1r")
        s2_pct=$(format_pct "$s2r")
        if [[ "$s3status" == "OK" || "$s3status" == "?" ]]; then
            s3_pct=$(format_pct "$s3r")
        else
            s3_pct="$s3status"   # e.g. SKIPPED / EVAL_FAILED / INFRA_BROKEN
        fi
        cost_str=$(printf "\$%.2f" "$s3c")

        printf -v row "%-12s %14s %14s %14s %12s" "run_${sidx}" "$s1_pct" "$s2_pct" "$s3_pct" "$cost_str"
        log "$row"

        # Only a genuinely-evaluated stage can win "best run".
        if [[ "$s3status" == "OK" || "$s3status" == "?" ]]; then
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
    # C14: this is the BEST SINGLE RUN's aggregate S3 pass rate — NOT true pass@k
    # (which is the per-problem union of solutions across k samples). Label it
    # honestly so the number isn't over-claimed.
    log "Best single run of ${k} (max S3 pass rate; NOT per-problem pass@${k}):  run_${best_s3_sample}  →  ${best_pct}"
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
        log "Rust pipeline complete. All ${NUM_SAMPLES} sample(s) succeeded."
        PIPELINE_SUCCESS="true"
    elif [[ "$SAMPLES_COMPLETED" -gt 0 ]]; then
        log "Rust pipeline complete. ${SAMPLES_COMPLETED}/${NUM_SAMPLES} sample(s) succeeded."
        PIPELINE_SUCCESS="true"
    else
        log "Rust pipeline FAILED. No samples completed successfully."
    fi
}

cd "$BASE_DIR"
main

[[ "$PIPELINE_SUCCESS" == "true" ]] || exit 1