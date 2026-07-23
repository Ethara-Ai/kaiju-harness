"""Model-invocation abstraction for the non-deterministic half of verification
(TRUTH.md authoring, rubric generation, mutation, and the LLM-as-judge).

The abstraction exists so every piece of *logic* (prompt construction, response
parsing, scoring, the meta-verification loop) is unit-testable with a scripted
``MockClient``, while the real path uses the harness's existing ``litellm``
integration (the same call shape as ``agent_utils.summarize_specification``).

The judge default is **Opus 4.8 at max reasoning effort**, a DIFFERENT model
family than the agent under test (self-preference-bias mitigation).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

# The two subscription models the harness serves via its bridges (resolve_model.sh):
#   Claude  -> anthropic/claude-opus-4-8  via the claude-code bridge (:8765)
#   GPT     -> openai/gpt-5.x             via the codex bridge (:8788)
# Both are env-overridable so "latest" can move without a code change.
CLAUDE_JUDGE_MODEL = os.environ.get("KAIJU_VERIFY_CLAUDE_MODEL", "anthropic/claude-opus-4-8")
GPT_JUDGE_MODEL = os.environ.get("KAIJU_VERIFY_GPT_MODEL", "openai/gpt-5.5-2026-04-23")
JUDGE_MODEL = CLAUDE_JUDGE_MODEL   # backward-compatible default
MAX_REASONING_EFFORT = "high"     # litellm's max tier (maps to Anthropic extended thinking)

# Subscription-bridge endpoints (match run_trajectory.sh's fixed ports).
_CODEX_BRIDGE = os.environ.get("OPENAI_BASE_URL") or "http://127.0.0.1:8788"
_CC_BRIDGE = os.environ.get("ANTHROPIC_API_BASE") or "http://127.0.0.1:8765"


def model_family(model: str) -> str:
    """'claude' | 'gpt' | 'other' from a model name OR a model_short label."""
    m = (model or "").lower()
    if any(k in m for k in ("claude", "opus", "sonnet", "haiku")):
        return "claude"
    if any(m.startswith(p) or ("/" + p) in m or p in m
           for p in ("gpt", "openai", "codex", "o1", "o3", "o4")):
        return "gpt"
    return "other"


def cross_family_judge_model(run_model: str) -> str:
    """The CROSS-FAMILY judge model for a run (self-preference-bias mitigation):
    a Claude-produced run is judged by GPT/Codex; a GPT/Codex run by Claude.
    Anything else defaults to the Claude judge."""
    fam = model_family(run_model)
    if fam == "claude":
        return GPT_JUDGE_MODEL          # claude run -> codex/gpt judge
    return CLAUDE_JUDGE_MODEL           # gpt/other run -> claude judge


@dataclass
class ModelResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    raw: object = None
    reasoning: str = ""     # extended-thinking / reasoning trace, when the model returns it


class ModelClient(Protocol):
    def complete(self, system: str, user: str, *,
                 max_tokens: int = 8192,
                 reasoning_effort: str | None = None) -> ModelResponse: ...


@dataclass
class LiteLLMClient:
    """Real client, using the same ``litellm`` path as the rest of the harness.

    Not exercised in unit tests (needs model access + the bridge); it is the
    production backend wired for when verification runs in the pipeline env.
    """
    model: str = JUDGE_MODEL
    default_reasoning_effort: str = MAX_REASONING_EFFORT
    cache_prompts: bool = True

    def _bridge_kwargs(self) -> dict:
        """Point litellm at the right SUBSCRIPTION bridge for this model family."""
        fam = model_family(self.model)
        if fam == "gpt":
            return {"api_base": os.environ.get("OPENAI_BASE_URL") or _CODEX_BRIDGE,
                    "api_key": os.environ.get("OPENAI_API_KEY")
                    or os.environ.get("KAIJU_CODEX_BRIDGE_SECRET") or "bridge"}
        if fam == "claude" and os.environ.get("ANTHROPIC_API_BASE"):
            return {"api_base": os.environ.get("ANTHROPIC_API_BASE") or _CC_BRIDGE,
                    "api_key": os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("KAIJU_CC_BRIDGE_SECRET") or "bridge"}
        return {}

    def complete(self, system: str, user: str, *,
                 max_tokens: int = 8192,
                 reasoning_effort: str | None = None) -> ModelResponse:
        import litellm  # imported lazily; only needed on the real path

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        kwargs.update(self._bridge_kwargs())
        eff = reasoning_effort or self.default_reasoning_effort
        if eff:
            kwargs["reasoning_effort"] = eff
        resp = litellm.completion(**kwargs)
        text = ""
        reasoning = ""
        try:
            msg = resp.choices[0].message
            text = msg.content or ""
            # Anthropic/OpenAI reasoning surfaces differently across litellm versions.
            reasoning = getattr(msg, "reasoning_content", "") or ""
            if not reasoning:
                blocks = getattr(msg, "thinking_blocks", None) or []
                reasoning = "\n".join(
                    b.get("thinking", "") for b in blocks if isinstance(b, dict))
        except (AttributeError, IndexError, KeyError):
            pass
        pt = ct = 0
        usage = getattr(resp, "usage", None)
        if usage:
            pt = getattr(usage, "prompt_tokens", 0) or 0
            ct = getattr(usage, "completion_tokens", 0) or 0
        cost = 0.0
        try:
            cost = litellm.completion_cost(completion_response=resp) or 0.0
        except Exception:
            pass
        return ModelResponse(text=text, prompt_tokens=pt, completion_tokens=ct,
                             cost=cost, raw=resp, reasoning=reasoning)


@dataclass
class MockClient:
    """Scripted client for tests. ``responses`` are returned in order; if a
    ``responder`` callable is given it takes precedence (``responder(system, user)``)."""
    responses: list[str] = field(default_factory=list)
    responder: object = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    _i: int = 0

    def complete(self, system: str, user: str, *,
                 max_tokens: int = 8192,
                 reasoning_effort: str | None = None) -> ModelResponse:
        self.calls.append((system, user))
        if callable(self.responder):
            return ModelResponse(text=self.responder(system, user))
        if self._i < len(self.responses):
            text = self.responses[self._i]
            self._i += 1
            return ModelResponse(text=text)
        raise AssertionError("MockClient ran out of scripted responses")


def client_for_model(model: str, *, reasoning_effort: str = MAX_REASONING_EFFORT) -> LiteLLMClient:
    return LiteLLMClient(model=model, default_reasoning_effort=reasoning_effort)


def default_judge_client() -> LiteLLMClient:
    return LiteLLMClient(model=JUDGE_MODEL, default_reasoning_effort=MAX_REASONING_EFFORT)


def judge_client_for_run(run_model: str) -> LiteLLMClient:
    """Cross-family judge client for a run (Claude run -> GPT judge, and vice versa)."""
    return client_for_model(cross_family_judge_model(run_model))


def _preset_to_model(preset_or_model: str) -> str:
    """Map a run's model_short/preset to a litellm model name for the bridge that is
    already up. A claude* short -> the Claude sub model; a gpt* short -> the GPT sub."""
    fam = model_family(preset_or_model)
    if fam == "claude":
        return CLAUDE_JUDGE_MODEL
    if fam == "gpt":
        return GPT_JUDGE_MODEL
    return preset_or_model if "/" in preset_or_model else CLAUDE_JUDGE_MODEL


def default_generation_client() -> LiteLLMClient:
    """Client for authoring TRUTH.md / rubrics / predicates / mutants. Defaults to the
    Claude subscription model; override via KAIJU_VERIFY_GEN_MODEL."""
    return client_for_model(os.environ.get("KAIJU_VERIFY_GEN_MODEL", CLAUDE_JUDGE_MODEL))


def generation_client_for_run(run_model: str) -> LiteLLMClient:
    """Author with the RUN's own model family, whose bridge is already up during the
    pipeline (so generation works without starting a second bridge). Override wins."""
    override = os.environ.get("KAIJU_VERIFY_GEN_MODEL")
    return client_for_model(override or _preset_to_model(run_model))
