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

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "${BASE_DIR}/.env" ]]; then
    set -a
    source "${BASE_DIR}/.env"
    set +a
fi
"${BASE_DIR}/scripts/generate_aider_config.sh"
REPO_BASE="${BASE_DIR}/repos"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
BACKEND="local"
MAX_ITERATION=3

# Rust pipeline — spec info enabled by default (use --no-spec-info to disable)
LANGUAGE="rust"
USE_SPEC_INFO="true"
USE_UNIT_TESTS_INFO="true"
REPO_MAP_TOKENS=1024
STRIP_AUX_DOCS="false"
BLIND_LINT="false"
BLIND_TESTS="false"
STRIP_NON_STUBS="false"
INJECT_TEST_FILES_READONLY="true"
NAMES_ONLY_TESTS="false"

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
INACTIVITY_TIMEOUT=900
MAX_WALL_TIME=86400
SKIP_TO_STAGE=""
NUM_SAMPLES=1
MAX_TEST_OUTPUT_LENGTH=15000
MAX_PARALLEL_REPOS=1

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
  --max-iteration  <n>       Max agent iterations per stage (default: 3)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled, skipped if agent active)
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap in seconds (default: 86400, 0=disable)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --backend        <name>    Backend: local or modal (default: local)
  --no-spec-info             Disable spec/paper injection (default: enabled)
  --no-unit-tests-info       Disable inline-test injection into prompt (default: enabled; Stage 1 only)
  --no-repo-map              Disable aider's internal repo-map (default: enabled, map_tokens=1024)
  --strip-aux-docs           Hide README/CHANGELOG/HISTORY/etc. from agent's view (default: keep)
  --blind-lint               Stage 2 sees only "build failed: N errors" (default: full clippy output)
  --blind-tests              Stage 3 sees only summary line, no per-test failures (default: full output)
  --strip-non-stubs          Hide non-stubbed source files from agent context (default: visible)
  --names-only-tests         Stage 3 shows only failed test names, not tracebacks (default: full output)
  --no-test-files-readonly   Do not inject test files as read-only reference (default: inject)
  --no-stage3-lint           Disable lint in Stage 3 (for ablation experiments)
  --num-samples    <n>       Number of independent samples to run, pass@k (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages from existing branch)
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
        --no-spec-info) USE_SPEC_INFO="false"; shift ;;
        --no-unit-tests-info) USE_UNIT_TESTS_INFO="false"; shift ;;
        --no-repo-map) REPO_MAP_TOKENS=0; shift ;;
        --strip-aux-docs) STRIP_AUX_DOCS="true"; shift ;;
        --blind-lint) BLIND_LINT="true"; shift ;;
        --blind-tests) BLIND_TESTS="true"; shift ;;
        --strip-non-stubs) STRIP_NON_STUBS="true"; shift ;;
        --names-only-tests) NAMES_ONLY_TESTS="true"; shift ;;
        --no-test-files-readonly) INJECT_TEST_FILES_READONLY="false"; shift ;;
        --inactivity-timeout) [[ $# -lt 2 ]] && { echo "Error: --inactivity-timeout requires a value"; exit 1; }; INACTIVITY_TIMEOUT="$2"; shift 2 ;;
        --max-wall-time) [[ $# -lt 2 ]] && { echo "Error: --max-wall-time requires a value"; exit 1; }; MAX_WALL_TIME="$2"; shift 2 ;;
        --num-samples) [[ $# -lt 2 ]] && { echo "Error: --num-samples requires a value"; exit 1; }; NUM_SAMPLES="$2"; shift 2 ;;
        --skip-to-stage) [[ $# -lt 2 ]] && { echo "Error: --skip-to-stage requires a value"; exit 1; }; SKIP_TO_STAGE="$2"; shift 2 ;;
        --max-test-output-length) [[ $# -lt 2 ]] && { echo "Error: --max-test-output-length requires a value"; exit 1; }; MAX_TEST_OUTPUT_LENGTH="$2"; shift 2 ;;
        --max-parallel-repos) [[ $# -lt 2 ]] && { echo "Error: --max-parallel-repos requires a value"; exit 1; }; MAX_PARALLEL_REPOS="$2"; shift 2 ;;
        -h|--help)     print_usage ;;
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
    LOG_BASE="${BASE_DIR}/logs/agent/${DATASET_DIR_NAME}/${MODEL_DIR_NAME}/run_${sample_idx}"
    PIPELINE_LOG="${BASE_DIR}/logs/pipeline_${RUN_ID}_results.json"
    COMMIT0_CONFIG="${BASE_DIR}/.commit0_${RUN_ID}.yaml"
    AGENT_CONFIG="${BASE_DIR}/.agent_${RUN_ID}.yaml"
}

# Initialize with sample 1 defaults (used by preflight, etc.)
set_sample_vars 1

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
    for cmd in jq bc timeout cargo rustc docker; do
        if ! command -v "$cmd" &>/dev/null; then
            echo "Error: Required command '$cmd' not found"
            errors=$((errors + 1))
        fi
    done

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
        repo_list=$("$VENV_PYTHON" -c "
from commit0.harness.constants_rust import RUST_SPLIT
for r in sorted(RUST_SPLIT.get('${REPO_SPLIT}', [])):
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
language: rust
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

    # Validate get_mtime works before relying on it
    local _probe_mtime
    _probe_mtime=$(get_mtime "/proc/self/status")
    if [[ "$_probe_mtime" -eq 0 ]] 2>/dev/null; then
        log "  WATCHDOG: WARNING — get_mtime returned 0 for /proc/self/status. File-activity detection may be non-functional."
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
            fi
        else
            # No logs yet (slow startup, model download, dep install) — benefit of the doubt
            agent_active="true"
        fi

        # Absolute wall-time cap — unconditional, prevents unbounded spend
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
                    kill "$agent_pid" 2>/dev/null || true
                    sleep 2
                    kill -9 "$agent_pid" 2>/dev/null || true
                    wait "$agent_pid" 2>/dev/null || true
                    return 124
                fi
            fi
        fi

        # Inactivity timeout: kill if no log writes within the limit
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

    if [[ "$override" == "true" ]]; then
        cmd+=(--override-previous-changes)
    fi

    local agent_log="${log_dir}/agent_run.log"
    log "  Running agent (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s)"
    log "  Command: ${cmd[*]}"
    log "  Output → ${agent_log}"

    local start_time
    start_time=$(date +%s)

    set +e
    "${cmd[@]}" >>"$agent_log" 2>&1 &
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
}

# ============================================================
# Run Evaluate
# ============================================================

EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_RUNTIME="0.0"
EVAL_ELAPSED=0

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
        tail -15 "$build_log" | while IFS= read -r line; do log "    | $line"; done
        log "  Continuing pipeline; eval will fail with image-not-found if image absent."
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
            --timeout 300 \
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

    # Aggregate across all "repo,runtime,passed/total" lines
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
# Cost Extraction
# ============================================================

extract_all_stage_costs() {
    local log_dir="$1"
    if [[ ! -d "$log_dir" ]]; then
        echo "0.0000"
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
    print(f"{oj_total:.4f}")
    sys.exit(0)

# Fallback (no output.json present): aider.log session regex.
# Only counts aider's main edit loop; misses summarizer + commit_msg + cache.
COST_RE = re.compile(r"Cost:\s+\$\d+\.\d+\s+(?:message|request),\s+\$(\d+\.\d+)\s+session")
fallback_total = 0.0
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
    except (OSError, ValueError):
        pass
print(f"{fallback_total:.4f}")
PYEOF
) || true
    if [[ "$result" =~ ^[0-9]+\.[0-9]+$ ]]; then
        echo "$result"
    else
        echo "0.0000"
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
        --arg language "rust" \
        --arg start_time "$(ts)" \
        '{
            model: $model,
            model_short: $model_short,
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

    # Build Docker image FIRST — before the agent burns API credits on a
    # trajectory we can't evaluate. Idempotent: subsequent stages no-op via
    # _pipeline_build_done flag. Failure here doesn't abort the pipeline; the
    # eval step will surface a clearer image-not-found error if build broke.
    run_build_once

    write_agent_config "false" "false" "false" "$USE_UNIT_TESTS_INFO" "false" "$USE_SPEC_INFO"

    local stage_log_dir="${LOG_BASE}/stage1_draft"
    mkdir -p "$stage_log_dir"

    run_agent "$BRANCH_NAME" "true" "$stage_log_dir"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local cost
    cost=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 1 cost extraction failed"; return 1; }
    log "  Stage 1 cost: \$${cost}"

    run_evaluate "$BRANCH_NAME" "stage1"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 1 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Draft (no feedback)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost "$cost" \
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
            returncode: $rc,
            runtime: $runtime,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate
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
    local s2_incremental
    s2_incremental=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 2 cost extraction failed"; return 1; }
    local total_cost
    total_cost=$(echo "scale=4; $s1_cost + $s2_incremental" | bc) || { log "ERROR: Stage 2 cost calculation failed"; return 1; }

    log "  Stage 2 incremental cost: \$${s2_incremental} (cumulative: \$${total_cost})"

    run_evaluate "$BRANCH_NAME" "stage2"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 2 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Lint refine" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost_inc "$s2_incremental" \
        --argjson cost_cum "$total_cost" \
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
            returncode: $rc,
            runtime: $runtime,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate
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
    local s3_incremental
    s3_incremental=$(extract_all_stage_costs "$stage_log_dir") || { log "ERROR: Stage 3 cost extraction failed"; return 1; }
    local total_cost
    total_cost=$(echo "scale=4; $s2_cumulative + $s3_incremental" | bc) || { log "ERROR: Stage 3 cost calculation failed"; return 1; }

    log "  Stage 3 incremental cost: \$${s3_incremental} (cumulative: \$${total_cost})"

    run_evaluate "$BRANCH_NAME" "stage3"
    local eval_time="$EVAL_ELAPSED"

    log "  Stage 3 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Test refine" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost_inc "$s3_incremental" \
        --argjson cost_cum "$total_cost" \
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
            returncode: $rc,
            runtime: $runtime,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate
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
    if [[ -n "$AGENT_PID" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
        kill -- -"$AGENT_PID" 2>/dev/null || true
        sleep 2
        kill -9 -- -"$AGENT_PID" 2>/dev/null || true
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
trap 'exit' INT TERM

# ============================================================
# Main
# ============================================================

# All per-sample pipeline result files, collected for pass@k aggregation
declare -a SAMPLE_RESULT_FILES=()

run_single_sample() {
    local sample_idx="$1"

    set_sample_vars "$sample_idx"

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

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq --arg end "$(ts)" '.end_time = $end')

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
