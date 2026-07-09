"""Regression: embedded per-turn history[].usage must reconcile to `metrics`.

The per-turn usage blocks emitted by the formatter come from aider's own
per-message token accounting, which diverges from the authoritative litellm
call-log that `metrics` (by_source / total_*) is built from. The formatter now
rescales history[].usage so that `Σ history[].usage == metrics.total_*` exactly,
matching what pipeline_results consumes. These tests pin that invariant.
"""

import math

from agent.openhands_formatter import (
    _reconcile_history_usage,
    format_openhands_output,
)
from agent.thinking_capture import ThinkingCapture


def _sum_history_usage(events: list[dict]) -> dict:
    out = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "thinking_tokens": 0,
        "cost_usd": 0.0,
    }
    for e in events:
        u = e.get("usage")
        if isinstance(u, dict):
            out["prompt_tokens"] += u.get("prompt_tokens", 0)
            out["completion_tokens"] += u.get("completion_tokens", 0)
            out["thinking_tokens"] += u.get("thinking_tokens", 0)
            out["cost_usd"] += u.get("cost_usd", 0.0)
    return out


def _assert_reconciled(events: list[dict], metrics: dict) -> None:
    got = _sum_history_usage(events)
    assert got["prompt_tokens"] == metrics["total_prompt_tokens"]
    assert got["completion_tokens"] == metrics["total_completion_tokens"]
    assert got["thinking_tokens"] == metrics["total_thinking_tokens"]
    assert math.isclose(got["cost_usd"], metrics["total_cost"], abs_tol=1e-9)


class TestSingleTurnDivergence:
    """The live go-multierror case: one main_loop call, aider turn-capture
    disagreed with the call-log on every column.
    """

    def _metrics(self) -> dict:
        return {
            "total_cost": 0.05327,
            "total_prompt_tokens": 9742,
            "total_completion_tokens": 152,
            "total_thinking_tokens": 97,
            "total_llm_calls": 1,
            "num_turns": 1,
            "by_source": {
                "main_loop": {
                    "calls": 1,
                    "prompt_tokens": 9742,
                    "completion_tokens": 152,
                    "thinking_tokens": 97,
                    "cost_usd": 0.05327,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            },
        }

    def _events(self) -> list[dict]:
        # Aider turn-capture values (the DIVERGENT figures actually observed).
        return [
            {
                "kind": "ActionEvent",
                "tool_name": "file_editor",
                "usage": {
                    "prompt_tokens": 9780,
                    "completion_tokens": 49,
                    "thinking_tokens": 0,
                    "cost_usd": 0.05037,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            }
        ]

    def test_reconciles_to_call_log(self):
        events = self._events()
        _reconcile_history_usage(events, self._metrics())
        _assert_reconciled(events, self._metrics())
        # The lone event now carries the authoritative figures.
        u = events[0]["usage"]
        assert u["prompt_tokens"] == 9742
        assert u["completion_tokens"] == 152
        assert u["thinking_tokens"] == 97


class TestMultiTurnAndAuxiliary:
    """Multiple main-loop turns plus a summarizer/commit-msg call that has no
    conversational turn of its own — the auxiliary spend must still land in the
    history sum so it reconciles to metrics.total_*.
    """

    def _metrics(self) -> dict:
        return {
            # main_loop 100+40 prompt, aux 60 prompt -> grand 200 prompt.
            "total_cost": 0.30,
            "total_prompt_tokens": 200,
            "total_completion_tokens": 30,
            "total_thinking_tokens": 12,
            "total_llm_calls": 3,
            "num_turns": 2,
            "by_source": {
                "main_loop": {
                    "calls": 2,
                    "prompt_tokens": 140,
                    "completion_tokens": 20,
                    "thinking_tokens": 12,
                    "cost_usd": 0.20,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
                "aider_summarizer": {
                    "calls": 1,
                    "prompt_tokens": 60,
                    "completion_tokens": 10,
                    "thinking_tokens": 0,
                    "cost_usd": 0.10,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            },
        }

    def _events(self) -> list[dict]:
        return [
            {
                "kind": "ActionEvent",
                "tool_name": "file_editor",
                "usage": {
                    "prompt_tokens": 55,
                    "completion_tokens": 8,
                    "thinking_tokens": 4,
                    "cost_usd": 0.08,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            },
            {
                "kind": "ActionEvent",
                "tool_name": "file_editor",
                "usage": {
                    "prompt_tokens": 70,
                    "completion_tokens": 9,
                    "thinking_tokens": 5,
                    "cost_usd": 0.09,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            },
        ]

    def test_grand_invariant_includes_auxiliary(self):
        events = self._events()
        metrics = self._metrics()
        _reconcile_history_usage(events, metrics)
        _assert_reconciled(events, metrics)

    def test_main_loop_events_carry_only_main_plus_aux_tail(self):
        events = self._events()
        metrics = self._metrics()
        _reconcile_history_usage(events, metrics)
        # First event holds a share of the main-loop total only.
        # Last event holds its main-loop share PLUS the folded auxiliary spend.
        first, last = events[0]["usage"], events[1]["usage"]
        # main-loop prompt 140 split across the two; last also gets +60 aux.
        assert first["prompt_tokens"] + last["prompt_tokens"] == 200
        assert last["prompt_tokens"] >= 60  # aux folded into the tail


class TestDegradedCaptureUntouched:
    """No call-log (no by_source) -> turn-derived usage is the best signal and
    must be left exactly as-is.
    """

    def test_no_by_source_is_noop(self):
        events = [
            {
                "kind": "ActionEvent",
                "tool_name": "file_editor",
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost_usd": 0.01},
            }
        ]
        before = dict(events[0]["usage"])
        _reconcile_history_usage(events, {"total_cost": 0.01})
        assert events[0]["usage"] == before


class TestNoUsageEventsButSpend:
    """Every turn was an early-return with zero tokens, but the call-log has
    spend. The grand total must be anchored so it isn't silently dropped.
    """

    def test_anchors_grand_total(self):
        events = [{"kind": "ActionEvent", "tool_name": "think"}]
        metrics = {
            "total_cost": 0.02,
            "total_prompt_tokens": 30,
            "total_completion_tokens": 4,
            "total_thinking_tokens": 0,
            "by_source": {
                "aider_commit_msg": {
                    "calls": 1,
                    "prompt_tokens": 30,
                    "completion_tokens": 4,
                    "thinking_tokens": 0,
                    "cost_usd": 0.02,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            },
        }
        _reconcile_history_usage(events, metrics)
        _assert_reconciled(events, metrics)


class TestEndToEndFormatter:
    """Through the real formatter entrypoint with real Turn objects: the emitted
    history must reconcile to the passed-in metrics.
    """

    def test_format_openhands_output_reconciles(self):
        tc = ThinkingCapture()
        tc.add_user_turn("fix it", "draft", "mod", 0)
        tc.add_assistant_turn(
            content="```python\nfoo.py\n<<<<<<< SEARCH\nx=1\n=======\nx=2\n>>>>>>> REPLACE\n```",
            thinking="reasoning",
            thinking_tokens=3,
            prompt_tokens=111,  # divergent turn-capture value
            completion_tokens=7,
            cache_hit_tokens=0,
            cache_write_tokens=0,
            cost=0.011,
            stage="draft",
            module="mod",
            turn_number=1,
        )
        metrics = {
            "total_cost": 0.05,
            "total_prompt_tokens": 500,
            "total_completion_tokens": 20,
            "total_thinking_tokens": 9,
            "total_llm_calls": 1,
            "num_turns": 1,
            "by_source": {
                "main_loop": {
                    "calls": 1,
                    "prompt_tokens": 500,
                    "completion_tokens": 20,
                    "thinking_tokens": 9,
                    "cost_usd": 0.05,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }
            },
        }
        record = format_openhands_output(
            turns=tc.turns,
            instance_id="i",
            git_patch="",
            instruction="fix it",
            metadata={},
            metrics=metrics,
        )
        _assert_reconciled(record["history"], metrics)
