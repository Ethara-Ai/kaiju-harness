"""Guards for the pipeline results-summary rendering.

Regression coverage for two container-only failures:
  - `jq --arg end` collided with jq's `end` keyword (`if…then…end`), which the
    container's jq rejects -> the .end_time update failed -> RESULTS_JSON was
    emptied -> $0.00 costs + `bc` "(standard_in) 1: syntax error".
  - format_pct fed an empty pass rate (a 0/0 / COMPILE_FAILED eval) to `bc`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PIPELINES = [
    "run_pipeline_rust.sh", "run_pipeline_go.sh", "run_pipeline_js.sh",
    "run_pipeline_ts.sh", "run_pipeline_java.sh", "run_pipeline_cpp.sh",
]
ROOT = Path(__file__).resolve().parents[1]

# jq reserved words that must never be used as a `--arg` variable name.
JQ_KEYWORDS = {
    "end", "and", "or", "if", "then", "else", "elif", "as", "def",
    "reduce", "foreach", "try", "catch", "label", "import", "include", "not",
}


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_no_jq_keyword_arg_names(pipeline):
    """No `jq --arg <keyword>` / `--argjson <keyword>` anywhere (fails on the
    container's stricter jq)."""
    text = (ROOT / pipeline).read_text()
    import re
    for m in re.finditer(r"--arg(?:json)?\s+([A-Za-z_][A-Za-z0-9_]*)", text):
        assert m.group(1) not in JQ_KEYWORDS, (
            f"{pipeline}: jq --arg uses reserved keyword {m.group(1)!r}")


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("bc"),
                    reason="needs bash + bc")
@pytest.mark.parametrize("pipeline", PIPELINES)
def test_format_pct_handles_empty_and_zero(pipeline):
    """format_pct must not emit a `bc` syntax error on empty/non-numeric input
    (a 0/0 eval leaves the pass rate unset)."""
    text = (ROOT / pipeline).read_text()
    # Extract the format_pct function body.
    import re
    m = re.search(r"(format_pct\(\) \{.*?\n\})", text, re.DOTALL)
    assert m, f"{pipeline}: format_pct not found"
    fn = m.group(1)
    for val, expect in [("", "0.0%"), ("0.0", "0.0%"), ("0.85", "85.0%")]:
        r = subprocess.run(
            ["bash", "-c", f'{fn}\nformat_pct "{val}"'],
            capture_output=True, text=True)
        assert "syntax error" not in (r.stdout + r.stderr), (
            f"{pipeline}: format_pct({val!r}) -> bc error: {r.stderr}")
        assert r.stdout.strip() == expect, (
            f"{pipeline}: format_pct({val!r}) = {r.stdout.strip()!r}, want {expect!r}")


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_eval_artifacts_are_collected(pipeline):
    """Each pipeline must define + call collect_eval_artifacts so the eval's
    test_output.txt / patch.diff / exit codes are preserved under outputs/
    (the eval writes them to logs/, lost on container teardown)."""
    import re
    text = (ROOT / pipeline).read_text()
    assert "collect_eval_artifacts() {" in text, f"{pipeline}: collector missing"
    assert re.search(r'collect_eval_artifacts "\$\{stage_label', text), \
        f"{pipeline}: collector never called after eval"
