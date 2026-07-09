from agent.openhands_formatter import (
    _convert_file_read_turn,
    _convert_assistant_turn,
    turns_to_openhands_events,
)
from agent.thinking_capture import Turn


def _make_turn(**kwargs):
    defaults = {"role": "user", "content": "", "stage": "draft", "module": "mod"}
    defaults.update(kwargs)
    return Turn(**defaults)


class TestFileReadCapture:
    def test_view_events_from_file_read_turn(self):
        turn = _make_turn(content="[files:read]\nfoo.py\nbar.py")
        events = _convert_file_read_turn(turn, "2025-01-01T00:00:00Z")
        action_events = [e for e in events if e["kind"] == "ActionEvent"]
        obs_events = [e for e in events if e["kind"] == "ObservationEvent"]
        assert len(action_events) == 2
        assert len(obs_events) == 2
        assert action_events[0]["action"]["command"] == "view"
        assert action_events[0]["action"]["path"] == "foo.py"
        assert action_events[1]["action"]["path"] == "bar.py"
        assert obs_events[0]["observation"]["command"] == "view"
        assert not obs_events[0]["observation"]["is_error"]

    def test_empty_file_list(self):
        turn = _make_turn(content="[files:read]\n")
        events = _convert_file_read_turn(turn, "2025-01-01T00:00:00Z")
        assert len(events) == 0

    def test_single_file(self):
        turn = _make_turn(content="[files:read]\nsrc/main.py")
        events = _convert_file_read_turn(turn, "2025-01-01T00:00:00Z")
        assert len(events) == 2
        assert events[0]["action"]["path"] == "src/main.py"

    def test_file_read_dispatched_in_event_loop(self):
        turns = [
            _make_turn(content="[files:read]\nfoo.py"),
            _make_turn(content="Please fix the bug"),
        ]
        events = turns_to_openhands_events(turns)
        kinds = [e["kind"] for e in events]
        assert "ActionEvent" in kinds
        assert "MessageEvent" in kinds
        view_actions = [
            e
            for e in events
            if e["kind"] == "ActionEvent"
            and e.get("action", {}).get("command") == "view"
        ]
        assert len(view_actions) == 1

    def test_regular_user_turn_unchanged(self):
        turns = [_make_turn(content="Fix the import")]
        events = turns_to_openhands_events(turns)
        msg_events = [e for e in events if e["kind"] == "MessageEvent"]
        assert len(msg_events) == 1
        assert msg_events[0]["llm_message"]["content"][0]["text"] == "Fix the import"


class TestEditErrorCapture:
    def test_edit_error_none_by_default(self):
        turn = Turn(role="assistant", content="no edits")
        assert turn.edit_error is None

    def test_observation_no_error(self):
        content = (
            "```python\nfoo.py\n<<<<<<< SEARCH\nx=1\n=======\nx=2\n>>>>>>> REPLACE\n```"
        )
        turn = _make_turn(role="assistant", content=content, edit_error=None)
        events = _convert_assistant_turn(turn, "2025-01-01T00:00:00Z")
        obs = [e for e in events if e["kind"] == "ObservationEvent"]
        assert len(obs) == 1
        assert not obs[0]["observation"]["is_error"]

    def test_observation_with_error(self):
        content = (
            "```python\nfoo.py\n<<<<<<< SEARCH\nx=1\n=======\nx=2\n>>>>>>> REPLACE\n```"
        )
        turn = _make_turn(
            role="assistant", content=content, edit_error="malformed edit block"
        )
        events = _convert_assistant_turn(turn, "2025-01-01T00:00:00Z")
        obs = [e for e in events if e["kind"] == "ObservationEvent"]
        assert len(obs) == 1
        assert obs[0]["observation"]["is_error"] is True
        assert "malformed" in obs[0]["observation"]["content"][0]["text"]

    def test_backward_compatible(self):
        turn = Turn(role="assistant", content="test")
        assert not hasattr(turn, "_missing_field")
        assert turn.edit_error is None


class TestMonotonicTimestamps:
    """History timestamps must be non-decreasing in list order (OpenHands invariant).

    Regression for the go-multierror artifact: a `[files:read]` turn's synthetic
    file-view observation (+10ms offset on the read-turn timestamp) landed AFTER
    the real timestamp of the user-message turn that followed only a few ms later,
    producing a backwards step in the emitted history.
    """

    def _timestamps(self, events):
        from datetime import datetime

        return [
            datetime.fromisoformat(e["timestamp"])
            for e in events
            if e.get("timestamp")
        ]

    def test_file_read_then_close_user_turn_stays_monotonic(self):
        # Read turn at T; user message only 8ms later — the +10ms observation
        # offset would overrun it without the monotonic clamp.
        read_turn = _make_turn(
            content="[files:read]\nmultierror.go",
            timestamp="2026-07-09T04:11:22.669274+00:00",
        )
        msg_turn = _make_turn(
            content="Implement the stubs",
            timestamp="2026-07-09T04:11:22.677059+00:00",
        )
        events = turns_to_openhands_events([read_turn, msg_turn])
        ts = self._timestamps(events)
        assert ts == sorted(ts), "timestamps must be non-decreasing in list order"

    def test_module_boundary_and_finish_offsets_monotonic(self):
        # Two modules back-to-back: the -1ms module-boundary finish and the
        # +5000ms trailing finish must not break monotonicity.
        turns = [
            _make_turn(
                role="assistant",
                content="done a",
                module="a",
                timestamp="2026-07-09T04:11:22.000000+00:00",
            ),
            _make_turn(
                role="assistant",
                content="done b",
                module="b",
                timestamp="2026-07-09T04:11:22.000500+00:00",
            ),
        ]
        events = turns_to_openhands_events(turns)
        ts = self._timestamps(events)
        assert ts == sorted(ts)
        # And strictly increasing where clamped (no two identical adjacent).
        assert all(ts[i] <= ts[i + 1] for i in range(len(ts) - 1))

    def test_multi_edit_turn_monotonic(self):
        content = (
            "```python\nfoo.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nc\n=======\nd\n>>>>>>> REPLACE\n```"
        )
        turns = [
            _make_turn(
                role="assistant",
                content=content,
                timestamp="2026-07-09T04:11:22.000000+00:00",
            ),
            _make_turn(
                content="next",
                timestamp="2026-07-09T04:11:22.000050+00:00",
            ),
        ]
        events = turns_to_openhands_events(turns)
        ts = self._timestamps(events)
        assert ts == sorted(ts)
