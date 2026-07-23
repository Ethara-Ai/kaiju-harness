"""P6 (part 2) — the LLM-as-judge.

Scores each rubric criterion pass|fail against a candidate trajectory, using a
DIFFERENT-family judge (Opus 4.8) at max reasoning effort, REFERENCE-GUIDED by
TRUTH.md (never shown the golden diff). Binary per criterion, evidence-cited,
conservative (fail-if-uncertain) to resist leniency/self-preference bias.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .model_client import ModelClient, MAX_REASONING_EFFORT
from .rubric import Rubric, Criterion


@dataclass
class CriterionVerdict:
    criterion_id: str
    passed: bool
    evidence: str = ""
    justification: str = ""

    def to_dict(self) -> dict:
        return {"criterion_id": self.criterion_id, "passed": self.passed,
                "evidence": self.evidence, "justification": self.justification}


@dataclass
class JudgeResult:
    verdicts: list[CriterionVerdict] = field(default_factory=list)
    model: str = ""
    reasoning_effort: str = MAX_REASONING_EFFORT
    raw_verdicts: int = 0        # how many verdicts the JUDGE actually returned
    response_chars: int = 0      # length of the raw model response
    raw_response: str = ""       # the judge's full answer (for audit)
    thinking: str = ""           # the judge's extended-reasoning trace (for audit)

    def by_id(self) -> dict[str, CriterionVerdict]:
        return {v.criterion_id: v for v in self.verdicts}

    @property
    def ok(self) -> bool:
        """The judge actually produced verdicts. When False (empty/unparseable
        response), the result is INCONCLUSIVE — it must NOT be read as 'all failed'."""
        return self.response_chars > 0 and self.raw_verdicts > 0

    def to_dict(self) -> dict:
        return {"model": self.model, "reasoning_effort": self.reasoning_effort,
                "ok": self.ok, "raw_verdicts": self.raw_verdicts,
                "response_chars": self.response_chars,
                "thinking": self.thinking, "raw_response": self.raw_response,
                "verdicts": [v.to_dict() for v in self.verdicts]}

    @staticmethod
    def from_dict(d: dict) -> "JudgeResult":
        return JudgeResult(
            model=str(d.get("model") or ""),
            reasoning_effort=str(d.get("reasoning_effort") or MAX_REASONING_EFFORT),
            raw_verdicts=int(d.get("raw_verdicts") or 0),
            response_chars=int(d.get("response_chars") or 0),
            thinking=str(d.get("thinking") or ""),
            raw_response=str(d.get("raw_response") or ""),
            verdicts=[CriterionVerdict(
                criterion_id=str(v.get("criterion_id") or ""),
                passed=bool(v.get("passed")),
                evidence=str(v.get("evidence") or ""),
                justification=str(v.get("justification") or ""),
            ) for v in (d.get("verdicts") or [])])


_SYSTEM = (
    "You are a rigorous, skeptical code-review JUDGE. You score a candidate solution's "
    "TRAJECTORY against a rubric, using TRUTH.md as the reference for what a correct "
    "solution must achieve. You judge the PATH and the QUALITY, not whether tests passed "
    "(that is checked separately).\n"
    "For EACH criterion return a binary verdict:\n"
    "- pass ONLY if the trajectory clearly satisfies it, with concrete cited evidence.\n"
    "- fail if it is violated OR if the evidence is insufficient to be confident "
    "(default to fail when uncertain).\n"
    "TRUTH.md describes a DESTINATION; a different but valid route that reaches the "
    "behavioral contract must still pass (do not penalize a solution merely for differing "
    "from any single expected approach).\n"
    'Return ONLY a JSON array, one object per criterion: '
    '[{"criterion_id": "...", "verdict": "pass"|"fail", "evidence": "<quote/span>", '
    '"justification": "<one sentence>"}].'
)


def build_judge_prompt(truth_md: str, rubric: Rubric, trajectory_digest: str) -> tuple[str, str]:
    crit_lines = "\n".join(f"- {c.id}: {c.text}" for c in rubric.criteria)
    user = (
        f"# TRUTH.md (reference)\n\n{truth_md}\n\n"
        f"# Rubric criteria to score\n{crit_lines}\n\n"
        f"# Candidate trajectory\n{trajectory_digest}\n\n"
        "Score every criterion now. Return the JSON array."
    )
    return _SYSTEM, user


def _extract_json_array(text: str) -> list:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def parse_judge_response(text: str, rubric: Rubric) -> list[CriterionVerdict]:
    """Parse verdicts, keyed to the rubric. A criterion the judge omitted is
    treated as FAIL (conservative — an unscored criterion is not a pass)."""
    raw = {}
    for d in _extract_json_array(text):
        if isinstance(d, dict) and d.get("criterion_id"):
            raw[str(d["criterion_id"])] = d
    verdicts: list[CriterionVerdict] = []
    for c in rubric.criteria:
        d = raw.get(c.id)
        if d is None:
            verdicts.append(CriterionVerdict(c.id, passed=False,
                                             justification="not scored by judge (treated as fail)"))
            continue
        verdict = str(d.get("verdict") or "").strip().lower()
        passed = verdict == "pass"
        verdicts.append(CriterionVerdict(
            c.id, passed=passed, evidence=str(d.get("evidence") or "")[:500],
            justification=str(d.get("justification") or "")[:500]))
    return verdicts


def judge_trajectory(truth_md: str, rubric: Rubric, trajectory_digest: str,
                     client: ModelClient, *, max_tokens: int = 32000) -> JudgeResult:
    """Judge with MAX reasoning effort. The token budget must cover the extended
    thinking AND the answer — too small and thinking consumes it all, leaving an
    empty response (silently mis-read as 'all fail'). If the first pass comes back
    empty, retry once with a larger budget before giving up."""
    system, user = build_judge_prompt(truth_md, rubric, trajectory_digest)
    model = getattr(client, "model", "")
    resp = client.complete(system, user, max_tokens=max_tokens,
                           reasoning_effort=MAX_REASONING_EFFORT)
    if not (resp.text or "").strip():
        resp = client.complete(system, user, max_tokens=max(max_tokens, 48000),
                               reasoning_effort=MAX_REASONING_EFFORT)
    raw = _extract_json_array(resp.text)
    return JudgeResult(verdicts=parse_judge_response(resp.text, rubric), model=model,
                       raw_verdicts=len(raw), response_chars=len(resp.text or ""),
                       raw_response=resp.text or "", thinking=resp.reasoning or "")
