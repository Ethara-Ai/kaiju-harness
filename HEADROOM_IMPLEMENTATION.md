# Headroom Integration — kaiju-harness

This document describes how `headroom-ai` is integrated into the kaiju-harness agent to compress LLM context across all three pipeline stages and all supported model providers.

---

## 1. What this integration does

**Goal:** Add real token-budget headroom on every LLM call without changing the harness's behavior, semantics, or output format.

**How:** A monkey-patch on `litellm.completion` runs Headroom's compression algorithms on the outgoing messages before they reach the provider. The system prompt and the most-recent turns are preserved exactly; older accumulated chat history is compressed.

**Result on representative data:** A 7-message Aider conversation (9,805 tokens) compresses to 2,695 tokens — **72.5% savings** — with system prompt and the last two messages preserved byte-identical.

---

## 2. Files changed

All paths relative to the repo root.

| File | Type | What it carries |
|---|---|---|
| `agent/headroom_util.py` | NEW (~241 lines) | The compression helper module — env-driven config, `maybe_compress_text`, `maybe_compress_messages`, thread-safe stats buffer, telemetry rollup |
| `agent/agents.py` | EDIT | Added `_patch_litellm_completion_with_headroom()` directly after the existing `_patch_litellm_output_config_passthrough()` (lines 49–99) |
| `pyproject.toml` | EDIT | Added `"headroom-ai>=0.24,<0.25"` to `[project].dependencies` |
| `.env.example` | EDIT | Documented four `KAIJU_HEADROOM_*` env knobs |

---

## 3. Architecture: where compression happens

```
agent/__main__.py
   ↓ imports
agent.agents
   ↓ at import time, runs:
   ├─ _patch_litellm_output_config_passthrough()   ← pre-existing (Opus 4.7 thinking depth)
   └─ _patch_litellm_completion_with_headroom()    ← NEW (compression)
        │
        │  wraps litellm.completion with a closure
        ↓
Aider's coder.run(message)
   ↓ internally calls
litellm.completion(model=..., messages=[...])
   ↓ wrapped
   ┌────────────────────────────────────────────┐
   │  if KAIJU_HEADROOM_ENABLED and msgs:       │
   │      new_msgs, stats = maybe_compress_     │
   │          messages(msgs, model=...)         │
   │      record_stats(stats)                   │
   │      kwargs["messages"] = new_msgs         │
   │  return _orig_completion(...)              │
   └────────────────────────────────────────────┘
        ↓
Provider (Bedrock / OpenAI / Vertex)
```

The patch is **at import time** and **idempotent** (guarded by `litellm._kaiju_headroom_completion_patched`). It mirrors the shape of the existing `_patch_litellm_output_config_passthrough` so reviewers can read the two patches side-by-side.

---

## 4. Coverage — all stages, all models

### All three pipeline stages

The patch sits at the universal LLM-call boundary, so every `litellm.completion` call from anywhere in the harness flows through it.

| Stage | What gets compressed | Impact |
|---|---|---|
| **Stage 1 — Draft** | System + initial user message (with spec/repo/tests blobs concatenated) | Moderate — single call, but spec PDF + repo info can be large |
| **Stage 2 — Lint Refine** | System + Stage 1 history + ruff output for the dir + each iteration's response | Significant — ruff output can be megabytes; each `--max-iteration` accumulates history |
| **Stage 3 — Test Refine** | System + Stage 1+2 history + test failure output + iterations | **Highest** — chat history compounds the worst here; this is where compression pays most |

### All seven providers

`litellm` is the universal router; every model goes through the same patched function.

| Provider | Example model | Covered? |
|---|---|---|
| Bedrock | `bedrock/converse/arn:...claude-opus-4-7-v1` (and Opus 4.6, Kimi K2.5, GLM-5, MiniMax M2.5, Nova Lite, Nova Premier) | ✅ |
| OpenAI | `openai/gpt-5.4` | ✅ |
| Vertex AI | `vertex_ai/gemini-2.5-pro`, `vertex_ai/gemini-2.5-flash`, `vertex_ai/gemini-3.1-pro-preview` | ✅ |
| Google AI Studio | `gemini/*` | ✅ |
| Anything else Aider supports | varies | ✅ |

### Per-provider tokenizer handling

`_tokenizer_model_hint()` in `headroom_util.py` adjusts how Headroom counts tokens per provider:

| Provider | Model string in | Tokenizer hint | Reason |
|---|---|---|---|
| Bedrock (opaque ARN) | `bedrock/converse/arn:...application-inference-profile/<id>` | `anthropic/claude-sonnet-4-5-20250929` | Headroom can't infer a tokenizer from an opaque profile ID; Anthropic Sonnet is protocol-compatible for counting |
| OpenAI | `openai/gpt-5.4` | passthrough | Headroom recognizes via `tiktoken` |
| Vertex AI | `vertex_ai/gemini-2.5-pro` | passthrough | Headroom uses Google's tokenizer or a sensible fallback |

**This affects token COUNTING only — the model that actually gets called is unchanged.**

---

## 5. What is preserved vs compressed

On every call, controlled by `_build_config()` in `agent/headroom_util.py`:

**Preserved verbatim:**
- The **system prompt** (`compress_system_messages=False`) — Aider's SEARCH/REPLACE format directives would silently break edits if mutated.
- The **last 2 messages** (`protect_recent=2`) — the freshest lint output / test failures / tool results always reach the model exactly as captured.

**Compressed:**
- All older user/assistant turns are eligible for the Headroom text router.
- `protect_analysis_context=False` is set explicitly — without this, Headroom routes user messages to `router:protected:user_message` and never compresses them. (This was the bug we hit during smoke-testing.)

---

## 6. Configuration — env vars

All four read **live every call** (never cached at import). Bad values fall back to defaults; the agent never raises on Headroom errors.

| Env var | Default | Meaning |
|---|---|---|
| `KAIJU_HEADROOM_ENABLED` | `true` | Master switch. Set `false` to disable without uninstalling. |
| `KAIJU_HEADROOM_TARGET_RATIO` | `0.4` | Target compression ratio (0.0–1.0). Lower = more aggressive. |
| `KAIJU_HEADROOM_MIN_TOKENS` | `2000` | Skip compression when total content is below this token count. |
| `KAIJU_HEADROOM_PROTECT_RECENT` | `2` | Number of most-recent messages to leave untouched. |

Documented in `.env.example`.

---

## 7. Safety — what happens on failure

The integration is **best-effort by design**. On any error, the original messages flow through unchanged:

- `KAIJU_HEADROOM_ENABLED=false` → instant passthrough
- Message list below `min_tokens * 4` chars → skip
- `headroom-ai` not installed → skip
- Headroom internal error → caught, logged at WARNING, original messages used
- Bad env var value → caught, default used

Inside `_patch_litellm_completion_with_headroom()` there's a **second** try/except wrapping `maybe_compress_messages` — belt-and-suspenders so a compression failure can never fail a real provider call.

---

## 8. Telemetry

`agent/headroom_util.py` exposes:

- `record_stats(stats)` — append a per-call stats dict to a thread-safe buffer.
- `drain_stats()` / `aggregate_stats(items=None)` — pull all collected stats out at the per-run write site.

`aggregate_stats()` returns a roll-up like:

```json
{
  "enabled": true,
  "calls": 12,
  "tokens_before_total": 142853,
  "tokens_after_total": 38241,
  "tokens_saved_total": 104612,
  "per_kind": {
    "aider_call": { "calls": 12, "tokens_before": 142853, "tokens_after": 38241, "tokens_saved": 104612 }
  }
}
```

**Not yet wired:** the call to `aggregate_stats()` from `agent/run_agent.py`'s per-repo result writer. The buffer exists and works; the sink into `logs/pipeline_<run_id>_results.json` is a one-line edit when you want it surfaced.

---

## 9. Verified behavior — smoke-test results

Both helpers tested end-to-end on 2026-06-11:

| Helper | Tokens before | Tokens after | Saved | Ratio |
|---|---:|---:|---:|---:|
| `maybe_compress_text` (single blob) | 5,633 | 2,672 | **2,961 (52.6%)** | 0.47 |
| `maybe_compress_messages` (7-msg Aider-shaped conversation) | 9,805 | 2,695 | **7,110 (72.5%)** | 0.27 |

Safety invariants all held:
- System prompt preserved byte-identical
- Last 2 messages preserved byte-identical
- Message count preserved (7 → 7)
- No exceptions raised
- Patch flags `_kaiju_headroom_completion_patched=True` and `_output_config_patched=True` both set after `import agent.agents`

`transforms_applied: ['router:text:0.40']` confirms the text router fired at the configured target ratio.

---

## 10. How to install and run

### Install (one-time)

```bash
cd /Users/apple/Desktop/kaiju/_support/kaiju-harness
uv lock                      # refresh lockfile with headroom-ai
uv sync --all-extras         # install all deps including aider-chat
source .venv/bin/activate
```

> **Note on compression algorithms:** the base `headroom-ai` package does not ship the text-compression algorithm (Kompress + transformers). For the smoke test we installed `headroom-ai[all]` via `uv pip install "headroom-ai[all]"`. To make compression work automatically on a fresh clone, change the dep in `pyproject.toml` from `"headroom-ai>=0.24,<0.25"` to `"headroom-ai[ml]>=0.24,<0.25"` (lighter; just the compressors) or `"headroom-ai[all]>=0.24,<0.25"` (full surface).

### Smoke-test the patches are active

```bash
.venv/bin/python -c "
import agent.agents, litellm
print('headroom patched     :', getattr(litellm, '_kaiju_headroom_completion_patched', False))
print('output_config patched:', getattr(litellm.llms.bedrock.chat.converse_transformation.AmazonConverseConfig, '_output_config_patched', False))
"
```

Both should print `True`.

### Smoke-test compression actually saves tokens

```bash
.venv/bin/python -c "
from agent.headroom_util import maybe_compress_messages
big = ('def factorial(n):\n    return 1 if n <= 1 else n * factorial(n-1)\n' * 100)
msgs = [
    {'role': 'system',    'content': 'You are an AI coding assistant. Use SEARCH/REPLACE blocks.'},
    {'role': 'user',      'content': 'repo: ' + big},
    {'role': 'assistant', 'content': 'ok ' + big},
    {'role': 'user',      'content': 'tests: ' + big},
    {'role': 'assistant', 'content': 'noted ' + big},
    {'role': 'user',      'content': 'now implement bar()'},
    {'role': 'assistant', 'content': 'here is bar()'},
]
_, stats = maybe_compress_messages(msgs, model='openai/gpt-4o', kind='smoke')
print(stats)
"
```

Expect tokens_saved > 0 and compression_ratio < 1.0.

### Run a real pipeline

```bash
# With Headroom on (default)
./run_pipeline.sh --model opus --repo-split lite --backend local --max-iteration 1

# With Headroom off (baseline for A/B)
KAIJU_HEADROOM_ENABLED=false ./run_pipeline.sh --model opus --repo-split lite --backend local --max-iteration 1
```

Then compare the last `Cost: $X.XX session` line in `logs/<run_id>/aider.log` between the two runs.

### Quick toggles while iterating

```bash
export KAIJU_HEADROOM_ENABLED=false           # disable without uninstalling
export KAIJU_HEADROOM_MIN_TOKENS=999999       # force functional-identity check
export KAIJU_HEADROOM_TARGET_RATIO=0.3        # more aggressive
export KAIJU_HEADROOM_PROTECT_RECENT=4        # less aggressive
```

---

## 11. What is NOT yet wired (optional follow-ups)

1. **Telemetry sink.** `aggregate_stats()` is implemented but not yet called from `agent/run_agent.py`'s per-repo result writer. Wire it in when you want the `headroom` block in `logs/pipeline_<run_id>_results.json`.
2. **Zone 2 precision overlay.** Smart compression at the blob loaders (`get_spec_info`, `get_repo_info`, `get_unit_tests_info`, `get_lint_info`) in `agent/agent_utils.py`. This replaces the dumb `[: max_*_length]` 10,000-char cap with a token-budget-aware compress, so you don't lose detail from a long spec before Aider ever sees it.
3. **`run_pipeline.sh` env passthrough.** Surface `KAIJU_HEADROOM_*` as named flags so operators can flip Headroom from the launch script instead of via env.
4. **Pin compression algorithms.** Decide whether to commit `headroom-ai[ml]` or `headroom-ai[all]` in `pyproject.toml` so fresh clones automatically get the compressors. Currently only base `headroom-ai` is locked.

---

## 12. Validation steps before merging

1. `uv lock && uv sync --all-extras` — refresh dependency tree.
2. With `KAIJU_HEADROOM_ENABLED=false`, run a small repo Stage 1 → confirm baseline output matches pre-Headroom byte-for-byte.
3. With `KAIJU_HEADROOM_MIN_TOKENS=999999`, run the same → confirm functional identity (compression skipped on every call).
4. With defaults on, run a Python repo Stage 1 → confirm Aider's SEARCH/REPLACE edits still apply (broken system prompt would fail visibly).
5. Compare Stage 3 cost with/without Headroom on one repo → ROI signal.

---

## 13. References

- Headroom library: <https://github.com/chopratejas/headroom>
- WildClawBench reference integration (judge council, urllib + LiteLLM library mode): <https://github.com/Ethara-Ai/WildClawBench/commit/ad670b846d18dc8f82700dcb7931ce4e3e60b5fa>
- Existing kaiju-harness litellm monkey-patch pattern: `agent/agents.py` lines 16–46 (`_patch_litellm_output_config_passthrough`)
