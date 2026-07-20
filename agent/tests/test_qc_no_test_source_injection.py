"""Guard: stage-3 does NOT inject test-SOURCE files into the agent prompt, but
STILL protects them from edits and STILL feeds summarized results.

Background (QC): injecting the full test-file bodies as read-only context (a)
leaked the exact expected values (the model could reverse-engineer / hardcode)
and (b) on test-heavy repos ballooned the prompt to ~315k tokens, which made the
model return an EMPTY completion (mis-surfaced by aider as "check your provider
account?"). Decision: drop test-source injection by default; keep the summarized
test-output signal and the anti-cheat protection.

This pins three invariants across all 8 languages so the leak/blow-up cannot
return silently:
  1. anti-cheat is UNCONDITIONAL — protected_paths always uses test_files_readonly
     (the model can never edit a test file, regardless of the injection flag);
  2. injection is GATED — read_only_fnames is added only when
     inject_test_files_readonly is true;
  3. the pipeline DEFAULT is off — INJECT_TEST_FILES_READONLY="false".
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "agent"

_AGENTS = [
    "agents.py", "agents_c.py", "agents_go.py", "agents_java.py",
    "agents_cpp.py", "agents_rust.py", "agents_js.py", "agents_ts.py",
]
_PIPELINES = [
    "run_pipeline.sh", "run_pipeline_c.sh", "run_pipeline_cpp.sh",
    "run_pipeline_go.sh", "run_pipeline_java.sh", "run_pipeline_js.sh",
    "run_pipeline_rust.sh", "run_pipeline_ts.sh",
]


@pytest.mark.parametrize("mod", _AGENTS)
def test_anti_cheat_protection_is_unconditional(mod):
    src = (AGENT / mod).read_text(encoding="utf-8")
    assert "protected_paths=set(test_files_readonly" in src, (
        f"agent/{mod}: test files must ALWAYS be in GuardedInputOutput "
        f"protected_paths (anti-cheat), independent of the injection flag"
    )


@pytest.mark.parametrize("mod", _AGENTS)
def test_source_injection_is_gated_on_the_flag(mod):
    src = (AGENT / mod).read_text(encoding="utf-8")
    # read_only_fnames must be conditional on inject_test_files_readonly, so the
    # test SOURCE is only added to the prompt when explicitly opted in.
    assert re.search(
        r"read_only_fnames\s*=.*inject_test_files_readonly", src, re.S
    ) or re.search(
        r"inject_test_files_readonly\s+(?:and|else).*read_only|_read_only\s*=.*inject_test_files_readonly",
        src, re.S,
    ), (
        f"agent/{mod}: read_only_fnames (test-source injection) must be gated on "
        f"inject_test_files_readonly"
    )


@pytest.mark.parametrize("drv", _PIPELINES)
def test_pipeline_default_is_no_injection(drv):
    src = (REPO / drv).read_text(encoding="utf-8")
    assert 'INJECT_TEST_FILES_READONLY="false"' in src, (
        f"{drv}: INJECT_TEST_FILES_READONLY must default to \"false\" — test "
        f"SOURCE is not injected into the prompt by default"
    )
    assert 'INJECT_TEST_FILES_READONLY="true"' not in re.sub(
        r'--test-files-readonly\)[^\n]*', "", src
    ), (
        f"{drv}: found a default INJECT_TEST_FILES_READONLY=\"true\" outside the "
        f"opt-in flag clause"
    )
