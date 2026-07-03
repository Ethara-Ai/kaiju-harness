"""Harsh tests for ground-truth applied-edit capture (agent/edit_capture.py) and
its consumption by the OpenHands formatter.

Covers: the dry-run gate, turn attribution, accumulation, robustness to malformed
input / missing capture, the never-break-the-edit-path guarantee, and the
consumer's ground-truth-vs-text-parser fallback (including the key win: an empty
applied-edit list must NOT be overridden by a false-positive text parse)."""

from __future__ import annotations


from agent import edit_capture
from agent.edit_capture import (
    edits_to_records,
    install_edit_capture,
    record_applied_edits,
    _make_wrapped_apply_edits,
)
from agent.thinking_capture import ThinkingCapture, Turn


# --------------------------------------------------------------------------
# edits_to_records — normalization + robustness
# --------------------------------------------------------------------------
class TestEditsToRecords:
    def test_basic_tuple(self):
        assert edits_to_records([("src/a.rs", "old", "new")]) == [
            {"path": "src/a.rs", "old_str": "old", "new_str": "new"}
        ]

    def test_new_file_empty_original(self):
        # A new-file edit legitimately has empty old_str — must be kept.
        assert edits_to_records([("src/new.rs", "", "body")]) == [
            {"path": "src/new.rs", "old_str": "", "new_str": "body"}
        ]

    def test_skips_malformed_and_pathless(self):
        got = edits_to_records([("p", "o", "n"), ("only_one",), None, 42, ("", "x", "y")])
        assert got == [{"path": "p", "old_str": "o", "new_str": "n"}]

    def test_none_and_empty_input(self):
        assert edits_to_records(None) == []
        assert edits_to_records([]) == []

    def test_non_string_fields_coerced(self):
        # None original/updated -> "" ; non-str -> str
        assert edits_to_records([("p", None, None)]) == [
            {"path": "p", "old_str": "", "new_str": ""}
        ]

    def test_wholefile_tuple_shape(self):
        # WholeFileCoder: (path, fname_source, new_lines_LIST) -> whole-file write,
        # empty old_str, content = joined lines.
        assert edits_to_records([("replica.rs", None, ["fn a() {}\n", "fn b() {}\n"])]) == [
            {"path": "replica.rs", "old_str": "", "new_str": "fn a() {}\nfn b() {}\n"}
        ]

    def test_mixed_shapes_in_one_list(self):
        got = edits_to_records([("a.rs", "o", "n"), ("b.rs", "src", ["whole\n"])])
        assert got == [
            {"path": "a.rs", "old_str": "o", "new_str": "n"},
            {"path": "b.rs", "old_str": "", "new_str": "whole\n"},
        ]


# --------------------------------------------------------------------------
# record_applied_edits — attribution + safety
# --------------------------------------------------------------------------
class _Coder:
    def __init__(self, tc=None):
        self._thinking_capture = tc


def _tc_with(*roles):
    tc = ThinkingCapture()
    for r in roles:
        tc.turns.append(Turn(role=r, content=r))
    return tc


class TestRecordAppliedEdits:
    def test_attaches_to_last_assistant_turn_not_user(self):
        tc = _tc_with("user", "assistant", "user")  # trailing user turn
        record_applied_edits(_Coder(tc), [("a.rs", "o", "n")])
        # attached to the assistant turn (index 1), not the trailing user turn
        assert tc.turns[1].applied_edits == [{"path": "a.rs", "old_str": "o", "new_str": "n"}]
        assert tc.turns[0].applied_edits is None
        assert tc.turns[2].applied_edits is None

    def test_accumulates_across_calls_same_turn(self):
        tc = _tc_with("user", "assistant")
        c = _Coder(tc)
        record_applied_edits(c, [("a.rs", "o1", "n1")])
        record_applied_edits(c, [("b.rs", "o2", "n2")])
        assert [e["path"] for e in tc.turns[-1].applied_edits] == ["a.rs", "b.rs"]

    def test_empty_edits_records_empty_list_not_none(self):
        # KEY: a turn that applied ZERO edits must record [] (authoritative), not None.
        tc = _tc_with("user", "assistant")
        record_applied_edits(_Coder(tc), [])
        assert tc.turns[-1].applied_edits == []

    def test_no_thinking_capture_is_noop(self):
        record_applied_edits(_Coder(None), [("a", "o", "n")])  # must not raise

    def test_no_turns_is_noop(self):
        tc = ThinkingCapture()
        record_applied_edits(_Coder(tc), [("a", "o", "n")])  # must not raise

    def test_no_assistant_turn_is_noop(self):
        tc = _tc_with("user", "user")
        record_applied_edits(_Coder(tc), [("a", "o", "n")])
        assert all(t.applied_edits is None for t in tc.turns)


# --------------------------------------------------------------------------
# the apply_edits wrapper — dry-run gate + never-break guarantee
# --------------------------------------------------------------------------
class TestApplyEditsWrapper:
    def _fake_cls(self):
        class _FakeCoder:
            def __init__(self):
                self._thinking_capture = None
                self.calls = []

            def apply_edits(self, edits, dry_run=False):
                self.calls.append(dry_run)
                return ("orig", dry_run)

        return _FakeCoder

    def test_records_only_on_real_call_not_dry_run(self):
        Cls = self._fake_cls()
        assert install_edit_capture(Cls) is True
        tc = _tc_with("user", "assistant")
        c = Cls(); c._thinking_capture = tc

        # dry-run probe first — must NOT record
        c.apply_edits([("a.rs", "o", "n")], dry_run=True)
        assert tc.turns[-1].applied_edits is None

        # real call — must record, and still return the original result
        res = c.apply_edits([("a.rs", "o", "n")], dry_run=False)
        assert res == ("orig", False)
        assert tc.turns[-1].applied_edits == [{"path": "a.rs", "old_str": "o", "new_str": "n"}]
        assert c.calls == [True, False]  # original called both times

    def test_wrapper_never_breaks_edit_path_if_record_raises(self, monkeypatch):
        # If capture blows up, the edit still applies and the result is returned.
        def _boom(coder, edits):
            raise RuntimeError("capture bug")

        monkeypatch.setattr(edit_capture, "record_applied_edits", _boom)
        called = {}

        def _orig(self, edits, dry_run=False):
            called["ran"] = True
            return "applied"

        wrapped = _make_wrapped_apply_edits(_orig)
        out = wrapped(object(), [("a", "o", "n")], dry_run=False)
        assert out == "applied" and called["ran"] is True  # edit path intact

    def test_install_is_idempotent(self):
        Cls = self._fake_cls()
        install_edit_capture(Cls)
        first = Cls.apply_edits
        install_edit_capture(Cls)  # second call must NOT double-wrap
        assert Cls.apply_edits is first
        assert getattr(Cls.apply_edits, "_kaiju_original", None) is not None

    def test_installs_on_both_real_coders(self):
        # Must patch BOTH the diff coder and the whole-file coder — the rust
        # pipeline runs WholeFileCoder because claude-opus-4-8 is newer than
        # aider's registry (default edit_format falls back to "whole").
        assert install_edit_capture() is True
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.coders.wholefile_coder import WholeFileCoder
        assert getattr(EditBlockCoder.apply_edits, "_kaiju_original", None) is not None
        assert getattr(WholeFileCoder.apply_edits, "_kaiju_original", None) is not None


# --------------------------------------------------------------------------
# consumer: _convert_assistant_turn prefers ground truth, falls back to parser
# --------------------------------------------------------------------------
class TestFormatterUsesGroundTruth:
    def _edits_in(self, turn):
        from agent.openhands_formatter import _convert_assistant_turn
        events = _convert_assistant_turn(turn, base_timestamp="2026-01-01T00:00:00+00:00")
        # collect file_editor edits from the action events
        paths = []
        for e in events:
            act = (e.get("action") or {})
            if act.get("path"):
                paths.append(act["path"])
        return paths

    def test_uses_applied_edits_when_present(self):
        t = Turn(role="assistant", content="I edited things.", module="m", stage="draft")
        t.applied_edits = [{"path": "src/real.rs", "old_str": "a", "new_str": "b"}]
        assert self._edits_in(t) == ["src/real.rs"]

    def test_empty_applied_edits_yields_no_edits_even_if_text_looks_like_one(self):
        # The KEY win: text that a heuristic parser might mis-read as an edit is
        # overridden by the authoritative empty capture -> zero fabricated edits.
        content = (
            "Here's how the file looks:\n"
            "config.yaml\n"
            "```\nkey: value\n```\n"
        )
        t = Turn(role="assistant", content=content, module="m", stage="draft")
        t.applied_edits = []  # aider applied nothing
        assert self._edits_in(t) == []

    def test_falls_back_to_parser_when_not_captured(self):
        # applied_edits is None -> use the text parser (real SEARCH/REPLACE block)
        content = (
            "src/x.rs\n"
            "```rust\n"
            "<<<<<<< SEARCH\n"
            "old line\n"
            "=======\n"
            "new line\n"
            ">>>>>>> REPLACE\n"
            "```\n"
        )
        t = Turn(role="assistant", content=content, module="m", stage="draft")
        assert t.applied_edits is None
        assert self._edits_in(t) == ["src/x.rs"]

    def test_ground_truth_overrides_parser_disagreement(self):
        # content has a SEARCH/REPLACE for x.rs, but aider actually applied y.rs.
        # Ground truth (y.rs) must win.
        content = (
            "src/x.rs\n```\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n```\n"
        )
        t = Turn(role="assistant", content=content, module="m", stage="draft")
        t.applied_edits = [{"path": "src/y.rs", "old_str": "a", "new_str": "b"}]
        assert self._edits_in(t) == ["src/y.rs"]


class TestRealEditBlockCoderIntegration:
    """End-to-end against a real aider EditBlockCoder writing a real file."""

    def test_real_apply_edits_records_ground_truth_and_writes(self, tmp_path):
        install_edit_capture()
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.io import InputOutput

        fpath = tmp_path / "a.txt"
        fpath.write_text("line one\nline two\nline three\n")

        coder = EditBlockCoder.__new__(EditBlockCoder)
        coder.root = str(tmp_path)
        coder.io = InputOutput(yes=True)
        coder.abs_fnames = {str(fpath)}
        coder.fence = ("```", "```")
        coder.dry_run = False
        coder.abs_root_path_cache = {}
        coder.abs_read_only_fnames = set()

        tc = ThinkingCapture()
        tc.turns.append(Turn(role="user", content="edit it"))
        tc.turns.append(Turn(role="assistant", content="editing line two"))
        coder._thinking_capture = tc

        edits = [("a.txt", "line two\n", "LINE TWO CHANGED\n")]

        coder.apply_edits(edits, dry_run=True)     # dry-run probe: no record
        assert tc.turns[-1].applied_edits is None

        coder.apply_edits(edits, dry_run=False)     # real: records + writes
        assert "LINE TWO CHANGED" in fpath.read_text()
        assert tc.turns[-1].applied_edits == [
            {"path": "a.txt", "old_str": "line two\n", "new_str": "LINE TWO CHANGED\n"}
        ]


class TestRealWholeFileCoderIntegration:
    """End-to-end against a real aider WholeFileCoder — the coder the rust pipeline
    actually runs (claude-opus-4-8 -> default 'whole' edit_format). This is the case
    the original EditBlockCoder-only patch silently missed."""

    def test_real_wholefile_apply_edits_records_ground_truth_and_writes(self, tmp_path):
        install_edit_capture()
        from aider.coders.wholefile_coder import WholeFileCoder
        from aider.io import InputOutput

        fpath = tmp_path / "replica.rs"
        fpath.write_text('fn x() { panic!("STUB: not implemented") }\n')

        coder = WholeFileCoder.__new__(WholeFileCoder)
        coder.root = str(tmp_path)
        coder.io = InputOutput(yes=True)
        coder.abs_fnames = {str(fpath)}
        coder.abs_read_only_fnames = set()
        coder.abs_root_path_cache = {}
        coder.dry_run = False

        tc = ThinkingCapture()
        tc.turns.append(Turn(role="user", content="implement x"))
        tc.turns.append(Turn(role="assistant", content="here is the whole file"))
        coder._thinking_capture = tc

        # WholeFileCoder edit tuple: (path, fname_source, new_lines_list)
        coder.apply_edits([("replica.rs", None, ["fn x() { 42 }\n"])])

        assert fpath.read_text() == "fn x() { 42 }\n"          # whole file written
        assert tc.turns[-1].applied_edits == [
            {"path": "replica.rs", "old_str": "", "new_str": "fn x() { 42 }\n"}
        ]


class TestPartialFailureCapture:
    """The bug the live little-raft run exposed: aider's apply_edits RAISES when a
    SEARCH block doesn't match, and recording input edits after the call missed
    everything -> parser fallback showed failed attempts as phantom edits. The fix
    records only files aider ACTUALLY wrote (io.write_text hook), surviving the raise."""

    def _coder(self, tmp_path, files):
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.io import InputOutput
        abs_fnames = set()
        for name, content in files.items():
            p = tmp_path / name
            p.write_text(content)
            abs_fnames.add(str(p))
        c = EditBlockCoder.__new__(EditBlockCoder)
        c.root = str(tmp_path); c.io = InputOutput(yes=True)
        c.abs_fnames = abs_fnames; c.abs_read_only_fnames = set()
        c.fence = ("```", "```"); c.dry_run = False; c.abs_root_path_cache = {}
        return c

    def _turn(self, coder):
        tc = ThinkingCapture()
        tc.turns.append(Turn(role="user", content="x"))
        tc.turns.append(Turn(role="assistant", content="y"))
        coder._thinking_capture = tc
        return tc

    def test_mixed_pass_fail_records_only_applied(self, tmp_path):
        install_edit_capture()
        c = self._coder(tmp_path, {"timer.rs": "fn n() { OLD }\n", "replica.rs": "fn r() { OLD }\n"})
        tc = self._turn(c)
        edits = [("timer.rs", "fn n() { OLD }", "fn n() { NEW }"),      # matches -> applied
                 ("replica.rs", "fn r() { NOMATCH }", "fn r() { NEW }")]  # no match -> raises
        try:
            c.apply_edits(edits, dry_run=False)
        except ValueError:
            pass
        assert tc.turns[-1].applied_edits is not None
        assert [e["path"] for e in tc.turns[-1].applied_edits] == ["timer.rs"]
        assert (tmp_path / "replica.rs").read_text() == "fn r() { OLD }\n"  # unchanged

    def test_all_fail_records_empty_not_none(self, tmp_path):
        # [] (authoritative "nothing applied") must be set so the formatter does
        # NOT fall back to the phantom-producing text parser.
        install_edit_capture()
        c = self._coder(tmp_path, {"replica.rs": "fn r() { OLD }\n"})
        tc = self._turn(c)
        try:
            c.apply_edits([("replica.rs", "NOMATCH", "X")], dry_run=False)
        except ValueError:
            pass
        assert tc.turns[-1].applied_edits == []

    def test_all_pass_records_all_granular(self, tmp_path):
        install_edit_capture()
        c = self._coder(tmp_path, {"a.rs": "fn a() { OLD }\n", "b.rs": "fn b() { OLD }\n"})
        tc = self._turn(c)
        edits = [("a.rs", "fn a() { OLD }", "fn a() { NEW }"),
                 ("b.rs", "fn b() { OLD }", "fn b() { NEW }")]
        c.apply_edits(edits, dry_run=False)
        assert sorted(e["path"] for e in tc.turns[-1].applied_edits) == ["a.rs", "b.rs"]


class TestFullRustWiringIntegration:
    """Definitive end-to-end: the SAME wiring the rust pipeline uses
    (capture_module_calls installs the hook; _apply_thinking_capture_patches sets
    _thinking_capture) must yield an output.json whose history reflects only the
    edits aider ACTUALLY wrote — not the model's failed attempts."""

    def test_full_wiring_drops_failed_edit_no_phantom(self, tmp_path):
        import json
        from agent.llm_cost_capture import capture_module_calls
        from agent.agents import _apply_thinking_capture_patches
        from agent.openhands_formatter import write_module_output_json
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.io import InputOutput

        fa = tmp_path / "timer.rs"; fb = tmp_path / "replica.rs"
        fa.write_text("fn n() { OLD }\n"); fb.write_text("fn r() { OLD }\n")
        tc = ThinkingCapture()
        with capture_module_calls(thinking_capture=tc, module="mod", model_short="m"):
            coder = EditBlockCoder.__new__(EditBlockCoder)
            coder.root = str(tmp_path); coder.io = InputOutput(yes=True)
            coder.abs_fnames = {str(fa), str(fb)}; coder.abs_read_only_fnames = set()
            coder.fence = ("```", "```"); coder.dry_run = False; coder.abs_root_path_cache = {}
            coder.partial_response_content = "reasoning"
            _apply_thinking_capture_patches(coder, tc, current_stage="draft", current_module="mod")
            tc.add_assistant_turn(content="reasoning", thinking=None, thinking_tokens=0,
                prompt_tokens=0, completion_tokens=0, cache_hit_tokens=0, cache_write_tokens=0,
                cost=0.0, stage="draft", module="mod", turn_number=0)
            try:
                coder.apply_edits([("timer.rs", "fn n() { OLD }", "fn n() { NEW }"),
                                   ("replica.rs", "fn r() { NOMATCH }", "fn r() { NEW }")], dry_run=False)
            except ValueError:
                pass

        out = tmp_path / "out"
        write_module_output_json(output_dir=str(out), module_turns=tc.get_module_turns("mod"),
            module="m", instance_id="i", git_patch="", instruction="", metadata={},
            metrics=tc.get_module_metrics("mod"), stage="draft")
        rec = json.load(open(out / "output.json"))
        paths = [e.get("action", {}).get("path") for e in rec["history"] if e.get("action", {}).get("path")]
        assert paths == ["timer.rs"], paths


class TestEarlyReturnNoPhantom:
    """Regression for the live little-raft phantom (RCA: aider's send_message can
    return BEFORE apply_updates — add-files reflection / max_tokens / interrupted —
    so apply_edits, and thus edit-capture, never runs for that turn). Under active
    capture such a turn must read as '0 edits applied' (applied_edits == []), NOT
    fall back to the parser which fabricates the model's un-applied SEARCH block."""

    def _turn(self, tc, content):
        tc.add_assistant_turn(content=content, thinking=None, thinking_tokens=0,
            prompt_tokens=0, completion_tokens=0, cache_hit_tokens=0,
            cache_write_tokens=0, cost=0.0, stage="draft", module="m", turn_number=0)
        return tc.turns[-1]

    _CONTENT = (
        "I'll implement replica.rs; note it references timer.rs.\n"
        "little_raft/src/replica.rs\n```rust\n"
        "<<<<<<< SEARCH\n    pub fn new() { panic!(\"STUB\") }\n"
        "=======\n    pub fn new() { Replica {} }\n>>>>>>> REPLACE\n```\n"
    )

    def _paths(self, turn):
        from agent.openhands_formatter import _convert_assistant_turn
        ev = _convert_assistant_turn(turn, "2026-01-01T00:00:00+00:00")
        return [e.get("action", {}).get("path") for e in ev if e.get("action", {}).get("path")]

    def test_active_capture_turn_starts_empty_not_none(self):
        tc = ThinkingCapture(); tc.edit_capture_active = True
        assert self._turn(tc, "x").applied_edits == []          # [] not None

    def test_inactive_capture_turn_starts_none(self):
        tc = ThinkingCapture()                                   # default inactive
        assert self._turn(tc, "x").applied_edits is None         # legacy -> parser

    def test_early_return_yields_no_phantom(self):
        # THE bug: active capture, turn captured, apply_edits NEVER called.
        tc = ThinkingCapture(); tc.edit_capture_active = True
        turn = self._turn(tc, self._CONTENT)                     # no apply_edits call
        assert turn.applied_edits == []
        assert self._paths(turn) == []                           # phantom eliminated

    def test_inactive_capture_still_uses_parser(self):
        # Legacy behavior preserved for uncaptured data.
        tc = ThinkingCapture()
        turn = self._turn(tc, self._CONTENT)
        assert turn.applied_edits is None
        assert self._paths(turn) == ["little_raft/src/replica.rs"]  # parser extracts

    def test_active_capture_with_real_apply_records_and_shows_it(self, tmp_path):
        # Active capture + apply_edits fires -> [] gets extended with the real edit.
        install_edit_capture()
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.io import InputOutput
        fp = tmp_path / "a.rs"; fp.write_text("fn a() { OLD }\n")
        tc = ThinkingCapture(); tc.edit_capture_active = True
        turn = self._turn(tc, "edit a.rs")
        assert turn.applied_edits == []
        c = EditBlockCoder.__new__(EditBlockCoder)
        c.root = str(tmp_path); c.io = InputOutput(yes=True); c.abs_fnames = {str(fp)}
        c.abs_read_only_fnames = set(); c.fence = ("```", "```"); c.dry_run = False
        c.abs_root_path_cache = {}; c._thinking_capture = tc
        c.apply_edits([("a.rs", "fn a() { OLD }", "fn a() { NEW }")], dry_run=False)
        assert [e["path"] for e in turn.applied_edits] == ["a.rs"]  # [] extended

    def test_full_wiring_early_return_end_to_end(self, tmp_path):
        # The definitive end-to-end: capture_module_calls sets the flag, a turn is
        # captured with a SEARCH block, apply_edits NEVER fires -> output.json has
        # zero edit events (no phantom).
        import json
        from agent.llm_cost_capture import capture_module_calls
        from agent.openhands_formatter import write_module_output_json
        tc = ThinkingCapture()
        with capture_module_calls(thinking_capture=tc, module="m", model_short="ms"):
            assert tc.edit_capture_active is True
            self._turn(tc, self._CONTENT)                        # early-return: no apply
        out = tmp_path / "out"
        write_module_output_json(output_dir=str(out), module_turns=tc.get_module_turns("m"),
            module="m", instance_id="i", git_patch="", instruction="", metadata={},
            metrics=tc.get_module_metrics("m"), stage="draft")
        rec = json.load(open(out / "output.json"))
        paths = [e.get("action", {}).get("path") for e in rec["history"] if e.get("action", {}).get("path")]
        assert paths == []


class TestWrittenSafetyNet:
    """The write-hook records path->(display, content). record_applied_edits must:
    keep granular input edits for written files, add a whole-file record for any
    written file an input edit didn't cover (never miss), and drop unwritten edits."""

    def _tc(self):
        tc = ThinkingCapture()
        tc.turns.append(Turn(role="user", content="x"))
        tc.turns.append(Turn(role="assistant", content="y"))
        return tc

    class _Coder:
        def __init__(self, tc, root="/repo"):
            self._thinking_capture = tc; self.root = root
        def get_rel_fname(self, p):
            import os
            return os.path.relpath(p, self.root)
        def abs_root_path(self, p):
            import os
            return os.path.join(self.root, p)

    def test_written_but_unmatched_edit_added_as_wholefile(self):
        import os
        tc = self._tc(); c = self._Coder(tc)
        # input edits reference a.rs; but aider actually wrote a.rs AND b.rs
        # (b.rs has no matching input edit -> must be added from content).
        written = {
            os.path.realpath("/repo/a.rs"): ("a.rs", "AAA"),
            os.path.realpath("/repo/b.rs"): ("b.rs", "BBB"),
        }
        record_applied_edits(c, [("a.rs", "old", "AAA")], written)
        got = {r["path"]: r["new_str"] for r in tc.turns[-1].applied_edits}
        assert got == {"a.rs": "AAA", "b.rs": "BBB"}, got   # b.rs not missed

    def test_input_edit_for_unwritten_file_is_dropped(self):
        import os
        tc = self._tc(); c = self._Coder(tc)
        # edit references a.rs but nothing was written -> dropped (no phantom)
        record_applied_edits(c, [("a.rs", "old", "new")], {})
        assert tc.turns[-1].applied_edits == []

    def test_granular_kept_for_written_file(self):
        import os
        tc = self._tc(); c = self._Coder(tc)
        written = {os.path.realpath("/repo/a.rs"): ("a.rs", "whole-file-content")}
        # input edit IS matched -> keep the granular old/new, not the whole-file content
        record_applied_edits(c, [("a.rs", "OLD", "NEW")], written)
        r = tc.turns[-1].applied_edits
        assert r == [{"path": "a.rs", "old_str": "OLD", "new_str": "NEW"}]

    def test_none_written_records_all_input_edits(self):
        # write-hook unavailable -> pre-fix best-effort (all input edits).
        tc = self._tc(); c = self._Coder(tc)
        record_applied_edits(c, [("a.rs", "o", "n")], None)
        assert tc.turns[-1].applied_edits == [{"path": "a.rs", "old_str": "o", "new_str": "n"}]


class TestNoIoLeak:
    """The write_text hook must be fully removed after every apply_edits call —
    including when it raises — leaving the io object byte-for-byte as before."""

    def _coder(self, tmp_path, content='fn a() { OLD }\n'):
        from aider.coders.editblock_coder import EditBlockCoder
        from aider.io import InputOutput
        fp = tmp_path / "a.rs"; fp.write_text(content)
        io = InputOutput(yes=True)
        c = EditBlockCoder.__new__(EditBlockCoder)
        c.root = str(tmp_path); c.io = io; c.abs_fnames = {str(fp)}
        c.abs_read_only_fnames = set(); c.fence = ("```", "```")
        c.dry_run = False; c.abs_root_path_cache = {}
        tc = ThinkingCapture(); tc.edit_capture_active = True
        tc.turns.append(Turn(role="user", content="x"))
        tc.turns.append(Turn(role="assistant", content="y"))
        c._thinking_capture = tc
        return c, io, fp

    def test_hook_removed_after_success(self, tmp_path):
        install_edit_capture()
        from aider.io import InputOutput
        c, io, fp = self._coder(tmp_path)
        assert "write_text" not in io.__dict__          # class method before
        c.apply_edits([("a.rs", "fn a() { OLD }", "fn a() { NEW }")], dry_run=False)
        assert "write_text" not in io.__dict__          # no lingering shadow after
        assert io.write_text.__func__ is InputOutput.write_text

    def test_hook_removed_after_raise(self, tmp_path):
        install_edit_capture()
        from aider.io import InputOutput
        c, io, fp = self._coder(tmp_path)
        try:
            c.apply_edits([("a.rs", "WONT_MATCH", "x")], dry_run=False)  # raises
        except ValueError:
            pass
        assert "write_text" not in io.__dict__          # restored even on raise
        assert io.write_text.__func__ is InputOutput.write_text

    def test_preexisting_instance_attr_restored(self, tmp_path):
        # If write_text was already an instance attribute, restore that exact value.
        install_edit_capture()
        c, io, fp = self._coder(tmp_path)
        sentinel = io.write_text        # bind original
        io.write_text = sentinel        # make it an instance attr
        assert "write_text" in io.__dict__
        c.apply_edits([("a.rs", "fn a() { OLD }", "fn a() { NEW }")], dry_run=False)
        assert "write_text" in io.__dict__ and io.write_text is sentinel

