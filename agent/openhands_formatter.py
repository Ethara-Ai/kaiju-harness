"""Translate aider ThinkingCapture turns into OpenHands-style event format."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.thinking_capture import Turn

logger = logging.getLogger(__name__)


@dataclass
class EditBlock:
    path: str
    old_str: str
    new_str: str


# Open-list extension match: starts with a letter, 1-8 alphanumeric chars.
# Covers every common source/config extension (.rs, .toml, .lock, .gitignore,
# .dockerfile, .conf, etc.) without requiring updates as new file types ship.
# Constrained to avoid matching prose like "see section 3.4" or numeric suffixes.
_FILENAME_RE = re.compile(
    r"^([^\s`#>][^\n]*?\.[a-zA-Z][a-zA-Z0-9]{0,7})\s*$",
    re.MULTILINE,
)

_SEARCH_MARKER = "<<<<<<< SEARCH"
_DIVIDER_MARKER = "======="
_REPLACE_MARKER = ">>>>>>> REPLACE"


_WHOLE_FILE_RE = re.compile(
    r"^([^\s`#>][^\n]*?\.[a-zA-Z][a-zA-Z0-9]{0,7})\s*\n"
    r"```\w*\n(.*?)```",
    re.MULTILINE | re.DOTALL,
)


def parse_edit_blocks(content: str) -> tuple[str, list[EditBlock]]:
    if _SEARCH_MARKER in content:
        return _parse_search_replace_blocks(content)
    return _parse_whole_file_blocks(content)


def _parse_whole_file_blocks(content: str) -> tuple[str, list[EditBlock]]:
    edit_blocks: list[EditBlock] = []
    reasoning = content

    for match in _WHOLE_FILE_RE.finditer(content):
        path = match.group(1).strip()
        new_content = match.group(2)
        edit_blocks.append(EditBlock(path=path, old_str="", new_str=new_content))
        reasoning = reasoning.replace(match.group(0), "")

    return reasoning.strip(), edit_blocks


def _parse_search_replace_blocks(content: str) -> tuple[str, list[EditBlock]]:
    if _SEARCH_MARKER not in content:
        return content.strip(), []

    edit_blocks: list[EditBlock] = []
    reasoning_parts: list[str] = []
    current_file: str | None = None

    lines = content.split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        fname_match = _FILENAME_RE.match(stripped)
        if fname_match and _has_search_block_ahead(lines, i + 1):
            current_file = fname_match.group(1).strip()
            i += 1
            continue

        if stripped.startswith("```") and _has_search_marker_in_fence(lines, i + 1):
            block_file, block_edits, end_idx = _parse_fenced_block(
                lines, i, current_file
            )
            if block_file:
                current_file = block_file
            edit_blocks.extend(block_edits)
            i = end_idx + 1
            continue

        if stripped == _SEARCH_MARKER:
            edit, end_idx = _parse_bare_search_replace(lines, i, current_file)
            if edit:
                edit_blocks.append(edit)
            i = end_idx + 1
            continue

        reasoning_parts.append(line)
        i += 1

    reasoning_text = "\n".join(reasoning_parts).strip()
    reasoning_text = _clean_reasoning(reasoning_text)
    return reasoning_text, edit_blocks


def _has_search_block_ahead(lines: list[str], start: int) -> bool:
    end = min(start + 50, len(lines))
    for i in range(start, end):
        if _SEARCH_MARKER in lines[i]:
            return True
        if (
            i > start + 2
            and lines[i].strip()
            and not lines[i].strip().startswith("```")
        ):
            if _FILENAME_RE.match(lines[i].strip()):
                return False
    return False


def _has_search_marker_in_fence(lines: list[str], start: int) -> bool:
    for i in range(start, min(start + 200, len(lines))):
        stripped = lines[i].strip()
        if stripped == _SEARCH_MARKER:
            return True
        if stripped.startswith("```") and i > start:
            return False
    return False


def _parse_fenced_block(
    lines: list[str],
    fence_start: int,
    default_file: str | None,
) -> tuple[str | None, list[EditBlock], int]:
    edits: list[EditBlock] = []
    detected_file = default_file
    i = fence_start + 1
    n = len(lines)

    while i < n:
        stripped = lines[i].strip()

        if stripped.startswith("```"):
            return detected_file, edits, i

        # Detect filename lines inside fenced blocks (e.g., "pipfile/api.py")
        fname_match = _FILENAME_RE.match(stripped)
        if fname_match and _has_search_block_ahead(lines, i + 1):
            detected_file = fname_match.group(1).strip()
            i += 1
            continue

        if stripped == _SEARCH_MARKER:
            old_lines: list[str] = []
            new_lines: list[str] = []
            i += 1
            in_old = True

            while i < n:
                inner_stripped = lines[i].strip()

                if inner_stripped.startswith("```"):
                    break
                if inner_stripped == _DIVIDER_MARKER:
                    in_old = False
                    i += 1
                    continue
                if inner_stripped == _REPLACE_MARKER:
                    break

                if in_old:
                    old_lines.append(lines[i])
                else:
                    new_lines.append(lines[i])
                i += 1

            if detected_file:
                edits.append(
                    EditBlock(
                        path=detected_file,
                        old_str="\n".join(old_lines),
                        new_str="\n".join(new_lines),
                    )
                )
            i += 1
            continue

        i += 1

    return detected_file, edits, min(i, n - 1)


def _parse_bare_search_replace(
    lines: list[str],
    start: int,
    current_file: str | None,
) -> tuple[EditBlock | None, int]:
    old_lines: list[str] = []
    new_lines: list[str] = []
    i = start + 1
    n = len(lines)
    in_old = True

    while i < n:
        stripped = lines[i].strip()

        if stripped == _DIVIDER_MARKER:
            in_old = False
            i += 1
            continue
        if stripped == _REPLACE_MARKER:
            if current_file:
                return (
                    EditBlock(
                        path=current_file,
                        old_str="\n".join(old_lines),
                        new_str="\n".join(new_lines),
                    ),
                    i,
                )
            return None, i

        if in_old:
            old_lines.append(lines[i])
        else:
            new_lines.append(lines[i])
        i += 1

    return None, min(i, n - 1)


def _clean_reasoning(text: str) -> str:
    text = re.sub(r"\n*```bash\n.*?```\s*$", "", text, flags=re.DOTALL)
    text = re.sub(r"\n*```shell\n.*?```\s*$", "", text, flags=re.DOTALL)
    return text.strip()


def _make_id() -> str:
    return str(uuid.uuid4())


def _make_timestamp_from_turn(turn: "Turn", offset_ms: int = 0) -> str:
    ts_raw = getattr(turn, "timestamp", "") or ""
    if ts_raw:
        try:
            ts = datetime.fromisoformat(ts_raw)
            if offset_ms:
                ts += timedelta(milliseconds=offset_ms)
            return ts.isoformat()
        except ValueError:
            pass
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    ts = base + timedelta(seconds=turn.turn_number * 10, milliseconds=offset_ms)
    return ts.isoformat()


def _offset_timestamp(iso_timestamp: str, offset_ms: int) -> str:
    dt = datetime.fromisoformat(iso_timestamp)
    dt += timedelta(milliseconds=offset_ms)
    return dt.isoformat()


def _make_thinking_blocks(thinking: str | None) -> list[dict]:
    if not thinking:
        return []
    return [{"type": "thinking", "text": thinking}]


def _file_editor_tool_def() -> dict:
    return {
        "name": "file_editor",
        "description": (
            "Custom editing tool for viewing, creating, and editing files. "
            "Commands: view, create, str_replace, insert."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert"],
                },
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["command", "path"],
        },
    }


def make_system_prompt_event(
    system_prompt: str,
    tools: list[dict] | None = None,
    timestamp: str | None = None,
) -> dict:
    if tools is None:
        tools = [_file_editor_tool_def()]
    return {
        "id": _make_id(),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "source": "agent",
        "system_prompt": {"type": "text", "text": system_prompt},
        "tools": tools,
        "kind": "SystemPromptEvent",
    }


def make_message_event(
    content: str,
    source: str = "user",
    timestamp: str | None = None,
) -> dict:
    return {
        "id": _make_id(),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "source": source,
        "llm_message": {
            "role": "user",
            "content": [{"type": "text", "text": content}],
            "thinking_blocks": [],
        },
        "activated_skills": [],
        "extended_content": None,
        "kind": "MessageEvent",
    }


def make_action_event(
    thought: str,
    edit: EditBlock | None = None,
    thinking_blocks: list[dict] | None = None,
    tool_call_id: str | None = None,
    timestamp: str | None = None,
    summary: str | None = None,
    llm_response_id: str | None = None,
    usage: dict | None = None,
) -> dict:
    thought_content = [{"type": "text", "text": thought}] if thought else []
    thinking = thinking_blocks or []
    ts = timestamp or datetime.now(timezone.utc).isoformat()

    if edit is not None:
        tcid = tool_call_id or _make_id()
        command = "create" if not edit.old_str else "str_replace"
        arguments = json.dumps(
            {
                "command": command,
                "path": edit.path,
                "old_str": edit.old_str,
                "new_str": edit.new_str,
            }
        )
        return {
            "id": _make_id(),
            "timestamp": ts,
            "source": "agent",
            "thought": thought_content,
            "thinking_blocks": thinking,
            "action": {
                "command": command,
                "path": edit.path,
                "old_str": edit.old_str,
                "new_str": edit.new_str,
                "kind": "FileEditorAction",
            },
            "tool_name": "file_editor",
            "tool_call_id": tcid,
            "tool_call": {
                "id": tcid,
                "name": "file_editor",
                "arguments": arguments,
                "origin": "completion",
            },
            "llm_response_id": llm_response_id,
            "security_risk": "UNKNOWN",
            "summary": summary or f"Edit {edit.path}",
            "kind": "ActionEvent",
            **({"usage": usage} if usage else {}),
        }

    tcid = _make_id()
    return {
        "id": _make_id(),
        "timestamp": ts,
        "source": "agent",
        "thought": thought_content,
        "thinking_blocks": thinking,
        "action": {"thought": thought, "kind": "ThinkAction"},
        "tool_name": "think",
        "tool_call_id": tcid,
        "tool_call": None,
        "summary": summary or "Agent thinking (no edits)",
        "kind": "ActionEvent",
        "llm_response_id": llm_response_id,
        **({"usage": usage} if usage else {}),
    }


def make_observation_event(
    edit: EditBlock,
    tool_call_id: str,
    is_error: bool = False,
    error_message: str | None = None,
    timestamp: str | None = None,
) -> dict:
    if is_error:
        obs_content = [{"type": "text", "text": error_message or "Edit failed"}]
    else:
        obs_content = [
            {
                "type": "text",
                "text": f"The file {edit.path} has been edited.",
            }
        ]
    command = "create" if not edit.old_str else "str_replace"
    return {
        "id": _make_id(),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "source": "environment",
        "tool_name": "file_editor",
        "tool_call_id": tool_call_id,
        "observation": {
            "content": obs_content,
            "is_error": is_error,
            "command": command,
            "path": edit.path,
            "prev_exist": bool(edit.old_str),
            "kind": "FileEditorObservation",
        },
        "action_id": tool_call_id,
        "kind": "ObservationEvent",
    }


def make_finish_event(
    message: str = "Task completed.",
    timestamp: str | None = None,
) -> dict:
    tcid = _make_id()
    return {
        "id": _make_id(),
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "source": "agent",
        "thought": [],
        "thinking_blocks": [],
        "action": {"message": message, "kind": "FinishAction"},
        "tool_name": "finish",
        "tool_call_id": tcid,
        "tool_call": {
            "id": tcid,
            "name": "finish",
            "arguments": json.dumps({"message": message}),
            "origin": "completion",
        },
        "summary": message,
        "kind": "ActionEvent",
    }


def _convert_file_read_turn(turn: "Turn", base_timestamp: str) -> list[dict]:
    lines = turn.content.split("\n", 1)
    file_list = lines[1].strip().split("\n") if len(lines) > 1 else []
    events: list[dict] = []
    for idx, fpath in enumerate(file_list):
        fpath = fpath.strip()
        if not fpath:
            continue
        tool_call_id = _make_id()
        ts = _offset_timestamp(base_timestamp, offset_ms=idx * 50)
        arguments = json.dumps({"command": "view", "path": fpath})
        events.append(
            {
                "id": _make_id(),
                "timestamp": ts,
                "source": "agent",
                "thought": [{"type": "text", "text": f"Reading file: {fpath}"}]
                if idx == 0
                else [],
                "thinking_blocks": [],
                "action": {
                    "command": "view",
                    "path": fpath,
                    "kind": "FileEditorAction",
                },
                "tool_name": "file_editor",
                "tool_call_id": tool_call_id,
                "tool_call": {
                    "id": tool_call_id,
                    "name": "file_editor",
                    "arguments": arguments,
                    "origin": "completion",
                },
                "llm_response_id": None,
                "security_risk": "UNKNOWN",
                "summary": f"View {fpath}",
                "kind": "ActionEvent",
            }
        )
        events.append(
            {
                "id": _make_id(),
                "timestamp": _offset_timestamp(ts, offset_ms=10),
                "source": "environment",
                "tool_name": "file_editor",
                "tool_call_id": tool_call_id,
                "observation": {
                    "content": [
                        {"type": "text", "text": f"File {fpath} loaded into context."}
                    ],
                    "is_error": False,
                    "command": "view",
                    "path": fpath,
                    "prev_exist": True,
                    "kind": "FileEditorObservation",
                },
                "action_id": tool_call_id,
                "kind": "ObservationEvent",
            }
        )
    return events


def _convert_assistant_turn(turn: "Turn", base_timestamp: str) -> list[dict]:
    reasoning, edits = parse_edit_blocks(turn.content)
    thinking_blocks = _make_thinking_blocks(turn.thinking)
    events: list[dict] = []
    rid = getattr(turn, "llm_response_id", None)
    turn_usage: dict = {
        "prompt_tokens": int(getattr(turn, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(turn, "completion_tokens", 0) or 0),
        "thinking_tokens": int(getattr(turn, "thinking_tokens", 0) or 0),
        "cost_usd": float(getattr(turn, "cost", 0.0) or 0.0),
    }
    if getattr(turn, "provider", "") == "vertex_ai_gemini":
        turn_usage["cached_content_tokens"] = int(getattr(turn, "cache_hit_tokens", 0) or 0)
    else:
        turn_usage["cache_read_tokens"] = int(getattr(turn, "cache_hit_tokens", 0) or 0)
        turn_usage["cache_write_tokens"] = int(getattr(turn, "cache_write_tokens", 0) or 0)
    if not any(v for v in turn_usage.values()):
        turn_usage_payload: dict | None = None
    else:
        turn_usage_payload = turn_usage

    if not edits:
        events.append(
            make_action_event(
                thought=reasoning,
                edit=None,
                thinking_blocks=thinking_blocks,
                timestamp=base_timestamp,
                summary=f"Reasoning (no edits) — {turn.stage}/{turn.module}",
                llm_response_id=rid,
                usage=turn_usage_payload,
            )
        )
        return events

    for idx, edit in enumerate(edits):
        tb = thinking_blocks if idx == 0 else []
        thought = reasoning if idx == 0 else ""
        tool_call_id = _make_id()
        ts = _offset_timestamp(base_timestamp, offset_ms=idx * 100)

        events.append(
            make_action_event(
                thought=thought,
                edit=edit,
                thinking_blocks=tb,
                tool_call_id=tool_call_id,
                timestamp=ts,
                summary=f"str_replace in {edit.path}",
                llm_response_id=rid if idx == 0 else None,
                usage=turn_usage_payload if idx == 0 else None,
            )
        )
        events.append(
            make_observation_event(
                edit=edit,
                tool_call_id=tool_call_id,
                is_error=bool(turn.edit_error),
                error_message=turn.edit_error,
                timestamp=_offset_timestamp(ts, offset_ms=10),
            )
        )

    return events


def turns_to_openhands_events(
    turns: list["Turn"],
    system_prompt: str | None = None,
) -> list[dict]:
    if not turns:
        return []

    events: list[dict] = []
    modules_seen: set[str] = set()
    current_module: str | None = None

    for turn in turns:
        module = turn.module or "__default__"

        if module not in modules_seen:
            if current_module is not None:
                events.append(
                    make_finish_event(
                        message=f"Completed module: {current_module}",
                        timestamp=_make_timestamp_from_turn(turn, offset_ms=-1),
                    )
                )

            modules_seen.add(module)
            current_module = module

            prompt = system_prompt or f"Stage: {turn.stage}, Module: {turn.module}"
            events.append(
                make_system_prompt_event(
                    system_prompt=prompt,
                    timestamp=_make_timestamp_from_turn(turn, offset_ms=0),
                )
            )

        ts_base = _make_timestamp_from_turn(turn)

        if turn.role == "user":
            if turn.content.startswith("[files:read]"):
                events.extend(_convert_file_read_turn(turn, ts_base))
            else:
                events.append(
                    make_message_event(
                        content=turn.content,
                        source="user",
                        timestamp=ts_base,
                    )
                )
        elif turn.role == "assistant":
            events.extend(_convert_assistant_turn(turn, ts_base))

    if current_module is not None and turns:
        events.append(
            make_finish_event(
                message=f"Completed module: {current_module}",
                timestamp=_make_timestamp_from_turn(turns[-1], offset_ms=5000),
            )
        )

    return events


def _count_tool_calls(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in events:
        if e.get("kind") == "ActionEvent" and e.get("tool_name"):
            name = e["tool_name"]
            counts[name] = counts.get(name, 0) + 1
    return counts


def format_openhands_output(
    turns: list["Turn"],
    instance_id: str,
    git_patch: str,
    instruction: str,
    metadata: dict,
    metrics: dict,
    system_prompt: str | None = None,
    error: str | None = None,
    attempt: int = 1,
    stage_runtime_seconds: float = 0.0,
) -> dict:
    events = turns_to_openhands_events(turns, system_prompt=system_prompt)
    tool_counts = _count_tool_calls(events)

    metrics = {
        **metrics,
        "stage_runtime_seconds": round(stage_runtime_seconds, 2),
        "tool_calls": tool_counts,
        "total_tool_calls": sum(tool_counts.values()),
    }

    return {
        "instance_id": instance_id,
        "attempt": attempt,
        "test_result": {"git_patch": git_patch},
        "instruction": instruction,
        "metadata": metadata,
        "history": events,
        "metrics": metrics,
        "error": error,
        "instance": None,
        "runtime_runs": None,
    }


def write_openhands_jsonl(
    output_path: str,
    turns: list["Turn"],
    instance_id: str,
    git_patch: str,
    instruction: str,
    metadata: dict,
    metrics: dict,
    system_prompt: str | None = None,
    error: str | None = None,
    attempt: int = 1,
    stage_runtime_seconds: float = 0.0,
) -> None:
    from pathlib import Path

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    record = format_openhands_output(
        turns=turns,
        instance_id=instance_id,
        git_patch=git_patch,
        instruction=instruction,
        metadata=metadata,
        metrics=metrics,
        system_prompt=system_prompt,
        error=error,
        attempt=attempt,
        stage_runtime_seconds=stage_runtime_seconds,
    )

    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError as e:
        logger.error("Failed to write OpenHands JSONL to %s: %s", path, e)
        raise


def write_module_output_json(
    output_dir: str,
    module_turns: list["Turn"],
    module: str,
    instance_id: str,
    git_patch: str,
    instruction: str,
    metadata: dict,
    metrics: dict,
    stage: str,
    system_prompt: str | None = None,
    error: str | None = None,
    stage_runtime_seconds: float = 0.0,
) -> None:
    from pathlib import Path

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    events = turns_to_openhands_events(module_turns, system_prompt=system_prompt)
    tool_counts = _count_tool_calls(events)

    metrics_public = dict(metrics)
    audit_mismatch = metrics_public.pop("capture_mismatch", None)

    record = {
        "module": module,
        "instance_id": instance_id,
        "stage": stage,
        "instruction": instruction,
        "test_result": {"git_patch": git_patch},
        "metadata": metadata,
        "history": events,
        "metrics": {
            **metrics_public,
            "stage_runtime_seconds": round(stage_runtime_seconds, 2),
            "tool_calls": tool_counts,
            "total_tool_calls": sum(tool_counts.values()),
        },
        "error": error,
    }

    output_path = out_dir / "output.json"
    try:
        with open(output_path, "w") as f:
            json.dump(record, f, indent=2, default=str)
    except OSError as e:
        logger.error("Failed to write module output to %s: %s", output_path, e)
        raise

    if audit_mismatch is not None:
        audit_path = out_dir / "audit.jsonl"
        audit_record = {
            "instance_id": instance_id,
            "stage": stage,
            "module": module,
            "capture_mismatch": audit_mismatch,
        }
        try:
            with open(audit_path, "a") as f:
                f.write(json.dumps(audit_record, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write audit JSONL to %s: %s", audit_path, e)
