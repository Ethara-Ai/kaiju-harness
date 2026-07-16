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
#   --org ORG            GitHub org to fork into (default: Zahgon).
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
# Portable: run from the repo root (this script's own directory), never a
# hardcoded user path. Works regardless of where the repo is checked out.
# (Supersedes the coworker's equivalent `cd "$(dirname "$0")"` — BASH_SOURCE also
# survives `source`, the guard fails loudly, and the PATH loop is cross-platform.)
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)" || { echo "ERROR: cannot cd to script dir"; exit 1; }
# Add common toolchain locations to PATH only if they exist (macOS homebrew,
# rustup, go, openjdk, homebrew-on-Linux) — portable, no duplicates.
for _d in /opt/homebrew/bin /opt/homebrew/opt/openjdk/bin /home/linuxbrew/.linuxbrew/bin \
          "$HOME/.cargo/bin" "$HOME/go/bin" /usr/local/go/bin /usr/lib/go/bin; do
  [ -d "$_d" ] || continue
  case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
done
export PATH

# ---- defaults ----
# Default fork/push org is zahgon (the shared org). Override per-run with
# --org <name> or $KAIJU_FORK_ORG. NOTE: your token must have WRITE access to
# this org — otherwise prepare fails fast with an actionable message (a bad org
# -> push 403 -> the dataset would record commits that were never pushed -> the
# container build git-fails with "not our ref"); prepare now refuses that.
REPO=""; LNG=""; MODEL="gpt55"; ITER=""; ORG="zahgon"
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
# java's cli_java reads its config from the DOT-form name by default and its build
# takes neither --commit0-config-file nor --single-arch (handled at BUILD_CMD).
[ "$LNG" = "java" ] && CONFIG=".commit0.java.yaml"

# If THIS script is interrupted (Ctrl-C / terminal close / kill), reap this run's
# container so it can't orphan. Belt-and-suspenders: the runner also self-reaps
# (signal handlers + auto-remove TTL + startup sweep). Data is on the host mount,
# so removing the container never loses anything. Only on interrupt, not on exit.
_reap_container() {
  local pat="kaiju.pipeline.$(printf '%s' "$SPLIT" | tr 'A-Z' 'a-z')."
  docker ps -aq --filter "name=$pat" 2>/dev/null | xargs -r docker rm -f >/dev/null 2>&1 || true
  echo "== interrupted: reaped container(s) matching $pat (data safe on host mount) =="
}
# Bridge users are ref-counted via one PID file per run under this dir, so the
# single shared bridge (fixed port per model family: codex 8788 / cc 8765) is
# only torn down when the LAST concurrent run exits. Without this, two runs for
# the same model (e.g. js + rust both on gpt55) each stopped the shared bridge on
# exit, killing it out from under the still-running peer -> "Connection error" at
# the peer's model preflight.
_bridge_users_dir() { echo "${TMPDIR:-/tmp}/kaiju_bridge_${BPORT:-0}.users"; }

_register_bridge_user() {
  [[ "${BRIDGE:-none}" == "none" ]] && return 0
  local d; d="$(_bridge_users_dir)"
  mkdir -p "$d" 2>/dev/null || true
  touch "$d/$$" 2>/dev/null || true
}

_cleanup_bridge_if_owned() {
  # Stop the shared bridge ONLY when no other live run is using it (ref-count).
  # No-op paths:
  #   --reuse-bridge — caller opted into a persistent shared bridge; leave it up.
  #   BRIDGE=none    — model uses direct host creds (vertex/bedrock/gemini).
  # For cc: dedicated stop script (tears down monitor + bridge). For codex:
  #   _force_free_port (bare-python bridge, single-process, no watchdog).
  [[ "${BRIDGE:-none}" == "none" ]] && return 0
  local d; d="$(_bridge_users_dir)"
  rm -f "$d/$$" 2>/dev/null || true
  [[ "${REUSE_BRIDGE:-0}" == "1" ]] && return 0
  # Any OTHER live user? Prune dead PID files; if a live one remains, keep the
  # bridge up for it.
  if [[ -d "$d" ]]; then
    for _f in "$d"/*; do
      [[ -e "$_f" ]] || continue
      if kill -0 "$(basename "$_f")" 2>/dev/null; then return 0; fi
      rm -f "$_f" 2>/dev/null || true
    done
  fi
  case "$BRIDGE" in
    codex)
      [[ -n "${BPORT:-}" ]] && _force_free_port "$BPORT" >/dev/null 2>&1 || true
      echo "== codex bridge stopped (port $BPORT freed) =="
      ;;
    cc)
      KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh stop >/dev/null 2>&1 || true
      echo "== claude_code bridge stopped =="
      ;;
  esac
}
# INT/TERM/HUP: reap container + stop bridge, exit with signal-equivalent code.
trap '_reap_container; _cleanup_bridge_if_owned; exit 130' INT TERM HUP
# EXIT: always stop bridge (covers normal + error exit paths). Idempotent with
# the interrupt trap because both bridge stop paths tolerate re-invocation.
trap '_cleanup_bridge_if_owned' EXIT

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

echo "== fork/push org: $ORG (default zahgon; override with --org) =="

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
# --single-arch is a native-only build (faster, avoids flaky multi-arch). Only
# python/go/c/js/ts build CLIs accept it; rust/cpp/java's do not (they'd error
# "No such option: --single-arch"), so omit it for those.
_ARCH_FLAG="--single-arch"; case "$LNG" in rust|cpp|java) _ARCH_FLAG="";; esac
if [ "$LNG" = "java" ]; then
  BUILD_CMD="python -m ${BUILD_MOD} build"
elif [ "$LNG" = "cpp" ]; then
  # cpp build defaults to linux/amd64,linux/arm64 which needs cross-arch buildx.
  # Pin to native arch unless the operator sets COMMIT0_BUILD_PLATFORMS themselves.
  export COMMIT0_BUILD_PLATFORMS="${COMMIT0_BUILD_PLATFORMS:-linux/$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')}"
  BUILD_CMD="python -m ${BUILD_MOD} build --dataset-path $DATASET"
else
  BUILD_CMD="python -m ${BUILD_MOD} build $_ARCH_FLAG --commit0-config-file $CONFIG"
fi
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
# Already staged under the consolidated datasets dir?
inv=glob.glob(f"outputs/{e['id']}/datasets/*_test_ids.bz2")
# Not staged yet — the per-language prepare writes the inventory under
# commit0/data/<subdir>/<repo>.bz2 and the containerized runner stages it at run
# time (copy_inference_inputs). Check that source so this isn't a false "MISSING".
if not inv:
    reponame=e.get("repo","/").split("/")[-1]
    inv=glob.glob(f"commit0/data/*/{reponame}.bz2")
print("   base_compiles:", bc, "" if bc is not False else "  <-- WARNING: base does NOT compile; a 0% is infra not model")
print("   test-id inventory:", inv or "MISSING (containerized eval will fall back to observed count)")
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

# Liveness is decided by a real HTTP /healthz probe, NOT by `lsof ... LISTEN`.
# A bridge process that is SUSPENDED (state T — e.g. a stray Ctrl-Z on a prior
# run's process group) or otherwise wedged STILL HOLDS its listening socket, so
# an lsof check passes — but it never answers a request. The container's model
# preflight then hangs until PROBE_TIMEOUT (~120s) and the whole run aborts
# ("MODEL API PREFLIGHT FAILED", blank error) before doing any work. Both bridges
# expose an unauthenticated GET /healthz, so probe that.
_bridge_healthy() {  # $1=port
  curl -sf -m 5 "http://127.0.0.1:$1/healthz" >/dev/null 2>&1
}
# Force-free a TCP port even when the holder is STOPPED. A plain `kill` (SIGTERM)
# is NOT delivered to a T-state process until it is continued, so the old
# teardown left the stale bridge alive and silently reused it. SIGCONT first (so
# a stopped process can run its handler and exit), then SIGTERM, then SIGKILL any
# ORIGINAL holder still alive — tracked by PID, not by re-probing the port, since
# a half-terminated process can drop the socket while still lingering. SIGKILL is
# delivered even to a T-state process, so this always wins.
_force_free_port() {  # $1=port
  local pids p
  pids=$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null) || true
  [ -z "$pids" ] && return 0
  kill -CONT $pids 2>/dev/null || true
  kill $pids 2>/dev/null || true
  sleep 1
  for p in $pids; do
    if kill -0 "$p" 2>/dev/null; then kill -9 "$p" 2>/dev/null || true; fi
  done
  sleep 0.5
}

# B2 audit fix: verify a REUSED bridge actually accepts OUR secret before proceeding.
# Without this, a stale bridge (started with a different KAIJU_*_BRIDGE_SECRET,
# e.g. from a prior operator's session) passes /healthz but rejects every request
# from our container with 401 — surfacing only at model-preflight time (~120s in).
# Sends a POST with our secret; expects NOT-401 (400/404/422/etc all mean auth OK).
_bridge_secret_matches() {  # $1=port, $2=bridge kind (cc|codex), $3=secret
  local port="$1" kind="$2" secret="$3" path code
  case "$kind" in
    cc)    path="/v1/messages" ;;
    codex) path="/v1/responses" ;;
    *)     return 1 ;;
  esac
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST \
    -H "Authorization: Bearer $secret" \
    -H "Content-Type: application/json" \
    -d '{}' \
    "http://127.0.0.1:$port$path" 2>/dev/null) || return 1
  # 401 = auth failed. Any other response (including 400/422 malformed body) means
  # the bridge accepted our secret and is proceeding to validate the request body.
  # 000 = no response / connection refused — treat as mismatch to force restart.
  [[ "$code" != "401" && "$code" != "000" ]]
}

if [ "$BRIDGE" = "codex" ]; then
  # Reuse only if the caller opted in AND the bridge actually answers /healthz.
  # B2 audit fix: reuse ONLY if bridge answers /healthz AND accepts our secret.
  # A stale bridge (different secret from prior operator's session) passes /healthz
  # but 401s our requests — detect that now, not 120s later at model preflight.
  # Reuse a healthy bridge whose secret ALREADY matches ours — regardless of
  # --reuse-bridge. secret_matches guarantees the container can authenticate, so
  # a needless force-restart here would only tear down a bridge a CONCURRENT run
  # (same model, same fixed secret) is actively using. Force-restart is reserved
  # for an unhealthy or stale-secret (401) bridge.
  if _bridge_healthy "$BPORT" && _bridge_secret_matches "$BPORT" "codex" "$KAIJU_CODEX_BRIDGE_SECRET"; then
    echo "   reusing healthy codex bridge on $BPORT (secret matches; shared-safe)"
  else
    # Default path (or --reuse-bridge but the bridge is unhealthy): (re)start so
    # the bridge uses OUR secret — a stale one may hold a different secret the
    # container can't authenticate against. Force-free the port first so a
    # suspended/wedged holder is actually replaced, not silently reused.
    if lsof -i :$BPORT 2>/dev/null | grep -q LISTEN; then
      echo "   replacing bridge on $BPORT (forced restart or failed health check)"
      _force_free_port "$BPORT"
    fi
    python -m agent.openai_codex --check || { echo "ERROR: codex auth check failed (is ~/.codex/auth.json valid?)"; exit 1; }
    python -m agent.openai_codex --host 0.0.0.0 --port $BPORT >/tmp/codex_bridge.log 2>&1 &
    for _i in $(seq 1 30); do _bridge_healthy "$BPORT" && break; sleep 1; done
  fi
  _bridge_healthy "$BPORT" && echo "   codex bridge healthy on $BPORT (secret matches container)" || { echo "ERROR: bridge not answering /healthz on $BPORT (see /tmp/codex_bridge.log)"; exit 1; }
  _register_bridge_user
elif [ "$BRIDGE" = "cc" ]; then
  # Reuse a healthy same-secret bridge regardless of --reuse-bridge (see codex
  # note above): don't tear down a bridge a concurrent same-model run is using.
  if _bridge_healthy "$BPORT" && _bridge_secret_matches "$BPORT" "cc" "$KAIJU_CC_BRIDGE_SECRET"; then
    echo "   reusing healthy cc bridge on $BPORT (secret matches; shared-safe)"
  else
    if lsof -i :$BPORT 2>/dev/null | grep -q LISTEN; then
      echo "   replacing bridge on $BPORT (forced restart or failed health check)"
      # `stop` first so the watchdog is torn down and won't respawn the bridge
      # mid-shutdown; then force-free in case the process was stopped (T-state,
      # which `stop`'s SIGTERM can't reap).
      KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh stop >/dev/null 2>&1 || true
      _force_free_port "$BPORT"
    fi
    KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh start || { echo "ERROR: cc bridge start failed"; exit 1; }
    for _i in $(seq 1 30); do _bridge_healthy "$BPORT" && break; sleep 1; done
  fi
  _bridge_healthy "$BPORT" && echo "   cc bridge healthy on $BPORT (secret matches container)" || { echo "ERROR: bridge not answering /healthz on $BPORT"; exit 1; }
  _register_bridge_user
  # Always (re)arm the self-healing watchdog. Covers --reuse-bridge (where we
  # skipped `start`, so no monitor would otherwise be attached) and re-arms a
  # monitor whose supervisor died — so a mid-run bridge crash is auto-restarted
  # with no manual intervention.
  KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh ensure-monitor >/dev/null 2>&1 || true
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

# ---- 3b. canonical test-id inventory (JAVA only) ----
# Like python, java has NO deps-free inline lister in prepare (prepare_repo_java
# stubs + stages via copy_inference_inputs but never GENERATES the inventory), so
# nothing lands in commit0/data/java_test_ids/ and the centralized staging in
# run_pipeline_containerized finds nothing -> observed-count fallback. Generate it
# here with tools.generate_test_ids_java in host source-scan mode (deps-free, no
# docker needed — mirrors go/rust's inline capture) and --install it to
# commit0/data/java_test_ids/<name>.bz2, which both prepare's copy_inference_inputs
# and the runner's centralized staging then pick up. Non-fatal.
if [ "$LNG" = "java" ]; then
  _TID_NAME="$(printf '%s' "$SPLIT" | tr 'A-Z.' 'a-z-')"
  _TID_FILE="commit0/data/java_test_ids/${_TID_NAME}.bz2"
  # java's prepare clones owner/repo -> <clone-dir>/<repo_short> (basename only,
  # NOT owner__repo), so the clone dir is $CLONE/$SPLIT.
  _REPO_DIR="$CLONE/$SPLIT"
  # per-repo TEMP output dir (OUTSIDE outputs/) so --install only globs THIS
  # repo's bz2 without leaving a stray outputs/<uuid>/java_test_ids/ folder — the
  # canonical homes are commit0/data/java_test_ids/ (frozen) + datasets/ (staged),
  # same as every other language. No other lang writes a per-run test_ids folder.
  _TID_OUT="$(mktemp -d 2>/dev/null || echo "/tmp/kaiju_java_tid_${UUID}")"
  if [ -f "$_TID_FILE" ]; then
    echo "== [3b/5] java test-id inventory present ($_TID_FILE) =="
  elif [ -d "$_REPO_DIR" ]; then
    echo "== [3b/5] generating canonical java test-id inventory (host source scan) =="
    python -m tools.generate_test_ids_java --repo-dir "$_REPO_DIR" --name "$SPLIT" --output-dir "$_TID_OUT" --install \
      && echo "   wrote $_TID_FILE (frozen scoring denominator)" \
      || echo "   WARN: java test-id generation failed — eval will fall back to observed count (still correct)"
    rm -rf "$_TID_OUT" 2>/dev/null || true
  else
    echo "== [3b/5] SKIP java test-id gen: clone $_REPO_DIR not present (eval uses observed count) =="
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
# Language-agnostic incompleteness check: a module whose transient-error retries
# were exhausted is left WITHOUT .done plus a .needs_retry breadcrumb (every
# run_agent_<lang>.py does this). If ANY exist the score is NOT a clean result —
# surface it loudly so a 0/N incomplete run is never mistaken for a real 0.
needs_retry=glob.glob(f"outputs/{uuid}/runs/*/agent/run_1/**/.needs_retry", recursive=True)
if needs_retry:
    mods=sorted({p.rsplit('/',2)[-2] for p in needs_retry})
    print(f"   ** INCOMPLETE: {len(needs_retry)} module(s) left .needs_retry (transient LLM error "
          f"persisted) — NOT a clean result; use --resume. modules: {mods[:8]}")
else:
    print("   needs_retry: none (all modules completed)")
print("   turns.jsonl files:", len(glob.glob(f"outputs/{uuid}/runs/*/agent/run_1/**/turns.jsonl", recursive=True)))
print("   ATIF trajectory files:", len(glob.glob(f"Harbor_Data/Trajectory/**/*{split}*/trajectory.json", recursive=True)))
PY
if [[ "${REUSE_BRIDGE:-0}" == "1" ]]; then
  echo "== done. outputs/$UUID  (bridge LEFT RUNNING per --reuse-bridge; stop manually with 'bash scripts/claude_code_bridge.sh stop' or 'pkill -f openai_codex') =="
else
  echo "== done. outputs/$UUID  (bridge cleanup via EXIT trap; use --reuse-bridge to keep it alive for subsequent runs) =="
fi
