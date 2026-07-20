#!/usr/bin/env bash
# Shared failure-detection helpers for run_pipeline_*.sh.
#
# History: a Java run produced pipeline_results.json with all-zero stats and a
# top-level "All 1 sample(s) succeeded." message even though the agent crashed
# before its first LLM call. Two independent bugs:
#   (2a) The per-repo agent loop logged repo_rc but returned 0 on `read` EOF,
#        so the caller reported "returncode=0".
#   (2b) With rc=0 spoofed, each stage unconditionally ran evaluate on a branch
#        that was never pushed (rc=2, "bad revision …"), recorded 0/0 stats,
#        and the sample was counted as completed.
#
# `_agent_crashed_pre_llm` is the shared predicate every pipeline uses to gate
# `evaluate` on a real agent success signal (some per-module artifact exists),
# not just AGENT_RC.

# _agent_crashed_pre_llm <log_dir> <agent_rc>
#
# Returns 0 (true) iff `<agent_rc>` is non-zero AND the stage log dir has NO
# per-module artifacts anywhere beneath it — no `aider.log`, no `output.json`,
# no `.done` marker. That is the exact "module init failure / crash before
# first LLM call" fingerprint: no branch was pushed, so downstream `evaluate`
# can only emit `bad revision …` noise.
#
# Returns 1 (false) in every other case, including rc=0 (nothing to detect) and
# rc!=0 WITH artifacts (partial work — evaluate what we have).
_agent_crashed_pre_llm() {
    local log_dir="$1"
    local rc="$2"
    [[ "${rc:-0}" -eq 0 ]] && return 1
    [[ -d "$log_dir" ]] || return 0
    local artifact_count done_count
    artifact_count=$(find "$log_dir" \( -name aider.log -o -name output.json \) 2>/dev/null | wc -l | tr -d ' ')
    done_count=$(find "$log_dir" -name .done 2>/dev/null | wc -l | tr -d ' ')
    [[ "${artifact_count:-0}" == "0" && "${done_count:-0}" == "0" ]]
}
