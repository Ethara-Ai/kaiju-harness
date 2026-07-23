"""P4 — TRUTH.md authoring.

TRUTH.md is the path-agnostic answer key: what a correct solve must ACHIEVE and
how a skilled engineer would get there — derived FROM the golden patch (base ->
reference diff) but abstracted so it prescribes a destination, not a route. The
golden diff is a *private authoring aid*; TRUTH.md must not reproduce it verbatim
(the leak-guard) and must enumerate valid alternative approaches (path-agnosticism).

Authoring is host-side only and never mounted into the agent container.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model_client import ModelClient

# Required TRUTH.md sections (markdown ## headings), in order. The generator is
# instructed to produce exactly these; the guard verifies they are present.
TRUTH_SECTIONS = (
    "Problem",
    "Behavioral contract",
    "Solution decomposition",
    "Solution space",          # valid alternatives — the path-agnostic core
    "Known pitfalls",
    "Cheat surface",
    "Success criteria",
)

_SYSTEM = (
    "You are authoring TRUTH.md — a path-agnostic specification of a CORRECT solution "
    "to a software task, used to verify (not produce) solutions.\n"
    "You are given a reference (golden) diff ONLY as a private authoring aid to make the "
    "specification correct and complete. You MUST NOT reproduce it: do not copy its code "
    "lines verbatim, and do not say 'the fix is <code>'. Describe the DESTINATION and the "
    "TERRAIN (behavior, sub-goals, pitfalls), never the exact route.\n"
    "A single golden diff is ONE correct solution; other valid routes exist. Explicitly "
    "enumerate valid alternative approaches so a different-but-correct solution is not "
    "penalized.\n"
    f"Output GitHub-flavored markdown with exactly these ## sections, in order: "
    f"{', '.join(TRUTH_SECTIONS)}."
)


@dataclass
class TruthInputs:
    repo: str
    language: str
    spec_text: str
    golden_diff: str                       # private authoring aid — never emitted
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    stub_files: list[str] = field(default_factory=list)


@dataclass
class TruthDoc:
    text: str
    sections: dict[str, str]
    leak_violations: list[str] = field(default_factory=list)
    regenerations: int = 0

    @property
    def ok(self) -> bool:
        return not self.leak_violations and _missing_sections(self.sections) == []


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n...[truncated {len(s) - n} chars]"


def build_truth_prompt(inp: TruthInputs) -> tuple[str, str]:
    """Pure: construct (system, user) messages for TRUTH.md authoring."""
    user = (
        f"# Task: implement stubs in `{inp.repo}` ({inp.language})\n\n"
        f"## Specification\n{_truncate(inp.spec_text, 12000)}\n\n"
        f"## Files that were stubbed (to be implemented)\n"
        f"{chr(10).join('- ' + f for f in inp.stub_files) or '(not recorded)'}\n\n"
        f"## Tests that must flip to passing (fail_to_pass)\n"
        f"{chr(10).join('- ' + t for t in inp.fail_to_pass[:200]) or '(none listed)'}\n\n"
        f"## Tests that must stay passing (pass_to_pass)\n"
        f"{chr(10).join('- ' + t for t in inp.pass_to_pass[:100]) or '(none listed)'}\n\n"
        f"## Reference solution diff (PRIVATE authoring aid — DO NOT reproduce)\n"
        f"```diff\n{_truncate(inp.golden_diff, 16000)}\n```\n\n"
        "Author TRUTH.md now."
    )
    return _SYSTEM, user


def parse_sections(md: str) -> dict[str, str]:
    """Split a markdown doc into {canonical_section: body} by ## headings."""
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in md.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)", line)
        if m:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            heading = m.group(1).strip()
            cur = _canonical_section(heading)
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def _canonical_section(heading: str) -> str:
    h = heading.lower()
    for s in TRUTH_SECTIONS:
        if s.lower() in h:
            return s
    return heading


def _missing_sections(sections: dict[str, str]) -> list[str]:
    return [s for s in TRUTH_SECTIONS if not sections.get(s)]


def _golden_added_lines(golden_diff: str) -> list[str]:
    """Substantive added code lines from the golden diff (candidates for a leak)."""
    out = []
    for line in golden_diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            code = line[1:].strip()
            # ignore trivial lines (braces, short tokens, comments) — a verbatim
            # match on those isn't a meaningful route leak.
            if len(code) >= 24 and not code.startswith(("//", "#", "*", "/*")):
                out.append(code)
    return out


def leak_guard(truth_md: str, golden_diff: str, *,
               min_leaked_lines: int = 3) -> list[str]:
    """Return leak/abstraction violations. Two checks:
      * verbatim route leak — >= min_leaked_lines substantive golden code lines
        appear verbatim in TRUTH.md (it copied the solution instead of abstracting);
      * missing required sections (incl. the Solution-space path-agnosticism section).
    """
    violations: list[str] = []
    body = truth_md
    leaked = [ln for ln in set(_golden_added_lines(golden_diff)) if ln in body]
    if len(leaked) >= min_leaked_lines:
        violations.append(
            f"verbatim route leak: {len(leaked)} golden code line(s) reproduced "
            f"(e.g. {leaked[0][:60]!r})")
    for s in _missing_sections(parse_sections(truth_md)):
        violations.append(f"missing required section: {s}")
    return violations


def generate_truth(inp: TruthInputs, client: ModelClient, *,
                   max_regen: int = 3, max_tokens: int = 8192) -> TruthDoc:
    """Author TRUTH.md, regenerating (with the specific violations fed back) until
    it passes the leak/section guard or the bound is hit."""
    system, user = build_truth_prompt(inp)
    violations: list[str] = []
    text = ""
    for attempt in range(max_regen + 1):
        prompt = user
        if violations:
            prompt = (user + "\n\n## Your previous draft had these problems — fix them:\n"
                      + "\n".join(f"- {v}" for v in violations))
        text = client.complete(system, prompt, max_tokens=max_tokens).text
        violations = leak_guard(text, inp.golden_diff)
        if not violations:
            return TruthDoc(text=text, sections=parse_sections(text),
                            leak_violations=[], regenerations=attempt)
    return TruthDoc(text=text, sections=parse_sections(text),
                    leak_violations=violations, regenerations=max_regen)
