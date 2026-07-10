#!/usr/bin/env bash
# =============================================================================
# run_batch.sh — production batch runner for the commit0 trajectory pipeline.
#
# Drives run_trajectory.sh across MANY (language, repo, model) tasks with bounded
# concurrency, shared subscription bridges, disk/CPU guards, resume, and retry —
# designed to scale to 1000+ tasks per language.
#
# It is a thin, robust ORCHESTRATOR: all per-language prepare/build/run logic
# lives in run_trajectory.sh (single source of truth). This script only decides
# WHAT to run, HOW MANY at once, and WHETHER to skip/retry.
#
# ---- manifest format (one task per line; blank / #comment lines ignored) ----
#   lang | repo | model | prepare_args | run_args
#     lang        one of: python go rust c cpp js ts java
#     repo        owner/name (upstream; prepare forks into --org)
#     model       alias (gpt55, opus48cc, ...) OR empty -> round-robin --models
#     prepare_args extra args to run_trajectory --prepare-args (may be empty)
#     run_args     extra args to run_trajectory --run-args     (may be empty)
#   Only `lang` and `repo` are required; trailing fields may be omitted.
#
# ---- usage ----
#   bash run_batch.sh --manifest batch.txt
#   bash run_batch.sh --manifest batch.txt --max-parallel 3 --models gpt55,opus48cc
#   bash run_batch.sh --max-parallel 2 -- python:pallets/itsdangerous go:google/uuid
#   bash run_batch.sh --manifest batch.txt --iter 3 --dry-run
#
# ---- key flags ----
#   --manifest FILE     task manifest (see format above)
#   --max-parallel N    concurrent tasks (default: min(3, ncpu/2-ish); each task
#                       spawns docker builds + an agent + eval, so keep it small)
#   --min-disk-gb G     pause launching new tasks below this free space (default 25)
#   --retries R         retry a task R times on TRANSIENT failure (default 1)
#   --models "a,b,..."  models to round-robin when a task's model field is empty
#                       (default: gpt55,opus48cc — mixes codex + claude-code bridges)
#   --iter N            --max-iteration passed to every run (default 1)
#   --skip-prepare      reuse existing prep for every task (default: full prepare)
#   --rebuild-agent     pass --rebuild-agent-image to every run (bake current code)
#   --no-resume         re-run tasks even if a complete result already exists
#   --dry-run           print the plan (per-task commands) and exit
#   --list-only         print resolved task list and exit
# =============================================================================
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)" || { echo "ERROR: cannot cd to script dir"; exit 1; }
# Add common toolchain locations to PATH only if they exist (portable, no dupes).
for _d in /opt/homebrew/bin /opt/homebrew/opt/openjdk/bin /home/linuxbrew/.linuxbrew/bin \
          "$HOME/.cargo/bin" "$HOME/go/bin" /usr/local/go/bin /usr/lib/go/bin; do
  [ -d "$_d" ] || continue
  case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
done
export PATH

# ---------------------------------------------------------------- defaults ----
MANIFEST=""
MAX_PARALLEL=""
MIN_DISK_GB=25
RETRIES=1
MODELS="gpt55,opus48cc"
ITER=1
SKIP_PREP=0
REBUILD_AGENT=0
RESUME=1
DRY_RUN=0
LIST_ONLY=0
declare -a INLINE_PAIRS=()

# ---------------------------------------------------------------- arg parse ---
while [ $# -gt 0 ]; do case "$1" in
  --manifest)      MANIFEST="$2"; shift 2;;
  --max-parallel)  MAX_PARALLEL="$2"; shift 2;;
  --min-disk-gb)   MIN_DISK_GB="$2"; shift 2;;
  --retries)       RETRIES="$2"; shift 2;;
  --models)        MODELS="$2"; shift 2;;
  --iter)          ITER="$2"; shift 2;;
  --skip-prepare)  SKIP_PREP=1; shift;;
  --rebuild-agent) REBUILD_AGENT=1; shift;;
  --no-resume)     RESUME=0; shift;;
  --dry-run)       DRY_RUN=1; shift;;
  --list-only)     LIST_ONLY=1; shift;;
  --)              shift; while [ $# -gt 0 ]; do INLINE_PAIRS+=("$1"); shift; done;;
  -h|--help)       sed -n '2,50p' "$0"; exit 0;;
  *)               echo "unknown arg: $1 (see --help)"; exit 2;;
esac; done

# concurrency default: cores are shared with docker's VM, so be conservative.
if [ -z "$MAX_PARALLEL" ]; then
  _cores=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 8)
  MAX_PARALLEL=$(( _cores/3 )); [ "$MAX_PARALLEL" -lt 1 ] && MAX_PARALLEL=1
  [ "$MAX_PARALLEL" -gt 4 ] && MAX_PARALLEL=4
fi

BATCH_LOG="batch_$(date +%Y%m%d_%H%M%S).log"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$BATCH_LOG"; }

# ---------------------------------------------------------------- helpers -----
disk_free_gb() { df -g /System/Volumes/Data 2>/dev/null | awk 'NR==2{print $4}'; }

IFS=',' read -r -a MODEL_POOL <<< "$MODELS"
_mi=0
next_model() { local m="${MODEL_POOL[$(( _mi % ${#MODEL_POOL[@]} ))]}"; _mi=$((_mi+1)); echo "$m"; }

# bridge for a model (mirror run_trajectory.sh's selection).
bridge_for_model() { case "$1" in
  gpt*|openai*|codex*|o1*|o3*|o4*) echo "codex 8788";;
  *claude*|opus*|sonnet*|*cc)      echo "cc 8765";;
  *)                               echo "none 0";;
esac; }

# Start a bridge on its port if not already listening (once, up front). All
# per-task runs then pass --reuse-bridge so they never restart each other's.
ensure_bridge() {
  local kind="$1" port="$2"
  [ "$kind" = "none" ] && return 0
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN; then
    say "bridge $kind already up on :$port"; return 0
  fi
  say "starting bridge $kind on :$port"
  if [ "$kind" = "codex" ]; then
    python -m agent.openai_codex --check >/dev/null 2>&1 || { say "ERROR: codex auth check failed (~/.codex/auth.json)"; return 1; }
    nohup python -m agent.openai_codex --host 0.0.0.0 --port "$port" >/tmp/codex_bridge.log 2>&1 &
  elif [ "$kind" = "cc" ]; then
    export KAIJU_CC_BRIDGE_SECRET="${KAIJU_CC_BRIDGE_SECRET:-kaiju-cc-fixed}"
    KAIJU_CC_BRIDGE_HOST=0.0.0.0 nohup bash scripts/claude_code_bridge.sh start >/tmp/cc_bridge.log 2>&1 &
  fi
  local i; for i in $(seq 1 20); do
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | grep -q LISTEN && { say "bridge $kind up on :$port"; return 0; }
    sleep 1
  done
  say "ERROR: bridge $kind did not come up on :$port"; return 1
}

# resume: a task is COMPLETE if its dataset exists and a pipeline_results.json
# with all 3 stages is present under outputs/<uuid>/.
task_complete() {
  local split="$1" ds="${split}_dataset.json"
  [ -f "$ds" ] || return 1
  python3 - "$ds" <<'PY' 2>/dev/null
import json,sys,glob
try: uid=json.load(open(sys.argv[1]))[0]["id"]
except Exception: sys.exit(1)
fs=glob.glob(f"outputs/{uid}/runs/*/agent/run_1/pipeline_results.json")
if not fs: sys.exit(1)
try: d=json.load(open(fs[0]))
except Exception: sys.exit(1)
sys.exit(0 if all(k in d for k in ("stage1","stage2","stage3")) else 1)
PY
}

# classify a finished run's log as transient (retryable) vs terminal.
is_transient_failure() {
  grep -qiE "docker daemon|Cannot connect to the Docker|input/output error|no space left|bridge: unauthorized|500 Server Error|connection reset|failed to solve|context deadline exceeded" "$1" 2>/dev/null
}

# ---------------------------------------------------------------- build tasks -
declare -a T_LANG T_REPO T_MODEL T_PREP T_RUN
add_task() {
  local lang="$1" repo="$2" model="$3" prep="$4" run="$5"
  [ -z "$lang" ] || [ -z "$repo" ] && return 0
  [ -z "$model" ] && model="$(next_model)"
  T_LANG+=("$lang"); T_REPO+=("$repo"); T_MODEL+=("$model"); T_PREP+=("$prep"); T_RUN+=("$run")
}

if [ -n "$MANIFEST" ]; then
  [ -f "$MANIFEST" ] || { echo "ERROR: manifest not found: $MANIFEST"; exit 1; }
  while IFS='|' read -r lang repo model prep run || [ -n "$lang" ]; do
    lang="$(echo "$lang" | xargs)"; [ -z "$lang" ] && continue
    case "$lang" in \#*) continue;; esac
    # trim leading/trailing whitespace on prep/run (preserve internal + quotes)
    prep="$(printf '%s' "${prep:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    run="$(printf '%s' "${run:-}"  | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    add_task "$lang" "$(echo "$repo" | xargs)" "$(echo "$model" | xargs)" "$prep" "$run"
  done < "$MANIFEST"
fi
for pair in "${INLINE_PAIRS[@]:-}"; do
  [ -z "$pair" ] && continue
  add_task "${pair%%:*}" "${pair#*:}" "" "" ""
done

N=${#T_LANG[@]}
[ "$N" -eq 0 ] && { echo "ERROR: no tasks (use --manifest or -- lang:repo ...)"; exit 1; }

say "batch: $N task(s), max_parallel=$MAX_PARALLEL, min_disk=${MIN_DISK_GB}G, retries=$RETRIES, models=[$MODELS], iter=$ITER, resume=$RESUME"
for i in $(seq 0 $((N-1))); do
  printf "  [%d] %-6s %-32s model=%s\n" "$i" "${T_LANG[$i]}" "${T_REPO[$i]}" "${T_MODEL[$i]}" | tee -a "$BATCH_LOG"
done
[ "$LIST_ONLY" = "1" ] && exit 0

# ---------------------------------------------------------------- bridges -----
# Ensure every bridge any task needs is up ONCE, before the pool starts.
# bash 3.2 (macOS default) has no associative arrays — track needed bridges as flags.
_need_codex=0; _need_cc=0
for i in $(seq 0 $((N-1))); do
  read -r bkind bport <<< "$(bridge_for_model "${T_MODEL[$i]}")"
  case "$bkind" in codex) _need_codex=1;; cc) _need_cc=1;; esac
done
if [ "$DRY_RUN" != "1" ]; then
  [ "$_need_codex" = "1" ] && { ensure_bridge codex 8788 || { say "FATAL: codex bridge unavailable"; exit 1; }; }
  [ "$_need_cc" = "1" ]    && { ensure_bridge cc 8765    || { say "FATAL: cc bridge unavailable"; exit 1; }; }
fi

# ---------------------------------------------------------------- launch one --
launch_task() {
  local i="$1" attempt="$2"
  local lang="${T_LANG[$i]}" repo="${T_REPO[$i]}" model="${T_MODEL[$i]}"
  local split="${repo##*/}"
  local log="seq_${lang}_${split}.log"
  local -a cmd=(bash run_trajectory.sh --repo "$repo" --lang "$lang" --model "$model" --reuse-bridge)
  [ "$SKIP_PREP" = "1" ]     && cmd+=(--skip-prepare)
  [ -n "${T_PREP[$i]}" ]     && cmd+=(--prepare-args "${T_PREP[$i]}")
  local runargs="--max-iteration $ITER"
  cmd+=(--pipeline-args "$runargs")
  local extra_run="${T_RUN[$i]}"
  [ "$REBUILD_AGENT" = "1" ] && extra_run="--rebuild-agent-image $extra_run"
  [ -n "$extra_run" ]        && cmd+=(--run-args "$extra_run")
  if [ "$DRY_RUN" = "1" ]; then echo "  DRY[$i]: ${cmd[*]} > $log"; return 0; fi
  say "launch [$i] $lang $repo (model=$model attempt=$attempt) -> $log"
  ( "${cmd[@]}" > "$log" 2>&1; echo $? > "${log}.rc" ) &
  echo $!
}

# ---------------------------------------------------------------- pool loop ---
declare -a VERDICT
declare -a RUNNING=()          # entries "pid|idx|attempt" (bash 3.2: no assoc arrays)
next=0; launched=0; done_cnt=0
trap 'say "SIGINT — no new launches; waiting on running tasks"; STOP=1' INT
STOP=0

running_count() { local c=0 e; for e in "${RUNNING[@]:-}"; do [ -n "$e" ] && c=$((c+1)); done; echo "$c"; }

reap() {
  local newrun=() e pid rest i att lang repo split log rc np
  for e in "${RUNNING[@]:-}"; do
    [ -z "$e" ] && continue
    pid="${e%%|*}"; rest="${e#*|}"; i="${rest%%|*}"; att="${rest##*|}"
    if kill -0 "$pid" 2>/dev/null; then newrun+=("$e"); continue; fi
    wait "$pid" 2>/dev/null
    lang="${T_LANG[$i]}"; repo="${T_REPO[$i]}"; split="${repo##*/}"
    log="seq_${lang}_${split}.log"; rc=$(cat "${log}.rc" 2>/dev/null || echo "?")
    if task_complete "$split"; then
      VERDICT[$i]="OK (rc=$rc)"; say "done  [$i] $lang $repo -> OK"; done_cnt=$((done_cnt+1))
    elif [ "$att" -lt "$RETRIES" ] && is_transient_failure "$log"; then
      say "retry [$i] $lang $repo (transient failure, attempt $((att+2)))"
      np=$(launch_task "$i" "$((att+1))"); newrun+=("${np}|${i}|$((att+1))")
    else
      VERDICT[$i]="FAIL (rc=$rc)"; say "done  [$i] $lang $repo -> FAIL (see $log)"; done_cnt=$((done_cnt+1))
    fi
  done
  RUNNING=("${newrun[@]:-}")
}

if [ "$DRY_RUN" = "1" ]; then
  for i in $(seq 0 $((N-1))); do launch_task "$i" 0; done
  exit 0
fi

while [ "$done_cnt" -lt "$N" ]; do
  reap
  while [ "$(running_count)" -lt "$MAX_PARALLEL" ] && [ "$next" -lt "$N" ] && [ "$STOP" = "0" ]; do
    i="$next"; next=$((next+1))
    split="${T_REPO[$i]##*/}"
    if [ "$RESUME" = "1" ] && task_complete "$split"; then
      VERDICT[$i]="SKIP (already complete)"; say "skip  [$i] ${T_LANG[$i]} ${T_REPO[$i]} (complete)"; done_cnt=$((done_cnt+1)); continue
    fi
    df_gb="$(disk_free_gb)"
    if [ -n "$df_gb" ] && [ "$df_gb" -lt "$MIN_DISK_GB" ]; then
      say "LOW DISK (${df_gb}G < ${MIN_DISK_GB}G) — holding launches; 'docker system df' to reclaim"
      next=$((next-1)); break
    fi
    pid=$(launch_task "$i" 0); RUNNING+=("${pid}|${i}|0"); launched=$((launched+1))
  done
  [ "$(running_count)" -eq 0 ] && [ "$next" -ge "$N" ] && break
  sleep 5
done
reap

# ---------------------------------------------------------------- summary -----
say "================= BATCH SUMMARY ================="
ok=0; fail=0; skip=0
for i in $(seq 0 $((N-1))); do
  v="${VERDICT[$i]:-UNKNOWN}"
  case "$v" in OK*) ok=$((ok+1));; SKIP*) skip=$((skip+1));; *) fail=$((fail+1));; esac
  printf "  [%d] %-6s %-32s %s\n" "$i" "${T_LANG[$i]}" "${T_REPO[$i]}" "$v" | tee -a "$BATCH_LOG"
done
say "totals: OK=$ok FAIL=$fail SKIP=$skip / $N   (log: $BATCH_LOG)"
[ "$fail" -eq 0 ]
