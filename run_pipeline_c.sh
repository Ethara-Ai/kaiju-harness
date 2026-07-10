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

if [[ -f "${BASE_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${BASE_DIR}/.env"
    set +a
fi
source "${BASE_DIR}/scripts/_outputs_layout.sh"
"${BASE_DIR}/scripts/generate_aider_config.sh"

REPO_BASE="${BASE_DIR}/repos"
VENV_PYTHON="${BASE_DIR}/.venv/bin/python"
BACKEND="local"
MAX_ITERATION=3

MODEL_ARG=""
USE_CLAUDE_CODE="false"
DATASET_ARG=""
BRANCH_OVERRIDE=""
REPO_SPLIT_OVERRIDE=""
STAGE_TIMEOUT=0
EVAL_TIMEOUT=3600
NO_STAGE3_LINT="false"
USE_SPEC_INFO="true"
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
INJECT_TEST_FILES_READONLY="true"

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
  --inactivity-timeout <s>   Kill agent if no log activity for N seconds (default: 900)
  --max-wall-time  <secs>    Absolute per-stage wall-time cap (default: 86400)
  --num-samples    <n>       Number of independent samples (default: 1)
  --skip-to-stage  <1|2|3>   Skip to stage N (reuse prior stages)
  --blind-lint               Stage 2 sees only "lint failed: N issues" (default: full output)
  --blind-tests              Stage 3 sees only summary line, no per-test failures (default: full output)
  --names-only-tests         Stage 3 sees only failed test node IDs + count (default: full output)
  --strip-non-stubs          Hide non-stubbed source files from agent context (default: visible)
  --no-test-files-readonly   Remove test source files from read-only agent context (default: injected)
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
        --use-spec-info) USE_SPEC_INFO="true"; shift ;;
        --no-spec-info) USE_SPEC_INFO="false"; shift ;;
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
        --resume)      RESUME="true"; shift ;;
        -h|--help) print_usage ;;
        --use-claude-code) USE_CLAUDE_CODE="true"; shift ;;
        *) echo "Unknown argument: $1"; print_usage ;;
    esac
done

[[ -z "$MODEL_ARG" ]] && { echo "Error: --model is required"; print_usage; }
[[ -z "$DATASET_ARG" ]] && { echo "Error: --dataset is required"; print_usage; }

# Source shared model resolution (Go/Java/Rust/TS/Python all use this).
if [[ -f "${BASE_DIR}/commit0/harness/resolve_model.sh" ]]; then
    # shellcheck disable=SC1091
    source "${BASE_DIR}/commit0/harness/resolve_model.sh"
    resolve_model "$MODEL_ARG"

# ============================================================
# Claude Code OAuth bridge (optional --use-claude-code)
# ============================================================
source "${BASE_DIR}/scripts/_claude_code_pipeline_helper.sh"
claude_code_maybe_start_bridge "$MODEL_NAME"
else
    MODEL_NAME="$MODEL_ARG"
    MODEL_SHORT="$MODEL_ARG"
fi

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
# Agent config writer (per-stage)
# ------------------------------------------------------------
write_agent_config() {
    local stage="$1"           # draft | lint | test
    local lint_info="$2"       # true | false
    local run_tests="$3"       # true | false
    local run_dir_lint="$4"    # true | false

    local user_prompt
    user_prompt=$(cat <<'EOP'
You need to complete the implementations for all stubbed functions
(those whose body calls `STUB_PANIC("...")`) and pass the unit tests.
Do not change function signatures or modify any test file
(files under tests/, test/, or matching test_*.c / *_test.c / check_*.c).
Code MUST compile before any test will run: if a function is incomplete,
leave a minimal placeholder that builds rather than a broken stub.
EOP
)

    "$VENV_PYTHON" -m agent.config_c config aider \
        --model-name "$MODEL_NAME" \
        --use-user-prompt \
        --max-iteration "$MAX_ITERATION" \
        --use-repo-info \
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
AGENT_ELAPSED=0
AGENT_RC=0

run_agent_stage() {
    local stage_label="$1"
    local agent_config="$2"

    log "Stage [${stage_label}]: invoking agent.config_c run on branch=${BRANCH_NAME}"
    local start_time
    start_time=$(date +%s)
    set +e
    "$VENV_PYTHON" -m agent.config_c run "$BRANCH_NAME" \
        --backend "$BACKEND" \
        --agent-config-file "$agent_config" \
        --commit0-config-file "$COMMIT0_CONFIG" \
        --log-dir "$LOG_BASE/${stage_label}" \
        --max-parallel-repos "$MAX_PARALLEL_REPOS"
    AGENT_RC=$?
    set -e
    local end_time
    end_time=$(date +%s)
    AGENT_ELAPSED=$(( end_time - start_time ))
    log "  Agent finished in ${AGENT_ELAPSED}s (rc=${AGENT_RC})"
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
        --timeout "$EVAL_TIMEOUT" \
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

    # Refine status from the eval process exit code. A non-zero rc with no
    # parseable results is a broken eval, NOT a 0% score.
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

# ------------------------------------------------------------
# Results JSON
# ------------------------------------------------------------
RESULTS_JSON=""

init_results() {
    RESULTS_JSON=$(jq -n \
        --arg run_id "$RUN_ID" \
        --arg model "$MODEL_SHORT" \
        --arg model_short "$MODEL_SHORT" \
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

    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        [[ "$line" == repo,* ]] && continue
        # A data row has "<repo>,<compile_errors>,<passed>/<total>[,status]".
        if [[ "$line" == *","*"/"* ]]; then
            local passed_total passed total
            passed_total=$(echo "$line" | cut -d',' -f3 | tr -d ' ')
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
        EVAL_STATUS="OK"
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
    # call `write_agent_config "false" "false" "false" "false"`.)
    write_agent_config "draft" false false false
    run_agent_stage "stage1_draft" "$AGENT_CONFIG"
    local elapsed="$AGENT_ELAPSED"
    local rc="$AGENT_RC"

    local _co cost cost_source
    _co=$(extract_all_stage_costs "$LOG_BASE/stage1_draft")
    cost="${_co%% *}"; cost_source="${_co#* }"
    log "  Stage 1 cost: \$${cost} (source: ${cost_source})"

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
            runtime: 0,
            mean_compile_errors: $compile_errors,
            eval_status: $eval_status
        }')
    save_results
    log "Stage 1 complete: pass_rate=$EVAL_PASS_RATE compile_errors=$EVAL_COMPILE_ERRORS"
}

stage_2_lint_refine() {
    log "===== Stage 2: Lint refine (clang-tidy + cppcheck) ====="
    write_agent_config "lint" true false true
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
            runtime: 0,
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
    write_agent_config "test" "$lint_info" true false
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
            runtime: 0,
            mean_compile_errors: $compile_errors,
            eval_status: $eval_status
        }')
    save_results
    log "Stage 3 complete: pass_rate=$EVAL_PASS_RATE compile_errors=$EVAL_COMPILE_ERRORS"
}

cleanup() {
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
        "${BASE_DIR}/.venv/bin/python" "${BASE_DIR}/scripts/commit0_to_atif_v2.py" \
            "$LOG_BASE" \
            "${BASE_DIR}/Harbor_Data/Trajectory" \
            --kaiju-mode \
            --pipeline "$PIPELINE_LOG" \
            --task-name "$DATASET_DIR_NAME" \
            && log "ATIF conversion complete" \
            || log "[WARN] ATIF conversion failed"
    fi
}

main() {
    mkdir -p "${BASE_DIR}/logs"
    # 1-indexed (mirrors run_pipeline_go.sh) so the first sample lands in run_1.
    for sample_idx in $(seq 1 "$NUM_SAMPLES"); do
        log "===== Sample ${sample_idx} / ${NUM_SAMPLES} ====="
        run_single_sample "$sample_idx"
    done
}

main "$@"
