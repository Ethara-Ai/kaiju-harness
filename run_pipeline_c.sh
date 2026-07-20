#!/usr/bin/env bash
# ============================================================
# 3-Stage C Pipeline for Commit0
# ============================================================
#
# Mirrors run_pipeline_go.sh in shape, adapted for C:
#   * cli_c.py / config_c.py entry points
#   * constants_c.C_SPLIT for split resolution
#   * STUB_PANIC marker and test_*.c read-only invariants in the user prompt
#   * clang-tidy / cppcheck advisory lint
#
# Usage:
#     bash run_pipeline_c.sh --model <preset|model_id> --dataset <name>
#
# Examples:
#     bash run_pipeline_c.sh --model opus --dataset ./cJSON_c_dataset.json
#     bash run_pipeline_c.sh --model claude-sonnet-4 --dataset cJSON_c
#
# Requirements: jq, bc, .venv with the commit0 package installed
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
export LANGUAGE="c"  # H8: parity with other drivers so child processes can rely on $LANGUAGE

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
Usage: run_pipeline_c.sh --model <preset|model_id> --dataset <name> [OPTIONS]

Required:
  --model    <preset|id>   Model preset or full model ID
  --dataset  <name|path>   Dataset name or path to JSON file

Options:
  --branch         <name>    Override auto-generated branch name
  --repo-split     <name>    Override repo_split
  --max-iteration  <n>       Max agent iterations per stage (default: 3)
  --stage-timeout  <secs>    Hard stage timeout in seconds (default: 0=disabled)
  --eval-timeout   <secs>    Eval timeout in seconds (default: 3600)
  --backend        <name>    Backend: local or modal (default: local)
  --no-stage3-lint           Disable lint in Stage 3
  --no-spec-info             Disable spec doc provisioning (enabled by default, matching go/rust)
  --no-strict-inventory      Warn (do not FATAL) when a repo's frozen test-id inventory is missing
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap (default: 86400)
  --num-samples    <n>       Number of independent samples (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages)
  --blind-lint               Stage 2 sees only "lint failed: N issues" (default: full output)
  --blind-tests              Stage 3 sees only summary line, no per-test failures (default: full output)
  --names-only-tests         Stage 3 sees only failed test node IDs + count (default: full output)
  --strip-non-stubs          Hide non-stubbed source files from agent context (default: visible)
  --no-test-files-readonly   Disable test-source read-only context (now the DEFAULT)
  --test-files-readonly           Inject test SOURCE as read-only context (opt-in; leaky)
  -h, --help                 Show this help
  --use-claude-code        Route anthropic/* models through the local Claude Code OAuth bridge
USAGE
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)       [[ $# -lt 2 ]] && { echo "Error: --model requires a value"; exit 1; }; MODEL_ARG="$2"; shift 2 ;;
        --dataset)     [[ $# -lt 2 ]] && { echo "Error: --dataset requires a value"; exit 1; }; DATASET_ARG="$2"; shift 2 ;;
        --branch)      [[ $# -lt 2 ]] && { echo "Error: --branch requires a value"; exit 1; }; BRANCH_OVERRIDE="$2"; shift 2 ;;
        --repo-split)  [[ $# -lt 2 ]] && { echo "Error: --repo-split requires a value"; exit 1; }; REPO_SPLIT_OVERRIDE="$2"; shift 2 ;;
        --max-iteration) [[ $# -lt 2 ]] && { echo "Error: --max-iteration requires a value"; exit 1; }; MAX_ITERATION="$2"; shift 2 ;;
        --stage-timeout) [[ $# -lt 2 ]] && { echo "Error: --stage-timeout requires a value"; exit 1; }; STAGE_TIMEOUT="$2"; shift 2 ;;
        --eval-timeout) [[ $# -lt 2 ]] && { echo "Error: --eval-timeout requires a value"; exit 1; }; EVAL_TIMEOUT="$2"; shift 2 ;;
        --backend)     [[ $# -lt 2 ]] && { echo "Error: --backend requires a value"; exit 1; }; BACKEND="$2"; shift 2 ;;
        --no-stage3-lint) NO_STAGE3_LINT="true"; shift ;;
        --go-crazy) GO_CRAZY="true"; shift ;;
        --preflight-timeout) [[ $# -lt 2 ]] && { echo "Error: --preflight-timeout requires a value (seconds)"; exit 1; }; PROBE_TIMEOUT="$2"; shift 2 ;;
        --use-spec-info) USE_SPEC_INFO="true"; shift ;;
        --no-spec-info) USE_SPEC_INFO="false"; shift ;;
        --no-strict-inventory) STRICT_INVENTORY="false"; shift ;;
        --inactivity-timeout) [[ $# -lt 2 ]] && { echo "Error: --inactivity-timeout requires a value"; exit 1; }; INACTIVITY_TIMEOUT="$2"; shift 2 ;;
        --max-wall-time) [[ $# -lt 2 ]] && { echo "Error: --max-wall-time requires a value"; exit 1; }; MAX_WALL_TIME="$2"; shift 2 ;;
        --num-samples) [[ $# -lt 2 ]] && { echo "Error: --num-samples requires a value"; exit 1; }; NUM_SAMPLES="$2"; shift 2 ;;
        --skip-to-stage) [[ $# -lt 2 ]] && { echo "Error: --skip-to-stage requires a value"; exit 1; }; SKIP_TO_STAGE="$2"; shift 2 ;;
        --max-parallel-repos) [[ $# -lt 2 ]] && { echo "Error: --max-parallel-repos requires a value"; exit 1; }; MAX_PARALLEL_REPOS="$2"; shift 2 ;;
        --blind-lint) BLIND_LINT="true"; shift ;;
        --blind-tests) BLIND_TESTS="true"; shift ;;
        --names-only-tests) NAMES_ONLY_TESTS="true"; shift ;;
        --strip-non-stubs) STRIP_NON_STUBS="true"; shift ;;
        --no-test-files-readonly) INJECT_TEST_FILES_READONLY="false"; shift ;;
        --test-files-readonly) INJECT_TEST_FILES_READONLY="true"; shift ;;
        --max-test-output-length) [[ $# -lt 2 ]] && { echo "Error: --max-test-output-length requires a value"; exit 1; }; MAX_TEST_OUTPUT_LENGTH="$2"; shift 2 ;;
        --resume)      RESUME="true"; shift ;;
        -h|--help) print_usage ;;
        --use-claude-code) USE_CLAUDE_CODE="true"; shift ;;
        *) echo "Unknown argument: $1"; print_usage ;;
    esac
done

# Propagate strict-blocking toggle (--go-crazy) to all subprocesses.
export KAIJU_GO_CRAZY="$GO_CRAZY"
export PROBE_TIMEOUT

[[ -z "$MODEL_ARG" ]] && { echo "Error: --model is required"; print_usage; }
[[ -z "$DATASET_ARG" ]] && { echo "Error: --dataset is required"; print_usage; }

# Source shared model resolution (Go/Java/Rust/TS/Python all use this).
# Sourced UNCONDITIONALLY, exactly like the 7 sibling pipelines: if the file is
# ever missing/moved, `source` fails under `set -e` and the run aborts loudly
# rather than silently skipping model resolution AND the Claude Code bridge
# (which would make --use-claude-code a silent no-op).
# shellcheck disable=SC1091
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
    export AWS_SHARED_CREDENTIALS_FILE="/dev/null"
fi

# ============================================================
# Claude Code OAuth bridge (optional --use-claude-code)
# ============================================================
# shellcheck disable=SC1091
source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
claude_code_maybe_start_bridge "$MODEL_NAME"

# ------------------------------------------------------------
# Dataset resolution
# ------------------------------------------------------------
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
            basename="${basename%_c}"
            REPO_SPLIT="$basename"
        fi
        DATASET_SHORT=$(basename "$arg" .json)
        return
    fi

    local candidate="${BASE_DIR}/${arg}_c_dataset.json"
    [[ ! -f "$candidate" ]] && candidate="${BASE_DIR}/${arg}_dataset.json"
    if [[ -f "$candidate" ]]; then
        DATASET_FILE="$candidate"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="${arg}"
        return
    fi

    local known_splits
    known_splits=$("$VENV_PYTHON" -c "
from commit0.harness.constants_c import C_SPLIT
for k in sorted(C_SPLIT.keys()):
    print(k)
" 2>/dev/null || true)

    if echo "$known_splits" | grep -qx "$arg"; then
        DATASET_FILE="wentingzhao/commit0_c"
        REPO_SPLIT="${REPO_SPLIT_OVERRIDE:-$arg}"
        DATASET_SHORT="$arg"
        DATASET_SPLIT="test"
        return
    fi

    echo "Error: Cannot resolve dataset '$arg'"
    echo ""
    echo "Provide one of:"
    echo "  - A path to a .json dataset file"
    echo "  - A known name with a local <name>_c_dataset.json file"
    echo "  - A C_SPLIT key ($(echo "$known_splits" | tr '\n' ',' | sed 's/,$//'))"
    exit 1
}

DATASET_FILE=""
REPO_SPLIT=""
DATASET_SHORT=""
DATASET_SPLIT="test"
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


BASE_BRANCH_NAME="${BRANCH_OVERRIDE:-aider-c-${MODEL_SHORT}-${DATASET_SHORT}}"
if [[ -z "$BRANCH_OVERRIDE" ]] && [[ "$NO_STAGE3_LINT" == "true" ]]; then
    BASE_BRANCH_NAME="${BASE_BRANCH_NAME}-nolint-s3"
fi

BASE_RUN_ID_FLAT=$(echo "${MODEL_SHORT}_${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
DATASET_DIR_NAME=$(echo "${DATASET_SHORT}" | tr -dc 'a-zA-Z0-9._-')
MODEL_DIR_NAME=$(echo "${MODEL_SHORT}" | tr -dc 'a-zA-Z0-9._-')

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
    mkdir -p "$LOG_BASE"
}

# ------------------------------------------------------------
# Logging helpers
# ------------------------------------------------------------
ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] [${RUN_ID:-init}] $1"; }

# ------------------------------------------------------------
# Preflight: clone, build, verify dataset
# ------------------------------------------------------------
preflight() {
    log "Preflight: dataset=${DATASET_FILE} repo_split=${REPO_SPLIT}"
    log "  Model: ${MODEL_NAME} (short=${MODEL_SHORT})"
    log "  Backend: ${BACKEND}"
    log "  Branch: ${BASE_BRANCH_NAME}"

    cat > "${COMMIT0_CONFIG}" <<EOF
dataset_name: ${DATASET_FILE}
dataset_split: ${DATASET_SPLIT}
repo_split: ${REPO_SPLIT}
base_dir: ${REPO_BASE}
EOF

    # Check API keys based on model provider
    if [[ "$MODEL_NAME" == gemini/* ]]; then
        if [[ -z "${GOOGLE_API_KEY:-}" ]]; then
            echo "Error: GOOGLE_API_KEY not set (required for model: $MODEL_NAME)" >&2; exit 1
        fi
    elif [[ "$MODEL_NAME" == vertex_ai/*claude* ]] || [[ "$MODEL_NAME" == vertex_ai_beta/*claude* ]]; then
        if [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "Error: GOOGLE_APPLICATION_CREDENTIALS not set (required for Vertex AI Claude model: $MODEL_NAME)" >&2; exit 1
        fi
    elif [[ "$MODEL_NAME" == vertex_ai/* ]]; then
        if [[ -z "${VERTEX_AI_API_KEY:-}" ]] && [[ -z "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]]; then
            echo "Error: VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS required for model: $MODEL_NAME" >&2; exit 1
        fi
        if [[ -z "${VERTEXAI_LOCATION:-}" ]]; then
            echo "Error: VERTEXAI_LOCATION not set for model: $MODEL_NAME (regional endpoints return HTTP 404; set VERTEXAI_LOCATION=global)" >&2; exit 1
        fi
    fi

    # Containerized run: the repo is already the image's /testbed checkout and
    # eval uses local_inplace (no docker), so skip the host clone + docker build.
    if [[ "${KAIJU_IN_CONTAINER:-0}" == "1" ]]; then
        log "  [in-container] Skipping commit0-c setup + build (image is the sandbox)."
        return 0
    fi

    log "Preflight: running 'commit0-c setup ${REPO_SPLIT}'"
    "$VENV_PYTHON" -m commit0.cli_c setup "$REPO_SPLIT" \
        --dataset-name "$DATASET_FILE" \
        --dataset-split "$DATASET_SPLIT" \
        --base-dir "$REPO_BASE" \
        --commit0-config-file "$COMMIT0_CONFIG"

    log "Preflight: running 'commit0-c build' (backend=${BACKEND})"
    "$VENV_PYTHON" -m commit0.cli_c build \
        --commit0-config-file "$COMMIT0_CONFIG"
}

# ------------------------------------------------------------
# Spec-doc provisioning (mirrors run_pipeline_go.sh)
# ------------------------------------------------------------
ensure_spec_docs_c() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        log "  Spec docs disabled — skipping."
        return 0
    fi

    log "Ensuring spec docs are available for all C repos..."

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

verify_spec_docs_c() {
    if [[ "$USE_SPEC_INFO" != "true" ]]; then
        return 0
    fi

    log "Verifying all C repos have spec docs..."

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
from commit0.harness.constants_c import C_SPLIT
for r in sorted(C_SPLIT.get('${REPO_SPLIT}', [])):
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
        log "FATAL: ${missing} C repo(s) missing spec docs (use_spec_info=true)."
        log "  The pipeline requires spec docs for all repos when --use-spec-info"
        log "  is passed. Missing repos:"
        echo -e "$missing_repos" | while IFS= read -r line; do [[ -n "$line" ]] && log "$line"; done
        log ""
        log "  Options:"
        log "    1. Place spec.pdf or spec.pdf.bz2 in each repo directory"
        log "    2. Add 'specification' URLs to the dataset JSON and re-run"
        log "    3. Re-run without --use-spec-info to skip spec context"
        log "======================================================================"
        return 1
    fi

    log "  All C repos have spec docs. ✓"
}

# ------------------------------------------------------------
# Frozen test-id inventory gate (mirrors verify_spec_docs_c).
# A missing inventory makes the eval SILENTLY score against ALL discovered
# tests — a wrong, non-reproducible denominator. Resolve it with the SAME
# function the eval uses (kaiju.verify_inventory -> find_test_ids_file) so a
# "present" verdict here means the eval will actually find it. FATAL by default;
# --no-strict-inventory (or KAIJU_REQUIRE_INVENTORY=0) downgrades to warn-only.
# ------------------------------------------------------------
verify_inventory_c() {
    log "Verifying all C repos have a frozen test-id inventory..."
    local strict_flag="--strict"
    [[ "$STRICT_INVENTORY" != "true" ]] && strict_flag="--no-strict"
    local ds_arg=()
    [[ -n "${DATASET_FILE:-}" ]] && ds_arg=(--dataset "$DATASET_FILE")
    local split_arg=()
    [[ -n "${REPO_SPLIT:-}" ]] && split_arg=(--repo-split "$REPO_SPLIT")
    if "$VENV_PYTHON" -m kaiju.verify_inventory --language c \
            "${ds_arg[@]}" "${split_arg[@]}" "$strict_flag"; then
        return 0
    fi
    return 1
}

# ------------------------------------------------------------
# Agent config writer (per-stage)
# ------------------------------------------------------------
write_agent_config() {
    # A1 audit fix: aligned with Python/Go/Rust/etc. — (run_tests, use_lint_info,
    # run_entire_dir_lint) all booleans. Removed the unused `stage` string arg that
    # was a maintenance-drift risk (C runner ignored it; only positional 2–4 mattered).
    local run_tests="$1"       # true | false
    local lint_info="$2"       # true | false
    local run_dir_lint="$3"    # true | false

    local user_prompt
    user_prompt=$(cat <<'EOP'
Complete the implementation of the stubbed functions in the ONE source file
added to the chat - the functions whose body calls `STUB_PANIC("...")`.
Focus ONLY on that file. The other stubbed files in this repo are implemented
in SEPARATE runs, so do not read, reference, or try to modify them.
Do not change function signatures or modify any test file
(files under tests/, test/, or matching test_*.c / *_test.c / check_*.c).
Code MUST compile before any test will run: if a function is incomplete,
leave a minimal placeholder that builds rather than a broken stub.
EOP
)

    # NOTE: --use-repo-info is intentionally OFF. It injected the WHOLE repo dir
    # tree into every per-module session (matching python, which keeps it off),
    # framing the agent repo-wide when it may only edit its single target file.
    "$VENV_PYTHON" -m agent.config_c config aider \
        --model-name "$MODEL_NAME" \
        --use-user-prompt \
        --max-iteration "$MAX_ITERATION" \
        --use-unit-tests-info \
        $( [[ "$USE_SPEC_INFO" == "true" ]] && echo "--use-spec-info" || true ) \
        $( [[ "$lint_info" == "true" ]] && echo "--use-lint-info" || true ) \
        $( [[ "$run_tests" == "true" ]] && echo "--run-tests" || true ) \
        $( [[ "$run_dir_lint" == "true" ]] && echo "--run-entire-dir-lint" || echo "--no-run-entire-dir-lint" ) \
        --max-test-output-length "$MAX_TEST_OUTPUT_LENGTH" \
        --agent-config-file "$AGENT_CONFIG" \
        <<< "$user_prompt" > /dev/null
    # Append anti-leakage flags (not in config_c CLI) plus trajectory-capture
    # flags. capture_thinking/output_jsonl are OFF by default in class_types, so
    # without these the C agent writes NO per-module output.json / turns.jsonl /
    # trajectory.md (mirrors run_pipeline_go.sh:497-499 which sets them ON).
    cat >> "$AGENT_CONFIG" <<EOF
blind_lint: ${BLIND_LINT}
blind_tests: ${BLIND_TESTS}
names_only_tests: ${NAMES_ONLY_TESTS}
strip_non_stubs: ${STRIP_NON_STUBS}
inject_test_files_readonly: ${INJECT_TEST_FILES_READONLY}
capture_thinking: true
trajectory_md: true
output_jsonl: true
EOF
}

# ------------------------------------------------------------
# Agent + eval invocation
# ------------------------------------------------------------
AGENT_PID=""
AGENT_ELAPSED=0
AGENT_RC=0
AGENT_NEEDS_RETRY=0

# ============================================================
# Inactivity / wall-time watchdog (QC-C2-002 — ported verbatim from the 6
# sibling pipelines; C was the ONLY driver with no watchdog, so a hung/stuck C
# agent ran unbounded, burning API + wall-time budget with no kill). These
# helpers are PID/log-dir based and language-agnostic.
# ============================================================

get_mtime() {
    stat -c '%Y' "$1" 2>/dev/null \
        || stat -f '%m' "$1" 2>/dev/null \
        || "$VENV_PYTHON" -c "import os,sys; print(int(os.path.getmtime(sys.argv[1])))" "$1" 2>/dev/null \
        || echo "0"
}

# An INTENTIONAL rate-limit pause (recovery drops a re-touched `.rate_limit_paused`
# marker) is not a hang. Returns 0 iff a marker under $1 has mtime within $2s of now.
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

# Signal the whole process group (agent launched under `set -m` leads its own
# group), falling back to the single PID, so forked cmake/gcc/aider children are
# reaped instead of orphaned to keep burning budget after a kill.
_kill_tree() {
    local pid="$1" sig="${2:-TERM}"
    [[ -z "$pid" ]] && return 0
    kill "-${sig}" "-${pid}" 2>/dev/null \
        || kill "-${sig}" "${pid}" 2>/dev/null \
        || true
}

# Cumulative CPU seconds for the whole process group led by $1 — a "still
# computing locally" liveness gate.
_pgroup_cpu_secs() {
    ps -o time= -g "$1" 2>/dev/null | awk '
        { gsub(/ /,""); n=split($0,a,":"); s=0; for(i=1;i<=n;i++) s=s*60+a[i]; t+=s }
        END { printf "%d", t+0 }'
}

# True if any process in group $1 has an ESTABLISHED outbound TCP connection —
# i.e. an LLM request is in flight (server-side thinking is ~0% CPU + no logs).
_pgroup_has_live_conn() {
    command -v lsof >/dev/null 2>&1 || return 2
    local pids
    pids=$(pgrep -g "$1" 2>/dev/null | paste -sd, -)
    [[ -z "$pids" ]] && return 1
    lsof -nP -a -p "$pids" -iTCP -sTCP:ESTABLISHED >/dev/null 2>&1
}

# Return code contract:
#   0    = agent exited successfully
#   124  = watchdog killed agent (inactivity / hard / wall-time)
#   other= agent error (non-zero exit)
watchdog_run() {
    local agent_pid="$1"
    local log_dir="$2"
    local inactivity_limit="$3"
    local hard_timeout="$4"
    local absolute_max="${5:-86400}"
    local start_time
    start_time=$(date +%s)
    local hard_timeout_warned="false"
    local _veto_start=0

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
                _veto_start=0
            fi
        else
            agent_active="true"
            _veto_start=0
        fi

        if [[ "$absolute_max" -gt 0 ]]; then
            local wall_elapsed=$(( now_epoch - start_time ))
            if [[ $wall_elapsed -ge $absolute_max ]]; then
                log "  WATCHDOG: Absolute wall-time cap ${absolute_max}s reached. Force-killing agent."
                _kill_tree "$agent_pid" TERM; sleep 2; _kill_tree "$agent_pid" KILL
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
                    _kill_tree "$agent_pid" TERM; sleep 2; _kill_tree "$agent_pid" KILL
                    wait "$agent_pid" 2>/dev/null || true
                    return 124
                fi
            fi
        fi

        if [[ "$latest_mtime" -gt 0 ]] && [[ "$agent_active" == "false" ]]; then
            if _pause_marker_fresh "$log_dir" "$(( inactivity_limit * 2 ))"; then
                log "  WATCHDOG: log idle ${idle}s but a fresh rate-limit pause marker is present — intentionally paused, not stuck. Continuing."
                _veto_start=0
                continue
            fi
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

run_agent_stage() {
    local stage_label="$1"
    local agent_config="$2"

    log "Stage [${stage_label}]: invoking agent.config_c run on branch=${BRANCH_NAME}"
    local start_time
    start_time=$(date +%s)
    mkdir -p "$LOG_BASE/${stage_label}"
    local _agent_run_log="$LOG_BASE/${stage_label}/agent_run.log"
    log "  Running agent (watchdog: inactivity=${INACTIVITY_TIMEOUT}s, hard=${STAGE_TIMEOUT}s, wall-cap=${MAX_WALL_TIME}s) — output → ${_agent_run_log}"
    set +e
    # Unbuffered Python so streamed progress advances the log mtime the watchdog
    # reads; launch under monitor mode so the agent leads its own process group
    # and the watchdog can reap the whole tree on a kill.
    export PYTHONUNBUFFERED=1
    set -m
    "$VENV_PYTHON" -m agent.config_c run "$BRANCH_NAME" \
        --backend "$BACKEND" \
        --agent-config-file "$agent_config" \
        --commit0-config-file "$COMMIT0_CONFIG" \
        --log-dir "$LOG_BASE/${stage_label}" \
        --max-parallel-repos "$MAX_PARALLEL_REPOS" \
        >>"$_agent_run_log" 2>&1 &
    local _apid=$!
    set +m
    AGENT_PID=$_apid
    watchdog_run "$_apid" "$LOG_BASE/${stage_label}" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
    AGENT_RC=$?
    AGENT_PID=""
    set -e
    local end_time
    end_time=$(date +%s)
    AGENT_ELAPSED=$(( end_time - start_time ))
    if [[ $AGENT_RC -eq 124 ]]; then
        log "  Agent KILLED by watchdog after ${AGENT_ELAPSED}s (inactivity/hard/wall-time)"
    else
        log "  Agent finished in ${AGENT_ELAPSED}s (rc=${AGENT_RC})"
    fi

    # A module that exhausted its transient-error retries is left WITHOUT a .done
    # marker plus a .needs_retry breadcrumb (run_agent_c.py::_skip_failed_module),
    # and the repo continues so other modules aren't discarded.
    _sweep_limbo_modules "$LOG_BASE/${stage_label}"
    AGENT_NEEDS_RETRY=$(find "$LOG_BASE/${stage_label}" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')

    # AUTO-RESUME: never leave the run needing a MANUAL --resume. For large batches
    # that's wasteful (re-setup, split data, a human in the loop). Instead re-run
    # the failed module(s) in-place, right here, up to K rounds with a pause so a
    # sustained provider outage has time to clear. KAIJU_RESUME=1 rebuilds the
    # branch from per-module patches and the agent skips modules that already have
    # a .done marker, so each round only re-attempts the .needs_retry ones. A module
    # that succeeds now clears its .needs_retry and gets .done (_mark_module_done),
    # so the count converges to 0 unless the failure is GENUINELY persistent.
    local _auto_max="${KAIJU_AUTO_RESUME_ROUNDS:-3}"
    local _auto=0
    while [[ "${AGENT_NEEDS_RETRY:-0}" -gt 0 && "$_auto" -lt "$_auto_max" ]]; do
        _auto=$((_auto + 1))
        local _pause="${KAIJU_AUTO_RESUME_PAUSE:-60}"
        log "  AUTO-RESUME ${_auto}/${_auto_max}: ${AGENT_NEEDS_RETRY} module(s) left .needs_retry — waiting ${_pause}s then re-running the failed module(s) in-place (no manual --resume)."
        sleep "$_pause"
        local _rstart _rend
        _rstart=$(date +%s)
        set +e
        export PYTHONUNBUFFERED=1
        set -m
        KAIJU_RESUME=1 "$VENV_PYTHON" -m agent.config_c run "$BRANCH_NAME" \
            --backend "$BACKEND" \
            --agent-config-file "$agent_config" \
            --commit0-config-file "$COMMIT0_CONFIG" \
            --log-dir "$LOG_BASE/${stage_label}" \
            --max-parallel-repos "$MAX_PARALLEL_REPOS" \
            >>"$LOG_BASE/${stage_label}/agent_run.log" 2>&1 &
        local _rpid=$!
        set +m
        AGENT_PID=$_rpid
        watchdog_run "$_rpid" "$LOG_BASE/${stage_label}" "$INACTIVITY_TIMEOUT" "$STAGE_TIMEOUT" "$MAX_WALL_TIME"
        AGENT_RC=$?
        AGENT_PID=""
        set -e
        _rend=$(date +%s)
        AGENT_ELAPSED=$(( AGENT_ELAPSED + (_rend - _rstart) ))
        _sweep_limbo_modules "$LOG_BASE/${stage_label}"
    AGENT_NEEDS_RETRY=$(find "$LOG_BASE/${stage_label}" -name '.needs_retry' 2>/dev/null | wc -l | tr -d ' ')
        log "  AUTO-RESUME ${_auto}/${_auto_max} finished (rc=${AGENT_RC}); ${AGENT_NEEDS_RETRY} module(s) still .needs_retry."
    done

    if [[ "${AGENT_NEEDS_RETRY:-0}" -gt 0 ]]; then
        log "  WARNING: ${AGENT_NEEDS_RETRY} module(s) STILL .needs_retry after ${_auto_max} auto-resume round(s) — GENUINELY persistent (not a passing transient); run is INCOMPLETE."
        # STRICT-BLOCKING: fail loudly unless --go-crazy was passed. Enforces the
        # "no proceeding past .needs_retry orphans" contract so batch scores stay
        # meaningful (a silent skip lets unimplementable modules dilute the result).
        if [[ "${GO_CRAZY:-false}" != "true" ]]; then
            log "  FATAL (strict-blocking): halting stage. Pass --go-crazy to bypass and continue anyway."
            exit 1
        fi
    elif [[ "$_auto" -gt 0 ]]; then
        log "  AUTO-RESUME succeeded: all modules completed after ${_auto} round(s); run is COMPLETE (no manual --resume needed)."
    fi
}

# ------------------------------------------------------------
# Eval globals (mirror run_pipeline_go.sh) — populated by run_evaluate /
# parse_eval_output so each stage records the full canonical result.
# ------------------------------------------------------------
EVAL_NUM_PASSED=0
EVAL_NUM_TESTS=0
EVAL_PASS_RATE="0.0"
EVAL_COMPILE_ERRORS="0"
EVAL_ELAPSED=0
# Distinguishes a real "0 of N passed" from "eval did not run". Values:
# OK | NO_RESULTS | EVAL_FAILED | EVAL_TIMEOUT.
EVAL_STATUS="OK"

# Evaluate a bc expression and emit JSON-safe output (re-add a leading 0 that bc
# drops on values < 1, which jq --argjson rejects). Mirrors run_pipeline_go.sh.
bc_json() {
    local _out
    _out=$(echo "$1" | bc) || return 1
    printf '%s\n' "$_out" | sed -E 's/^(-?)\./\10./'
}

# Preserve per-repo eval artifacts into the run's output tree. The C harness
# writes test_report.xml / compile_errors.txt / *_exit_code.txt / eval.sh /
# patch.diff under logs/c_test*/<repo>/<BRANCH_NAME>/<hash>/, which is NOT under
# outputs/<uuid>/ and is lost on teardown — leaving a COMPILE_FAILED undebuggable.
# Copied per stage so each keeps its own snapshot. Mirrors run_pipeline_go.sh.
collect_eval_artifacts() {
    local stage_label="${1:-eval}"
    [[ -n "${LOG_BASE:-}" && -n "${BRANCH_NAME:-}" ]] || return 0
    local dest="${LOG_BASE}/${stage_label}_eval_artifacts"
    local bdir hdir repo out found=0
    shopt -s nullglob
    for bdir in logs/*_test*/*/"${BRANCH_NAME}"; do
        [[ -d "$bdir" ]] || continue
        repo=$(basename "$(dirname "$bdir")")
        for hdir in "$bdir"/*/; do
            [[ -d "$hdir" ]] || continue
            out="${dest}/${repo}"
            mkdir -p "$out"
            find "$hdir" -maxdepth 1 -type f \( \
                -name 'test_output.txt' -o -name 'test_output.json' \
                -o -name 'test_report.xml' -o -name 'compile_errors.txt' \
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

run_evaluate() {
    local stage_label="$1"
    local eval_log="${LOG_BASE}/${stage_label}_eval.log"
    log "Stage [${stage_label}]: running 'commit0-c evaluate' (timeout=${EVAL_TIMEOUT}s)"

    local start_time
    start_time=$(date +%s)

    # Redirect (not tee) + wrap in timeout so the real eval rc is preserved (tee
    # would mask it) and a hung eval is bounded/classified. Mirrors go.
    set +e
    timeout "$EVAL_TIMEOUT" "$VENV_PYTHON" -m commit0.cli_c evaluate \
        --branch "$BRANCH_NAME" \
        --backend "$BACKEND" \
        --timeout "${KAIJU_EVAL_HARNESS_TIMEOUT:-$EVAL_TIMEOUT}" \
        --commit0-config-file "$COMMIT0_CONFIG" \
        >"$eval_log" 2>&1
    local eval_rc=$?
    set -e

    local end_time
    end_time=$(date +%s)
    EVAL_ELAPSED=$(( end_time - start_time ))
    log "  Evaluation finished in ${EVAL_ELAPSED}s (rc=${eval_rc})"

    collect_eval_artifacts "${stage_label}"

    parse_eval_output "$eval_log"   # sets EVAL_* incl. EVAL_STATUS=OK|NO_RESULTS

    # Refine status from the eval process exit code, but ONLY when parsing found
    # no results at all — a non-zero rc with no parseable rows is a broken eval,
    # NOT a 0% score. A parsed COMPILE_FAILED/OUTPUT_MISSING is a real (excluded)
    # outcome and must be preserved, not clobbered into EVAL_FAILED.
    if [[ "$EVAL_STATUS" == "NO_RESULTS" ]]; then
        if [[ $eval_rc -eq 124 ]]; then
            EVAL_STATUS="EVAL_TIMEOUT"
        elif [[ $eval_rc -ne 0 ]]; then
            EVAL_STATUS="EVAL_FAILED"
        fi
    fi

    # An incomplete run (a module left .needs_retry) is not a clean measurement,
    # even when the eval parsed OK — flag it so the recorded score is treated as
    # partial/resumable, not final. A worse status (COMPILE_FAILED/EVAL_*) is kept.
    if [[ "${AGENT_NEEDS_RETRY:-0}" -gt 0 && "$EVAL_STATUS" == "OK" ]]; then
        EVAL_STATUS="INCOMPLETE_NEEDS_RETRY"
    fi

    if [[ $eval_rc -ne 0 || "$EVAL_STATUS" != "OK" ]]; then
        log "  Evaluation issue (rc=${eval_rc}, status=${EVAL_STATUS}) — last 10 lines:"
        tail -10 "$eval_log" 2>/dev/null | while IFS= read -r line; do log "    | $line"; done
    fi
}

# ------------------------------------------------------------
# Results JSON
# ------------------------------------------------------------
RESULTS_JSON=""

init_results() {
    RESULTS_JSON=$(jq -n \
        --arg run_id "$RUN_ID" \
        --arg model "$MODEL_SHORT" \
        --arg model_short "$MODEL_SHORT" \
        --arg model_name "$MODEL_NAME" \
        --arg branch "$BRANCH_NAME" \
        --arg backend "$BACKEND" \
        --arg repo_split "$REPO_SPLIT" \
        --arg dataset "$DATASET_FILE" \
        --arg dataset_short "$DATASET_SHORT" \
        --argjson max_iter "$MAX_ITERATION" \
        --arg cache_prompts "true" \
        --arg start_time "$(ts)" \
        --arg language "c" \
        '{
            language: $language,
            run_id: $run_id,
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
    save_results
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
    # Atomic write: write to a temp file, validate non-empty valid JSON, then
    # rename. Keeps a prior good run intact if jq fails. Mirrors go.
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

format_pct() {
    local val="$1"
    if ! [[ "$val" =~ ^-?[0-9]*\.?[0-9]+$ ]]; then
        val=0
    fi
    printf "%.1f%%" "$(echo "$val * 100" | bc)"
}

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
            # Redirect to stderr: this function's stdout is captured via $(...)
            # by the stage functions; a log line on stdout would be prepended to
            # the "<cost> <source>" payload and break the downstream jq --argjson.
            log "  WARNING: cost extraction found NO output.json/aider.log cost in ${log_dir} — reporting \$0.0000 but this is an EXTRACTION FAILURE, not a free run." >&2
        fi
        echo "$cost_part ${source_part:-none}"
    else
        log "  WARNING: cost extraction returned unparseable result [${result}] for ${log_dir}; defaulting to \$0.0000." >&2
        echo "0.0000 parse_error"
    fi
}

# Parse the C evaluate output. CSV shape (evaluate_c.py):
#   repo,compile_errors,num_passed/num_tests,status
# Sums num_passed / num_tests across repos (mirrors go's parse), reads the
# summary "average pass rate" + "mean compile_errors" lines, and sets
# EVAL_STATUS=OK|NO_RESULTS so a broken eval never masquerades as a 0/N score.
parse_eval_output() {
    local eval_file="$1"

    EVAL_NUM_PASSED=0
    EVAL_NUM_TESTS=0
    EVAL_PASS_RATE="0.0"
    EVAL_COMPILE_ERRORS="0"

    local total_passed=0
    local total_tests=0
    local found_any="false"
    local row_status=""

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        [[ "$line" == repo,* ]] && continue
        # A data row has "<repo>,<compile_errors>,<passed>/<total>[,status]".
        if [[ "$line" == *","*"/"* ]]; then
            local passed_total passed total _st
            passed_total=$(echo "$line" | cut -d',' -f3 | tr -d ' ')
            # 4th column = per-repo status (TESTS_RAN on success; COMPILE_FAILED /
            # OUTPUT_MISSING when the build broke or a module never completed, e.g.
            # a .needs_retry that timed out). Any non-TESTS_RAN status means 0/N is
            # NOT a genuine model score — surface it so eval_status isn't a bogus OK.
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
                    found_any="true"
                fi
            fi
        fi
    done < "$eval_file"

    if [[ "$found_any" == "true" ]]; then
        # A parsed COMPILE_FAILED/OUTPUT_MISSING row means the 0/N is a build/infra
        # failure (not a real 0% model score); propagate it so downstream never
        # reads it as a legit result. TESTS_RAN (or an old statusless row) -> OK.
        if [[ -n "$row_status" ]]; then
            EVAL_STATUS="$row_status"
        else
            EVAL_STATUS="OK"
        fi
        EVAL_NUM_PASSED="$total_passed"
        EVAL_NUM_TESTS="$total_tests"
        if [[ "$total_tests" -gt 0 ]]; then
            EVAL_PASS_RATE=$(bc_json "scale=6; $total_passed / $total_tests")
        fi
    else
        EVAL_STATUS="NO_RESULTS"
    fi

    # Prefer the harness-computed (excluded-aware) average pass rate when present.
    local avg_line rate
    avg_line=$(grep -i "average pass rate:" "$eval_file" | tail -n1 || true)
    if [[ -n "$avg_line" ]]; then
        rate=$(echo "$avg_line" | awk -F':' '{print $NF}' | tr -d ' ')
        if [[ -n "$rate" ]] && [[ "$rate" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
            EVAL_PASS_RATE="$rate"
        fi
    fi

    local ce_line ce
    ce_line=$(grep -oE 'mean compile_errors: [0-9.]+' "$eval_file" | tail -n1 || true)
    if [[ -n "$ce_line" ]]; then
        ce=$(echo "$ce_line" | awk '{print $NF}')
        [[ "$ce" =~ ^[0-9]+(\.[0-9]+)?$ ]] && EVAL_COMPILE_ERRORS="$ce"
    fi
}

# ------------------------------------------------------------
# Three-stage pipeline
# ------------------------------------------------------------
stage_1_draft() {
    log "===== Stage 1: Draft (no lint, no tests) ====="
    # Draft implements the stubbed function bodies: run_tests=false AND
    # run_dir_lint=false so the C runner takes the draft `else` branch
    # (coder.run(message) with current_stage="draft"), NOT the lint branch.
    # (Was `true`, which forced run_entire_dir_lint -> the agent LINTED in the
    # draft stage and every turn was mislabeled stage=lint. Matches Go's draft
    # call `write_agent_config false false false`  # (run_tests, lint_info, run_dir_lint).)
    write_agent_config false false false
    run_agent_stage "stage1_draft" "$AGENT_CONFIG"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local _co cost cost_source
    _co=$(extract_all_stage_costs "$LOG_BASE/stage1_draft")
    cost="${_co%% *}"; cost_source="${_co#* }"
    log "  Stage 1 cost: \$${cost} (source: ${cost_source})"

    if _agent_crashed_pre_llm "$LOG_BASE/stage1_draft" "$rc"; then
        log "  Stage 1 AGENT CRASHED PRE-LLM (rc=${rc}, no artifacts under $LOG_BASE/stage1_draft). Skipping evaluate; see agent_run.log."
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
                num_passed: 0,
                num_tests: 0,
                pass_rate: 0,
                runtime: 0,
                mean_compile_errors: 0,
                eval_status: "not_run",
                sample_failed: true,
                failure_reason: "agent_crashed_pre_llm"
            }')
        save_results
        return 1
    fi

    run_evaluate "stage1"
    local eval_time="$EVAL_ELAPSED"
    log "  Stage 1 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Draft (no feedback)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost "$cost" \
        --arg cost_source "$cost_source" \
        --argjson rc "$rc" \
        --argjson num_passed "$EVAL_NUM_PASSED" \
        --argjson num_tests "$EVAL_NUM_TESTS" \
        --argjson pass_rate "$EVAL_PASS_RATE" \
        --argjson compile_errors "$EVAL_COMPILE_ERRORS" \
        --arg eval_status "$EVAL_STATUS" \
        '.stage1 = {
            name: $name,
            elapsed_s: $elapsed,
            eval_time_s: $eval_time,
            cost_usd: $cost,
            cost_source: $cost_source,
            returncode: $rc,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate,
            runtime: $eval_time,
            mean_compile_errors: $compile_errors,
            eval_status: $eval_status
        }')
    save_results
    log "Stage 1 complete: pass_rate=$EVAL_PASS_RATE compile_errors=$EVAL_COMPILE_ERRORS"
}

stage_2_lint_refine() {
    log "===== Stage 2: Lint refine (clang-tidy + cppcheck) ====="
    write_agent_config false true true
    run_agent_stage "stage2_lint" "$AGENT_CONFIG"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s1_cost
    s1_cost=$(echo "$RESULTS_JSON" | jq -r '.stage1.cost_usd // 0')
    local _co s2_incremental cost_source
    _co=$(extract_all_stage_costs "$LOG_BASE/stage2_lint")
    s2_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(bc_json "scale=4; $s1_cost + $s2_incremental")
    log "  Stage 2 incremental cost: \$${s2_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    if _agent_crashed_pre_llm "$LOG_BASE/stage2_lint" "$rc"; then
        log "  Stage 2 AGENT CRASHED PRE-LLM (rc=${rc}, no artifacts under $LOG_BASE/stage2_lint). Skipping evaluate; see agent_run.log."
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
            --arg name "Lint refine (clang-tidy+cppcheck)" \
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
                num_passed: 0,
                num_tests: 0,
                pass_rate: 0,
                runtime: 0,
                mean_compile_errors: 0,
                eval_status: "not_run",
                sample_failed: true,
                failure_reason: "agent_crashed_pre_llm"
            }')
        save_results
        return 1
    fi

    run_evaluate "stage2"
    local eval_time="$EVAL_ELAPSED"
    log "  Stage 2 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Lint refine (clang-tidy+cppcheck)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost_inc "$s2_incremental" \
        --argjson cost_cum "$total_cost" \
        --arg cost_source "$cost_source" \
        --argjson rc "$rc" \
        --argjson num_passed "$EVAL_NUM_PASSED" \
        --argjson num_tests "$EVAL_NUM_TESTS" \
        --argjson pass_rate "$EVAL_PASS_RATE" \
        --argjson compile_errors "$EVAL_COMPILE_ERRORS" \
        --arg eval_status "$EVAL_STATUS" \
        '.stage2 = {
            name: $name,
            elapsed_s: $elapsed,
            eval_time_s: $eval_time,
            cost_usd_incremental: $cost_inc,
            cost_usd_cumulative: $cost_cum,
            cost_source: $cost_source,
            returncode: $rc,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate,
            runtime: $eval_time,
            mean_compile_errors: $compile_errors,
            eval_status: $eval_status
        }')
    save_results
    log "Stage 2 complete: pass_rate=$EVAL_PASS_RATE compile_errors=$EVAL_COMPILE_ERRORS"
}

stage_3_test_refine() {
    log "===== Stage 3: Test refine (run CTest, feed failures back) ====="
    local lint_info="true"
    [[ "$NO_STAGE3_LINT" == "true" ]] && lint_info="false"
    write_agent_config true "$lint_info" false
    run_agent_stage "stage3_test" "$AGENT_CONFIG"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local s2_cumulative
    s2_cumulative=$(echo "$RESULTS_JSON" | jq -r '.stage2.cost_usd_cumulative // 0')
    local _co s3_incremental cost_source
    _co=$(extract_all_stage_costs "$LOG_BASE/stage3_test")
    s3_incremental="${_co%% *}"; cost_source="${_co#* }"
    local total_cost
    total_cost=$(bc_json "scale=4; $s2_cumulative + $s3_incremental")
    log "  Stage 3 incremental cost: \$${s3_incremental} (cumulative: \$${total_cost}, source: ${cost_source})"

    if _agent_crashed_pre_llm "$LOG_BASE/stage3_test" "$rc"; then
        log "  Stage 3 AGENT CRASHED PRE-LLM (rc=${rc}, no artifacts under $LOG_BASE/stage3_test). Skipping evaluate; see agent_run.log."
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
            --arg name "Test refine (CTest feedback)" \
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
                num_passed: 0,
                num_tests: 0,
                pass_rate: 0,
                runtime: 0,
                mean_compile_errors: 0,
                eval_status: "not_run",
                sample_failed: true,
                failure_reason: "agent_crashed_pre_llm"
            }')
        save_results
        return 1
    fi

    run_evaluate "stage3"
    local eval_time="$EVAL_ELAPSED"
    log "  Stage 3 results: ${EVAL_NUM_PASSED}/${EVAL_NUM_TESTS} ($(format_pct "$EVAL_PASS_RATE"))"

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --arg name "Test refine (CTest feedback)" \
        --argjson elapsed "$elapsed" \
        --argjson eval_time "$eval_time" \
        --argjson cost_inc "$s3_incremental" \
        --argjson cost_cum "$total_cost" \
        --arg cost_source "$cost_source" \
        --argjson rc "$rc" \
        --argjson num_passed "$EVAL_NUM_PASSED" \
        --argjson num_tests "$EVAL_NUM_TESTS" \
        --argjson pass_rate "$EVAL_PASS_RATE" \
        --argjson compile_errors "$EVAL_COMPILE_ERRORS" \
        --arg eval_status "$EVAL_STATUS" \
        '.stage3 = {
            name: $name,
            elapsed_s: $elapsed,
            eval_time_s: $eval_time,
            cost_usd_incremental: $cost_inc,
            cost_usd_cumulative: $cost_cum,
            cost_source: $cost_source,
            returncode: $rc,
            num_passed: $num_passed,
            num_tests: $num_tests,
            pass_rate: $pass_rate,
            runtime: $eval_time,
            mean_compile_errors: $compile_errors,
            eval_status: $eval_status
        }')
    save_results
    log "Stage 3 complete: pass_rate=$EVAL_PASS_RATE compile_errors=$EVAL_COMPILE_ERRORS"
}

cleanup() {
    # Idempotent/re-entrant: reset traps immediately so a second Ctrl-C (or a
    # SIGTERM arriving during our own kill escalation) doesn't re-enter cleanup
    # and leave the agent tree alive. Parity with the sibling pipelines.
    trap - INT TERM EXIT
    # Reap the backgrounded agent + its cmake/gcc/aider children on interrupt so
    # they don't orphan and keep burning API/wall-time budget (the agent now runs
    # under a watchdog in its own process group — QC-C2-002).
    if [[ -n "${AGENT_PID:-}" ]] && kill -0 "$AGENT_PID" 2>/dev/null; then
        _kill_tree "$AGENT_PID" TERM
        sleep 2
        _kill_tree "$AGENT_PID" KILL
    fi
    if [[ -f "$COMMIT0_CONFIG" ]]; then
        log "Cleanup: leaving $COMMIT0_CONFIG (delete manually if desired)"
    fi
}

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

    mkdir -p "$LOG_BASE"
    exec > >(tee -a "$LOG_BASE/pipeline.log") 2>&1

    if [[ "$sample_idx" -eq 1 ]]; then
        preflight
        ensure_spec_docs_c
        if ! verify_spec_docs_c; then
            exit 1
        fi
        if ! verify_inventory_c; then
            exit 1
        fi
    fi

    # When skipping to a later stage, load the prior results into RESULTS_JSON so
    # stage2/stage3 can read the prior cumulative cost; otherwise start fresh.
    if [[ -n "${SKIP_TO_STAGE:-}" ]] && [[ "${SKIP_TO_STAGE}" != "1" ]]; then
        if [[ ! -f "$PIPELINE_LOG" ]]; then
            log "ERROR: Cannot skip to stage ${SKIP_TO_STAGE}: no prior results at ${PIPELINE_LOG}"
            return 1
        fi
        RESULTS_JSON=$(cat "$PIPELINE_LOG")
        log "  Loaded prior results from: ${PIPELINE_LOG}"
    else
        init_results
    fi

    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq \
        --argjson sample_idx "$sample_idx" \
        --argjson num_samples "$NUM_SAMPLES" \
        '.sample_index = $sample_idx | .num_samples = $num_samples')

    local pipeline_error=""

    case "${SKIP_TO_STAGE:-}" in
        ""|1)
            stage_1_draft || pipeline_error="Stage 1 failed"
            [[ -z "$pipeline_error" ]] && { stage_2_lint_refine || pipeline_error="Stage 2 failed"; }
            [[ -z "$pipeline_error" ]] && { stage_3_test_refine || pipeline_error="Stage 3 failed"; }
            ;;
        2)
            stage_2_lint_refine || pipeline_error="Stage 2 failed"
            [[ -z "$pipeline_error" ]] && { stage_3_test_refine || pipeline_error="Stage 3 failed"; }
            ;;
        3)
            stage_3_test_refine || pipeline_error="Stage 3 failed"
            ;;
        *)
            echo "Error: invalid --skip-to-stage value '${SKIP_TO_STAGE}'"
            exit 1
            ;;
    esac

    if [[ -n "$pipeline_error" ]]; then
        log "PIPELINE ERROR: ${pipeline_error}"
        RESULTS_JSON=$(echo "$RESULTS_JSON" | jq --arg err "$pipeline_error" '.error = $err')
    fi
    RESULTS_JSON=$(echo "$RESULTS_JSON" | jq --arg end_ts "$(ts)" '.end_time = $end_ts')
    save_results

    cleanup
    log "Pipeline complete. Results: $PIPELINE_LOG"
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
            && log "ATIF conversion complete -> ${_HARBOR_TRAJ_OUT}" \
            || log "[WARN] ATIF conversion failed"
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

main() {
    local any_failed=0
    mkdir -p "${BASE_DIR}/logs"
    # 1-indexed (mirrors run_pipeline_go.sh) so the first sample lands in run_1.
    for sample_idx in $(seq 1 "$NUM_SAMPLES"); do
        # (Re-)arm the exit/interrupt trap for THIS sample. run_single_sample
        # calls cleanup() on its success path (which resets the EXIT trap), so
        # re-arm each iteration to keep the interrupt-reap active for the agent
        # launched in the next sample.
        trap cleanup EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        log "===== Sample ${sample_idx} / ${NUM_SAMPLES} ====="
        if ! run_single_sample "$sample_idx"; then
            any_failed=1
            log "WARNING: sample ${sample_idx} failed — continuing with remaining samples."
        fi
    done
    return $any_failed
}

main "$@"
[[ $? -eq 0 ]] || exit 1
