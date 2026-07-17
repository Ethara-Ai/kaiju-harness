"""Cross-language parity guards for the run_pipeline_*.sh drivers.

These pin invariants that drifted between the eight copy-then-edit sibling
pipelines (QC C2 axis). Each test iterates every driver so a future edit to one
language cannot silently diverge from the canonical run_pipeline.sh.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# All eight production pipeline drivers.
DRIVERS = {
    "python": REPO_ROOT / "run_pipeline.sh",
    "c": REPO_ROOT / "run_pipeline_c.sh",
    "cpp": REPO_ROOT / "run_pipeline_cpp.sh",
    "go": REPO_ROOT / "run_pipeline_go.sh",
    "java": REPO_ROOT / "run_pipeline_java.sh",
    "js": REPO_ROOT / "run_pipeline_js.sh",
    "rust": REPO_ROOT / "run_pipeline_rust.sh",
    "ts": REPO_ROOT / "run_pipeline_ts.sh",
}

_IDS = sorted(DRIVERS)


@pytest.mark.parametrize("lang", _IDS)
def test_bash_syntax_ok(lang):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not on PATH")
    r = subprocess.run(
        [bash, "-n", str(DRIVERS[lang])], capture_output=True, text=True, timeout=20
    )
    assert r.returncode == 0, f"bash -n {lang}: {r.stderr!r}"


@pytest.mark.parametrize("lang", _IDS)
def test_save_results_is_atomic(lang):
    """QC-C2-001: save_results must write a temp file, validate it, then mv -f.

    The non-atomic `echo "$RESULTS_JSON" | jq . > "$PIPELINE_LOG"` truncates a
    whole run's results to 0 bytes if jq fails or RESULTS_JSON is empty (which
    silently zeroes every stage score + ATIF reward).
    """
    src = DRIVERS[lang].read_text(encoding="utf-8")
    assert "save_results()" in src, f"{lang}: no save_results() defined"
    # The atomic temp-file promotion must be present.
    assert '"${PIPELINE_LOG}.tmp' in src, (
        f"{lang}: save_results must write PIPELINE_LOG atomically via a "
        f'"${{PIPELINE_LOG}}.tmp.$$" file + mv -f, not a raw redirect'
    )
    # The dangerous non-atomic redirect must NOT be present.
    assert 'jq \'.\' > "$PIPELINE_LOG"' not in src, (
        f"{lang}: save_results still uses the non-atomic "
        f"`jq '.' > \"$PIPELINE_LOG\"` redirect that can truncate results"
    )


@pytest.mark.parametrize("lang", _IDS)
def test_has_inactivity_watchdog(lang):
    """QC-C2-002: every pipeline must run the agent under watchdog_run so a
    hung/stuck agent is killed on inactivity/wall-time instead of burning budget
    unbounded. C was the sole driver missing it."""
    src = DRIVERS[lang].read_text(encoding="utf-8")
    assert "watchdog_run()" in src, f"{lang}: no watchdog_run() defined"
    assert 'watchdog_run "' in src, f"{lang}: watchdog_run is never called"
    # The declared timeout knobs must actually be passed to the watchdog (not
    # dead SC2034 vars as they were in C before the fix).
    assert "INACTIVITY_TIMEOUT" in src and "MAX_WALL_TIME" in src, (
        f"{lang}: watchdog timeout knobs missing"
    )


@pytest.mark.parametrize("lang", _IDS)
def test_max_parallel_repos_parity(lang):
    """QC-C2-006: MAX_PARALLEL_REPOS must have a default AND a CLI parse clause
    (JS previously hardcoded `--max-parallel-repos 1`)."""
    src = DRIVERS[lang].read_text(encoding="utf-8")
    assert "MAX_PARALLEL_REPOS=" in src, f"{lang}: no MAX_PARALLEL_REPOS default"
    assert "--max-parallel-repos)" in src, (
        f"{lang}: no --max-parallel-repos parse clause"
    )
    assert "--max-parallel-repos 1" not in src, (
        f"{lang}: hardcodes --max-parallel-repos 1 instead of the variable"
    )


@pytest.mark.parametrize("lang", _IDS)
def test_env_loaded_via_shared_whitelist(lang):
    """QC-C2-009: every pipeline must load .env through the shared whitelist
    helper (exports only known-safe vars) rather than the blanket
    `set -a; source .env; set +a` that leaked every .env secret into children."""
    src = DRIVERS[lang].read_text(encoding="utf-8")
    assert "_load_env_whitelist.sh" in src, (
        f"{lang}: does not source scripts/_load_env_whitelist.sh"
    )
    # The blanket auto-export around a .env source must be gone. Check for the
    # dangerous adjacency (a `set -a` line within 3 lines of a `source .env`),
    # not the word `set -a` in a comment.
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip() == "set -a":
            window = "\n".join(lines[i : i + 4])
            assert ".env" not in window, (
                f"{lang}: still blanket-exports .env via `set -a; source .env` "
                f"near line {i + 1}"
            )


# Build-heavy languages compile inside the eval container (CMake/Maven), so they
# keep a larger, EVAL_TIMEOUT-derived default — an explicit whitelisted deviation.
_EVAL_TIMEOUT_BUILD_HEAVY = {"c", "cpp", "java"}
# The inner per-run test cap (EVAL_TEST_TIMEOUT) that the outer cap must exceed.
_INNER_TEST_CAP = 900


@pytest.mark.parametrize("lang", _IDS)
def test_eval_timeout_uses_shared_var(lang):
    """QC-C2-010: the outer `commit0 evaluate --timeout` must derive from the
    single shared KAIJU_EVAL_HARNESS_TIMEOUT var in every pipeline (was 5
    divergent policies: 300 / $EVAL_TIMEOUT / 600 / 700)."""
    src = DRIVERS[lang].read_text(encoding="utf-8")
    m = re.search(
        r'--timeout "\$\{KAIJU_EVAL_HARNESS_TIMEOUT:-([^}]+)\}"', src
    )
    assert m, (
        f"{lang}: `commit0 evaluate --timeout` must use "
        f'"${{KAIJU_EVAL_HARNESS_TIMEOUT:-<default>}}" (shared override var)'
    )
    default = m.group(1)
    if lang in _EVAL_TIMEOUT_BUILD_HEAVY:
        # documented deviation: keep the generous EVAL_TIMEOUT build margin
        assert default == "$EVAL_TIMEOUT", (
            f"{lang} is build-heavy; its eval --timeout default should stay "
            f"$EVAL_TIMEOUT (got {default!r})"
        )
    else:
        # fast langs: a concrete default that exceeds the inner per-run test cap
        assert default.isdigit() and int(default) > _INNER_TEST_CAP, (
            f"{lang}: eval --timeout default {default!r} must be a number > the "
            f"inner EVAL_TEST_TIMEOUT cap ({_INNER_TEST_CAP}) so a slow-but-legit "
            f"suite is not falsely timed out to a 0 score"
        )


# QC-C2-005: canonical default values every pipeline shares, and the ONLY
# permitted per-language deviations — each justified in-line in the driver. A
# new silent drift (any other value) fails this guard.
_CANONICAL_DEFAULTS = {
    "BACKEND": "local",
    "INACTIVITY_TIMEOUT": "900",
    "EVAL_TIMEOUT": "3600",
    "USE_SPEC_INFO": "true",
}
_ALLOWED_DEVIATIONS = {
    ("java", "BACKEND"): "local_inplace",       # Java builds in-place
    ("cpp", "INACTIVITY_TIMEOUT"): "1800",       # slow C++ compiles between turns
    ("java", "INACTIVITY_TIMEOUT"): "1800",      # slow Maven builds between turns
    ("cpp", "EVAL_TIMEOUT"): "7200",             # slow C++ compiles inside eval
    ("cpp", "USE_SPEC_INFO"): "false",           # cpp spec docs default-off
    ("js", "USE_SPEC_INFO"): "false",            # JS uses README, not PDF spec
}


def _default_value(src: str, knob: str):
    """Extract KNOB=<value>'s effective default (handles ${KNOB:-x} + quotes)."""
    m = re.search(rf'^{knob}=(.+)$', src, re.M)
    if not m:
        return None
    raw = m.group(1).strip().strip('"').strip("'")
    dm = re.match(rf'\$\{{{knob}:-([^}}]+)\}}', raw)   # ${KNOB:-default}
    return dm.group(1) if dm else raw


# Pipelines that emit the agent-config YAML inline (c shells out to
# agent.config_c; java writes it per-stage in run_agent_java), so the inline
# emitter-parity guard applies only to these six.
_INLINE_YAML_PIPELINES = {"python", "cpp", "go", "js", "rust", "ts"}


@pytest.mark.parametrize(
    "lang", sorted(_INLINE_YAML_PIPELINES)
)
def test_agent_config_knobs_are_parameterized_not_hardcoded(lang):
    """QC-C2-008: scoring-relevant agent-config knobs must be emitted as shell
    variables (per-stage settable), never hardcoded literals. py/go used to
    hardcode `use_unit_tests_info: false` while cpp/js/rust/ts parameterized it,
    so the same knob silently meant different things per language."""
    src = DRIVERS[lang].read_text(encoding="utf-8")
    for knob in ("use_unit_tests_info", "add_import_module_to_context"):
        # must appear parameterized: `knob: ${knob}` (or ${knob:-default})
        param = re.search(rf'^{knob}:\s*\$\{{{knob}', src, re.M)
        hard = re.search(rf'^{knob}:\s*(true|false)\s*$', src, re.M)
        assert param and not hard, (
            f"{lang}: agent-config `{knob}` must be emitted as ${{{knob}}} "
            f"(per-stage settable), not a hardcoded literal — otherwise the knob "
            f"silently diverges from the parameterized pipelines"
        )


@pytest.mark.parametrize("lang", _IDS)
def test_default_value_parity_or_whitelisted_deviation(lang):
    """QC-C2-005: each scoring-relevant default must equal the canonical value
    unless it is an explicitly-whitelisted, in-line-justified deviation."""
    src = DRIVERS[lang].read_text(encoding="utf-8")
    for knob, canonical in _CANONICAL_DEFAULTS.items():
        val = _default_value(src, knob)
        assert val is not None, f"{lang}: {knob} default not found"
        allowed = _ALLOWED_DEVIATIONS.get((lang, knob), canonical)
        assert val == allowed, (
            f"{lang}: {knob}={val!r} but canonical is {canonical!r} and the only "
            f"allowed deviation is {allowed!r}. Either restore the canonical value "
            f"or add a justified entry to _ALLOWED_DEVIATIONS (silent default "
            f"drift makes per-language scores incomparable)."
        )
