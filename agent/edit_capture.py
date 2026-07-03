"""Capture aider's ACTUAL applied edits (ground truth) instead of re-parsing the
model's response text.

The trajectory representation (OpenHands ``file_editor`` events + the ``tool_calls``
metric) was built by re-parsing the assistant message with a heuristic text parser
(``openhands_formatter.parse_edit_blocks``). That parser is ~96% accurate and, when
it over-matches, can FABRICATE an edit from prose — corrupting the published
trajectory.

Aider itself knows exactly which files/blocks it applied. We class-patch the
``apply_edits`` of BOTH coder families it may pick (idempotent, mirroring
``llm_cost_capture``'s litellm wrapper) so EVERY coder, in EVERY language, records
the real edits onto the current assistant turn; ``openhands_formatter`` prefers
these when present and falls back to the text parser otherwise:

  - ``EditBlockCoder`` (edit_format "diff"): ``(path, original, updated)`` tuples.
  - ``WholeFileCoder`` (edit_format "whole"): ``(path, fname_source, new_lines)``.

Patching whole-file matters: for a model ID newer than the installed aider's
registry (e.g. ``claude-opus-4-8``), aider falls back to the default "whole"
format, so the rust pipeline actually runs ``WholeFileCoder`` — capturing only
``EditBlockCoder`` would silently never fire.

Consistency: the patch is installed from ``llm_cost_capture.capture_module_calls``
— the single shared choke point every language's runner already wraps ``agent.run``
with — so there is NO per-language wiring and the drifted per-language
``_apply_thinking_capture_patches`` copies are untouched.

Best-effort: if aider is absent or its API changed, installation silently no-ops and
the text-parser fallback remains, so a bad hook can never break a run.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

# Aider coder classes we know how to capture. IMPORTANT: the coder used at runtime
# depends on main_model.edit_format — and for a model ID newer than the installed
# aider's registry (e.g. claude-opus-4-8) aider FALLS BACK to the default
# "whole" format => WholeFileCoder, NOT EditBlockCoder. So we must patch BOTH the
# SEARCH/REPLACE coder ("diff") and the whole-file coder ("whole"); their
# subclasses (fenced / editor-*) inherit apply_edits and are covered transitively.
_CODER_IMPORTS = (
    ("aider.coders.editblock_coder", "EditBlockCoder"),
    ("aider.coders.wholefile_coder", "WholeFileCoder"),
)

_INSTALLED = False


def _abs_of(coder: Any, path: Any) -> str:
    """Resolve a path (an io.write_text target OR an edit's repo-relative path) to a
    canonical absolute path, so writes and edits can be matched regardless of
    symlinks (e.g. macOS /var -> /private/var) or relative vs absolute form.

    Prefers aider's own ``abs_root_path`` — the exact function aider uses to turn an
    edit path into the file it writes — so an edit and its write resolve identically.
    """
    import os

    if not path:
        return ""
    s = str(path)
    if not os.path.isabs(s):
        ar = getattr(coder, "abs_root_path", None)
        if callable(ar):
            try:
                s = str(ar(s))
            except Exception:  # noqa: BLE001
                root = getattr(coder, "root", "") or ""
                s = os.path.join(str(root), s)
        else:
            root = getattr(coder, "root", "") or ""
            s = os.path.join(str(root), s)
    try:
        return os.path.realpath(s)
    except Exception:  # noqa: BLE001
        return os.path.normpath(s)


def _rel_of(coder: Any, path: Any) -> str:
    """Repo-relative display path for a file aider wrote (for the edit record)."""
    import os

    if not path:
        return ""
    get_rel = getattr(coder, "get_rel_fname", None)
    if callable(get_rel):
        try:
            return str(get_rel(str(path)))
        except Exception:  # noqa: BLE001
            pass
    root = getattr(coder, "root", None)
    if root:
        try:
            return os.path.relpath(str(path), str(root))
        except Exception:  # noqa: BLE001
            pass
    return str(path)


def _make_wrapped_apply_edits(original_apply_edits: Any) -> Any:
    """Build a signature-agnostic ``apply_edits`` wrapper around the original.

    Coders differ: ``EditBlockCoder.apply_edits(self, edits, dry_run=False)`` is
    called once with ``dry_run=True`` (the probe) then once real; ``WholeFileCoder
    .apply_edits(self, edits)`` has NO dry_run and is called once (real). We accept
    ``*args, **kwargs`` so both work.

    GROUND TRUTH via actual writes: aider's ``apply_edits`` RAISES the moment any
    SEARCH block fails to exactly match (the model routinely guesses the stub
    content), and it writes the passing edits to disk BEFORE raising. Recording the
    input edits *after* the call would therefore miss everything on a partial/total
    failure — and the formatter would fall back to the text parser, showing the
    failed attempts as if applied (phantom edits). Instead we hook ``self.io
    .write_text`` for the duration of the call to record which files were ACTUALLY
    written (path -> content), in a ``finally`` so it survives the raise. A turn
    where nothing was written records ``[]`` (authoritative "no edits applied"),
    which suppresses the phantom-producing parser fallback.

    ``written`` maps the resolved absolute path to ``(display_path, content)`` so
    ``record_applied_edits`` can (a) keep the granular input edit for a written file
    when we understand its tuple shape, and (b) fall back to a whole-file record
    built from the captured content for any written file we can't otherwise attribute
    — guaranteeing an applied file is NEVER dropped, for ANY coder.
    """

    def _wrapped_apply_edits(self: Any, edits: Any, *args: Any, **kwargs: Any) -> Any:
        dry_run = kwargs.get("dry_run", args[0] if args else False)
        if dry_run:
            return original_apply_edits(self, edits, *args, **kwargs)

        written: dict[str, tuple[str, str]] = {}
        hooked = False
        io = getattr(self, "io", None)
        orig_write = getattr(io, "write_text", None) if io is not None else None
        # Remember whether write_text was an INSTANCE attribute (vs a class method)
        # so we can restore precisely: delete our shadow if it wasn't, else put the
        # original instance value back. Prevents leaving a lingering instance attr.
        had_instance_attr = io is not None and "write_text" in getattr(io, "__dict__", {})
        if callable(orig_write):
            def _capturing_write(fname: Any, content: Any = "", *a: Any, **k: Any) -> Any:
                try:
                    display = _rel_of(self, fname)
                    written[_abs_of(self, fname)] = (
                        display,
                        content if isinstance(content, str) else str(content or ""),
                    )
                except Exception:  # noqa: BLE001
                    pass
                return orig_write(fname, content, *a, **k)

            try:
                io.write_text = _capturing_write  # type: ignore[attr-defined]
                hooked = True
            except Exception:  # noqa: BLE001 — some IO objects may be read-only
                hooked = False

        try:
            return original_apply_edits(self, edits, *args, **kwargs)
        finally:
            if hooked:
                try:
                    if had_instance_attr:
                        io.write_text = orig_write  # type: ignore[attr-defined]
                    else:
                        # write_text was a class method; remove our instance shadow
                        # so the object is byte-for-byte what it was before.
                        del io.write_text  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    # Last resort: at least point it back at the original callable.
                    try:
                        io.write_text = orig_write  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
            try:
                # written=None when we couldn't hook writes -> best-effort
                # (record all input edits, the pre-fix behavior).
                record_applied_edits(self, edits, written if hooked else None)
            except Exception:  # noqa: BLE001 — capture must never break the edit path
                _logger.debug("edit capture: record failed", exc_info=True)

    _wrapped_apply_edits._kaiju_original = original_apply_edits  # type: ignore[attr-defined]
    return _wrapped_apply_edits


def _patch_class(coder_cls: Any) -> None:
    """Idempotently wrap one coder class's apply_edits (no double-wrap)."""
    if getattr(coder_cls.apply_edits, "_kaiju_original", None) is None:
        coder_cls.apply_edits = _make_wrapped_apply_edits(coder_cls.apply_edits)  # type: ignore[assignment]
        _logger.debug("edit capture: installed on %s.apply_edits", coder_cls.__name__)


def install_edit_capture(coder_cls: Any = None) -> bool:
    """Idempotently class-patch the relevant aider coders' ``apply_edits`` to
    record the real applied edits onto the coder's current assistant turn.

    With no argument, patches every class in ``_CODER_IMPORTS`` (both the
    SEARCH/REPLACE and whole-file coders). Pass an explicit ``coder_cls`` to patch
    a single class (used by tests). Safe to call many times / from many places
    (all languages call it via ``capture_module_calls``). Returns True if at least
    one target class is patched, False if aider was unavailable.
    """
    global _INSTALLED
    if coder_cls is not None:
        _patch_class(coder_cls)  # explicit target (tests); does not touch the guard
        return True

    if _INSTALLED:
        return True
    patched_any = False
    for mod_name, cls_name in _CODER_IMPORTS:
        try:
            mod = __import__(mod_name, fromlist=[cls_name])
            _patch_class(getattr(mod, cls_name))
            patched_any = True
        except Exception:  # noqa: BLE001 — a missing coder just narrows coverage
            _logger.debug("edit capture: %s.%s unavailable", mod_name, cls_name, exc_info=True)
    if patched_any:
        _INSTALLED = True
    else:
        _logger.debug("edit capture: no aider coder available; using text-parser fallback")
    return patched_any


def edits_to_records(edits: Any) -> list[dict]:
    """Normalize an aider edit list into ``{"path", "old_str", "new_str"}`` records.

    Handles both coder tuple shapes:
      - EditBlockCoder:  ``(path, original_str, updated_str)``  -> old_str/new_str
      - WholeFileCoder:  ``(path, fname_source, new_lines_list)`` -> whole-file write
        (3rd element is a list of lines); old_str is empty because a whole-file
        write has no in-place "before" text.

    Skips malformed tuples and edits with no path. A NEW-file edit legitimately
    has an empty ``old_str``.
    """
    records: list[dict] = []
    for edit in edits or []:
        try:
            path, second, third = edit[0], edit[1], edit[2]
        except (TypeError, IndexError, ValueError, KeyError):
            continue
        if not path:
            continue
        if isinstance(third, (list, tuple)):
            # Whole-file coder: content is the joined line list; no prior text.
            old_str = ""
            new_str = "".join(str(x) for x in third)
        else:
            old_str = second if isinstance(second, str) else ("" if second is None else str(second))
            new_str = third if isinstance(third, str) else ("" if third is None else str(third))
        records.append({"path": str(path), "old_str": old_str, "new_str": new_str})
    return records


def record_applied_edits(coder: Any, edits: Any, written_paths: Any = None) -> None:
    """Attach the applied edits to the coder's most-recent ASSISTANT turn.

    Aider calls ``apply_edits`` immediately AFTER ``add_assistant_reply`` captured
    the turn (verified against base_coder.send_message ordering), so the last
    assistant turn in ``thinking_capture.turns`` is the one that produced these
    edits. A turn that applies edits across multiple ``apply_edits`` calls (rare)
    accumulates them.

    ``written_paths`` — what aider ACTUALLY wrote this call, as a dict mapping the
    resolved absolute path to ``(display_path, content)`` (captured via the
    io.write_text hook). When provided (possibly empty):
      * input edits whose target file was written are kept (granular representation);
      * any written file NOT matched by an input edit (e.g. an unrecognized coder
        tuple shape) is added as a whole-file record from the captured content, so an
        applied file is never dropped;
      * failed SEARCH blocks that raised (file never written) are excluded, so no
        phantom edits.
    ``None`` means the write-hook was unavailable -> fall back to all input edits.
    """
    tc = getattr(coder, "_thinking_capture", None)
    if tc is None:
        return
    turns = getattr(tc, "turns", None)
    if not turns:
        return
    turn = None
    for t in reversed(turns):
        if getattr(t, "role", "") == "assistant":
            turn = t
            break
    if turn is None:
        return

    records = edits_to_records(edits)
    if written_paths is not None:
        kept = []
        covered: set[str] = set()
        for r in records:
            ab = _abs_of(coder, r.get("path"))
            if ab in written_paths:
                kept.append(r)
                covered.add(ab)
        # Safety net: any written file not represented by an input edit is added as
        # a whole-file record from the actually-written content (never miss an edit).
        for ab, (display, content) in written_paths.items():
            if ab not in covered:
                kept.append({"path": display or ab, "old_str": "", "new_str": content})
        records = kept

    existing = getattr(turn, "applied_edits", None)
    if existing is None:
        turn.applied_edits = records
    else:
        existing.extend(records)
