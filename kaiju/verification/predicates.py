"""#1 — the generated task-specific DETERMINISTIC layer (plan §3.3).

The rubric generates *judgment* criteria; this generates *decidable* criteria — a
whitelist of predicate types the model instantiates from TRUTH.md and that our code
evaluates deterministically against the agent's produced code (the cumulative
patch). This is the missing "generate pytest from TRUTH.md" half.

Only truly-decidable predicate types are allowed (no free-form semantic assertions —
those belong in the rubric). Each generated predicate SHOULD ship a negative fixture
(a snippet that must FAIL it); a predicate whose negative fixture still passes is
vacuous and rejected (`prune_vacuous`), turning "cites TRUTH.md" into "provably
exercises TRUTH.md".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .model_client import ModelClient

# Whitelist of decidable predicate types.
PREDICATE_TYPES = {
    "pattern_present",   # regex must appear in ADDED code (a required construct)
    "pattern_absent",    # regex must NOT appear in added code (anti-pattern / cheat)
    "symbol_present",    # a literal token/identifier must appear in added code
    "literal_absent",    # a specific literal must NOT appear in added code (hardcode guard)
    "import_present",    # an import/include line must be added
}


@dataclass
class Predicate:
    id: str
    type: str
    target: str                  # regex (pattern_*) or literal (symbol/literal/import)
    truth_ref: str = ""
    description: str = ""
    negative_fixture: str = ""   # a snippet that MUST fail this predicate

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "target": self.target,
                "truth_ref": self.truth_ref, "description": self.description,
                "negative_fixture": self.negative_fixture}

    @staticmethod
    def from_dict(d: dict) -> "Predicate":
        return Predicate(id=str(d.get("id") or ""), type=str(d.get("type") or ""),
                         target=str(d.get("target") or ""),
                         truth_ref=str(d.get("truth_ref") or ""),
                         description=str(d.get("description") or ""),
                         negative_fixture=str(d.get("negative_fixture") or ""))


def added_code(patches: list[str]) -> str:
    """The added lines across the agent's patches (what the solution introduced)."""
    lines = []
    for patch in patches:
        for ln in patch.splitlines():
            if ln.startswith("+") and not ln.startswith("+++"):
                lines.append(ln[1:])
    return "\n".join(lines)


def evaluate_predicate(p: Predicate, code: str) -> tuple[bool, str]:
    """Deterministically evaluate a predicate against a code string. Returns
    (passed, detail). An unknown type or invalid regex is a non-fatal skip -> True
    (a malformed generated predicate must never spuriously quarantine)."""
    t = p.type
    try:
        if t == "pattern_present":
            ok = re.search(p.target, code) is not None
            return ok, ("matched" if ok else "required pattern not found")
        if t == "pattern_absent":
            ok = re.search(p.target, code) is None
            return ok, ("absent" if ok else "forbidden pattern present")
        if t == "symbol_present":
            ok = p.target in code
            return ok, ("present" if ok else "required symbol missing")
        if t == "literal_absent":
            ok = p.target not in code
            return ok, ("absent" if ok else f"forbidden literal {p.target!r} present")
        if t == "import_present":
            ok = p.target in code
            return ok, ("import present" if ok else "required import missing")
    except re.error as e:
        return True, f"skipped (bad regex: {e})"
    return True, f"skipped (unknown type {t})"


def prune_vacuous(predicates: list[Predicate]) -> list[Predicate]:
    """Drop predicates that PASS their own negative fixture — they don't actually
    discriminate (vacuous). A predicate with no fixture is kept but flagged weak."""
    kept = []
    for p in predicates:
        if p.negative_fixture:
            passed, _ = evaluate_predicate(p, p.negative_fixture)
            if passed:
                continue  # negative fixture should FAIL; it passed -> vacuous, drop
        kept.append(p)
    return kept


_SYSTEM = (
    "You generate DECIDABLE task-specific checks from a TRUTH.md answer key. Each check is "
    "evaluated by a program (regex/substring) against the ADDED code of a candidate solution — "
    "so it must be objectively decidable, NOT a judgment call.\n"
    f"Allowed types ONLY: {', '.join(sorted(PREDICATE_TYPES))}.\n"
    "- pattern_present/absent: a regex. symbol_present/import_present/literal_absent: a literal.\n"
    "- Tie each to a specific TRUTH.md item (behavioral contract / pitfalls / cheat surface).\n"
    "- literal_absent is for the cheat surface (a value a fail_to_pass test asserts that must "
    "not be hardcoded). pattern_absent is for anti-patterns.\n"
    "- Provide a `negative_fixture`: a short code snippet that MUST FAIL the check (proves it "
    "discriminates).\n"
    'Return ONLY JSON: [{"id":"pt.<slug>","type":"...","target":"...","truth_ref":"...",'
    '"description":"...","negative_fixture":"..."}].'
)


def build_predicate_prompt(truth_md: str) -> tuple[str, str]:
    return _SYSTEM, f"# TRUTH.md\n\n{truth_md}\n\nGenerate the decidable checks JSON now."


def _extract_json_array(text: str) -> list:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, list) else []
    except ValueError:
        return []


def parse_predicates(text: str) -> list[Predicate]:
    out = []
    for i, d in enumerate(_extract_json_array(text)):
        if not isinstance(d, dict):
            continue
        p = Predicate.from_dict(d)
        if p.type not in PREDICATE_TYPES or not p.target:
            continue
        if not p.id:
            p.id = f"pt.{i}"
        if not p.id.startswith("pt."):
            p.id = "pt." + p.id
        out.append(p)
    return out


def generate_predicates(truth_md: str, client: ModelClient, *,
                        max_tokens: int = 4096) -> list[Predicate]:
    system, user = build_predicate_prompt(truth_md)
    preds = parse_predicates(client.complete(system, user, max_tokens=max_tokens).text)
    return prune_vacuous(preds)
