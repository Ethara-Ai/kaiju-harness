"""Closed generate → validate → LLM-verify → prune / regenerate loop that yields a
SOUND + COMPLETE verifier set before anything is frozen.

Soundness gate (both verifier types): every item must PASS on the golden AND FAIL
on the stub. Items failing that gate are LLM-classified — a *wrong verifier*
(wrong-oracle test / too-strict criterion → drop) vs a genuine *golden defect*
(flag, don't silently drop). Sound items are kept; if too few survive to be
COMPLETE, the set is regenerated with the specific failures fed back, bounded. If a
sound+complete set can't be produced, the task is hard-flagged (never proceed with
an unsound/incomplete verifier set).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .model_client import ModelClient

MIN_SOUND_TESTS = 4
MIN_SOUND_CRITERIA = 6


@dataclass
class LoopResult:
    ok: bool
    regenerations: int
    sound: list = field(default_factory=list)
    dropped_wrong_oracle: list = field(default_factory=list)
    dropped_non_discriminating: list = field(default_factory=list)
    golden_defects: list = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "regenerations": self.regenerations,
                "sound": self.sound, "dropped_wrong_oracle": self.dropped_wrong_oracle,
                "dropped_non_discriminating": self.dropped_non_discriminating,
                "golden_defects": self.golden_defects, "detail": self.detail}


# --------------------------------------------------------------------------- #
# LLM-verify: is a golden failure a WRONG VERIFIER or a real GOLDEN DEFECT?
# --------------------------------------------------------------------------- #
_VERIFY_SYSTEM = (
    "You audit VERIFIERS. Each verifier (a test assertion or a rubric criterion) FAILED on "
    "the KNOWN-CORRECT (golden) solution. For each, decide the cause:\n"
    "- 'wrong_verifier': the verifier itself is wrong (a test asserting an incorrect expected "
    "value, or a criterion that is too strict / out of scope / mis-specified). The golden is "
    "correct; the verifier should be DROPPED.\n"
    "- 'golden_defect': the golden solution genuinely violates a real, spec-mandated behavior "
    "the verifier correctly checks (rare). The golden is at fault.\n"
    "Default to 'wrong_verifier' unless the golden clearly violates the spec.\n"
    'Return ONLY JSON: [{"id": "<verifier id>", "cause": "wrong_verifier"|"golden_defect", '
    '"why": "<one sentence>"}].'
)


def classify_golden_failures(items: dict[str, str], golden_code: str,
                             client: ModelClient) -> dict[str, str]:
    """items: {id: description-of-the-failing-verifier-and-its-failure}. Returns
    {id: 'wrong_verifier'|'golden_defect'}. Missing ids default to wrong_verifier."""
    if not items:
        return {}
    listing = "\n".join(f"### {i}\n{desc}" for i, desc in items.items())
    user = (f"# Golden solution code\n```\n{golden_code[:16000]}\n```\n\n"
            f"# Verifiers that failed on the golden\n{listing}\n\nClassify each. Return the JSON.")
    try:
        text = client.complete(_VERIFY_SYSTEM, user, max_tokens=4096).text
        arr = json.loads(re.search(r"\[.*\]", text, re.DOTALL).group(0))
    except Exception:
        arr = []
    out = {i: "wrong_verifier" for i in items}
    for d in arr:
        if isinstance(d, dict) and d.get("id") in out:
            out[d["id"]] = "golden_defect" if d.get("cause") == "golden_defect" else "wrong_verifier"
    return out


# --------------------------------------------------------------------------- #
# Sound-PYTEST loop: keep only tests that pass golden ∧ fail stub; regen if too few.
#   run_pytest(code, "golden"|"stub") -> {test_name: 'pass'|'fail'|'error'|'skip'}
# --------------------------------------------------------------------------- #
def sound_pytest_loop(truth_md: str, stub_files, golden_code: str, run_pytest,
                      client: ModelClient, *, max_regen: int = 2) -> tuple[str, LoopResult]:
    from .pytest_gen import generate_pytest, prune_tests
    feedback = ""
    best: tuple[str, LoopResult] = ("", LoopResult(ok=False, regenerations=0))
    for attempt in range(max_regen + 1):
        code = generate_pytest(truth_md, client, stub_files=stub_files, feedback=feedback)
        golden_pt = run_pytest(code, "golden") or {}
        stub_pt = run_pytest(code, "stub") or {}
        names = set(golden_pt) | set(stub_pt)
        fail_golden = sorted(n for n in names if golden_pt.get(n) != "pass")
        pass_stub = sorted(n for n in names if stub_pt.get(n) == "pass")
        sound = {n for n in names if golden_pt.get(n) == "pass"
                 and stub_pt.get(n) in ("fail", "error")}
        cls = classify_golden_failures(
            {n: f"pytest test `{n}` failed on the correct golden solution" for n in fail_golden},
            golden_code, client) if fail_golden else {}
        defects = [n for n, c in cls.items() if c == "golden_defect"]
        clean = prune_tests(code, sound)
        res = LoopResult(ok=len(sound) >= MIN_SOUND_TESTS, regenerations=attempt,
                         sound=sorted(sound), dropped_wrong_oracle=fail_golden,
                         dropped_non_discriminating=pass_stub, golden_defects=defects)
        if res.ok:
            return clean, res
        best = (clean, res)
        feedback = (f"{len(fail_golden)} test(s) FAILED on the correct solution (wrong expected "
                    f"values: {fail_golden[:6]}); {len(pass_stub)} PASSED on the empty stub "
                    f"(non-discriminating: {pass_stub[:6]}). Write tests whose expected values a "
                    "CORRECT implementation produces and an unimplemented stub does NOT.")
    best[1].detail = f"could not reach {MIN_SOUND_TESTS} sound tests in {max_regen + 1} attempts"
    return best


# --------------------------------------------------------------------------- #
# Sound-RUBRIC loop: keep only criteria golden passes ∧ stub fails; regen if too few.
#   judge_code(rubric, code) -> JudgeResult   (judges a bare solution's code)
# --------------------------------------------------------------------------- #
def sound_rubric_loop(truth_md: str, golden_code: str, stub_code: str, judge_code,
                      client: ModelClient, *, max_regen: int = 2):
    from .rubric import generate_rubric, Rubric
    from .rubric_anchor import validate_criteria
    feedback = ""
    best = None
    for attempt in range(max_regen + 1):
        rubric = generate_rubric(truth_md, client, feedback=feedback)
        # Anchor ONLY the anchorable (code-and-behavior) criteria. Process criteria
        # (reasoning faithfulness, stage legitimacy) judge the TRAJECTORY, which bare
        # golden/stub code has none of — judging them here just records meaningless
        # "fails on golden", so they're excluded from the anchor judgement entirely.
        anchor_rubric = Rubric(criteria=[c for c in rubric.criteria if c.anchorable])
        golden_jr = judge_code(anchor_rubric, golden_code)
        stub_jr = judge_code(anchor_rubric, stub_code)
        validation = validate_criteria(rubric, golden_jr, stub_jr)
        gv = golden_jr.by_id()
        anchorable = [c for c in rubric.criteria if c.anchorable]
        fail_golden = [c for c in anchorable if c.id in gv and not gv[c.id].passed]
        cls = classify_golden_failures(
            {c.id: f"criterion `{c.text}` failed on golden: "
                   f"{(gv[c.id].justification or '')}" for c in fail_golden},
            golden_code, client) if fail_golden else {}
        defects = [cid for cid, c in cls.items() if c == "golden_defect"]
        kept = [c for c in rubric.criteria if validation[c.id].kept]
        kept_rubric = Rubric(criteria=kept)
        n_anchorable = sum(1 for c in kept if c.anchorable)
        dropped = [cid for cid, v in validation.items() if not v.kept]
        res = LoopResult(ok=n_anchorable >= MIN_SOUND_CRITERIA, regenerations=attempt,
                         sound=[c.id for c in kept],
                         dropped_wrong_oracle=[c.id for c in fail_golden],
                         dropped_non_discriminating=[cid for cid, v in validation.items()
                                                     if not v.kept and "stub" in v.reason],
                         golden_defects=defects)
        if res.ok:
            return kept_rubric, golden_jr, stub_jr, res
        best = (kept_rubric, golden_jr, stub_jr, res)
        feedback = (f"{len(fail_golden)} criteria FAILED on the correct solution (too strict / "
                    f"out of scope: {[c.id for c in fail_golden][:6]}); some passed on the empty "
                    "stub (non-discriminating). Write criteria a CORRECT solution clearly meets "
                    "and an unimplemented stub clearly fails, scoped to the implemented behavior.")
    if best:
        best[3].detail = f"could not reach {MIN_SOUND_CRITERIA} sound criteria"
    return best
