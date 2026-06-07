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
