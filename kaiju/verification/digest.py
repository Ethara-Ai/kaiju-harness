"""Builds a bounded, judge-readable digest of a produced trajectory: the final
diff plus, per stage, each module's reasoning (thoughts), the edits it made, and
the feedback it received. This is what the LLM-as-judge scores against TRUTH.md.
"""
from __future__ import annotations

from .content import _text_of, _FEEDBACK_MARKERS
from .trajectory import TrajectoryBundle

_STAGE_LABEL = {"stage1": "draft", "stage2": "lint-refine", "stage3": "test-refine"}


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f" …[+{len(s) - n} chars]"


def _module_digest(output_json: dict, *, budget: int) -> str:
    history = output_json.get("history")
    if not isinstance(history, list):
        return ""
    lines: list[str] = []
    used = 0
    for e in history:
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        piece = ""
        if kind == "ActionEvent" and e.get("thought"):
            piece = "  reasoning: " + _trunc(_text_of(e.get("thought")), 600)
        elif kind == "ObservationEvent":
            obs = e.get("observation")
            txt = _text_of(obs.get("content") if isinstance(obs, dict) else obs)
            if "edited" in txt or "created" in txt or (isinstance(obs, dict) and obs.get("is_error")):
                piece = "  edit: " + _trunc(txt, 200)
        elif kind == "MessageEvent" and e.get("source") == "user":
            txt = _text_of((e.get("llm_message") or {}).get("content"))
            if any(mk in txt for mk in _FEEDBACK_MARKERS):
                piece = "  feedback: " + _trunc(txt, 400)
        if piece:
            lines.append(piece)
            used += len(piece)
            if used > budget:
                lines.append("  …[digest truncated]")
                break
    return "\n".join(lines)


def build_trajectory_digest(b: TrajectoryBundle, *, total_budget: int = 24000) -> str:
    parts: list[str] = []
    patches = b.all_agent_patches()
    parts.append("## Final diff (agent's cumulative changes)\n"
                 + _trunc(patches[0] if patches else "(no diff recorded)", 9000))
    remaining = total_budget - len(parts[0])
    per_module = max(600, remaining // max(1, sum(len(st.modules) for st in b.stages.values())))
    for key in ("stage1", "stage2", "stage3"):
        st = b.stages.get(key)
        if not st or not st.modules:
            continue
        seg = [f"\n## Stage: {_STAGE_LABEL.get(key, key)}"]
        for m in st.modules:
            if not isinstance(m.output_json, dict):
                continue
            md = _module_digest(m.output_json, budget=per_module)
            if md:
                seg.append(f"### module {m.name}\n{md}")
        if len(seg) > 1:
            parts.append("\n".join(seg))
    digest = "\n".join(parts)
    return _trunc(digest, total_budget + 12000)
