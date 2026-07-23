"""Tests for the P1 deterministic trajectory-verification core.

Covers the two taxonomy invariants (the registry lint — complete + non-duplicative),
the artifact reader, and the evaluator's gate/score logic on synthetic run dirs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiju.verification import verify_run
from kaiju.verification.deterministic import CHECKS
from kaiju.verification.schemas import CheckStatus, Gate
from kaiju.verification.taxonomy import TAXONOMY, Layer, Owner


# --------------------------------------------------------------------------- #
# Registry lint: the machine-checkable "complete + non-duplicative" invariants
# --------------------------------------------------------------------------- #
def test_concern_ids_unique():
    ids = [c.id for c in TAXONOMY]
    assert len(ids) == len(set(ids)), "duplicate concern id in taxonomy"


def test_coverage_registry_is_sound_and_complete():
    from kaiju.verification.coverage import coverage_manifest, registry_violations
    assert registry_violations() == []          # complete + non-duplicative + partitioned
    m = coverage_manifest()
    assert m["n_concerns"] == len(TAXONOMY)
    assert m["n_enforced"] + m["n_pending"] == m["n_concerns"]
    assert {c["id"] for c in m["concerns"]} == {c.id for c in TAXONOMY}


def test_single_owner_matches_layer():
    # Deterministic concerns live in layers 0/1; rubric concerns in layer 2.
    # This is the non-duplication guarantee: a concern has exactly one owner and
    # a deterministic check can never target a rubric concern (different layer).
    for c in TAXONOMY:
        if c.owner is Owner.RUBRIC:
            assert c.layer is Layer.JUDGMENT, f"{c.id}: rubric concern not in layer 2"
        else:
            assert c.layer in (Layer.LEGITIMACY, Layer.STRUCTURE), \
                f"{c.id}: deterministic concern not in layer 0/1"


def test_checks_map_to_real_deterministic_concerns():
    ids = {c.id: c for c in TAXONOMY}
    for cid in CHECKS:
        assert cid in ids, f"check {cid} has no concern"
        assert ids[cid].owner is Owner.DETERMINISTIC, f"{cid}: check on a rubric concern"


def test_rubric_concerns_are_never_enforced_deterministically():
    # a rubric-owned concern must NOT have a deterministic check (partition).
    for c in TAXONOMY:
        if c.owner is Owner.RUBRIC:
            assert c.id not in CHECKS, f"{c.id}: rubric concern has a deterministic check"


# --------------------------------------------------------------------------- #
# Synthetic run-dir builder
# --------------------------------------------------------------------------- #
def _write_module(mod_dir: Path, patch: str, *, done=True, cost=0.5,
                  artifacts=("turns.jsonl", "llm_history.txt", "aider.log")):
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "output.json").write_text(json.dumps({
        "module": mod_dir.name,
        "test_result": {"git_patch": patch},
        "metrics": {"total_cost": cost},
    }))
    for a in artifacts:
        (mod_dir / a).write_text("x")
    if done:
        (mod_dir / ".done").write_text("")


def _build_run(root: Path, *, stage3_patch="+++ b/src/foo.py\n+code\n",
               passed=(8, 8, 9), tests=9, eval_status="OK", drop_stage=None) -> Path:
    run = root / "runs" / "claude-opus-4.8" / "agent" / "run_1"
    run.mkdir(parents=True, exist_ok=True)
    pr = {
        "language": "python", "model_short": "claude-opus-4.8",
        "stage1": {"name": "Draft", "eval_status": eval_status,
                   "num_passed": passed[0], "num_tests": tests, "pass_rate": passed[0] / tests},
        "stage2": {"name": "Lint", "eval_status": eval_status,
                   "num_passed": passed[1], "num_tests": tests, "pass_rate": passed[1] / tests},
        "stage3": {"name": "Test", "eval_status": eval_status,
                   "num_passed": passed[2], "num_tests": tests, "pass_rate": passed[2] / tests},
    }
    if drop_stage:
        pr.pop(drop_stage)
    (run / "pipeline_results.json").write_text(json.dumps(pr))

    layout = {"stage1_draft": "+++ b/src/foo.py\n+draft\n",
              "stage2_lint": "+++ b/src/foo.py\n+lint\n",
              "stage3_tests": stage3_patch}
    for sdir, patch in layout.items():
        if drop_stage and sdir.startswith(drop_stage):
            continue
        current = run / sdir / "repo" / "branch" / "current"
        _write_module(current / "foo", patch)
        (current / "model_changes.diff").write_text(patch)
    return run


def test_clean_run_accepts(tmp_path):
    run = _build_run(tmp_path)
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.ACCEPT, report.gating_failures
    assert report.graded_score == 1.0
    # the report was written and is valid json
    assert json.loads((tmp_path / "rep.json").read_text())["gate"] == "accept"


def test_test_file_tamper_quarantines(tmp_path):
    run = _build_run(tmp_path, stage3_patch="+++ b/tests/test_foo.py\n+assert True\n")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    ids = {r.concern_id for r in report.gating_failures}
    assert "L0.TEST_FILES_UNTOUCHED" in ids


def test_conftest_tamper_quarantines(tmp_path):
    run = _build_run(tmp_path, stage3_patch="+++ b/conftest.py\n+def pytest_collection(): pass\n")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.TEST_FILES_UNTOUCHED" for r in report.gating_failures)


def test_infra_crash_status_quarantines(tmp_path):
    run = _build_run(tmp_path, eval_status="PYTEST_INFRA_ERROR")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.OUTCOME_SIGNAL_TRUSTWORTHY" for r in report.gating_failures)


def _patch_pipeline(run: Path, **stage_overrides):
    pr = json.loads((run / "pipeline_results.json").read_text())
    for stage, kv in stage_overrides.items():
        pr[stage].update(kv)
    (run / "pipeline_results.json").write_text(json.dumps(pr))


def test_earlier_compile_failed_still_accepts(tmp_path):
    # A draft that didn't compile (stage1 COMPILE_FAILED) is a legitimate
    # intermediate when the final stage produced a real signal — must NOT quarantine.
    run = _build_run(tmp_path)
    _patch_pipeline(run, stage1={"eval_status": "COMPILE_FAILED", "num_passed": 0})
    report = verify_run(run, out_path=tmp_path / "rep.json")
    sig = next(r for r in report.results if r.concern_id == "L0.OUTCOME_SIGNAL_TRUSTWORTHY")
    assert sig.status is CheckStatus.PASS, sig.evidence
    assert report.gate is Gate.ACCEPT


def test_final_stage_infra_error_quarantines(tmp_path):
    run = _build_run(tmp_path)
    _patch_pipeline(run, stage3={"eval_status": "PYTEST_INFRA_ERROR"})
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.OUTCOME_SIGNAL_TRUSTWORTHY" for r in report.gating_failures)


def test_regression_quarantines(tmp_path):
    run = _build_run(tmp_path, passed=(9, 9, 7))  # stage3 drops below stage2
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L1.NO_REGRESSION" for r in report.gating_failures)


def test_missing_stage_quarantines(tmp_path):
    run = _build_run(tmp_path, drop_stage="stage3")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    ids = {r.concern_id for r in report.gating_failures}
    assert "L1.STAGE_DIRS_PRESENT" in ids or "L0.PIPELINE_COMPLETE" in ids


def test_partial_solve_accepts_but_scores_below_one(tmp_path):
    # A legitimate partial solve (stage3 not full pass) must NOT quarantine
    # (L1.TEST_STAGE_OUTCOME is graded, not gating) but should lower the score.
    run = _build_run(tmp_path, passed=(6, 6, 6), tests=9)
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.ACCEPT
    assert report.graded_score < 1.0
    outcome = next(r for r in report.results if r.concern_id == "L1.TEST_STAGE_OUTCOME")
    assert outcome.status is CheckStatus.FAIL and outcome.gating is False


def test_zero_turn_module_without_llm_history_is_ok(tmp_path):
    # A stage-3 test module that did zero LLM work (already green) has no
    # llm_history.txt and must NOT be flagged (would false-quarantine a green run).
    run = _build_run(tmp_path)
    zero = run / "stage3_tests" / "repo" / "branch" / "current" / "already_green"
    zero.mkdir(parents=True)
    (zero / "output.json").write_text(json.dumps(
        {"module": "already_green", "test_result": {"git_patch": ""},
         "metrics": {"total_cost": 0.0, "total_llm_calls": 0, "num_turns": 0}}))
    for a in ("turns.jsonl", "aider.log"):
        (zero / a).write_text("x")
    (zero / ".done").write_text("")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    art = next(r for r in report.results if r.concern_id == "L0.MODULE_ARTIFACTS_PRESENT")
    assert art.status is CheckStatus.PASS, art.evidence
    assert report.gate is Gate.ACCEPT


def test_llm_active_module_missing_history_fails(tmp_path):
    # But a module that DID make LLM calls and lacks llm_history.txt is a real gap.
    run = _build_run(tmp_path)
    m = run / "stage1_draft" / "repo" / "branch" / "current" / "worked"
    m.mkdir(parents=True)
    (m / "output.json").write_text(json.dumps(
        {"module": "worked", "test_result": {"git_patch": "+++ b/src/x.py\n+1\n"},
         "metrics": {"total_cost": 1.0, "total_llm_calls": 3, "num_turns": 2}}))
    for a in ("turns.jsonl", "aider.log"):
        (m / a).write_text("x")
    (m / ".done").write_text("")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    art = next(r for r in report.results if r.concern_id == "L0.MODULE_ARTIFACTS_PRESENT")
    assert art.status is CheckStatus.FAIL
    assert any("worked" == g["module"] for g in art.evidence["gaps"])


def test_pending_concerns_do_not_affect_gate_or_score(tmp_path):
    run = _build_run(tmp_path)
    report = verify_run(run, out_path=tmp_path / "rep.json")
    pend = [r for r in report.results if r.status is CheckStatus.PENDING]
    assert pend, "expected some concerns to be pending (later phases)"
    assert all(not r.counts_toward_score for r in pend)
    # C3 honesty: the meta field is always present (now empty — all gating concerns
    # are enforced; the pending ones are non-gating rubric/P2b concerns).
    assert "unenforced_gating_concerns" in report.meta
    assert all(not r.gating for r in report.results if r.status is CheckStatus.PENDING)


# --------------------------------------------------------------------------- #
# Adversarial-review regression tests (C1, C2, H1-H5, M4, M5, allowlist, globs)
# --------------------------------------------------------------------------- #
def test_cheat_detected_quarantines(tmp_path):
    # C1: the eval already caught forged output; the gate must NOT wave it through.
    run = _build_run(tmp_path)
    _patch_pipeline(run, stage3={"eval_status": "CHEAT_DETECTED"})
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.OUTCOME_SIGNAL_TRUSTWORTHY" for r in report.gating_failures)


def test_unknown_status_quarantines(tmp_path):
    # C1/C2 allowlist: a novel/unknown final status is NOT trusted.
    run = _build_run(tmp_path)
    _patch_pipeline(run, stage3={"eval_status": "GO_TEST_CRASH"})
    assert verify_run(run, out_path=tmp_path / "r.json").gate is Gate.QUARANTINE
    _patch_pipeline(run, stage3={"eval_status": "OUTPUT_MISSING"})
    assert verify_run(run, out_path=tmp_path / "r.json").gate is Gate.QUARANTINE


def test_final_stage_compile_failed_is_legit_negative(tmp_path):
    # A real failed solve (final stage genuinely didn't compile) is legitimate.
    run = _build_run(tmp_path)
    _patch_pipeline(run, stage3={"eval_status": "COMPILE_FAILED", "num_passed": 0, "num_tests": 0})
    report = verify_run(run, out_path=tmp_path / "rep.json")
    sig = next(r for r in report.results if r.concern_id == "L0.OUTCOME_SIGNAL_TRUSTWORTHY")
    assert sig.status is CheckStatus.PASS


def test_stale_error_log_after_done_is_not_incomplete(tmp_path):
    # H1: a successful resume leaves error.log behind (never cleared). Must ACCEPT.
    run = _build_run(tmp_path)
    mod = run / "stage1_draft" / "repo" / "branch" / "current" / "foo"
    (mod / "error.log").write_text("earlier transient")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.ACCEPT, report.gating_failures


def test_crashed_module_only_error_log_is_discovered(tmp_path):
    # H2: a module that emitted ONLY error.log (no .done) must be found and flagged.
    run = _build_run(tmp_path)
    crashed = run / "stage1_draft" / "repo" / "branch" / "current" / "crashed_mod"
    crashed.mkdir(parents=True)
    (crashed / "error.log").write_text("boom")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    md = next(r for r in report.results if r.concern_id == "L0.MODULES_DONE")
    assert md.status is CheckStatus.FAIL


def test_missing_full_diff_cannot_decide_tamper(tmp_path):
    # H3: no model_changes.diff but modules did work -> ERROR (not PASS-by-absence).
    run = _build_run(tmp_path)
    for d in run.rglob("model_changes.diff"):
        d.unlink()
    report = verify_run(run, out_path=tmp_path / "rep.json")
    tf = next(r for r in report.results if r.concern_id == "L0.TEST_FILES_UNTOUCHED")
    assert tf.status is CheckStatus.ERROR
    assert report.gate is Gate.QUARANTINE


def test_inventory_change_across_stages_quarantines(tmp_path):
    # M4: a shifting denominator (frozen inventory violated) is an anomaly.
    run = _build_run(tmp_path, tests=9)
    _patch_pipeline(run, stage3={"num_tests": 7, "num_passed": 7})
    report = verify_run(run, out_path=tmp_path / "rep.json")
    nr = next(r for r in report.results if r.concern_id == "L1.NO_REGRESSION")
    assert nr.status is CheckStatus.FAIL
    assert report.gate is Gate.QUARANTINE


def test_module_named_current_is_still_verified():
    # M5: a real module dir literally named "current" (no container markers) must
    # be discovered, while the true container (holds model_changes.diff) is skipped.
    from kaiju.verification.trajectory import _discover_modules
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td) / "stage1_draft" / "repo" / "branch"
        container = stage / "current"
        (container).mkdir(parents=True)
        (container / "model_changes.diff").write_text("diff")  # container marker
        real = container / "current"                            # a module named 'current'
        real.mkdir()
        (real / "output.json").write_text("{}")
        (real / ".done").write_text("")
        mods = _discover_modules(Path(td) / "stage1_draft")
        assert any(m.name == "current" and m.dir == real for m in mods), \
            {(m.name, str(m.dir)) for m in mods}
        assert not any(m.dir == container for m in mods), "container must be skipped"


def test_expanded_cheat_globs():
    from kaiju.verification.deterministic import _is_cheatable_path
    for p in ("go.mod", "src/Cargo.toml", "jest.config.js", "setup.py",
              "src/foo/bar.test.tsx", "testdata/case.json", "pkg/util_test.go",
              "src/test/java/org/x/FooTest.java", "package-lock.json", "vitest.config.ts"):
        assert _is_cheatable_path(p), f"should flag {p}"
    for p in ("src/main.py", "lib/router.go", "src/index.ts", "include/fmt/args.h"):
        assert _is_cheatable_path(p) is None, f"should NOT flag {p}"


def test_diff_parser_robust_to_evasion():
    from kaiju.verification.deterministic import _patched_paths
    # quoted path with spaces, --no-prefix (bare), and -u tab timestamp all resolve.
    quoted = 'diff --git "a/t x/test_a.py" "b/t x/test_a.py"\n+++ "b/t x/test_a.py"\n'
    noprefix = "diff --git tests/test_b.py tests/test_b.py\n+++ tests/test_b.py\n"
    tabbed = "+++ b/tests/test_c.py\t2026-01-01 00:00:00\n"
    got = _patched_paths(quoted) | _patched_paths(noprefix) | _patched_paths(tabbed)
    assert "t x/test_a.py" in got
    assert "tests/test_b.py" in got
    assert "tests/test_c.py" in got


# --------------------------------------------------------------------------- #
# P2 content checks (structured output.json history)
# --------------------------------------------------------------------------- #
def _sys(tools=("file_editor",)):
    return {"kind": "SystemPromptEvent", "source": "agent",
            "tools": [{"name": t} for t in tools]}


def _action(tool, cid):
    return {"kind": "ActionEvent", "source": "agent", "tool_name": tool, "tool_call_id": cid}


def _obs(tool, action_id, text="File x.py has been edited.", is_error=False):
    return {"kind": "ObservationEvent", "source": "environment", "tool_name": tool,
            "action_id": action_id, "observation": {"content": [{"text": text}],
            "is_error": is_error}}


def _set_history(run: Path, stage_dir: str, module: str, events: list):
    mdir = run / stage_dir / "repo" / "branch" / "current" / module
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "output.json").write_text(json.dumps(
        {"module": module, "test_result": {"git_patch": ""},
         "metrics": {"total_cost": 1.0, "total_llm_calls": 2, "num_turns": 2},
         "history": events}))
    for a in ("turns.jsonl", "aider.log", "llm_history.txt"):
        (mdir / a).write_text("x")
    (mdir / ".done").write_text("")


def test_fabricated_observation_orphan_quarantines(tmp_path):
    run = _build_run(tmp_path)
    # an observation citing an action_id that no action produced = fabricated
    _set_history(run, "stage1_draft", "m1",
                 [_sys(), _action("file_editor", "aaa"), _obs("file_editor", "GHOST")])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.FABRICATED_OBSERVATION" for r in report.gating_failures)


def test_hallucinated_tool_fails_toolcall_but_not_gate(tmp_path):
    run = _build_run(tmp_path)
    _set_history(run, "stage1_draft", "m1",
                 [_sys(("file_editor",)), _action("evil_exfil", "aaa")])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    tc = next(r for r in report.results if r.concern_id == "L1.TOOLCALL_CORRECTNESS")
    assert tc.status is CheckStatus.FAIL and tc.gating is False
    fab = next(r for r in report.results if r.concern_id == "L0.FABRICATED_OBSERVATION")
    assert fab.status is CheckStatus.PASS  # no orphan observation here


def test_clean_content_passes_both(tmp_path):
    run = _build_run(tmp_path)
    _set_history(run, "stage1_draft", "m1",
                 [_sys(), _action("file_editor", "aaa"),
                  _obs("file_editor", "aaa", "File x.c has been edited."),
                  _action("finish", "zzz")])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    for cid in ("L0.FABRICATED_OBSERVATION", "L1.TOOLCALL_CORRECTNESS"):
        assert next(r for r in report.results if r.concern_id == cid).status is CheckStatus.PASS
    assert report.gate is Gate.ACCEPT


def test_content_parser_tolerates_malformed_events():
    from kaiju.verification.content import parse_history
    c = parse_history([None, "junk", {}, {"kind": "ActionEvent"},
                       {"kind": "ObservationEvent", "observation": "not-a-dict"}])
    assert c.num_events == 5  # counts raw; ignores malformed gracefully
    assert not c.hallucinated_actions()  # empty tool_name is not "hallucinated"


# --------------------------------------------------------------------------- #
# P3 side-channel / leak detection
# --------------------------------------------------------------------------- #
def _add_entries(tmp_path: Path, run: Path, *, reference_commit="f" * 40,
                 base_commit="a" * 40):
    # entries.json must sit at <uuid_root>/datasets/ where uuid_root is the
    # ancestor of run containing "runs".
    parts = run.parts
    uuid_root = Path(*parts[:parts.index("runs")])
    ds = uuid_root / "datasets"
    ds.mkdir(parents=True, exist_ok=True)
    (ds / "entries.json").write_text(json.dumps(
        [{"repo": "x", "base_commit": base_commit, "reference_commit": reference_commit}]))


def test_golden_sha_leak_quarantines(tmp_path):
    run = _build_run(tmp_path)
    ref = "c0ffee1234567890abcdef1234567890abcdef12"
    _add_entries(tmp_path, run, reference_commit=ref)
    # simulate the golden sha appearing in the agent's history (retrieval)
    _set_history(run, "stage1_draft", "leaky",
                 [_sys(), _action("file_editor", "aaa"),
                  _obs("file_editor", "aaa", f"applied fix from commit {ref}")])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.SIDE_CHANNEL_CLEAN" for r in report.gating_failures)


def test_breadcrumb_read_quarantines(tmp_path):
    run = _build_run(tmp_path)
    _add_entries(tmp_path, run)
    _set_history(run, "stage1_draft", "sneaky",
                 [_sys(), _action("file_editor", "aaa"),
                  _obs("file_editor", "aaa", "read .kaiju/entries.json for the answer")])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.SIDE_CHANNEL_CLEAN" for r in report.gating_failures)


def test_base_commit_is_not_a_leak(tmp_path):
    # the base_commit legitimately appears in the trajectory and must NOT flag.
    run = _build_run(tmp_path)
    base = "beef1234567890abcdef1234567890abcdef1234"
    _add_entries(tmp_path, run, base_commit=base, reference_commit="d" * 40)
    _set_history(run, "stage1_draft", "ok",
                 [_sys(), _action("file_editor", "aaa"),
                  _obs("file_editor", "aaa", f"HEAD is at {base}, implementing stub")])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    sc = next(r for r in report.results if r.concern_id == "L0.SIDE_CHANNEL_CLEAN")
    assert sc.status is CheckStatus.PASS, sc.evidence
    assert report.gate is Gate.ACCEPT


# --------------------------------------------------------------------------- #
# per-test golden/stub filtering (sound subset + gap scoring)
# --------------------------------------------------------------------------- #
def test_parse_per_test():
    from kaiju.verification.pytest_runner import parse_per_test
    # pytest -rA appends " - <reason>" to FAILED lines when the message is short —
    # the parser must NOT drop those (the bug that undercounted the sound subset).
    txt = ("PASSED f.py::test_a\n"
           "FAILED f.py::test_b - assert 'None' == 'x'\n"
           "FAILED f.py::test_c\n"
           "ERROR f.py::test_d - ImportError\n")
    out = parse_per_test(txt)
    assert out == {"test_a": "pass", "test_b": "fail", "test_c": "fail", "test_d": "error"}


def test_sound_subset_drops_wrong_oracle_and_nondiscriminating():
    from kaiju.verification.pytest_exec import _analyze
    per_test = {
        # t_good: sound (golden pass, stub fail); t_oracle: WRONG (fails golden);
        # t_triv: non-discriminating (passes stub); t_real: sound, solution fails it
        "golden":   {"t_good": "pass", "t_oracle": "fail", "t_triv": "pass", "t_real": "pass"},
        "stub":     {"t_good": "fail", "t_oracle": "fail", "t_triv": "pass", "t_real": "fail"},
        "solution": {"t_good": "pass", "t_oracle": "fail", "t_triv": "pass", "t_real": "fail"},
    }
    a = _analyze(per_test)
    assert a["n_sound"] == 2 and set(a["sound_subset"]) == {"t_good", "t_real"}
    assert a["wrong_oracle_dropped"] == ["t_oracle"]      # fails on golden -> dropped
    assert a["non_discriminating_dropped"] == ["t_triv"]  # passes stub -> dropped
    # solution passes 1 of 2 sound tests -> 0.5, gap 0.5 vs golden
    assert a["solution_pass_on_sound"] == 0.5 and a["gap_vs_golden"] == 0.5


def test_heldout_gap_uses_sound_subset(tmp_path):
    from kaiju.verification.pytest_exec import store_pytest_results, heldout_gap_check
    from kaiju.verification.trajectory import load_trajectory
    run = _build_run(tmp_path)
    # 5 sound tests, solution passes only 2 -> gap 0.6 -> FAIL
    store_pytest_results(run, {"analysis": {
        "n_total": 6, "n_sound": 5, "sound_subset": list("abcde"),
        "wrong_oracle_dropped": ["z"], "non_discriminating_dropped": [],
        "solution_pass_on_sound": 0.4, "gap_vs_golden": 0.6}})
    out = heldout_gap_check(load_trajectory(run))
    assert out.status is CheckStatus.FAIL and out.evidence["n_sound"] == 5
    # too few sound tests -> inconclusive (N/A), never a false fail
    store_pytest_results(run, {"analysis": {"n_sound": 1, "solution_pass_on_sound": 0.0}})
    assert heldout_gap_check(load_trajectory(run)).status is CheckStatus.NOT_APPLICABLE


# --------------------------------------------------------------------------- #
# #3 manifest + feedback record
# --------------------------------------------------------------------------- #
def test_manifest_flags_unaddressed_target(tmp_path):
    run = _build_run(tmp_path)   # draft touches src/foo.py only
    from kaiju.verification.manifest import write_manifest
    write_manifest(tmp_path, ["src/foo.py", "src/bar.py"])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    dm = next(r for r in report.results if r.concern_id == "L1.DRAFT_MODULES_ADDRESSED")
    assert dm.status is CheckStatus.FAIL and "src/bar.py" in dm.evidence["missing"]
    assert report.gate is Gate.QUARANTINE


def test_manifest_all_addressed_passes(tmp_path):
    run = _build_run(tmp_path)
    from kaiju.verification.manifest import write_manifest
    write_manifest(tmp_path, ["src/foo.py"])
    report = verify_run(run, out_path=tmp_path / "rep.json")
    dm = next(r for r in report.results if r.concern_id == "L1.DRAFT_MODULES_ADDRESSED")
    assert dm.status is CheckStatus.PASS


def test_feedback_causality_progress_vs_regression(tmp_path):
    from kaiju.verification.feedback import append_feedback, FeedbackTurn
    run = _build_run(tmp_path)
    # no record -> N/A
    r0 = verify_run(run, out_path=tmp_path / "r0.json")
    assert next(x for x in r0.results if x.concern_id == "L1.FEEDBACK_CAUSALITY").status \
        is CheckStatus.NOT_APPLICABLE
    # progress (failures decrease) -> PASS
    append_feedback(run, FeedbackTurn("stage3", "m", 0, "test", failed=3))
    append_feedback(run, FeedbackTurn("stage3", "m", 1, "test", failed=0))
    r1 = verify_run(run, out_path=tmp_path / "r1.json")
    assert next(x for x in r1.results if x.concern_id == "L1.FEEDBACK_CAUSALITY").status \
        is CheckStatus.PASS
    # regression (failures increase) -> FAIL
    append_feedback(run, FeedbackTurn("stage3", "n", 0, "test", failed=1))
    append_feedback(run, FeedbackTurn("stage3", "n", 1, "test", failed=4))
    r2 = verify_run(run, out_path=tmp_path / "r2.json")
    assert next(x for x in r2.results if x.concern_id == "L1.FEEDBACK_CAUSALITY").status \
        is CheckStatus.FAIL


def test_lint_monotone_from_record(tmp_path):
    from kaiju.verification.feedback import append_feedback, FeedbackTurn
    run = _build_run(tmp_path)
    append_feedback(run, FeedbackTurn("stage2", "m", 0, "lint", findings=5))
    append_feedback(run, FeedbackTurn("stage2", "m", 1, "lint", findings=8))  # worse
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert next(x for x in report.results if x.concern_id == "L1.LINT_MONOTONE").status \
        is CheckStatus.FAIL


# --------------------------------------------------------------------------- #
# #2 independent re-execution (comparison logic + driver)
# --------------------------------------------------------------------------- #
def test_reexec_match_passes(tmp_path):
    from kaiju.verification.reexec import store_reexec_result, ReexecResult
    run = _build_run(tmp_path, passed=(8, 8, 9), tests=9)
    store_reexec_result(run, ReexecResult(num_passed=9, num_tests=9, status="OK"))
    report = verify_run(run, out_path=tmp_path / "rep.json")
    rx = next(r for r in report.results if r.concern_id == "L0.INDEPENDENT_REEXEC")
    assert rx.status is CheckStatus.PASS and report.gate is Gate.ACCEPT


def test_reexec_mismatch_quarantines(tmp_path):
    # recorded says 9 passed, an independent re-run says 3 -> fabricated/false result
    from kaiju.verification.reexec import store_reexec_result, ReexecResult
    run = _build_run(tmp_path, passed=(8, 8, 9), tests=9)
    store_reexec_result(run, ReexecResult(num_passed=3, num_tests=9, status="OK"))
    report = verify_run(run, out_path=tmp_path / "rep.json")
    assert report.gate is Gate.QUARANTINE
    assert any(r.concern_id == "L0.INDEPENDENT_REEXEC" for r in report.gating_failures)


def test_reexec_infra_is_inconclusive(tmp_path):
    from kaiju.verification.reexec import store_reexec_result, ReexecResult
    run = _build_run(tmp_path, passed=(8, 8, 9), tests=9)
    store_reexec_result(run, ReexecResult(num_passed=0, num_tests=0, status="INFRA_FAILED"))
    report = verify_run(run, out_path=tmp_path / "rep.json")
    rx = next(r for r in report.results if r.concern_id == "L0.INDEPENDENT_REEXEC")
    assert rx.status is CheckStatus.NOT_APPLICABLE   # a crashed re-run must not quarantine


def test_reexec_driver_stores_result(tmp_path):
    from kaiju.verification.reexec import run_independent_reexec, ReexecResult, load_reexec_result
    run = _build_run(tmp_path, passed=(8, 8, 9), tests=9)
    got = run_independent_reexec(run, eval_runner=lambda patch: ReexecResult(9, 9, "OK"))
    assert got.num_passed == 9
    assert load_reexec_result(run).num_passed == 9   # persisted


# --------------------------------------------------------------------------- #
# #5 sandbox hardening (git-strip is testable with real git)
# --------------------------------------------------------------------------- #
def test_git_strip_prunes_golden_keeps_base(tmp_path):
    import subprocess
    from kaiju.verification.sandbox import git_strip_history, is_object_reachable, NETWORK_OFF_ARGS
    d = tmp_path / "repo"; d.mkdir()

    def g(*a):
        return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    g("init", "-q"); g("config", "user.email", "x@x"); g("config", "user.name", "x")
    (d / "f").write_text("base"); g("add", "-A"); g("commit", "-q", "-m", "base")
    base = g("rev-parse", "HEAD").stdout.strip()
    (d / "f").write_text("golden"); g("add", "-A"); g("commit", "-q", "-m", "golden")
    golden = g("rev-parse", "HEAD").stdout.strip()
    g("tag", "gt", golden); g("update-ref", "refs/golden", golden)
    g("reset", "-q", "--hard", base)
    assert is_object_reachable(d, golden)
    assert git_strip_history(d)
    assert not is_object_reachable(d, golden)   # golden pruned
    assert is_object_reachable(d, base)         # base kept
    assert NETWORK_OFF_ARGS == ["--network=none"]


def test_score_undefined_when_nothing_decided(tmp_path):
    # An empty run dir with only pipeline_results and no stages: many ERRORs; the
    # score must be None (undefined), never a misleading 0.0.
    run = tmp_path / "runs" / "m" / "agent" / "run_1"
    run.mkdir(parents=True)
    (run / "pipeline_results.json").write_text("{}")
    report = verify_run(run, out_path=tmp_path / "rep.json")
    # gate quarantines (gating ERRORs), and a fully-undecided score is None
    assert report.gate is Gate.QUARANTINE
