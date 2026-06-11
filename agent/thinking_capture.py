"""Capture and store model thinking/reasoning tokens from aider runs."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agent.llm_cost_capture import LlmCallLog


@dataclass
class SummarizerCost:
    """Cost info from a single summarizer LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


@dataclass
class SummarizerCostTracker:
    """Accumulates costs from all summarizer LLM calls (spec + test output)."""

    costs: list[SummarizerCost] = field(default_factory=list)

    def add(self, cost: SummarizerCost) -> None:
        self.costs.append(cost)

    @property
    def total_cost(self) -> float:
        return sum(c.cost for c in self.costs)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.costs)

    @property
    def total_completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.costs)

    def to_dict(self) -> dict:
        return {
            "summarizer_cost": self.total_cost,
            "summary_input_tokens": self.total_prompt_tokens,
            "summary_output_tokens": self.total_completion_tokens,
            "summarizer_call_count": len(self.costs),
        }


@dataclass
class Turn:
    """A single conversation turn (one user message + one assistant response)."""

    role: str  # "user" or "assistant"
    content: str  # The actual message/response text
    thinking: Optional[str] = None  # Reasoning content (assistant only)
    thinking_tokens: int = 0  # Token count for thinking
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    stage: str = ""  # "draft", "lint", or "test"
    module: str = ""  # e.g., "src__itsdangerous___json"
    turn_number: int = 0
    edit_error: str | None = None
    timestamp: str = ""
    llm_response_id: str | None = None
    provider: str = ""


@dataclass
class ThinkingCapture:
    """Accumulates turns with thinking across the entire pipeline run."""

    turns: list[Turn] = field(default_factory=list)
    summarizer_costs: SummarizerCostTracker = field(
        default_factory=SummarizerCostTracker
    )
    module_llm_calls: dict[str, "LlmCallLog"] = field(default_factory=dict)
    capture_mismatches: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_user_turn(
        self,
        content: str,
        stage: str,
        module: str,
        turn_number: int,
        timestamp: str = "",
    ) -> None:
        """Record a user message turn."""
        if not timestamp:
            from datetime import datetime, timezone
            timestamp = datetime.now(timezone.utc).isoformat()
        self.turns.append(
            Turn(
                role="user",
                content=content,
                stage=stage,
                module=module,
                turn_number=turn_number,
                timestamp=timestamp,
            )
        )

    def add_assistant_turn(
        self,
        content: str,
        thinking: Optional[str],
        thinking_tokens: int,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int,
        cache_write_tokens: int,
        cost: float,
        stage: str,
        module: str,
        turn_number: int,
        timestamp: str = "",
        llm_response_id: str | None = None,
        provider: str = "",
    ) -> None:
        """Record an assistant response turn with optional thinking content."""
        if not timestamp:
            from datetime import datetime, timezone
            timestamp = datetime.now(timezone.utc).isoformat()
        self.turns.append(
            Turn(
                role="assistant",
                content=content,
                thinking=thinking,
                thinking_tokens=thinking_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_hit_tokens=cache_hit_tokens,
                cache_write_tokens=cache_write_tokens,
                cost=cost,
                stage=stage,
                module=module,
                turn_number=turn_number,
                timestamp=timestamp,
                llm_response_id=llm_response_id,
                provider=provider,
            )
        )

    def to_history(self) -> list[dict]:
        """Convert to output.jsonl history format."""
        return [
            {
                "role": t.role,
                "content": t.content,
                **({"thinking": t.thinking} if t.thinking else {}),
                **({"thinking_tokens": t.thinking_tokens} if t.thinking_tokens else {}),
                "stage": t.stage,
                "module": t.module,
                "turn": t.turn_number,
            }
            for t in self.turns
        ]

    def get_module_turns(self, module: str) -> list[Turn]:
        """Return turns belonging to a specific module."""
        return [t for t in self.turns if t.module == module]

    def get_module_metrics(self, module: str) -> dict:
        """Aggregate metrics for a single module.

        When llm_cost_capture recorded calls for this module, totals come from
        the full call log (main loop + aider auxiliaries + our summarizers) so
        the numbers reconcile against provider billing. Otherwise fall back to
        turn-derived totals which only see the main loop.
        """
        module_turns = [
            t for t in self.turns if t.role == "assistant" and t.module == module
        ]
        is_vertex_module = bool(module_turns) and all(
            getattr(t, "provider", "") == "vertex_ai_gemini" for t in module_turns
        )
        metrics: dict = {
            "total_cost": sum(t.cost for t in module_turns),
            "total_prompt_tokens": sum(t.prompt_tokens for t in module_turns),
            "total_completion_tokens": sum(t.completion_tokens for t in module_turns),
            "total_thinking_tokens": sum(t.thinking_tokens for t in module_turns),
            "num_turns": len(module_turns),
        }
        if is_vertex_module:
            metrics["cached_content_tokens"] = sum(t.cache_hit_tokens for t in module_turns)
        else:
            metrics["cache_hit_tokens"] = sum(t.cache_hit_tokens for t in module_turns)
            metrics["cache_write_tokens"] = sum(t.cache_write_tokens for t in module_turns)

        call_log = self.module_llm_calls.get(module)
        if call_log is not None and call_log.calls:
            totals = call_log.grand_totals()
            metrics["total_cost"] = totals["cost_usd"]
            metrics["total_prompt_tokens"] = totals["prompt_tokens"]
            metrics["total_completion_tokens"] = totals["completion_tokens"]
            metrics["total_thinking_tokens"] = totals["thinking_tokens"]
            for k in ("cache_hit_tokens", "cache_write_tokens", "cached_content_tokens"):
                metrics.pop(k, None)
            if "cached_content_tokens" in totals:
                metrics["cached_content_tokens"] = totals["cached_content_tokens"]
            else:
                metrics["cache_hit_tokens"] = totals["cache_read_tokens"]
                metrics["cache_write_tokens"] = totals["cache_write_tokens"]
            metrics["by_source"] = call_log.by_source()
            metrics["llm_calls"] = [c.to_dict() for c in call_log.calls]

        mismatch = self.capture_mismatches.get(module)
        if mismatch is not None:
            metrics["capture_mismatch"] = mismatch

        return metrics

    def get_metrics(self) -> dict:
        """Aggregate metrics across all turns."""
        total_cost = sum(t.cost for t in self.turns if t.role == "assistant")
        total_prompt = sum(t.prompt_tokens for t in self.turns if t.role == "assistant")
        total_completion = sum(
            t.completion_tokens for t in self.turns if t.role == "assistant"
        )
        total_thinking = sum(
            t.thinking_tokens for t in self.turns if t.role == "assistant"
        )

        per_stage: dict = {}
        for t in self.turns:
            if t.role != "assistant":
                continue
            if t.stage not in per_stage:
                per_stage[t.stage] = {
                    "cost": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "thinking_tokens": 0,
                }
            per_stage[t.stage]["cost"] += t.cost
            per_stage[t.stage]["prompt_tokens"] += t.prompt_tokens
            per_stage[t.stage]["completion_tokens"] += t.completion_tokens
            per_stage[t.stage]["thinking_tokens"] += t.thinking_tokens

        result: dict = {
            "total_cost": total_cost + self.summarizer_costs.total_cost,
            "total_prompt_tokens": total_prompt
            + self.summarizer_costs.total_prompt_tokens,
            "total_completion_tokens": total_completion
            + self.summarizer_costs.total_completion_tokens,
            "total_thinking_tokens": total_thinking,
            "per_stage": per_stage,
            **self.summarizer_costs.to_dict(),
        }

        if self.module_llm_calls:
            aggregated: dict[str, dict[str, Any]] = {}
            grand_cost = 0.0
            grand_prompt = 0
            grand_completion = 0
            grand_thinking = 0
            grand_cache_read = 0
            grand_cache_write = 0
            grand_cached_content = 0
            grand_calls = 0
            all_vertex = True
            any_calls = False
            for log in self.module_llm_calls.values():
                for src, b in log.by_source().items():
                    a = aggregated.setdefault(src, {})
                    for k, v in b.items():
                        a[k] = a.get(k, 0) + v if isinstance(v, (int, float)) else v
                totals = log.grand_totals()
                grand_calls += totals["calls"]
                grand_cost += totals["cost_usd"]
                grand_prompt += totals["prompt_tokens"]
                grand_completion += totals["completion_tokens"]
                grand_thinking += totals["thinking_tokens"]
                if "cached_content_tokens" in totals:
                    grand_cached_content += totals["cached_content_tokens"]
                else:
                    grand_cache_read += totals["cache_read_tokens"]
                    grand_cache_write += totals["cache_write_tokens"]
                    if totals["calls"] > 0:
                        all_vertex = False
                if totals["calls"] > 0:
                    any_calls = True

            result["total_cost"] = grand_cost
            result["total_prompt_tokens"] = grand_prompt
            result["total_completion_tokens"] = grand_completion
            result["total_thinking_tokens"] = grand_thinking
            if any_calls and all_vertex:
                result["cached_content_tokens"] = grand_cached_content
            else:
                result["cache_hit_tokens"] = grand_cache_read
                result["cache_write_tokens"] = grand_cache_write
                if grand_cached_content > 0:
                    result["cached_content_tokens"] = grand_cached_content
            result["total_llm_calls"] = grand_calls
            result["by_source"] = aggregated

        if self.capture_mismatches:
            result["capture_mismatches"] = dict(self.capture_mismatches)

        return result
