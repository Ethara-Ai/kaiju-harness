"""Structured parser over a module's recorded conversation (``output.json`` →
``history``), the OpenHands-format event log the harness writes per module.

This is the reliable, cross-language (verified identical across all 8 languages)
source for trajectory-CONTENT verification — tool-call integrity and
observation authenticity — as opposed to reverse-engineering aider's verbose
raw ``llm_history.txt``.

Event shapes consumed:
  SystemPromptEvent  {source:agent, system_prompt, tools:[...]}
  ActionEvent        {source:agent, tool_name, tool_call_id, tool_call, thought, ...}
  ObservationEvent   {source:environment, tool_name, action_id, observation:{content:[{text}], is_error}}
  MessageEvent       {source:user, llm_message:{content:[{text}]}}   # injected test/lint feedback
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Meta tools that legitimately produce no environment observation.
_META_TOOLS = {"finish", "think"}
# Observation text fragments that mark a successful file EDIT (vs a mere load).
_EDIT_MARKERS = ("has been edited", "has been created", "have been made to")
_LOAD_MARKERS = ("loaded into context",)
# Feedback markers: the harness injects the test/lint command it ran.
_FEEDBACK_MARKERS = ("[tool:cmd_test]", "[tool:cmd_lint]", "I ran this command")


def _text_of(content: Any) -> str:
    """Flatten an OpenHands content field (list[{type,text}] | str | dict) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if isinstance(content, dict):
        return _text_of(content.get("content") or content.get("text") or "")
    return "" if content is None else str(content)


@dataclass
class Action:
    index: int
    tool_name: str
    call_id: str | None


@dataclass
class Observation:
    index: int
    tool_name: str
    action_id: str | None
    is_error: bool
    text: str

    @property
    def is_edit(self) -> bool:
        return any(mk in self.text for mk in _EDIT_MARKERS)


@dataclass
class Feedback:
    index: int
    text: str


@dataclass
class ModuleContent:
    module: str
    stage: str
    declared_tools: tuple[str, ...]
    actions: list[Action] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    feedback: list[Feedback] = field(default_factory=list)
    num_events: int = 0

    @property
    def allowed_tools(self) -> set[str]:
        return set(self.declared_tools) | _META_TOOLS

    def hallucinated_actions(self) -> list[Action]:
        allowed = self.allowed_tools
        return [a for a in self.actions if a.tool_name and a.tool_name not in allowed]

    def actions_without_call_id(self) -> list[Action]:
        return [a for a in self.actions if not a.call_id]

    def orphan_observations(self) -> list[Observation]:
        """Observations whose action_id matches NO recorded action call_id — an
        observation with no cause is a fabricated/tampered record."""
        call_ids = {a.call_id for a in self.actions if a.call_id}
        return [o for o in self.observations
                if o.action_id is not None and o.action_id not in call_ids]

    def mismatched_observations(self) -> list[Observation]:
        """Observations whose tool_name disagrees with the action they cite."""
        by_id = {a.call_id: a for a in self.actions if a.call_id}
        out = []
        for o in self.observations:
            a = by_id.get(o.action_id)
            if a and o.tool_name and a.tool_name and o.tool_name != a.tool_name:
                out.append(o)
        return out

    def edit_observations(self) -> list[Observation]:
        return [o for o in self.observations if o.is_edit and not o.is_error]

    def failed_observations(self) -> list[Observation]:
        return [o for o in self.observations if o.is_error]

    @property
    def has_conversation(self) -> bool:
        return bool(self.actions or self.observations)


def parse_history(events: list[dict], *, module: str = "", stage: str = "") -> ModuleContent:
    declared: tuple[str, ...] = ()
    actions: list[Action] = []
    observations: list[Observation] = []
    feedback: list[Feedback] = []
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind == "SystemPromptEvent":
            tools = e.get("tools") or []
            declared = tuple(
                (t.get("name") if isinstance(t, dict) else str(t)) for t in tools)
        elif kind == "ActionEvent":
            actions.append(Action(i, e.get("tool_name") or "", e.get("tool_call_id")))
        elif kind == "ObservationEvent":
            obs = e.get("observation")
            is_err = bool(obs.get("is_error")) if isinstance(obs, dict) else False
            text = _text_of(obs.get("content") if isinstance(obs, dict) else obs)
            observations.append(Observation(
                i, e.get("tool_name") or "", e.get("action_id"), is_err, text))
        elif kind == "MessageEvent" and e.get("source") == "user":
            text = _text_of((e.get("llm_message") or {}).get("content"))
            if any(mk in text for mk in _FEEDBACK_MARKERS):
                feedback.append(Feedback(i, text))
    return ModuleContent(module=module, stage=stage, declared_tools=declared,
                         actions=actions, observations=observations,
                         feedback=feedback, num_events=len(events))


def module_content(mod, stage: str = "") -> ModuleContent | None:
    """Build the ModuleContent from a ModuleInfo's already-parsed output.json.
    Returns None when there is no usable history."""
    oj = mod.output_json
    if not isinstance(oj, dict):
        return None
    history = oj.get("history")
    if not isinstance(history, list) or not history:
        return None
    return parse_history(history, module=mod.name, stage=stage)
