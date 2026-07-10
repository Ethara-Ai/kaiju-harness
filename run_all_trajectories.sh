#!/usr/bin/env bash
# Batch validation: run a trajectory per language via run_trajectory.sh and
# collect pass/fail + the key correctness signals (stage rates, cost_source,
# CHEAT scan, resume artifacts). Designed as a CHEAP SMOKE (--max-iteration 1) to
# exercise the run path end-to-end after the pipeline changes — NOT a scoring run.
#
#   bash run_all_trajectories.sh                 # ready langs (python/go/rust), --skip-prepare
#   bash run_all_trajectories.sh --iter 3        # deeper
#   bash run_all_trajectories.sh --full          # also (re)prepare — forks/pushes to GitHub
#   bash run_all_trajectories.sh py:arrow-py/arrow go:spf13/cobra   # explicit lang:repo pairs
#
# NOTE: each run is PAID (LLM) and needs the bridge; the script runs them
# SEQUENTIALLY. c/cpp/js/ts/java have no prepared repo here — pass explicit pairs
# (and drop --skip-prepare) to include them.
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)" || { echo "ERROR: cannot cd to script dir"; exit 1; }
# Add each language's toolchain dir to PATH only if present (portable, no dupes):
# node/cmake (homebrew), javac (openjdk), cargo (rustup), go.
for _d in /opt/homebrew/bin /opt/homebrew/opt/openjdk/bin /home/linuxbrew/.linuxbrew/bin \
          "$HOME/.cargo/bin" "$HOME/go/bin" /usr/local/go/bin /usr/lib/go/bin; do
  [ -d "$_d" ] || continue
  case ":$PATH:" in *":$_d:"*) ;; *) PATH="$_d:$PATH" ;; esac
done
export PATH

ITER=1; MODE="--skip-prepare"; MODEL="gpt55"
PAIRS=()
while [ $# -gt 0 ]; do case "$1" in
  --iter) ITER="$2"; shift 2;;
  --model) MODEL="$2"; shift 2;;
  --full) MODE=""; shift;;                 # re-prepare (fork/push) instead of --skip-prepare
  --skip-prepare) MODE="--skip-prepare"; shift;;
  *:*) PAIRS+=("$1"); shift;;              # lang:owner/repo
  *) echo "unknown arg $1"; exit 1;;
esac; done

# Default set = ONE fresh small repo per language, full prepare+build+run.
if [ ${#PAIRS[@]} -eq 0 ]; then
  MODE=""   # full prepare (fork/push) for these fresh repos
  PAIRS=(
    "python:un33k/python-slugify"
    "go:google/uuid"
    "rust:BurntSushi/byteorder"
    "c:benhoyt/inih"
    "cpp:fmtlib/fmt"
    "js:sindresorhus/slugify"
    "ts:blakeembrey/change-case"
    "java:stleary/JSON-java"
  )
fi

declare -a SUMMARY
for pair in "${PAIRS[@]}"; do
  lang="${pair%%:*}"; repo="${pair#*:}"; split="${repo##*/}"
  echo ""
  echo "########################################################################"
  echo "# TRAJECTORY: lang=$lang repo=$repo  (iter=$ITER, ${MODE:-full-prepare})"
  echo "########################################################################"
  # normalize a couple of short aliases
  case "$lang" in py) lang=python;; esac
  if bash run_trajectory.sh --repo "$repo" --lang "$lang" --model "$MODEL" $MODE \
        --pipeline-args "--max-iteration $ITER"; then
    rc=0
  else
    rc=$?
  fi
  # verdict: did a pipeline_results.json land with all 3 stages?
  uuid=$(python -c "import json,glob,os;fs=glob.glob('${split}_dataset.json');print(json.load(open(fs[0]))[0]['id']) if fs else print('')" 2>/dev/null)
  verdict="rc=$rc"
  if [ -n "$uuid" ]; then
    stages=$(python -c "import json,glob;fs=glob.glob('outputs/$uuid/runs/*/agent/run_1/pipeline_results.json');print(','.join(k for k in json.load(open(fs[0])) if k.startswith('stage'))) if fs else print('no-results')" 2>/dev/null)
    verdict="$verdict stages=[$stages]"
  fi
  SUMMARY+=( "$lang:$split -> $verdict" )
done

echo ""
echo "======================= BATCH SUMMARY ======================="
for s in "${SUMMARY[@]}"; do echo "  $s"; done
echo "============================================================="
echo "Not run (need a repo + prepare): c, cpp, js, ts, java —"
echo "  bash run_all_trajectories.sh --full c:owner/repo cpp:owner/repo js:owner/repo ts:owner/repo java:owner/repo"
