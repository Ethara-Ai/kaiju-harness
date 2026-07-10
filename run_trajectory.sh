#!/usr/bin/env bash
# ============================================================================
# run_trajectory.sh — Universal SDE-I trajectory generator (commit0 / kaiju)
# ----------------------------------------------------------------------------
# ONE command runs the whole flow for any repo / language / model:
#     prepare  ->  build repo image  ->  run (in container)  ->  verify
# It runs in bash (not zsh) so unmatched globs never error, and calls python
# via heredocs so multi-line paste can't break a string.
#
# WHAT EACH STAGE DOES
#   [1] prepare  clone the repo, stub the source (remove impls, keep signatures),
#                run the "stubbed base compiles" gate (A11), capture the canonical
#                test-id inventory (scoring denominator), fork+push, write the
#                dataset JSON, and stage the inventory for the container mount.
#   [2] bridge   start the subscription bridge on the host if it isn't already up
#                (the container reaches it at host.docker.internal). Auto-selected
#                from --model. Skipped for providers that use direct host creds.
#   [3] build    build the repo Docker image (commit0.cli_<lang> build).
#   [4] run      run the FULL pipeline INSIDE the container (all 3 stages:
#                draft -> lint -> test, per-stage reward-hardened eval). Outputs
#                are host-mounted, so a kill can't lose data.
#   [5] verify   print per-stage pass/N, cost_source, CHEAT scan, turns.jsonl and
#                ATIF trajectory counts.
#
# ============================================================================
# QUICK START
#   bash run_trajectory.sh --repo spf13/cobra --lang go
#   bash run_trajectory.sh --repo komora-io/concurrent-map --lang rust --iter 1
#   bash run_trajectory.sh --repo owner/name --lang go --skip-prepare      # reuse prep
#   bash run_trajectory.sh --repo owner/name --lang go --print             # preview only
#
# ============================================================================
# DESIGN: 4 wrapper "convenience" flags + 3 pass-through "buckets"
#   The pipeline has ~50 flags across 3 layers. This wrapper OWNS only the args
#   it derives, and forwards everything else verbatim through one bucket per
#   layer — so any current or future flag is reachable without editing this file.
#
# WRAPPER FLAGS (owned by this script)
#   --repo owner/name    (REQUIRED) repo to build a trajectory for.
#   --lang LANG          (REQUIRED) one of: go rust cpp c js ts java python.
#   --model MODEL        provider model or alias (default: gpt55). Also picks the
#                        bridge: gpt*/o* -> codex :8788 ; claude*/opus*/sonnet*/*cc
#                        -> claude-code :8765 ; vertex/bedrock/gemini -> no bridge.
#   --iter N             sugar for pipeline --max-iteration N (default: 3). Ignored
#                        if you set --max-iteration or --skip-to-stage yourself.
#   --org ORG            GitHub org to fork into (default: Aman-Yadav-Ethara-AI).
#   --clone-dir DIR      local clone staging dir (default: repos_staging).
#   --skip-prepare       reuse an existing prep (skip clone/stub/A11/inventory).
#   --resume             continue a run stopped by a subscription limit / kill,
#                        WITHOUT redoing finished modules: rebuilds the branch from
#                        the host-persisted per-module patches, skips completed
#                        stages/modules, and re-runs only what's left. Implies
#                        --skip-prepare (same uuid/outputs are required to resume).
#   --rebuild-agent      sugar for runner --rebuild-agent-image (bake code changes
#                        into a fresh agent image; use after editing agent/ code).
#   --keep-container     sugar for runner --keep-container (leave it up to debug).
#   --print / -n         print the 3 resolved commands and exit (no execution).
#   -h / --help          print this whole reference.
#
# PASS-THROUGH BUCKETS (forward ANY flag to the right layer, verbatim)
#   --prepare-args  "..."   -> tools.prepare_repo_<lang>       (layer 1, below)
#   --run-args      "..."   -> run_pipeline_containerized      (layer 2, below)
#   --pipeline-args "..."   -> in-container run_pipeline_<lang>.sh (layer 3, below)
#   --  <rest>              everything after -- is appended to --pipeline-args.
#   Repeatable/additive: each bucket flag concatenates, so you can pass it twice.
#
#   RULES: put wrapper flags (and --print) BEFORE any "--"; do NOT place a
#   wrapper-owned arg inside a bucket (layer1 --repo/--org/--output/--clone-dir;
#   layer2 --dataset/--repo-split/--language/--model) or argparse will see it twice.
#
# ----------------------------------------------------------------------------
# LAYER 1 — PREPARE flags   (pass via --prepare-args "...")   [x] = default state
#   Common (go + rust + others):
#     --dry-run              skip GitHub fork/push (+ rust: skip dataset writes).  [OFF — real fork+push]
#     --specs-dir DIR        directory of pre-fetched spec PDFs to use.            [none — spec is scraped]
#     --outputs-root DIR     root under which outputs/<uuid>/ is written.          [./outputs]
#     --layout {flat,consolidated}   dataset/output layout.                        [consolidated, via $KAIJU_LOG_LAYOUT]
#   Go only:
#     --tag TAG              git tag to checkout before stubbing.                  [none — default-branch HEAD]
#     --max-repos N          cap when a source lists many repos.                   [unset]
#   Rust only:
#     --crate NAME           crate to stub.                                        [auto-detected from Cargo.toml]
#     --src-dir DIR          source dir relative to repo root.                     [src]
#     --test-cmd "CMD"       test command.                                         [cargo test / cargo test -p CRATE]
#     --rust-version VER     toolchain version to pin.                             [unset — toolchain default]
#     --edition YEAR         Rust edition.                                         [2021]
#     --packages "PKGS"      system packages to install.                           [pkg-config libssl-dev ...]
#     --skip-spec            skip scraping the docs.rs spec PDF.                   [OFF — spec IS scraped/injected]
#     --keep-docs            preserve upstream doc comments / //! module docs.    [OFF — docs stripped]
#   (Authoritative per-language list: `python -m tools.prepare_repo_<lang> --help`.)
#
# LAYER 2 — RUNNER flags    (pass via --run-args "...")   [x] = default state
#     --bridge-url URL       bridge URL the CONTAINER uses; "" forces a direct key. [auto per model]
#     --eval-timeout SECS    hard cap for the WHOLE in-container pipeline.          [10800 = 3h]
#     --rebuild-agent-image  rebuild the agent image (or use sugar --rebuild-agent). [OFF — reuse cached image]
#     --keep-container       don't remove the container on exit (sugar --keep-container). [OFF — container removed]
#   (Authoritative: `python -m agent.container.run_pipeline_containerized --help`.)
#
# LAYER 3 — PIPELINE flags  (pass via --pipeline-args "..." or after "--")   [x] = default state
#   Common (go + rust):
#     --max-iteration N      max agent iterations per stage (wrapper --iter sets this). [go=3, rust=1; via --iter=3]
#     --num-samples N        independent samples for pass@k.                       [1]
#     --skip-to-stage 1|2|3  reuse earlier stages, start at stage N.              [none — start at stage 1]
#     --stage-timeout SECS   hard per-stage timeout.                              [0 = disabled]
#     --eval-timeout SECS    per-eval timeout.                                    [3600]
#     --inactivity-timeout S kill the agent after N s with no log activity.       [900]
#     --max-wall-time SECS   absolute per-stage wall-time cap.                    [86400]
#     --max-test-output-length N   cap test output chars fed to the model (summarizer trigger). [language config]
#     --max-parallel-repos N parallelism across repos.                            [1]
#     --no-stage3-lint       disable lint inside stage 3.                         [OFF — stage-3 lint ON]
#     --branch NAME          git branch to run against.                           [auto — per-stage branch]
#     --use-claude-code      route the agent through the claude-code path.        [OFF]
#     (backend is forced to local_inplace by the runner — do not override.)
#   Rust-only ablation / hardening knobs (all default to the FULL / most-informative behavior):
#     --no-spec-info         disable spec/paper injection into the prompt.        [OFF — spec injection ON]
#     --no-unit-tests-info   disable inline-test injection (stage 1 only).        [OFF — inline tests ON]
#     --no-repo-map          disable aider's internal repo-map.                   [OFF — repo-map ON (map_tokens=1024)]
#     --strip-aux-docs       hide README/CHANGELOG/HISTORY from the agent.        [OFF — aux docs shown]
#     --strip-non-stubs      hide non-stubbed source files from the agent's context. [OFF — source visible]
#     --blind-lint           stage 2 sees only "build failed: N errors".          [OFF — full clippy shown]
#     --blind-tests          stage 3 sees only the summary line.                  [OFF — full output shown]
#     --names-only-tests     stage 3 shows failed test NAMES only (no tracebacks). [OFF — full tracebacks]
#     --no-test-files-readonly    don't inject test files as read-only reference. [OFF — test files injected read-only]
#     --per-edit-compile-gate     cargo check after each edit; revert + retry on regression. [OFF]
#     --compile-gate-max-retries N  retries before reverting a module.            [2]
#     --no-stage3-skip-if-broken  run stage 3 even if the tree doesn't compile.   [OFF — stage 3 SKIPPED if base broken]
#     --quality-watchdog     kill the agent if cargo-check errors trend upward.   [OFF]
#     --quality-watchdog-interval S / --quality-watchdog-rising N / --quality-watchdog-min-delta N   [90 / 3 / 5]
#   (Layer-3 flags DIFFER by language — authoritative: `bash run_pipeline_<lang>.sh --help`.)
#
# ============================================================================
# MORE EXAMPLES
#   # 3 samples, no spec injection (rust ablation)
#   bash run_trajectory.sh --repo owner/x --lang rust --pipeline-args "--num-samples 3 --no-spec-info"
#   # reuse prep, longer eval budget, keep the container to inspect it
#   bash run_trajectory.sh --repo owner/x --lang go --skip-prepare --run-args "--eval-timeout 5400 --keep-container"
#   # prepare a specific tag and keep doc comments (rust)
#   bash run_trajectory.sh --repo owner/x --lang rust --prepare-args "--tag v1.2.0 --keep-docs"
#   # everything after -- goes to the pipeline
#   bash run_trajectory.sh --repo owner/x --lang go -- --skip-to-stage 3 --num-samples 2
#   # claude model auto-selects the :8765 bridge
#   bash run_trajectory.sh --repo owner/x --lang go --model opus48cc
# ============================================================================
set -uo pipefail
cd /Users/macbookpro/Desktop/kaiju-harness/kaiju-harness
export PATH="/opt/homebrew/bin:$PATH:$HOME/go/bin"

# ---- defaults ----
REPO=""; LNG=""; MODEL="gpt55"; ITER=""; ORG="Aman-Yadav-Ethara-AI"
CLONE="repos_staging"; SKIP_PREP=0; PRINT=0; REUSE_BRIDGE=0
PREPARE_ARGS=""; RUN_ARGS=""; PIPELINE_ARGS=""

# --help prints the whole comment reference above (stops at first non-comment line),
# so the docs can never drift from a hard-coded copy.
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

while [ $# -gt 0 ]; do case "$1" in
  --repo)          REPO="$2"; shift 2;;
  --lang|--language) LNG="$2"; shift 2;;
  --model)         MODEL="$2"; shift 2;;
  --iter|--max-iteration) ITER="$2"; shift 2;;
  --org)           ORG="$2"; shift 2;;
  --clone-dir)     CLONE="$2"; shift 2;;
  --skip-prepare)  SKIP_PREP=1; shift;;
  --reuse-bridge)  REUSE_BRIDGE=1; shift;;
  --resume)        PIPELINE_ARGS="$PIPELINE_ARGS --resume"; SKIP_PREP=1; shift;;
  --rebuild-agent) RUN_ARGS="$RUN_ARGS --rebuild-agent-image"; shift;;
  --keep-container) RUN_ARGS="$RUN_ARGS --keep-container"; shift;;
  --prepare-args)  PREPARE_ARGS="$PREPARE_ARGS $2"; shift 2;;
  --run-args)      RUN_ARGS="$RUN_ARGS $2"; shift 2;;
  --pipeline-args) PIPELINE_ARGS="$PIPELINE_ARGS $2"; shift 2;;
  --print|-n)      PRINT=1; shift;;
  --)              shift; PIPELINE_ARGS="$PIPELINE_ARGS $*"; break;;
  -h|--help)       usage; exit 0;;
  *) echo "ERROR: unknown arg '$1' (use a pass-through bucket: --prepare-args/--run-args/--pipeline-args, or --help)"; exit 1;;
esac; done

[ -n "$REPO" ] && [ -n "$LNG" ] || { echo "ERROR: --repo and --lang are required (see --help)"; exit 1; }
case "$LNG" in go|rust|cpp|c|js|ts|java|python) ;; *) echo "ERROR: unsupported --lang '$LNG'"; exit 1;; esac

SPLIT="${REPO##*/}"
DATASET="${SPLIT}_dataset.json"
# cpp's prepare hardcodes its dataset filename to <repo>_cpp_dataset.json and has
# NO --output flag (a stray --output silently prefix-matches --outputs-root), so
# align the wrapper's expected dataset name + drop --output for cpp below.
[ "$LNG" = "cpp" ] && DATASET="${SPLIT}_cpp_dataset.json"
# Per-language module names. Python is the original commit0 and does NOT use the
# `_<lang>` suffix convention: prepare=tools.prepare_repo, build=`commit0 build`,
# and its build config is NOT generated by prepare (we generate it below).
if [ "$LNG" = "python" ]; then
  PREP_MOD="tools.prepare_repo"; BUILD_MOD="commit0"; CONFIG=".commit0_python.yaml"; GEN_CONFIG=1
else
  PREP_MOD="tools.prepare_repo_${LNG}"; BUILD_MOD="commit0.cli_${LNG}"; CONFIG=".commit0_${LNG}.yaml"; GEN_CONFIG=0
fi

# If THIS script is interrupted (Ctrl-C / terminal close / kill), reap this run's
# container so it can't orphan. Belt-and-suspenders: the runner also self-reaps
# (signal handlers + auto-remove TTL + startup sweep). Data is on the host mount,
# so removing the container never loses anything. Only on interrupt, not on exit.
_reap_container() {
  local pat="kaiju.pipeline.$(printf '%s' "$SPLIT" | tr 'A-Z' 'a-z')."
  docker ps -aq --filter "name=$pat" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1 || true
  echo "== interrupted: reaped container(s) matching $pat (data safe on host mount) =="
}
trap '_reap_container; exit 130' INT TERM HUP

# --iter is sugar: inject --max-iteration only if the pipeline bucket doesn't already set one.
case " $PIPELINE_ARGS " in
  *" --max-iteration "*|*" --skip-to-stage "*) : ;;   # user drives iterations/staging explicitly
  *) PIPELINE_ARGS="--max-iteration ${ITER:-3} $PIPELINE_ARGS" ;;
esac
PIPELINE_ARGS="$(echo "$PIPELINE_ARGS" | sed 's/  */ /g;s/^ *//;s/ *$//')"
PREPARE_ARGS="$(echo "$PREPARE_ARGS" | sed 's/  */ /g;s/^ *//;s/ *$//')"
RUN_ARGS="$(echo "$RUN_ARGS" | sed 's/  */ /g;s/^ *//;s/ *$//')"

# ---- bridge selection by model ----
BRIDGE="none"; BPORT=""
case "$MODEL" in
  gpt*|openai*|codex*|o1*|o3*|o4*)
    BRIDGE="codex"; BPORT=8788
    export KAIJU_CODEX_BRIDGE_SECRET="${KAIJU_CODEX_BRIDGE_SECRET:-kaiju-trajectory-fixed}" ;;
  *claude*|opus*|sonnet*|*cc)
    BRIDGE="cc"; BPORT=8765
    export KAIJU_CC_BRIDGE_SECRET="${KAIJU_CC_BRIDGE_SECRET:-kaiju-cc-fixed}" ;;
  *) BRIDGE="none" ;;   # vertex/bedrock/gemini -> direct host creds, no bridge
esac

# resolved command lines (single source of truth for --print and for execution)
# c's prepare CLI has no --org (it forks to its default); every other lang accepts it.
# c uses --fork-org (not --org) and needs --push so the base_commit lands on the
# fork the repo-image setup.sh clones from (else setup.sh git-fails, exit 128).
_ORG_FLAG="--org $ORG"; [ "$LNG" = "c" ] && _ORG_FLAG="--fork-org $ORG --push"
# cpp prepare has no --output (it writes <repo>_cpp_dataset.json itself); passing
# one corrupts --outputs-root via argparse prefix-matching. Drop it for cpp.
_OUT_FLAG="--output $DATASET"; [ "$LNG" = "cpp" ] && _OUT_FLAG=""
PREP_CMD="python -m ${PREP_MOD} --repo $REPO $_ORG_FLAG $_OUT_FLAG --clone-dir $CLONE $PREPARE_ARGS"
# --single-arch: native-only build. The default multi-arch OCI build is slower and
# flaky for local single-repo validation (some repos fail the multi-arch step).
BUILD_CMD="python -m ${BUILD_MOD} build --single-arch --commit0-config-file $CONFIG"
RUN_CMD="python -m agent.container.run_pipeline_containerized --language $LNG --dataset $DATASET --repo-split $SPLIT --model $MODEL --pipeline-args \"$PIPELINE_ARGS\" $RUN_ARGS"

echo "== repo=$REPO lang=$LNG model=$MODEL split=$SPLIT bridge=$BRIDGE${BPORT:+:$BPORT} =="
echo "   prepare : $PREP_CMD"
echo "   build   : $BUILD_CMD"
echo "   run     : $RUN_CMD"
if [ "$PRINT" -eq 1 ]; then echo "== --print: not executing =="; exit 0; fi

# ---- 1. prepare ----
if [ "$SKIP_PREP" -eq 0 ]; then
  echo "== [1/5] prepare =="
  eval "$PREP_CMD" || { echo "ERROR: prepare failed"; exit 1; }
else
  echo "== [1/5] prepare SKIPPED (--skip-prepare) =="
fi
[ -f "$DATASET" ] || { echo "ERROR: $DATASET missing (prepare for $LNG may write a different filename — see prepare output)"; exit 1; }
# Guard the empty/invalid-dataset case: a rejected repo (failed validation) yields
# an empty [] dataset; indexing [0] would IndexError with a cryptic trace. Fail
# with a clear, actionable message instead.
UUID=$(python -c "import json,sys; d=json.load(open('$DATASET')); print(d[0]['id']) if (isinstance(d,list) and d and d[0].get('id')) else sys.exit(1)" 2>/dev/null) \
  || { echo "ERROR: $DATASET has no valid entries — prepare produced nothing (the repo was likely rejected by $LNG validation; see prepare output above)"; exit 1; }
echo "== id=$UUID =="
python - "$DATASET" <<'PY'
import json,glob,sys
e=json.load(open(sys.argv[1]))[0]
bc=e.get("base_compiles")
inv=glob.glob(f"outputs/{e['id']}/datasets/*_test_ids.bz2")
print("   base_compiles:", bc, "" if bc is not False else "  <-- WARNING: base does NOT compile; a 0% is infra not model")
print("   mount inventory:", inv or "MISSING (containerized eval will fall back to observed count)")
PY

# ---- 2. bridge ----
# CRITICAL: the container is handed OUR resolved secret ($KAIJU_*_BRIDGE_SECRET).
# A bridge left listening from a PRIOR session may have been started with a
# DIFFERENT secret (e.g. an earlier run used another value), which the container
# can't authenticate against -> "bridge: unauthorized (bad OPENAI_API_KEY)" and
# the run dies at model preflight. So we always (re)start the bridge on our port
# with our secret to guarantee a match. Set --reuse-bridge to skip the restart if
# you KNOW the running bridge already uses this secret.
echo "== [2/5] bridge ($BRIDGE) =="
if [ "$BRIDGE" = "codex" ]; then
  if [ "$REUSE_BRIDGE" != "1" ] && lsof -i :$BPORT | grep -q LISTEN; then
    echo "   restarting bridge on $BPORT to guarantee a matching secret (--reuse-bridge to skip)"
    lsof -nP -iTCP:$BPORT -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null || true
    sleep 1
  fi
  if ! lsof -i :$BPORT | grep -q LISTEN; then
    python -m agent.openai_codex --check || { echo "ERROR: codex auth check failed (is ~/.codex/auth.json valid?)"; exit 1; }
    python -m agent.openai_codex --host 0.0.0.0 --port $BPORT >/tmp/codex_bridge.log 2>&1 &
    sleep 3
  fi
  lsof -i :$BPORT | grep -q LISTEN && echo "   codex bridge up on $BPORT (secret matches container)" || { echo "ERROR: bridge not up (see /tmp/codex_bridge.log)"; exit 1; }
elif [ "$BRIDGE" = "cc" ]; then
  if [ "$REUSE_BRIDGE" != "1" ] && lsof -i :$BPORT | grep -q LISTEN; then
    echo "   restarting bridge on $BPORT to guarantee a matching secret (--reuse-bridge to skip)"
    KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh stop >/dev/null 2>&1 || true
    lsof -nP -iTCP:$BPORT -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null || true
    sleep 2
  fi
  if ! lsof -i :$BPORT | grep -q LISTEN; then
    KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh start || { echo "ERROR: cc bridge start failed"; exit 1; }
    sleep 3
  fi
  lsof -i :$BPORT | grep -q LISTEN && echo "   cc bridge up on $BPORT (secret matches container)" || { echo "ERROR: bridge not up"; exit 1; }
else
  echo "   no bridge (direct provider creds)"
fi

# ---- 3. build repo image ----
echo "== [3/5] build repo image =="
# Python's prepare does not emit a build config, so synthesize one pointing at the
# dataset we just prepared (base_dir=repos, this repo only). Other languages'
# prepare generates .commit0_<lang>.yaml themselves.
if [ "$GEN_CONFIG" = "1" ]; then
  cat > "$CONFIG" <<YAML
base_dir: $(pwd)/repos
dataset_name: $(pwd)/$DATASET
dataset_split: test
repo_split: all
YAML
  echo "   generated $CONFIG (dataset=$DATASET)"
fi
# Fallback config synthesis: go/rust prepare emit .commit0_<lang>.yaml themselves,
# but js/ts/c/cpp/java prepare do NOT — without this the build aborts on a missing
# config. If prepare didn't leave one, synthesize the same minimal config python
# uses, pointed at the dataset we just prepared. (Absolute paths so it resolves
# regardless of the build's CWD.)
if [ ! -f "$CONFIG" ]; then
  cat > "$CONFIG" <<YAML
base_dir: $(pwd)/repos
dataset_name: $(pwd)/$DATASET
dataset_split: test
repo_split: all
YAML
  echo "   synthesized $CONFIG (prepare for $LNG did not emit one; dataset=$DATASET)"
fi
[ -f "$CONFIG" ] || { echo "ERROR: $CONFIG missing (prepare for $LNG may not generate it; check the runbook)"; exit 1; }
[ -s "$DATASET" ] && python3 -c "import json,sys; d=json.load(open('$DATASET')); sys.exit(0 if isinstance(d,list) and d else 1)" 2>/dev/null \
  || { echo "ERROR: $DATASET is empty/invalid — prepare produced no entries (see prepare output above)"; exit 1; }
eval "$BUILD_CMD" || { echo "ERROR: build failed"; exit 1; }

# ---- 3b. canonical test-id inventory (PYTHON only) ----
# Unlike go/rust (which capture test-ids inline during prepare via a deps-free
# lister), python needs `pytest --collect-only`, which requires the repo's deps.
# Prepare can't do that on the host, so we generate the inventory here via the
# docker tier (deps present) and --install it to commit0/data/test_ids/<name>.bz2,
# which the runner's copy_inference_inputs then stages as the mounted, frozen
# scoring denominator. Without it the eval falls back to the observed count
# (still correct, since eval reverts test files — just less robust). Non-fatal.
if [ "$LNG" = "python" ]; then
  _TID_NAME="$(printf '%s' "$SPLIT" | tr 'A-Z.' 'a-z-')"
  _TID_FILE="commit0/data/test_ids/${_TID_NAME}.bz2"
  # prepare clones owner/repo -> <clone-dir>/owner__repo (slash becomes __).
  _REPO_DIR="$CLONE/$(printf '%s' "$REPO" | sed 's#/#__#g')"
  if [ -f "$_TID_FILE" ]; then
    echo "== [3b/5] test-id inventory present ($_TID_FILE) =="
  elif [ -d "$_REPO_DIR" ]; then
    echo "== [3b/5] generating canonical test-id inventory (docker tier — deps needed) =="
    python -m tools.generate_test_ids --repo-dir "$_REPO_DIR" --name "$SPLIT" --docker --install \
      && echo "   wrote $_TID_FILE (frozen scoring denominator)" \
      || echo "   WARN: test-id generation failed — eval will fall back to observed count (still correct)"
  else
    echo "== [3b/5] SKIP test-id gen: clone $_REPO_DIR not present (eval uses observed count) =="
  fi
fi

# ---- 4. run trajectory (host-mounted outputs -> survives a kill) ----
echo "== [4/5] run trajectory =="
eval "$RUN_CMD"

# ---- 5. verify ----
echo "== [5/5] results =="
python - "$UUID" "$SPLIT" <<'PY'
import json,glob,sys
uuid,split=sys.argv[1],sys.argv[2]
runs=glob.glob(f"outputs/{uuid}/runs/*/agent/run_1/pipeline_results.json")
if not runs:
    print(f"   no pipeline_results.json under outputs/{uuid} (run may have failed early)"); raise SystemExit
try:
    d=json.load(open(runs[0]))
except Exception as _e:
    print(f"   pipeline_results.json present but unreadable ({_e})"); raise SystemExit
for s in ("stage1","stage2","stage3"):
    st=d.get(s)
    if st:
        print(f"   {s}: {st.get('num_passed')}/{st.get('num_tests')}  {st.get('eval_status')}  cost_source={st.get('cost_source')}")
cheat=[p for p in glob.glob(f"outputs/{uuid}/runs/*/agent/run_1/stage*_eval_artifacts/*/test_output.json")
       if "CHEAT_DETECTED" in open(p,errors="replace").read()]
print("   CHEAT_DETECTED:", cheat or "none")
print("   turns.jsonl files:", len(glob.glob(f"outputs/{uuid}/runs/*/agent/run_1/**/turns.jsonl", recursive=True)))
print("   ATIF trajectory files:", len(glob.glob(f"Harbor_Data/Trajectory/**/*{split}*/trajectory.json", recursive=True)))
PY
echo "== done. outputs/$UUID  (bridge left running; 'pkill -f openai_codex' or 'bash scripts/claude_code_bridge.sh stop') =="
