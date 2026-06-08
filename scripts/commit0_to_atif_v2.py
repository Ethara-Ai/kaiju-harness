"""commit0 (Aider) -> Harbor ATIF converter — v2 (NATIVE-source).

v1 (commit0_to_atif.py) converted the SYNTHETIC `output.json` (an aider->openhands
re-rendering with fake 2025-01-01 timestamps and lossy edit bodies). v2 converts the
GENUINE native aider artifacts instead:

  - llm_history.txt        : aider's verbatim LLM I/O log (real timestamps, full
                             SEARCH/REPLACE edits). PRIMARY source.
  - .aider.chat.history.md : aider's human-readable chat log. CROSS-CHECK source
                             (real session start timestamp + turn corroboration).

Why v2 exists (proven from the data):
  - `output.json` is NOT produced by the bundled commit0/aider code (the data was made
    by a newer Ethara-Ai fork with `output_jsonl: true`); its timestamps are synthetic
    (2025-01-01) and ~12% of its `create` edit bodies are empty.
  - llm_history.txt content is aider's native `log_llm_history` format with REAL
    timestamps (e.g. 2026-04-28T06:53:00) and the real SEARCH/REPLACE code — so v2 is
    higher fidelity and suitable for action-level SFT, not just outcome-RL.

llm_history.txt structure:
  TO LLM <iso-ts>                # a full prompt sent to the model
    SYSTEM <line> ...            # system prompt
    USER <line> ...              # user turn(s) (task, then tool/lint/test feedback)
    ASSISTANT <line> ...         # prior assistant turn(s), echoed back as context
  LLM RESPONSE <iso-ts>          # the model's reply for that prompt
    ASSISTANT <line> ...         # reply text incl. SEARCH/REPLACE edit blocks
The LAST `TO LLM` block accumulates the whole conversation; the final `LLM RESPONSE`
is the last assistant turn. v2 reconstructs the exact role sequence aider used.

Decisions A/B/C/D/E unchanged from v1 (reward = stage pass_rate, outcome-RL, one
trajectory per module-stage, agent.name="aider", schema ATIF-v1.7). The difference is
SOURCE FIDELITY and that tool_calls now carry real edit content.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.utils.trajectory_validator import TrajectoryValidator

SCHEMA_VERSION = "ATIF-v1.7"
AIDER_NAME = "aider"
AIDER_VERSION = "0.1.dev13122+gafca02447"  # Ethara-Ai/aider fork; commit afca02447 (uv.lock)
STAGE_TO_PIPELINE_KEY = {"draft": "stage1", "lint": "stage2", "test": "stage3"}

_SYNTHETIC_TS_DATE = "2025-01-01"
_AIDER_VERSION_RE = re.compile(r"Aider\s+v?(\d[\w.+-]+)", re.IGNORECASE)

MODEL_SHORT_MAP: dict[str, str] = {
    # short dir slugs → canonical
    "opus4.7": "claude-opus-4.7",
    "opus47": "claude-opus-4.7",
    "kimik25": "kimi-k2.5",
    "glm5": "glm-5",
    "nova2lite": "nova-2-lite",
    # canonical names already in output.json metadata (passthrough, prevents false-positive warn)
    "claude-opus-4.7": "claude-opus-4.7",
    "kimi-k2.5": "kimi-k2.5",
    "glm-5": "glm-5",
    "nova-2-lite": "nova-2-lite",
}

# Kaiju harness tool surface (from kaiju/agent/openhands_formatter.py).
# The converter MUST NOT emit any function_name outside KAIJU_TOOL_NAMES, nor
# any file_editor command outside KAIJU_FILE_EDITOR_COMMANDS. Extending this
# tool surface requires adding the tool/command to the kaiju harness FIRST,
# never inventing in the converter.
KAIJU_TOOL_NAMES = frozenset({"file_editor", "think", "finish"})
KAIJU_FILE_EDITOR_COMMANDS = frozenset({"view", "create", "str_replace", "insert"})

# Reasoning-model thinking markers as they appear in llm_history.txt. <think>
# (or <thinking>) blocks are emitted inline by some open-weight reasoning
# models (DeepSeek R1, Kimi K2.5, Qwen-Thinking) and may also appear when
# Anthropic extended-thinking content blocks are serialized by aider's
# format_content(). When matched, the text inside is surfaced as a kaiju
# `think` tool call. If no matches exist in the source, NO `think` calls are
# emitted — strict no-invention semantics. View/insert/finish have no
# corresponding text marker in aider's llm_history.txt, so they are never
# emitted from this source either.
THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)

ROLE_RE = re.compile(r"^(SYSTEM|USER|ASSISTANT) ?(.*)$")
MARKER_RE = re.compile(r"^(TO LLM|LLM RESPONSE) (.+)$")
# Aider has TWO SEARCH/REPLACE edit layouts depending on the model's edit_format:
#  - 'diff'        : the file path is on its own line BEFORE the fence.
#  - 'diff-fenced' : the file path is INSIDE the fence, on the line after the opener.
# Different models in the same corpus use different ones (e.g. glm5=diff,
# nova2lite=diff-fenced), so we must match both or we silently drop a model's edits.
# The fence is 3+ backticks (aider widens to ```` when the code itself contains ```);
# the closing fence must match the opener length, hence the (?P=fence) backreference.
# ~7% of week_2 units used 4-backtick fences — a 3-only pattern dropped all their edits.
EDIT_RE_DIFF = re.compile(
    r"(?:^|\n)(?P<path>[^\n`]+?)\n(?P<fence>`{3,})[\w.]*\n"
    r"<{5,7} SEARCH\n(?P<search>.*?)\n={5,7}\n(?P<replace>.*?)\n>{5,7} REPLACE\n(?P=fence)",
    re.DOTALL,
)
EDIT_RE_FENCED = re.compile(
    r"(?P<fence>`{3,})[\w.]*\n(?P<path>[^\n`]+)\n"
    r"<{5,7} SEARCH\n(?P<search>.*?)\n={5,7}\n(?P<replace>.*?)\n>{5,7} REPLACE\n(?P=fence)",
    re.DOTALL,
)
EDIT_PATTERNS = (EDIT_RE_DIFF, EDIT_RE_FENCED)
# Aider hard-codes a few-shot example dialogue in its system prompt (editblock_prompts.py).
# When the accumulated conversation is echoed back, those example ASSISTANT turns get
# role-tagged and their SEARCH/REPLACE blocks parse as if they were real edits. They
# always use these fixed fixture paths, which never occur as real commit0 targets
# (commit0 edits package-qualified paths like src/<pkg>/<mod>.py or tests/test_*.py).
# We drop edits on these paths so they don't pollute tool_calls.
AIDER_EXAMPLE_FIXTURES = frozenset({
    "mathweb/flask/app.py", "hello.py", "main.py", "show_greeting.py",
})
# Matches a SEARCH marker + its file path in EITHER layout — used to count ALL
# example-fixture markers (incl. malformed example blocks the edit patterns skip), so
# the parse-rate guard isn't dragged down by prompt examples. Looser on purpose.
PATH_MARKER_RES = (
    re.compile(r"(?:^|\n)(?P<path>[^\n`]+?)\n`{3,}[\w.]*\n<{5,7} SEARCH"),   # diff
    re.compile(r"`{3,}[\w.]*\n(?P<path>[^\n`]+)\n<{5,7} SEARCH"),            # diff-fenced
)


@dataclass
class V2Stats:
    source: str
    instance_id: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    n_steps: int = 0
    n_agent: int = 0
    n_user: int = 0
    n_edits: int = 0          # SEARCH/REPLACE blocks parsed into tool_calls
    n_edits_empty: int = 0    # edits whose replace body is empty
    assistant_search_markers: int = 0  # '<<<<<<< SEARCH' in assistant turns (apples-to-apples vs n_edits)
    real_timestamps: bool = False
    md_edit_count: int | None = None   # SEARCH blocks seen in chat.history.md (cross-check)
    reward: float | None = None
    resolved: int | None = None
    validated: bool = False
    errors: list[str] = field(default_factory=list)
    out_data_missing: bool = False


# ---------------------------------------------------------------------------
# llm_history.txt parsing
# ---------------------------------------------------------------------------
def _split_blocks(text: str) -> list[tuple[str, str, list[str]]]:
    """Split llm_history.txt into (kind, timestamp, lines) blocks.

    kind is 'TO LLM' or 'LLM RESPONSE'. lines are the raw block lines (still
    role-prefixed), excluding the marker line and '-------' separators.
    """
    blocks: list[tuple[str, str, list[str]]] = []
    cur_kind: str | None = None
    cur_ts = ""
    cur: list[str] = []
    for line in text.splitlines():
        m = MARKER_RE.match(line)
        if m:
            if cur_kind is not None:
                blocks.append((cur_kind, cur_ts, cur))
            cur_kind, cur_ts, cur = m.group(1), m.group(2).strip(), []
        elif line.strip() == "-------":
            continue
        elif cur_kind is not None:
            cur.append(line)
    if cur_kind is not None:
        blocks.append((cur_kind, cur_ts, cur))
    return blocks


def _block_to_turns(lines: list[str]) -> list[tuple[str, str]]:
    """Collapse role-prefixed lines into [(role, text), ...] turns.

    A turn is a maximal run of consecutive lines sharing a role prefix. The role
    token + at most one following space is stripped; original indentation kept.
    Role transitions are suppressed inside code fences to prevent content lines
    that happen to start with a role keyword from creating a false turn split.
    """
    turns: list[tuple[str, list[str]]] = []
    cur_role: str | None = None
    in_fence: bool = False
    for line in lines:
        m = ROLE_RE.match(line)
        if m:
            parsed_role, content = m.group(1).lower(), m.group(2)
        else:
            parsed_role, content = None, line
        # Track code fences on the extracted content to suppress false role
        # transitions from continuation lines inside SEARCH/REPLACE blocks.
        fence_stripped = content.strip()
        was_in_fence = in_fence
        if fence_stripped.startswith("```") or fence_stripped.startswith("~~~"):
            in_fence = not in_fence
        role = parsed_role if (parsed_role is not None and not was_in_fence) else (cur_role or "user")
        if role != cur_role:
            turns.append((role, []))
            cur_role = role
        turns[-1][1].append(content)
    return [(r, "\n".join(ls).strip("\n")) for r, ls in turns]


def _parse_edits(assistant_text: str) -> tuple[list[dict[str, str]], int, int]:
    """Extract aider SEARCH/REPLACE edits from an assistant turn.

    Returns (edits, n_empty_replace, raw_markers). raw_markers = count of
    '<<<<<<< SEARCH' actually present in this assistant turn MINUS aider few-shot
    example blocks — compared against len(edits) it tells us whether EDIT_RE matched
    the model's edit_format. (Counted per assistant turn, NOT whole-file, so example
    blocks don't inflate it.) Edits whose path is an aider example fixture
    (AIDER_EXAMPLE_FIXTURES) are dropped, and their markers are subtracted so the
    parse-rate guard stays accurate. Each kept edit = {file_path, search, replace}.
    """
    edits = []
    empty = 0
    raw_markers = assistant_text.count("<<<<<<< SEARCH")
    # Subtract ALL example-fixture markers (both layouts, path-anchored so it also
    # catches malformed example blocks the edit patterns skip), so raw_markers reflects
    # only real edits. Dedupe by SEARCH-marker offset so the two layout regexes don't
    # double-count the same block.
    seen_fixture_at: set[int] = set()
    for pm_re in PATH_MARKER_RES:
        for pm in pm_re.finditer(assistant_text):
            if pm.group("path").strip() in AIDER_EXAMPLE_FIXTURES:
                pos = assistant_text.find("<<<<<<< SEARCH", pm.start())
                if pos not in seen_fixture_at:
                    seen_fixture_at.add(pos)
                    raw_markers -= 1
    # Parse real edits with both layouts; dedupe by the SEARCH-marker offset so a block
    # matched by both patterns is only emitted once.
    seen_edit_at: set[int] = set()
    for edit_re in EDIT_PATTERNS:
        for m in edit_re.finditer(assistant_text):
            pos = assistant_text.find("<<<<<<< SEARCH", m.start())
            if pos in seen_edit_at:
                continue
            path = m.group("path").strip()
            if path in AIDER_EXAMPLE_FIXTURES:
                continue   # prompt example — already excluded from raw_markers
            seen_edit_at.add(pos)
            replace = m.group("replace")
            if not replace.strip():
                empty += 1
            edits.append({
                "file_path": path,
                "search": m.group("search"),
                "replace": replace,
            })
    if raw_markers < 0:
        print(
            f"[WARN] fixture over-subtraction: raw_markers={raw_markers} in _parse_edits; "
            "clamping to 0. parse_rate may be artificially high for this turn.",
            file=sys.stderr,
        )
    return edits, empty, max(raw_markers, 0)


def _extract_think_blocks(assistant_text: str) -> list[str]:
    """Extract <think>...</think> (or <thinking>...</thinking>) block contents
    from an assistant turn. Returns one string per block found, in source order.
    Empty blocks are skipped. The text in `assistant_text` is NOT modified —
    markers stay in the message body for fidelity with what the model emitted
    (same convention as SEARCH/REPLACE blocks).
    """
    return [m.group(1).strip() for m in THINK_BLOCK_RE.finditer(assistant_text)
            if m.group(1).strip()]


def _assert_kaiju_tool(tc: ToolCall) -> None:
    """Enforce the no-invention rule: every emitted ToolCall must use a kaiju-
    registered tool name, and file_editor must use a kaiju-registered command.
    """
    if tc.function_name not in KAIJU_TOOL_NAMES:
        raise ValueError(
            f"function_name {tc.function_name!r} not in kaiju tool set "
            f"{sorted(KAIJU_TOOL_NAMES)}. The converter must not invent tools."
        )
    if tc.function_name == "file_editor":
        cmd = tc.arguments.get("command")
        if cmd not in KAIJU_FILE_EDITOR_COMMANDS:
            raise ValueError(
                f"file_editor command {cmd!r} not in kaiju command set "
                f"{sorted(KAIJU_FILE_EDITOR_COMMANDS)}."
            )


# --- aider prompt-scaffolding markers (from editblock_prompts.py + base_coder.py) ---
# The static few-shot demo aider injects into EVERY prompt: two scripted example
# exchanges, then a hardcoded "I switched to a new code base" transition. It is
# byte-identical across all trajectories and is NOT model output — recording it as
# agent turns falsely attributes scripted text to the model and poisons action-level
# SFT. We strip the demo turns up to and including the "switched code base" reset, then
# keep everything after (repo-map context + real task + genuine model turns).
_FEWSHOT_FIRST_USER = "Change get_factorial() to use math.factorial"
_FEWSHOT_RESET = "I switched to a new code base"


def _strip_fewshot(turns: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], bool]:
    """Drop aider's static few-shot demo. Returns (turns, stripped?).

    Only fires when the demo is present in its known position (first non-system user
    turn is the get_factorial example) AND the "switched code base" reset is found —
    so a real task that never contained the demo is never touched. Keeps the system
    prompt and everything from the reset onward (repo-map + real task + model turns).
    """
    starts = any(_FEWSHOT_FIRST_USER in t for _, t in turns)
    reset_idx = next((i for i, (_, t) in enumerate(turns) if _FEWSHOT_RESET in t), None)
    if not (starts and reset_idx is not None):
        return turns, False
    # keep leading system turns + everything AFTER the reset turn
    head = [(r, t) for r, t in turns[:reset_idx] if r == "system"]
    return head + turns[reset_idx + 1:], True


# Hardcoded assistant acknowledgements aider injects during the file-priming handshake
# (base_coder.py / base_prompts.py). They are NOT model output — they are static prompt
# furniture, so recording them as agent turns falsely attributes them to the policy and
# poisons action-level SFT. Matched EXACTLY (full-message equality) so a genuine short
# model turn that merely starts with "Ok" is never removed.
_AIDER_ACKS = frozenset({
    "Ok.",
    "Ok",
    "Ok, I won't try and edit those files without asking first.",
    "Ok, I will use these files as references.",
    "Ok, I will use these images as references.",
    "Ok, any changes I propose will be to those files.",
})
# Hardcoded USER priming turns aider injects (repo-map handshake + file-add notices).
# These are template scaffolding, not the real task; dropped when they pair with the acks.
_AIDER_PRIMING_USER_PREFIXES = (
    "Here are summaries of some files present in my git repository.",
    "I have *added these files to the chat*",
    "Here are some images",
)


def _strip_priming(turns: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], int]:
    """Drop aider's hardcoded file-priming handshake turns.

    Removes (a) agent turns whose message is EXACTLY a known aider ack, and (b) the
    paired user priming turns (repo-map / 'added these files to the chat' notices).
    Critically, a user turn that ALSO carries the real task (contains '>>> Here is the
    Task') is NEVER dropped — aider sometimes concatenates the file-add notice with the
    task into one turn, and we must keep that. Returns (turns, n_removed).
    """
    out = []
    removed = 0
    for role, text in turns:
        stripped = text.strip()
        # at this stage turns use the raw 'assistant'/'user' role tokens (not 'agent')
        if role == "assistant" and stripped in _AIDER_ACKS:
            removed += 1
            continue
        if (role == "user"
                and ">>> Here is the Task" not in text
                and any(stripped.startswith(p) for p in _AIDER_PRIMING_USER_PREFIXES)):
            removed += 1
            continue
        out.append((role, text))
    return out, removed


def _clean_content_block_repr(text: str) -> str:
    """Undo aider's occasional structured-content-block repr leak.

    Some priming acks are logged as a dumped content block, e.g.:
        type: text
        text: Ok.
        cache_control: {'type': 'ephemeral'}
    The real content is the `text:` value. Extract it; leave normal text untouched.
    """
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].strip() == "type: text" and lines[1].startswith("text:"):
        body = [lines[1][len("text:"):].lstrip()]
        for ln in lines[2:]:
            if ln.startswith("cache_control:"):
                break
            body.append(ln)
        return "\n".join(body).strip()
    return text


def reconstruct_conversation(text: str) -> tuple[list[tuple[str, str]], list[str], bool, bool, str | None]:
    """Reconstruct the full role sequence from llm_history.txt.

    Uses the LAST `TO LLM` block (it accumulates the whole conversation) plus the
    final `LLM RESPONSE` (the last assistant turn). Strips aider's static few-shot
    demo and cleans content-block-repr leaks. Returns
    (turns, timestamps, real_ts, fewshot_stripped).
    """
    blocks = _split_blocks(text)
    if not blocks:
        return [], [], False, False, None
    to_llm = [b for b in blocks if b[0] == "TO LLM"]
    responses = [b for b in blocks if b[0] == "LLM RESPONSE"]
    if not to_llm:
        return [], [], False, False, None
    last_prompt = to_llm[-1]
    turns = _block_to_turns(last_prompt[2])
    # append the final model response as the closing assistant turn
    if responses:
        final = _block_to_turns(responses[-1][2])
        final_text = "\n".join(t for r, t in final if r == "assistant").strip("\n")
        if final_text:
            turns.append(("assistant", final_text))
    turns, fewshot_stripped = _strip_fewshot(turns)
    # clean dict-repr leaks FIRST so repr-wrapped acks normalize to plain text, then the
    # exact-match priming/ack filter can catch them.
    turns = [(r, _clean_content_block_repr(t)) for r, t in turns]
    turns, n_priming = _strip_priming(turns)
    timestamps = [b[1] for b in blocks]
    real_ts = bool(timestamps) and not timestamps[0].startswith(_SYNTHETIC_TS_DATE)
    last_response_ts = next((b[1] for b in reversed(blocks) if b[0] == "LLM RESPONSE"), None)
    return turns, timestamps, real_ts, (fewshot_stripped or n_priming > 0), last_response_ts


def _md_search_replace_count(md_path: Path) -> int | None:
    """Cross-check signal: count SEARCH/REPLACE blocks in .aider.chat.history.md.

    This is the reliable corroboration between the two native sources — the chat
    log and the llm log should contain the same edits. (Counting "assistant turns"
    is unreliable because aider's md interleaves '####'-prefixed echoes; edit-block
    count is unambiguous.) Returns None if the md is absent/trivial.

    Fixture markers from aider's few-shot system prompt are subtracted using the
    same AIDER_EXAMPLE_FIXTURES exclusion as _parse_edits, so this number is
    comparable to n_edits (fixes E-1: false-negative crosscheck).
    """
    if not md_path.exists():
        return None
    text = md_path.read_text(errors="replace")
    if len(text) < 50:
        return None
    raw = text.count("<<<<<<< SEARCH")
    lines = text.splitlines()
    fixture_markers = 0
    for i, line in enumerate(lines):
        if "<<<<<<< SEARCH" not in line:
            continue
        # Look back a few lines for a fixture path (mirrors _parse_edits' PATH_RE
        # locality; aider's few-shot block always names the path within ~5 lines).
        for j in range(max(0, i - 5), i):
            if lines[j].strip() in AIDER_EXAMPLE_FIXTURES:
                fixture_markers += 1
                break
    return max(raw - fixture_markers, 0)



# ---------------------------------------------------------------------------
# supplemental data from output.json (metrics, tool defs, git patch, model)
# ---------------------------------------------------------------------------
def _canonical_model(dir_slug: str, out_data: dict[str, Any] | None) -> str:
    if out_data:
        meta_model = (out_data.get("metadata") or {}).get("llm", {}).get("model", "")
        if meta_model:
            result = MODEL_SHORT_MAP.get(meta_model, meta_model)
            if result == meta_model and meta_model not in MODEL_SHORT_MAP:
                print(f"[WARN] unrecognized model slug in metadata: {meta_model!r}", file=sys.stderr)
            return result
    result = MODEL_SHORT_MAP.get(dir_slug, dir_slug)
    if result == dir_slug and dir_slug not in MODEL_SHORT_MAP:
        print(f"[WARN] unrecognized model dir slug: {dir_slug!r}", file=sys.stderr)
    return result


def _read_output_data(unit_dir: Path) -> dict[str, Any] | None:
    p = unit_dir / "output.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(errors="replace"))
    except Exception:
        return None

def _read_aider_version(unit_dir: Path) -> str | None:
    """Extract aider version from aider.log (first 4 KB). Returns None if not found."""
    log = unit_dir / "aider.log"
    if not log.exists():
        return None
    try:
        text = log.read_bytes()[:4096].decode("utf-8", errors="replace")
        m = _AIDER_VERSION_RE.search(text)
        return m.group(1) if m else None
    except Exception:
        return None


def _extract_tool_definitions(out_data: dict[str, Any] | None) -> list[dict] | None:
    if not out_data:
        return None
    for event in out_data.get("history", []):
        if event.get("kind") == "SystemPromptEvent":
            tools = event.get("tools")
            if tools:
                return list(tools)
    return None


# ---------------------------------------------------------------------------
# reward join (same as v1)
# ---------------------------------------------------------------------------
def load_pipeline_rewards(p: Path) -> dict[str, float | None]:
    d = json.loads(p.read_text())
    if "stages" in d:  # C pipeline nested schema
        raw = d["stages"]
        remap = {"stage1_draft": "stage1", "stage2_lint": "stage2", "stage3_test": "stage3"}
        return {v: (raw.get(k) or {}).get("pass_rate") for k, v in remap.items()}
    return {k: (d.get(k) or {}).get("pass_rate") for k in ("stage1", "stage2", "stage3")}


def find_pipeline_for(output_path: Path) -> Path | None:
    """Find the nearest pipeline_*_results.json searching upward from the unit dir.

    Stops at (and includes) the logs_* ancestor so per-branch pipeline files are
    preferred over the top-level one (fixes RJ-2). Hits are sorted for determinism
    when multiple result files exist at the same level (fixes RJ-1).
    """
    for parent in output_path.parents:
        hits = sorted(parent.glob("pipeline_*_results.json"))
        if hits:
            return hits[0]
        if parent.name.startswith("logs_"):
            break
    return None


# ---------------------------------------------------------------------------
# build one ATIF trajectory from a work-unit directory
# ---------------------------------------------------------------------------
def convert_unit(unit_dir: Path, *, task: str, model: str, stage: str, module: str,
                 reward: float | None, resolved: int | None,
                 stage_pass_rate: dict[str, float | None],
                 aider_version_cli: str | None = None,
                 branch_suffix: str = "") -> tuple[Trajectory | None, V2Stats]:
    st = V2Stats(source=str(unit_dir))
    llm = unit_dir / "llm_history.txt"
    md = unit_dir / ".aider.chat.history.md"
    if not llm.exists():
        st.skipped = True
        st.skip_reason = "no_llm_history"
        return None, st

    turns, timestamps, real_ts, fewshot_stripped, last_response_ts = reconstruct_conversation(
        llm.read_text(errors="replace"))
    if not turns:
        st.skipped = True
        st.skip_reason = "empty_llm_history"
        return None, st
    st.real_timestamps = real_ts
    out_data = _read_output_data(unit_dir)
    if out_data is None:
        st.out_data_missing = True
    model_canonical = _canonical_model(model, out_data)

    # trajectory_id must include MODEL + stage + module so it is unique across the
    # whole corpus. A (task, module) appears once per stage AND once per model; omitting
    # either caused collisions (week_2: 16,690 ids shared by >1 file — same module/stage
    # across glm5/nova2lite, and draft/lint/test of the same module). The branch_suffix
    # disambiguates the rare case where one model dir contains >1 pipeline branch (e.g.
    # logs_nova2lite/ holding both aider-nova-2-lite-* and aider-glm-5-* for pexpect).
    instance_id = f"commit-0/{task}__{model_canonical}__{stage}__{module}"
    if branch_suffix:
        instance_id = f"{instance_id}__{branch_suffix}"
    st.instance_id = instance_id
    st.md_edit_count = _md_search_replace_count(md)
    start_ts = timestamps[0] if real_ts else None

    steps: list[Step] = []
    sid = 0
    call_seq = 0
    last_ast_idx = max((i for i, (r, _) in enumerate(turns) if r == "assistant"), default=-1)
    for i, (role, body) in enumerate(turns):
        if role == "system":
            sid += 1
            steps.append(Step(step_id=sid, source="system", message=body))
        elif role == "user":
            sid += 1
            steps.append(Step(step_id=sid, source="user", message=body))
            st.n_user += 1
        elif role == "assistant":
            # Strict no-invention rule: the only tool names emitted on agent steps
            # are those registered in kaiju/agent/openhands_formatter.py.
            # - <think>...</think> blocks      -> 'think' tool call (kaiju tool name)
            # - SEARCH/REPLACE blocks          -> 'file_editor' tool call
            #     - SEARCH empty                 => command='create'
            #     - SEARCH non-empty             => command='str_replace'
            # view/insert/finish have no text marker in aider's llm_history.txt
            # and are NEVER emitted from this source. Every ToolCall is validated
            # by _assert_kaiju_tool() before the step is appended.
            think_blocks = _extract_think_blocks(body)
            edits, empty, raw_markers = _parse_edits(body)
            st.n_edits += len(edits)
            st.n_edits_empty += empty
            st.assistant_search_markers += raw_markers
            tool_calls = []
            for tb in think_blocks:
                call_seq += 1
                tool_calls.append(ToolCall(
                    tool_call_id=f"think_{call_seq}",
                    function_name="think",
                    arguments={"thought": tb},
                ))
            for e in edits:
                call_seq += 1
                cid = f"edit_{call_seq}"
                # Match kaiju's openhands_formatter.py:344: command='create' when
                # SEARCH is empty (new file), else 'str_replace'. Aligns
                # function_name with the file_editor tool in Agent.tool_definitions.
                command = "create" if not e["search"].strip() else "str_replace"
                tool_calls.append(ToolCall(
                    tool_call_id=cid,
                    function_name="file_editor",
                    arguments={"command": command,
                               "path": e["file_path"],
                               "old_str": e["search"],
                               "new_str": e["replace"]},
                ))
            for _tc in tool_calls:
                _assert_kaiju_tool(_tc)
            step_ts = last_response_ts if (real_ts and i == last_ast_idx) else None
            sid += 1
            steps.append(Step(
                step_id=sid, source="agent", message=body,
                model_name=model_canonical,
                timestamp=step_ts,
                tool_calls=tool_calls or None,
            ))
            st.n_agent += 1

    if not steps:
        st.skipped = True
        st.skip_reason = "no_steps"
        return None, st

    _version_from_log = _read_aider_version(unit_dir)
    _resolved_version = aider_version_cli or _version_from_log or AIDER_VERSION
    _tool_defs = _extract_tool_definitions(out_data)
    agent = Agent(
        name=AIDER_NAME, version=_resolved_version, model_name=model_canonical,
        tool_definitions=_tool_defs,
        extra={"log_format": "aider-native-llm-history", "converted_from": "aider",
               "harness": "commit0-aider-pipeline", "pipeline_stage": stage,
               "module": module, "source_file": "llm_history.txt",
               "crosscheck_file": ".aider.chat.history.md",
               "aider_scaffolding_stripped": fewshot_stripped,
               "version_source": ("cli" if aider_version_cli else ("log" if _version_from_log else "constant")),
               **({"tool_definitions_source": "harness_system_prompt"} if _tool_defs is not None else {})}
    )
    _m = (out_data or {}).get("metrics") or {}
    fm_extra: dict[str, Any] = {}
    if reward is not None:
        fm_extra["reward"] = reward
        fm_extra["resolved"] = resolved
    fm_extra["stage_pass_rate"] = stage_pass_rate
    fm_extra["edit_count"] = st.n_edits
    if _m.get("stage_runtime_seconds") is not None:
        fm_extra["stage_runtime_seconds"] = _m["stage_runtime_seconds"]
    if _m.get("num_turns") is not None:
        fm_extra["num_turns"] = _m["num_turns"]
    if _m.get("total_thinking_tokens") is not None:
        fm_extra["total_thinking_tokens"] = _m["total_thinking_tokens"]
    if _m.get("cache_write_tokens") is not None:
        fm_extra["cache_write_tokens"] = _m["cache_write_tokens"]
    final_metrics = FinalMetrics(
        total_steps=len(steps),
        total_prompt_tokens=_m.get("total_prompt_tokens"),
        total_completion_tokens=_m.get("total_completion_tokens"),
        total_cached_tokens=_m.get("cache_hit_tokens"),
        total_cost_usd=_m.get("total_cost"),
        extra=fm_extra or None,
    )

    _extra: dict[str, Any] = {
        "instance_id": instance_id, "stage": stage, "module": module,
        "session_timestamps": ([timestamps[0], timestamps[-1]] if timestamps else None) if real_ts else None,
    }
    if out_data:
        _git = (out_data.get("test_result") or {}).get("git_patch")
        if _git:
            _extra["git_patch"] = _git
        _err = out_data.get("error")
        if _err is not None:
            _extra["run_error"] = _err
    traj = Trajectory(
        schema_version=SCHEMA_VERSION, trajectory_id=instance_id, agent=agent,
        steps=steps, final_metrics=final_metrics,
        notes=(
            "Converted v2 from the native aider log llm_history.txt "
            "(cross-checked against .aider.chat.history.md). "
            + (
                "Real wall-clock timestamps"
                + (f" (session start {start_ts})" if start_ts else "")
                if real_ts else "Synthetic timestamps (real_ts unavailable from log)"
            )
            + "; tool_calls carry real SEARCH/REPLACE edit content. Reward is "
            "repo/stage-level pass_rate (outcome-RL); suitable for action-level SFT."
        ),
        extra=_extra,
    )
    st.reward = reward
    st.resolved = resolved
    return traj, st


def discover_units(task_dir: Path) -> list[tuple[Path, str, str, str]]:
    """Find every work-unit dir: returns (unit_dir, model, stage, module)."""
    out = []
    stage_map = {"stage1": "draft", "stage2": "lint", "stage3": "test"}
    for llm in task_dir.glob("logs_*/**/current/*/llm_history.txt"):
        unit = llm.parent
        parts = unit.parts
        model = next((p[len("logs_"):] for p in parts if p.startswith("logs_")), "unknown")
        stage = next((stage_map[p] for p in parts if p in stage_map), "unknown")
        # disambiguate branch when needed: include branch in module key
        logs_idx = next((i for i, p in enumerate(parts) if p.startswith("logs_")), None)
        repo = parts[logs_idx + 1] if logs_idx is not None else "unknown"
        module = f"{repo}__{unit.name}"
        out.append((unit, model, stage, module))
    return out


_KAIJU_STAGE_MAP = {
    "stage1_draft": "draft",
    "stage2_lint":  "lint",
    "stage3_tests": "test",
    "stage3_test":  "test",
}


def discover_units_kaiju(run_dir: Path) -> list[tuple[Path, str, str, str]]:
    """Find every work-unit dir in a kaiju run directory."""
    out = []
    model = run_dir.parent.name
    for llm in run_dir.glob("stage*_*/**/current/*/llm_history.txt"):
        unit = llm.parent
        rel_parts = unit.relative_to(run_dir).parts
        stage = _KAIJU_STAGE_MAP.get(rel_parts[0], "unknown")
        repo = rel_parts[1] if len(rel_parts) > 1 else "unknown"
        module = f"{repo}__{unit.name}"
        out.append((unit, model, stage, module))
    return out

def convert_task(task_dir: Path, out_root: Path, task_name: str,
                 validate: bool = True, limit: int = 0,
                 aider_version: str | None = None,
                 kaiju_mode: bool = False,
                 pipeline_override: Path | None = None) -> dict[str, Any]:
    if kaiju_mode:
        units = sorted(discover_units_kaiju(task_dir), key=lambda u: str(u[0]))
    else:
        units = sorted(discover_units(task_dir), key=lambda u: str(u[0]))
    if limit:
        units = units[:limit]
    # branch disambiguation: if a (model,stage,module) repeats, append branch
    seen: dict[tuple, int] = {}
    for u, model, stage, module in units:
        seen[(model, stage, module)] = seen.get((model, stage, module), 0) + 1
    dup_keys = {k for k, v in seen.items() if v > 1}

    all_stats: list[V2Stats] = []
    validator = TrajectoryValidator() if validate else None
    used_dests: set[Path] = set()
    model_rewards: dict[tuple[str, str], dict[str, float | None]] = {}
    for unit, model, stage, module in units:
        pipeline = pipeline_override if pipeline_override else find_pipeline_for(unit)
        spr = load_pipeline_rewards(pipeline) if pipeline else {}
        key = STAGE_TO_PIPELINE_KEY.get(stage)
        reward = spr.get(key) if key else None
        resolved = (1 if (reward is not None and reward > 0) else 0) if reward is not None else None
        # when a (model,stage,module) has >1 pipeline branch, the branch disambiguates
        # BOTH the output path leaf AND the trajectory_id (else they collide).
        branch_suffix = ""
        if (model, stage, module) in dup_keys:
            aider_parts = [p for p in unit.parts if p.startswith("aider-")]
            if aider_parts:
                aider_idx = next(i for i, p in enumerate(unit.parts) if p.startswith("aider-"))
                repo_part = unit.parts[aider_idx - 1] if aider_idx > 0 else ""
                branch_suffix = "__".join(filter(None, [repo_part] + aider_parts))
            else:
                logs_idx = next(
                    (i for i, p in enumerate(unit.parts) if p.startswith("logs_")), None
                )
                branch_suffix = "_".join(
                    unit.parts[logs_idx + 1:] if logs_idx is not None else unit.parts[-2:]
                )
        try:
            traj, st = convert_unit(unit, task=task_name, model=model, stage=stage,
                                    module=module, reward=reward, resolved=resolved,
                                    stage_pass_rate=spr, branch_suffix=branch_suffix,
                                    aider_version_cli=aider_version)
        except Exception as e:  # noqa: BLE001
            st = V2Stats(source=str(unit))
            st.errors = [f"{type(e).__name__}: {e}"]
            all_stats.append(st)
            continue
        if traj is not None:
            leaf = f"{stage}__{module}"
            if branch_suffix:
                leaf = f"{leaf}__{branch_suffix}"
            dest = out_root / task_name / model / leaf
            if dest in used_dests:
                st.errors.append(
                    f"dest_collision: {dest} — branch_suffix failed to disambiguate"
                )
                all_stats.append(st)
                continue
            used_dests.add(dest)
            dest.mkdir(parents=True, exist_ok=True)
            payload = traj.to_json_dict()
            (dest / "trajectory.json").write_text(json.dumps(payload, indent=2))
            model_rewards[(model, branch_suffix)] = spr
            if validator is not None:
                ok = validator.validate(payload)
                st.validated = ok
                if not ok:
                    st.errors = [str(e) for e in validator.get_errors()]
        all_stats.append(st)

    # Reward signal is stage-level (one pipeline_*_results.json covers all modules in a
    # branch), so emit ONE reward.json per (model[, branch]) at the model-dir level
    # rather than fanning out N identical copies under every module leaf. The stage-
    # specific 'reward'/'resolved' scalars become a per-stage map of pass_rates +
    # derived resolved flags. Branched runs (rare) get a suffixed filename so a single
    # model dir can hold multiple branches without overwrite.
    for (model_name, branch_suffix), spr in model_rewards.items():
        if not spr:
            continue
        rd = out_root / task_name / model_name / "logs" / "verifier"
        rd.mkdir(parents=True, exist_ok=True)
        resolved_map = {k: (1 if (v is not None and v > 0) else 0)
                        for k, v in spr.items() if v is not None}
        fname = f"reward__{branch_suffix}.json" if branch_suffix else "reward.json"
        (rd / fname).write_text(json.dumps(
            {"stage_pass_rate": spr, "resolved": resolved_map}, indent=2))

    conv = [s for s in all_stats if not s.skipped and not s.errors]
    total_edits = sum(s.n_edits for s in conv)
    total_markers = sum(s.assistant_search_markers for s in conv)
    # Tripwire (edit_format mismatch). EDIT_RE only matches aider's 'diff' format
    # (path line -> ```fence -> <<<<<<< SEARCH). If the model emits a different format
    # (diff-fenced puts the path INSIDE the fence; udiff/whole don't use these markers
    # the same way), EDIT_RE matches ~none of the markers and tool_calls are silently
    # lost. We detect this by PARSE RATE = parsed_edits / search_markers (markers counted
    # in assistant turns only, so system-prompt examples don't inflate it). A healthy
    # 'diff' run parses ~85%+ (the small gap is benign model formatting noise); a format
    # mismatch parses ~0%. We flag below PARSE_RATE_FLOOR. Evaluated per-task because the
    # edit_format is uniform within a run (one model/config), which is far less noisy
    # than a per-unit equality check.
    PARSE_RATE_FLOOR = 0.5
    parse_rate = (total_edits / total_markers) if total_markers else None
    edit_format_ok = parse_rate is None or parse_rate >= PARSE_RATE_FLOOR
    # Honest empty-pct: None (not 0.0) when there are no edits at all, so a wholesale
    # parse failure can't masquerade as "0% empty / perfect".
    empty_pct = (round(100 * sum(s.n_edits_empty for s in conv) / total_edits, 2)
                 if total_edits else None)
    report = {
        "task": task_name, "version": "v2-native",
        "units": len(all_stats),
        "converted": len(conv),
        "skipped": sum(1 for s in all_stats if s.skipped),
        "with_errors": sum(1 for s in all_stats if s.errors),
        "validated_ok": sum(1 for s in all_stats if s.validated),
        "real_timestamps": sum(1 for s in all_stats if s.real_timestamps),
        "total_edits": total_edits,
        "assistant_search_markers": total_markers,
        "edit_parse_rate": round(parse_rate, 4) if parse_rate is not None else None,
        "empty_replace_edits": sum(s.n_edits_empty for s in conv),
        "empty_edit_pct": empty_pct,
        "edit_format_ok": edit_format_ok,
        "edit_crosscheck_ok": sum(
            1 for s in conv if s.md_edit_count is not None
            and s.md_edit_count == s.n_edits),
        "edit_crosscheck_checked": sum(1 for s in conv if s.md_edit_count is not None),
        "reward_count": sum(1 for s in conv if s.reward is not None),
        "reward_mean": round(sum(s.reward for s in conv if s.reward is not None)
                             / max(sum(1 for s in conv if s.reward is not None), 1), 6),
    }
    out_root.mkdir(parents=True, exist_ok=True)
    if not edit_format_ok:
        worst = sorted((s for s in conv if s.assistant_search_markers > 0),
                       key=lambda s: s.n_edits / s.assistant_search_markers)[:50]
        (out_root / f"{task_name}_v2_edit_format_warning.json").write_text(json.dumps(
            {"task": task_name,
             "msg": (f"Edit parse rate {parse_rate:.1%} is below the {PARSE_RATE_FLOOR:.0%} "
                     "floor. EDIT_RE only matches aider 'diff' format — the model's "
                     "edit_format is likely not 'diff' (e.g. diff-fenced/udiff/whole). "
                     "tool_calls have been silently dropped — DO NOT SHIP without fixing "
                     "EDIT_RE for this format and re-running."),
             "parse_rate": round(parse_rate, 4), "parsed_edits": total_edits,
             "search_markers": total_markers,
             "worst_units": [{"source": s.source, "markers": s.assistant_search_markers,
                              "parsed": s.n_edits} for s in worst]}, indent=2))
    (out_root / f"{task_name}_v2_report.json").write_text(json.dumps(report, indent=2))
    errs = [{"source": s.source, "errors": s.errors} for s in all_stats if s.errors]
    if errs:
        (out_root / f"{task_name}_v2_errors.json").write_text(json.dumps(errs, indent=2))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="commit0 -> ATIF v2 (native source)")
    ap.add_argument("task_dir")
    ap.add_argument("out_root")
    ap.add_argument("--task-name", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--batch", action="store_true")
    ap.add_argument("--aider-version", default=None, metavar="VER",
                    help="Override agent.version (e.g. '0.1.dev13122+gafca02447'). "
                         "Extracted from aider.log if available; falls back to AIDER_VERSION constant.")
    ap.add_argument("--kaiju-mode", action="store_true",
                    help="Use kaiju log layout instead of commit0 layout.")
    ap.add_argument("--pipeline", default=None, metavar="PATH",
                    help="Explicit path to pipeline_*_results.json (overrides upward walk).")
    args = ap.parse_args(argv)

    root = Path(args.task_dir)
    out_root = Path(args.out_root)
    if not args.batch:
        rep = convert_task(root, out_root, args.task_name or root.name,
                           validate=not args.no_validate, limit=args.limit,
                           aider_version=args.aider_version,
                           kaiju_mode=args.kaiju_mode,
                           pipeline_override=Path(args.pipeline) if args.pipeline else None)
        print(json.dumps(rep, indent=2))
        if not rep.get("edit_format_ok", True):
            print(f"[v2][WARN] edit parse rate {rep.get('edit_parse_rate')} below floor — "
                  f"edit_format likely not 'diff'. tool_calls dropped. See "
                  f"{args.task_name or root.name}_v2_edit_format_warning.json", file=sys.stderr)
        # Fail loud on either hard errors OR the silent-corruption tripwire.
        return 0 if (rep["with_errors"] == 0 and rep.get("edit_format_ok", True)) else 1

    out_root.mkdir(parents=True, exist_ok=True)
    subdirs = sorted(d for d in root.iterdir() if d.is_dir() and list(d.glob("logs_*/")))
    if not subdirs:
        print(f"[v2][ERROR] no task subdirs with logs_*/ found under {root}", file=sys.stderr)
        return 1
    batch = []
    for d in subdirs:
        try:
            rep = convert_task(d, out_root, d.name, validate=not args.no_validate,
                               limit=args.limit, aider_version=args.aider_version,
                               kaiju_mode=args.kaiju_mode,
                               pipeline_override=Path(args.pipeline) if args.pipeline else None)
        except Exception as e:  # noqa: BLE001
            rep = {"task": d.name, "units": 0, "with_errors": -1, "fatal": f"{type(e).__name__}: {e}"}
        batch.append(rep)
        flag = " <-- EDIT-FORMAT WARN" if not rep.get("edit_format_ok", True) else ""
        print(f"[v2] {d.name}: units={rep.get('units')} conv={rep.get('converted')} "
              f"valid={rep.get('validated_ok')} err={rep.get('with_errors')} "
              f"edits={rep.get('total_edits')} empty%={rep.get('empty_edit_pct')} "
              f"parse_rate={rep.get('edit_parse_rate')}{flag}", flush=True)
    tasks_with_fmt_warning = [r["task"] for r in batch
                              if not r.get("edit_format_ok", True)]
    total_edits = sum(r.get("total_edits", 0) for r in batch)
    total_markers = sum(r.get("assistant_search_markers", 0) for r in batch)
    totals = {
        "version": "v2-native", "tasks": len(batch),
        "total_units": sum(r.get("units", 0) for r in batch),
        "total_converted": sum(r.get("converted", 0) for r in batch),
        "total_valid": sum(r.get("validated_ok", 0) for r in batch),
        "total_errors": sum(max(r.get("with_errors", 0), 0) for r in batch),
        "total_edits": total_edits,
        "total_search_markers": total_markers,
        "overall_edit_parse_rate": round(total_edits / total_markers, 4) if total_markers else None,
        "edit_format_ok": not tasks_with_fmt_warning,
        "tasks_with_edit_format_warning": tasks_with_fmt_warning,
        "per_task": batch,
    }
    (out_root / "BATCH_SUMMARY_v2.json").write_text(json.dumps(totals, indent=2))
    print(json.dumps({k: v for k, v in totals.items() if k != "per_task"}, indent=2))
    if tasks_with_fmt_warning:
        print(f"[v2][WARN] {len(tasks_with_fmt_warning)} task(s) had SEARCH markers that "
              f"EDIT_RE failed to parse — edit_format likely not aider 'diff'. "
              f"tool_calls silently dropped/undercounted. Tasks: "
              f"{', '.join(tasks_with_fmt_warning[:20])}", file=sys.stderr)
    # Nonzero exit if hard errors OR the edit-format tripwire fired.
    return 0 if (totals["total_errors"] == 0 and totals["edit_format_ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
