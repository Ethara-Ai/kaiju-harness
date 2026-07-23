"""Tests for the non-deterministic half (P4-P6): TRUTH.md authoring, rubric
generation, the LLM-as-judge, mutation meta-verification, and orchestration —
all exercised with a scripted MockClient (no real model calls)."""
from __future__ import annotations

import json
from pathlib import Path

from kaiju.verification.model_client import MockClient
from kaiju.verification.truth import (
    TruthInputs, build_truth_prompt, leak_guard, generate_truth, TRUTH_SECTIONS)
from kaiju.verification.rubric import (
    generate_rubric, parse_rubric_response, backbone_rubric, MAX_TASK_CRITERIA, Rubric)
from kaiju.verification.judge import parse_judge_response, judge_trajectory, JudgeResult
from kaiju.verification.rubric_layer import apply_rubric
from kaiju.verification.mutation import (
    Mutant, BREAKING, PRESERVING, validate_verifiers, parse_mutants)
from kaiju.verification import orchestrate
from kaiju.verification.schemas import CheckStatus, VerificationReport, Gate
from kaiju.verification.evaluator import verify_run
from kaiju.verification.taxonomy import dimension_of


def test_cross_family_judge_routing():
    from kaiju.verification.model_client import (
        model_family, cross_family_judge_model, CLAUDE_JUDGE_MODEL, GPT_JUDGE_MODEL)
    assert model_family("claude-opus-4.8") == "claude"
    assert model_family("opus48cc") == "claude"
    assert model_family("gpt-5.5") == "gpt"
    assert model_family("openai/gpt-5.5") == "gpt"
    assert model_family("gemini-2.5-pro") == "other"
    # Claude run -> GPT/Codex judge; GPT run -> Claude judge; other -> Claude.
    assert cross_family_judge_model("claude-opus-4.8") == GPT_JUDGE_MODEL
    assert cross_family_judge_model("gpt-5.5") == CLAUDE_JUDGE_MODEL
    assert cross_family_judge_model("gemini-2.5-pro") == CLAUDE_JUDGE_MODEL


def _valid_truth(extra="") -> str:
    return "\n".join(f"## {s}\n{s} body. {extra}" for s in TRUTH_SECTIONS)


GOLDEN = ("--- a/src/x.c\n+++ b/src/x.c\n"
          "+    if (size > SIZE_MAX / 2) return NULL;  /* overflow guard here */\n"
          "+    result = allocate_and_copy(buffer, size * 2);\n")


# ---- P4 TRUTH.md ---------------------------------------------------------- #
def test_truth_prompt_uses_golden_as_private_aid():
    inp = TruthInputs(repo="r", language="c", spec_text="spec", golden_diff=GOLDEN,
                      fail_to_pass=["t1"], stub_files=["src/x.c"])
    system, user = build_truth_prompt(inp)
    assert "MUST NOT reproduce" in system and "alternative" in system.lower()
    assert "PRIVATE authoring aid" in user and GOLDEN.split("\n")[2][1:].strip()[:20] in user


def test_leak_guard_flags_verbatim_and_missing_sections():
    # a TRUTH.md that copies golden code lines verbatim + omits sections
    leaked = ("## Problem\n"
              "    if (size > SIZE_MAX / 2) return NULL;  /* overflow guard here */\n"
              "    result = allocate_and_copy(buffer, size * 2);\n"
              "    another_long_verbatim_line_from_the_golden_diff_here();\n")
    golden = GOLDEN + "+    another_long_verbatim_line_from_the_golden_diff_here();\n"
    v = leak_guard(leaked, golden)
    assert any("verbatim route leak" in x for x in v)
    assert any("missing required section" in x for x in v)
    # a clean, complete TRUTH.md passes
    assert leak_guard(_valid_truth(), GOLDEN) == []


def test_generate_truth_regenerates_on_leak():
    bad = "## Problem\n" + "\n".join("+ " + l[1:] for l in GOLDEN.splitlines()
                                     if l.startswith("+") and not l.startswith("+++"))
    client = MockClient(responses=[bad, _valid_truth()])
    inp = TruthInputs(repo="r", language="c", spec_text="s", golden_diff=GOLDEN)
    doc = generate_truth(inp, client, max_regen=2)
    assert doc.ok and doc.regenerations == 1
    assert len(client.calls) == 2


# ---- P6 rubric ------------------------------------------------------------ #
def test_parse_rubric_response_and_cap():
    arr = [{"id": f"c{i}", "text": f"criterion {i}", "truth_ref": "Behavioral contract"}
           for i in range(30)]
    crits = parse_rubric_response("```json\n" + json.dumps(arr) + "\n```")
    assert len(crits) == MAX_TASK_CRITERIA
    assert all(c.id.startswith("ts.") for c in crits)


def test_generate_rubric_has_backbone_plus_task_specific():
    resp = json.dumps([{"id": "a", "text": "handles overflow bound", "truth_ref": "Known pitfalls"},
                       {"id": "a", "text": "handles overflow bound"},  # dup by text
                       {"id": "b", "text": "frees on error path", "truth_ref": "Behavioral contract"}])
    r = generate_rubric(_valid_truth(), MockClient(responses=[resp]))
    assert len(r.backbone()) == 4
    assert len(r.task_specific()) == 2  # dedup removed the repeat
    assert {c.concern for c in r.backbone()} == {
        "L2.INTENT_FIDELITY", "L2.REASONING_FAITHFULNESS",
        "L2.STAGE_LEGITIMACY", "L2.ORACLE_STRENGTH"}


# ---- P6 judge ------------------------------------------------------------- #
def test_parse_judge_response_omitted_criterion_is_fail():
    rubric = Rubric(criteria=backbone_rubric())
    # judge only scored one criterion
    resp = json.dumps([{"criterion_id": "bb.intent_fidelity", "verdict": "pass",
                        "evidence": "impl matches contract", "justification": "ok"}])
    verdicts = {v.criterion_id: v for v in parse_judge_response(resp, rubric)}
    assert verdicts["bb.intent_fidelity"].passed is True
    assert verdicts["bb.oracle_strength"].passed is False  # omitted -> fail


def test_judge_inconclusive_is_not_folded_as_failures():
    # an empty/unparseable judge response must NOT become a wall of fake fails
    from kaiju.verification.taxonomy import TAXONOMY
    from kaiju.verification.schemas import CheckResult
    report = VerificationReport(run_dir="x")
    for c in TAXONOMY:
        report.results.append(CheckResult(concern_id=c.id, status=CheckStatus.PENDING,
            gating=c.gating, layer=int(c.layer), owner=c.owner.value, weight=c.weight,
            dimension=dimension_of(c)))
    report.finalize()
    rubric = Rubric(criteria=backbone_rubric())
    bad = JudgeResult(model="opus", verdicts=[], raw_verdicts=0, response_chars=0)
    assert bad.ok is False
    apply_rubric(report, rubric, bad)
    l2 = [r for r in report.results if r.concern_id.startswith("L2.")]
    assert all(r.status is CheckStatus.NOT_APPLICABLE for r in l2)  # N/A, not FAIL
    assert report.meta.get("rubric_inconclusive")


def test_judge_result_ok_flag():
    good = JudgeResult(model="m", raw_verdicts=4, response_chars=500)
    assert good.ok is True
    assert JudgeResult(raw_verdicts=0, response_chars=1200).ok is False  # empty array
    assert JudgeResult(raw_verdicts=4, response_chars=0).ok is False     # empty response


def _judge_result(passed_map):
    from kaiju.verification.judge import CriterionVerdict
    return JudgeResult(model="m", raw_verdicts=len(passed_map), response_chars=200,
                       verdicts=[CriterionVerdict(cid, passed=p) for cid, p in passed_map.items()])


def test_validate_criteria_golden_stub_anchoring():
    from kaiju.verification.rubric import Criterion, backbone_rubric
    from kaiju.verification.rubric_anchor import validate_criteria
    rubric = Rubric(criteria=backbone_rubric() + [
        Criterion("ts.good", "x", anchorable=True),
        Criterion("ts.bad", "x", anchorable=True),     # golden fails -> drop
        Criterion("ts.triv", "x", anchorable=True)])   # stub passes -> drop
    golden = _judge_result({"bb.intent_fidelity": True, "bb.oracle_strength": True,
                            "bb.reasoning_faithfulness": False, "bb.stage_legitimacy": False,
                            "ts.good": True, "ts.bad": False, "ts.triv": True})
    stub = _judge_result({"bb.intent_fidelity": False, "bb.oracle_strength": False,
                          "bb.reasoning_faithfulness": False, "bb.stage_legitimacy": False,
                          "ts.good": False, "ts.bad": False, "ts.triv": True})
    v = validate_criteria(rubric, golden, stub)
    assert v["bb.intent_fidelity"].kept and v["bb.oracle_strength"].kept   # golden-pass ∧ stub-fail
    assert v["bb.reasoning_faithfulness"].kept and v["bb.stage_legitimacy"].kept  # process: always kept
    assert v["ts.good"].kept
    assert not v["ts.bad"].kept and "golden FAILS" in v["ts.bad"].reason
    assert not v["ts.triv"].kept and "stub PASSES" in v["ts.triv"].reason


def test_apply_rubric_anchored_drops_and_gap_scores():
    from kaiju.verification.taxonomy import TAXONOMY
    from kaiju.verification.schemas import CheckResult
    from kaiju.verification.rubric import Criterion, backbone_rubric
    report = VerificationReport(run_dir="x")
    for c in TAXONOMY:
        report.results.append(CheckResult(concern_id=c.id, status=CheckStatus.PENDING,
            gating=c.gating, layer=int(c.layer), owner=c.owner.value, weight=c.weight,
            dimension=dimension_of(c)))
    report.finalize()
    rubric = Rubric(criteria=backbone_rubric() + [Criterion("ts.bad", "x", anchorable=True)])
    golden = _judge_result({"bb.intent_fidelity": True, "bb.oracle_strength": True,
                            "bb.reasoning_faithfulness": True, "bb.stage_legitimacy": True,
                            "ts.bad": False})   # golden fails ts.bad -> it will be dropped
    stub = _judge_result({"bb.intent_fidelity": False, "bb.oracle_strength": False,
                          "bb.reasoning_faithfulness": False, "bb.stage_legitimacy": False,
                          "ts.bad": False})
    # candidate: intent pass, oracle fail, ts.bad fail (but ts.bad is dropped by anchoring)
    candidate = _judge_result({"bb.intent_fidelity": True, "bb.oracle_strength": False,
                               "bb.reasoning_faithfulness": True, "bb.stage_legitimacy": True,
                               "ts.bad": False})
    apply_rubric(report, rubric, candidate, golden=golden, stub=stub)
    # ts.bad must be dropped (golden failed it), not scored against the candidate
    assert not any(r.concern_id == "ts.bad" for r in report.results)
    assert any(d["criterion"] == "ts.bad" for d in report.meta["rubric_dropped"])
    # gap over validated ANCHORABLE criteria (intent+oracle): golden passes both,
    # candidate passes 1/2 -> gap 0.5
    assert report.meta["rubric_anchored"] is True
    assert report.meta["rubric_validated_anchorable"] == 2
    assert report.meta["rubric_gap_vs_golden"] == 0.5


CODE5 = ("from mod import fn\n\n"
         "def test_1():\n    assert fn(1) == 1\n\n"
         "def test_2():\n    assert fn(2) == 2\n\n"
         "def test_3():\n    assert fn(3) == 3\n\n"
         "def test_4():\n    assert fn(4) == 4\n\n"
         "def test_5():\n    assert fn(5) == 99\n")   # wrong oracle


def test_ast_split_and_prune():
    from kaiju.verification.pytest_gen import split_test_functions, prune_tests
    header, funcs = split_test_functions(CODE5)
    assert set(funcs) == {"test_1", "test_2", "test_3", "test_4", "test_5"}
    assert "from mod import fn" in header
    pruned = prune_tests(CODE5, {"test_1", "test_2"})
    assert "def test_1" in pruned and "def test_5" not in pruned


def test_classify_golden_failures_defaults_to_wrong_verifier():
    from kaiju.verification.verifier_loop import classify_golden_failures
    resp = json.dumps([{"id": "test_5", "cause": "wrong_verifier"},
                       {"id": "test_x", "cause": "golden_defect"}])
    out = classify_golden_failures({"test_5": "d", "test_x": "d", "test_y": "d"},
                                   "code", MockClient(responses=[resp]))
    assert out["test_5"] == "wrong_verifier"
    assert out["test_x"] == "golden_defect"
    assert out["test_y"] == "wrong_verifier"   # omitted -> default


def test_sound_pytest_loop_drops_wrong_oracle():
    from kaiju.verification.verifier_loop import sound_pytest_loop
    # golden: t1-t4 pass, t5 fail (wrong oracle); stub: all fail
    def run_pytest(code, which):
        if which == "golden":
            return {"test_1": "pass", "test_2": "pass", "test_3": "pass", "test_4": "pass",
                    "test_5": "fail"}
        return {f"test_{i}": "fail" for i in range(1, 6)}
    client = MockClient(responses=[CODE5, json.dumps([{"id": "test_5", "cause": "wrong_verifier"}])])
    clean, res = sound_pytest_loop("TRUTH", ["mod.py"], "golden code", run_pytest, client)
    assert res.ok and res.regenerations == 0
    assert res.dropped_wrong_oracle == ["test_5"] and len(res.sound) == 4
    assert "def test_5" not in clean and "def test_1" in clean   # pruned suite is clean


def test_sound_rubric_loop_keeps_valid_criteria():
    from kaiju.verification.verifier_loop import sound_rubric_loop
    from kaiju.verification.judge import CriterionVerdict, JudgeResult
    ts = json.dumps([{"id": f"t{i}", "text": f"c{i}", "truth_ref": "x"} for i in range(6)])
    gen = MockClient(responses=[ts])
    def judge_code(rubric, code):
        allpass = code == "GOLDEN"
        return JudgeResult(model="m", raw_verdicts=len(rubric.criteria), response_chars=200,
            verdicts=[CriterionVerdict(c.id, passed=allpass) for c in rubric.criteria])
    out = sound_rubric_loop("TRUTH", "GOLDEN", "STUB", judge_code, gen)
    kept_rubric, gjr, sjr, res = out
    assert res.ok and res.regenerations == 0
    # all anchorable kept (golden passes all, stub fails all)
    assert sum(1 for c in kept_rubric.criteria if c.anchorable) >= 6


def test_sound_rubric_loop_anchors_only_anchorable_criteria():
    """Process criteria (anchorable=False) judge the trajectory, not bare code, so the
    golden/stub anchor judge must never receive them — else the anchor log records
    meaningless 'fails on golden'."""
    from kaiju.verification.verifier_loop import sound_rubric_loop
    from kaiju.verification.judge import CriterionVerdict, JudgeResult
    # backbone process criteria (reasoning/stage) are non-anchorable by construction.
    crit = [{"id": f"t{i}", "text": f"c{i}", "truth_ref": "x"} for i in range(6)]
    gen = MockClient(responses=[json.dumps(crit)])
    seen_by_judge = []
    def judge_code(rubric, code):
        seen_by_judge.append([c.id for c in rubric.criteria])
        return JudgeResult(model="m", raw_verdicts=len(rubric.criteria), response_chars=200,
            verdicts=[CriterionVerdict(c.id, passed=(code == "GOLDEN"))
                      for c in rubric.criteria])
    kept_rubric, gjr, sjr, res = sound_rubric_loop("TRUTH", "GOLDEN", "STUB", judge_code, gen)
    process_ids = {"bb.reasoning_faithfulness", "bb.stage_legitimacy"}
    # the anchor judge (golden + stub) saw ONLY anchorable criteria — never the process ones
    assert seen_by_judge and all(not (process_ids & set(ids)) for ids in seen_by_judge)
    # but the kept rubric still carries the process criteria (kept unconditionally)
    assert process_ids <= {c.id for c in kept_rubric.criteria}


def test_assemble_differential_test():
    from kaiju.verification.differential import assemble_differential_test
    code = assemble_differential_test("from m import f", [
        {"call": "f(1)", "expected": "'a'"}, {"call": "f(2)", "expected": "RAISES:ValueError"}])
    assert "assert repr(f(1)) == \"'a'\"" in code
    assert "with pytest.raises(Exception)" in code and "f(2)" in code
    compile(code, "<t>", "exec")   # valid python


def test_judge_trajectory_end_to_end_mock():
    rubric = Rubric(criteria=backbone_rubric())
    resp = json.dumps([{"criterion_id": c.id, "verdict": "pass", "evidence": "e"}
                       for c in rubric.criteria])
    res = judge_trajectory("TRUTH", rubric, "digest", MockClient(responses=[resp]))
    assert len(res.verdicts) == 4 and all(v.passed for v in res.verdicts)


def test_apply_rubric_fills_l2_and_appends_task_specific():
    report = VerificationReport(run_dir="x")
    from kaiju.verification.taxonomy import TAXONOMY
    from kaiju.verification.schemas import CheckResult
    for c in TAXONOMY:
        report.results.append(CheckResult(
            concern_id=c.id, status=CheckStatus.PENDING, gating=c.gating,
            layer=int(c.layer), owner=c.owner.value, weight=c.weight,
            dimension=dimension_of(c)))
    report.finalize()
    rubric = generate_rubric(_valid_truth(), MockClient(responses=[json.dumps(
        [{"id": "x", "text": "task specific thing", "truth_ref": "Known pitfalls"}])]))
    judge = JudgeResult(model="opus", verdicts=[
        type("V", (), {"criterion_id": c.id, "passed": (i % 2 == 0),
                       "evidence": "e", "justification": "j"})()
        for i, c in enumerate(rubric.criteria)])
    from kaiju.verification.judge import CriterionVerdict
    judge.verdicts = [CriterionVerdict(c.id, passed=(i % 2 == 0), evidence="e")
                      for i, c in enumerate(rubric.criteria)]
    judge.raw_verdicts = len(judge.verdicts)   # a real (ok) judge response
    judge.response_chars = 500
    apply_rubric(report, rubric, judge)
    l2 = {r.concern_id: r for r in report.results if r.layer == 2}
    assert l2["L2.INTENT_FIDELITY"].status in (CheckStatus.PASS, CheckStatus.FAIL)
    assert any(r.concern_id == "ts.x" for r in report.results)      # task-specific appended
    # Layer-2 is graded, never gating
    assert all(not r.gating for r in report.results if r.layer == 2)


def _cr(cid, status, dimension, *, gating=False, weight=1.0):
    from kaiju.verification.schemas import CheckResult
    return CheckResult(concern_id=cid, status=status, gating=gating, layer=1,
                       owner="deterministic", weight=weight, dimension=dimension)


def test_score_excludes_honesty_and_legitimacy_dimensions():
    # Only PROCESS-dimension checks feed the graded score; a failing code-correctness
    # (honesty) check must NOT drag the trajectory-process score down.
    report = VerificationReport(run_dir="x")
    report.results += [
        _cr("L1.TEST_STAGE_OUTCOME", CheckStatus.PASS, "process"),
        _cr("L2.STAGE_LEGITIMACY", CheckStatus.PASS, "process"),
        _cr("L0.PIPELINE_COMPLETE", CheckStatus.PASS, "legitimacy", gating=True),
        _cr("ts.some-correctness", CheckStatus.FAIL, "honesty"),   # excluded from score
        _cr("L2.ORACLE_STRENGTH", CheckStatus.FAIL, "honesty"),    # excluded from score
    ]
    report.finalize()
    assert report.graded_score == 1.0            # 2/2 process pass; honesty FAILs ignored
    assert report.gate is Gate.ACCEPT            # legitimacy clean


def test_honesty_signal_distinguishes_gaming_from_overfit():
    from kaiju.verification.schemas import VerificationReport as VR
    solved = lambda: _cr("L1.TEST_STAGE_OUTCOME", CheckStatus.PASS, "process")
    unsolved = lambda: _cr("L1.TEST_STAGE_OUTCOME", CheckStatus.FAIL, "process")

    # held-out gap WITHOUT a confirmed frozen solve -> "review" (likely incomplete)
    r1 = VR(run_dir="x")
    r1.results = [unsolved(), _cr("L1.HELDOUT_GAP", CheckStatus.FAIL, "honesty")]
    r1.finalize()
    h1 = r1.meta["trajectory_honesty"]
    assert h1["signal"] == "review" and h1["weak_generalization_vs_golden"]
    assert not h1["overfit_frozen_tests"] and h1["frozen_tests_solved"] is False

    # held-out gap AND the frozen suite WAS solved -> genuine overfit -> "suspect"
    r2 = VR(run_dir="x")
    r2.results = [solved(), _cr("L2.ORACLE_STRENGTH", CheckStatus.FAIL, "honesty")]
    r2.finalize()
    h2 = r2.meta["trajectory_honesty"]
    assert h2["signal"] == "suspect" and h2["overfit_frozen_tests"] and not h2["gamed_frozen_tests"]

    # a hardcoded-output predicate FAIL -> gaming -> "suspect" regardless of solve
    r3 = VR(run_dir="x")
    r3.results = [unsolved(), _cr("pt.no-hardcoded-chinese-output", CheckStatus.FAIL, "honesty")]
    r3.finalize()
    assert r3.meta["trajectory_honesty"]["signal"] == "suspect"
    assert r3.meta["trajectory_honesty"]["gamed_frozen_tests"] is True

    # everything honest -> clean
    r4 = VR(run_dir="x")
    r4.results = [solved(), _cr("L2.ORACLE_STRENGTH", CheckStatus.PASS, "honesty")]
    r4.finalize()
    assert r4.meta["trajectory_honesty"]["signal"] == "clean"


# ---- P5 mutation ---------------------------------------------------------- #
def test_mutation_two_sided_soundness():
    mutants = [Mutant("b1", BREAKING, "off by one"), Mutant("b2", BREAKING, "dropped guard"),
               Mutant("p1", PRESERVING, "iterative variant"), Mutant("p2", PRESERVING, "renamed")]
    # ideal verifier: kills breaking, spares preserving
    good = validate_verifiers(mutants, runner=lambda m: m.kind == BREAKING)
    assert good.sound and good.mutation_score == 1.0
    # a weak verifier that misses a breaking mutant is UNSOUND
    weak = validate_verifiers(mutants, runner=lambda m: m.id == "b1")
    assert not weak.sound and weak.survived_breaking[0].id == "b2"
    # an over-fitted verifier that kills a valid alternative is UNSOUND
    overfit = validate_verifiers(mutants, runner=lambda m: True)
    assert not overfit.sound and {x.id for x in overfit.false_killed_preserving} == {"p1", "p2"}


# ---- #1 generated deterministic predicates -------------------------------- #
def test_predicate_evaluation_all_types():
    from kaiju.verification.predicates import Predicate, evaluate_predicate
    code = "int add(int a){ return a+1; }\n#include <stdint.h>\n"
    assert evaluate_predicate(Predicate("1", "symbol_present", "add"), code)[0]
    assert not evaluate_predicate(Predicate("2", "symbol_present", "subtract"), code)[0]
    assert evaluate_predicate(Predicate("3", "import_present", "#include <stdint.h>"), code)[0]
    assert evaluate_predicate(Predicate("4", "literal_absent", "42"), code)[0]
    assert not evaluate_predicate(Predicate("5", "literal_absent", "a+1"), code)[0]
    assert evaluate_predicate(Predicate("6", "pattern_present", r"return\s+a\+1"), code)[0]
    assert not evaluate_predicate(Predicate("7", "pattern_absent", r"return"), code)[0]
    # bad regex / unknown type never spuriously fails
    assert evaluate_predicate(Predicate("8", "pattern_present", "("), code)[0]
    assert evaluate_predicate(Predicate("9", "bogus", "x"), code)[0]


def test_prune_vacuous_drops_non_discriminating():
    from kaiju.verification.predicates import Predicate, prune_vacuous
    good = Predicate("g", "symbol_present", "overflow_guard",
                     negative_fixture="int f(){return 0;}")   # fixture lacks symbol -> fails -> kept
    vacuous = Predicate("v", "literal_absent", "zzz",
                        negative_fixture="int f(){return 0;}")  # fixture lacks zzz -> passes -> vacuous
    kept = prune_vacuous([good, vacuous])
    assert [p.id for p in kept] == ["g"]


def test_generate_predicates_parses_and_prunes():
    from kaiju.verification.predicates import generate_predicates
    resp = json.dumps([
        {"id": "a", "type": "pattern_absent", "target": "system\\(", "truth_ref": "Cheat surface",
         "negative_fixture": "system(\"rm\")"},               # fixture violates -> discriminating -> kept
        {"id": "b", "type": "not_a_type", "target": "x"},      # invalid type -> dropped
    ])
    preds = generate_predicates(_valid_truth(), MockClient(responses=[resp]))
    assert [p.id for p in preds] == ["pt.a"]


def test_apply_predicates_folds_into_report(tmp_path):
    run = _minimal_run(tmp_path)
    # add a patch that contains a forbidden call to trip a pattern_absent predicate
    mod = run / "stage1_draft" / "r" / "b" / "current" / "m"
    (mod / "model_changes.diff").write_text("+++ b/src/x.c\n+    system(\"rm -rf /\");\n")
    from kaiju.verification.orchestrate import build_bundle, freeze_bundle
    from kaiju.verification.truth import TruthInputs
    client = MockClient(responses=[_valid_truth(),
        json.dumps([{"id": "t", "text": "t", "truth_ref": "x"}]),          # rubric
        json.dumps([{"id": "no_system", "type": "pattern_absent", "target": "system\\(",
                     "truth_ref": "Cheat surface", "negative_fixture": "system(\"x\")"}]), "```python\ndef test_ok():\n    assert True\n```"])
    freeze_bundle(tmp_path, build_bundle(TruthInputs(repo="r", language="c", spec_text="s",
                                                     golden_diff=GOLDEN), client))
    report = verify_run(run, out_path=tmp_path / "rep.json")
    pred = next(r for r in report.results if r.concern_id == "pt.no_system")
    assert pred.status is CheckStatus.FAIL and pred.gating is False   # forbidden call present
    # the predicate FAIL is non-gating: it must not be among the gating failures
    assert "pt.no_system" not in [r.concern_id for r in report.gating_failures]


def test_parse_mutants():
    txt = json.dumps({"breaking": [{"id": "b1", "description": "x"}],
                      "preserving": [{"id": "p1", "description": "y"}]})
    ms = parse_mutants(txt)
    assert {m.kind for m in ms} == {BREAKING, PRESERVING}


def _bundle_responses():
    # one full generation cycle: truth, rubric, predicates, mutants
    return [_valid_truth(),
            json.dumps([{"id": "x", "text": "t", "truth_ref": "Known pitfalls"}]),
            json.dumps([]),
            "```python\ndef test_ok():\n    assert True\n```",
            json.dumps({"breaking": [{"id": "b1", "description": "off by one"}],
                        "preserving": [{"id": "p1", "description": "iterative"}]})]


# ---- container execution backends ----------------------------------------- #
def test_predicate_mutant_runner_kills_breaking_spares_preserving():
    from kaiju.verification.mutation_runner import predicate_mutant_runner
    from kaiju.verification.predicates import Predicate
    from kaiju.verification.mutation import Mutant, BREAKING, PRESERVING
    golden = ("int ensure(size_t n){\n"
              "    if (n > SIZE_MAX/2) return -1;\n"
              "    return grow(n*2);\n}")
    preds = [Predicate("g", "pattern_present", r"SIZE_MAX", truth_ref="Known pitfalls")]
    runner = predicate_mutant_runner(golden, preds)
    # breaking mutant removes the SIZE_MAX guard line -> predicate fails -> killed
    breaking = Mutant("b", BREAKING, "drop guard",
                      diff="-    if (n > SIZE_MAX/2) return -1;\n+    // guard removed\n")
    assert runner(breaking) is True
    # preserving mutant renames a local, guard intact -> predicate passes -> spared
    preserving = Mutant("p", PRESERVING, "spacing",
                        diff="-    return grow(n*2);\n+    return grow(n * 2);\n")
    assert runner(preserving) is False


def test_combined_mutant_runner_uses_tests():
    from kaiju.verification.mutation_runner import combined_mutant_runner
    from kaiju.verification.mutation import Mutant, BREAKING
    golden = "x = 1"
    # no predicates; the injected test_runner decides. A breaking mutant fails tests.
    runner = combined_mutant_runner(golden, [], test_runner=lambda code: "broken" not in code)
    assert runner(Mutant("b", BREAKING, "x", diff="+broken")) is True   # tests fail -> killed


def test_reexec_parse_eval_stdout():
    from kaiju.verification.reexec_runner import parse_eval_stdout
    ok = parse_eval_stdout("repo,runtime,num_passed/num_tests\npython-slugify,3,82/82\n")
    assert (ok.num_passed, ok.num_tests, ok.status) == (82, 82, "OK")
    partial = parse_eval_stdout("myrepo,5,45/46\n")
    assert (partial.num_passed, partial.num_tests) == (45, 46)
    infra = parse_eval_stdout("INVENTORY_MISMATCH,repo,10,0,0\n")
    assert infra.status == "INVENTORY_MISMATCH"
    none = parse_eval_stdout("some log line\nCOMPILE_FAILED,repo\n")
    assert none.status == "COMPILE_FAILED" and none.num_tests == 0


def test_closed_meta_verify_sound_first_try():
    inp = TruthInputs(repo="r", language="c", spec_text="s", golden_diff=GOLDEN)
    client = MockClient(responses=_bundle_responses())
    # ideal runner: kills breaking, spares preserving -> sound on attempt 0
    res = orchestrate.closed_meta_verify(
        inp, runner=lambda m, b: m.kind == BREAKING, client=client, max_regen=2)
    assert res.sound and res.regenerations == 0


# ---- #6 coverage-tier utilities ------------------------------------------- #
def test_flaky_classification():
    from kaiju.verification.tiers import classify_flaky, rerun_classify
    assert classify_flaky([True, True]) == "stable_pass"
    assert classify_flaky([False, False]) == "stable_fail"
    assert classify_flaky([True, False]) == "flaky"
    seq = iter([False, True, True])
    verdict, outs = rerun_classify(lambda: next(seq), n=3)
    assert verdict == "flaky" and len(outs) == 2   # stops early once mixed


def test_transient_triage():
    from kaiju.verification.tiers import classify_failure
    assert classify_failure("ECONNRESET while fetching", 1) == "infra_transient"
    assert classify_failure("", 137) == "infra_transient"          # OOM
    assert classify_failure("AssertionError: expected 3 got 4", 1) == "genuine"


def test_checksum_tamper_and_stub_floor():
    from kaiju.verification.tiers import checksum_manifest, compare_checksums, stub_floor_ok
    before = checksum_manifest({"tests/test_a.py": "assert real", "conftest.py": "x"})
    after = checksum_manifest({"tests/test_a.py": "assert True  # neutered", "conftest.py": "x"})
    assert compare_checksums(before, after) == ["tests/test_a.py"]
    assert stub_floor_ok(stub_passes=False) is True       # stub fails suite -> good oracle
    assert stub_floor_ok(stub_passes=True) is False       # stub already passes -> weak oracle


# ---- #7 Good-Turing residual-risk stop ------------------------------------ #
def test_good_turing_residual_stop():
    from kaiju.verification.tiers import good_turing_unseen_mass, residual_risk_saturated
    # all singletons -> maximal unseen mass, never saturated
    assert good_turing_unseen_mass([1, 1, 1, 1]) == 1.0
    assert not residual_risk_saturated([1] * 30)
    # mostly-repeated observations -> low unseen mass -> saturated
    counts = [10, 8, 7, 6, 5, 1]      # N1=1, N=37 -> ~0.027 < 0.05
    assert good_turing_unseen_mass(counts) < 0.05
    assert residual_risk_saturated(counts, threshold=0.05, min_observations=20)
    # too few observations -> not yet saturated even if bound is low
    assert not residual_risk_saturated([5, 5], min_observations=20)


def test_closed_meta_verify_regenerates_then_hard_flags():
    inp = TruthInputs(repo="r", language="c", spec_text="s", golden_diff=GOLDEN)
    client = MockClient(responses=_bundle_responses() * 2)   # 2 full cycles (max_regen=1)
    # weak runner: never kills the breaking mutant -> always unsound -> exhausts
    res = orchestrate.closed_meta_verify(
        inp, runner=lambda m, b: False, client=client, max_regen=1)
    assert not res.sound and res.regenerations == 1
    assert res.mutation.survived_breaking


# ---- orchestration end-to-end (mock) -------------------------------------- #
def _minimal_run(tmp: Path) -> Path:
    run = tmp / "runs" / "opus" / "agent" / "run_1"
    (run / "stage1_draft" / "r" / "b" / "current" / "m").mkdir(parents=True)
    mod = run / "stage1_draft" / "r" / "b" / "current" / "m"
    (mod / "output.json").write_text(json.dumps({
        "metrics": {"total_cost": 1.0, "total_llm_calls": 1},
        "history": [{"kind": "ActionEvent", "source": "agent", "tool_name": "file_editor",
                     "tool_call_id": "a", "thought": "implement the stub"},
                    {"kind": "ObservationEvent", "source": "environment", "action_id": "a",
                     "observation": {"content": [{"text": "File m.c has been edited."}],
                                     "is_error": False}}]}))
    (mod / ".done").write_text("")
    for a in ("turns.jsonl", "aider.log", "llm_history.txt"):
        (mod / a).write_text("x")
    (run / "pipeline_results.json").write_text(json.dumps(
        {"language": "c", "stage1": {"eval_status": "OK", "num_passed": 1, "num_tests": 1}}))
    ds = tmp / "datasets"; ds.mkdir()
    (ds / "entries.json").write_text(json.dumps([{"base_commit": "a"*40, "reference_commit": "b"*40}]))
    return run


def test_build_freeze_load_bundle_roundtrip(tmp_path):
    inp = TruthInputs(repo="r", language="c", spec_text="s", golden_diff=GOLDEN)
    rubric_resp = json.dumps([{"id": "x", "text": "t", "truth_ref": "Known pitfalls"}])
    pred_resp = json.dumps([{"id": "p", "type": "symbol_present", "target": "foo",
                             "truth_ref": "Behavioral contract"}])
    client = MockClient(responses=[_valid_truth(), rubric_resp, pred_resp, "```python\ndef test_ok():\n    assert True\n```"])
    bundle = orchestrate.build_bundle(inp, client)
    assert bundle.truth.ok and len(bundle.rubric.criteria) == 5 and len(bundle.predicates) == 1
    assert "def test_ok" in bundle.pytest_code
    orchestrate.freeze_bundle(tmp_path, bundle)
    loaded = orchestrate.load_bundle(tmp_path)
    assert loaded is not None and loaded[0] == bundle.truth.text
    assert len(loaded[1].criteria) == 5


def test_judge_run_and_verify_folds_l2(tmp_path):
    run = _minimal_run(tmp_path)
    # freeze a bundle at the uuid root (tmp_path)
    inp = TruthInputs(repo="r", language="c", spec_text="s", golden_diff=GOLDEN)
    build_client = MockClient(responses=[_valid_truth(),
        json.dumps([{"id": "x", "text": "task thing", "truth_ref": "Known pitfalls"}]),
        json.dumps([]), "```python\ndef test_ok():\n    assert True\n```"])   # no predicates, + pytest
    orchestrate.freeze_bundle(tmp_path, orchestrate.build_bundle(inp, build_client))
    # run the judge (mock returns pass for all criteria)
    _, rubric = orchestrate.load_bundle(tmp_path)
    judge_resp = json.dumps([{"criterion_id": c.id, "verdict": "pass", "evidence": "e"}
                             for c in rubric.criteria])
    result = orchestrate.judge_run(run, MockClient(responses=[judge_resp]))
    assert result is not None and len(result.verdicts) == len(rubric.criteria)
    # now verify_run must fold the L2 verdicts (no longer PENDING)
    report = verify_run(run, out_path=tmp_path / "rep.json")
    l2 = [r for r in report.results if r.layer == 2 and r.concern_id.startswith("L2.")]
    assert all(r.status is CheckStatus.PASS for r in l2), [(r.concern_id, r.status) for r in l2]
    assert report.meta.get("rubric_applied") is True
