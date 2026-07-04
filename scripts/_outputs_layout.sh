#!/usr/bin/env bash
# Output-layout helper — bash companion to kaiju/paths.py.
#
# When ``KAIJU_LOG_LAYOUT=consolidated`` (or the same via ``--layout``),
# every producer writes under ``${KAIJU_OUTPUTS_ROOT}/<uuid>/…``. Legacy
# ``flat`` layout keeps the previous ``logs/…`` and repo-root output paths.
#
# Contract: caller must set BASE_DIR before sourcing. Sourcing MUST happen
# via ``source scripts/_outputs_layout.sh``; do NOT execute directly.

if [[ -z "${BASE_DIR:-}" ]]; then
    echo "ERROR: _outputs_layout.sh requires BASE_DIR to be set by caller" >&2
    return 1 2>/dev/null || exit 1
fi

: "${KAIJU_OUTPUTS_ROOT:=${BASE_DIR}/outputs}"
: "${KAIJU_LOG_LAYOUT:=consolidated}"

outputs_layout() {
    echo "$KAIJU_LOG_LAYOUT"
}

outputs_root() {
    echo "$KAIJU_OUTPUTS_ROOT"
}

is_consolidated() {
    [[ "$KAIJU_LOG_LAYOUT" == "consolidated" ]]
}

experiment_dir() {
    local uuid="$1"
    local d="${KAIJU_OUTPUTS_ROOT}/${uuid}"
    mkdir -p "$d"
    echo "$d"
}

datasets_dir()   { local d; d="$(experiment_dir "$1")/datasets";   mkdir -p "$d"; echo "$d"; }
configs_dir()    { local d; d="$(experiment_dir "$1")/configs";    mkdir -p "$d"; echo "$d"; }
build_logs_dir() { local d; d="$(experiment_dir "$1")/build_logs"; mkdir -p "$d"; echo "$d"; }
runs_dir()       { local d; d="$(experiment_dir "$1")/runs";       mkdir -p "$d"; echo "$d"; }
harbor_dir()     { local d; d="$(experiment_dir "$1")/harbor";     mkdir -p "$d"; echo "$d"; }
