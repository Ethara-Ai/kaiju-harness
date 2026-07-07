#!/usr/bin/env bash
# Generates .aider.model.metadata.json and .aider.model.settings.yml
# in the repo root from ARN environment variables defined in .env.
# Called automatically by pipeline scripts; safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "${ROOT}/.env"
  set +a
fi

python3 - >"${ROOT}/.aider.model.metadata.json" <<'PYEOF'
import json, os

meta = {}

# D6: Bedrock Claude deployments advertise a 200K context window, whereas the
# SAME model family is 1M on Anthropic-direct and Vertex (see the anthropic/ and
# vertex_ai/ entries below). This asymmetry is INTENTIONAL and per-deployment:
# under-claiming on Bedrock is the safe direction — aider truncates the repo map
# to fit rather than sending an over-length request that Bedrock would 400. Do
# NOT bump Bedrock to 1M without confirming the deployment actually accepts it.
# Always included — no ARN required
meta["bedrock/converse/global.anthropic.claude-opus-4-6-v1"] = {
    "max_input_tokens": 200000,
    "max_output_tokens": 32000,
    "max_tokens": 32000,
    "mode": "chat",
    "edit_format": "diff",
    "input_cost_per_token": 5e-6,
    "output_cost_per_token": 2.5e-5,
    "cache_creation_input_token_cost": 6.25e-6,
    "cache_read_input_token_cost": 5e-7,
    "litellm_provider": "bedrock",
    "supports_function_calling": True,
    "supports_system_messages": True,
    "supports_tool_choice": True,
    "supports_vision": True,
    "supports_prompt_caching": True,
    "supports_assistant_prefill": True,
}

# Populated only when the matching env var is set
ARN_CONFIGS = {
    "BEDROCK_OPUS_ARN": {
        "max_input_tokens": 200000,
        "max_output_tokens": 32000,
        "max_tokens": 32000,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 2.5e-5,
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_read_input_token_cost": 5e-7,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_prompt_caching": True,
        "supports_assistant_prefill": True,
    },
    "BEDROCK_KIMI_ARN": {
        "max_input_tokens": 262144,
        "max_output_tokens": 16384,
        "max_tokens": 16384,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 6e-7,
        "output_cost_per_token": 3e-6,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_assistant_prefill": False,
    },
    "BEDROCK_GLM5_ARN": {
        "max_input_tokens": 202752,
        "max_output_tokens": 32768,
        "max_tokens": 32768,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 1e-6,
        "output_cost_per_token": 3.2e-6,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": False,
        "supports_assistant_prefill": False,
    },
    "BEDROCK_MINIMAX_ARN": {
        "max_input_tokens": 196608,
        "max_output_tokens": 8192,
        "max_tokens": 8192,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 3e-7,
        "output_cost_per_token": 1.2e-6,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": False,
        "supports_assistant_prefill": False,
    },
    "BEDROCK_NOVA2_LITE_ARN": {
        "max_input_tokens": 1000000,
        "max_output_tokens": 65535,
        "max_tokens": 65535,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 3e-7,
        "output_cost_per_token": 2.5e-6,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_assistant_prefill": True,
    },
    "BEDROCK_NOVA_PREMIER_ARN": {
        "max_input_tokens": 1000000,
        "max_output_tokens": 25000,
        "max_tokens": 25000,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 2.5e-6,
        "output_cost_per_token": 1e-5,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_assistant_prefill": True,
    },
    "BEDROCK_OPUS47_ARN": {
        "max_input_tokens": 200000,
        "max_output_tokens": 128000,
        "max_tokens": 128000,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 2.5e-5,
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_read_input_token_cost": 5e-7,
        "litellm_provider": "bedrock",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_prompt_caching": True,
        "supports_assistant_prefill": True,
    },
}

for env_var, cfg in ARN_CONFIGS.items():
    arn_raw = os.environ.get(env_var, "").strip()
    if arn_raw:
        arn = arn_raw if arn_raw.startswith("bedrock/") else f"bedrock/converse/{arn_raw}"
        meta[arn] = cfg

# Anthropic-direct entries (used by the Claude Code OAuth bridge when --use-claude-code is set,
# or any time the user runs --model anthropic/<id>). Always emit -- harmless if not used.
_ANTHROPIC_DIRECT = {
    "anthropic/claude-opus-4-7": {
        "max_input_tokens": 1000000, "max_output_tokens": 128000, "max_tokens": 128000,
        "mode": "chat", "edit_format": "diff",
        "input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5,
        "cache_creation_input_token_cost": 6.25e-6, "cache_read_input_token_cost": 5e-7,
        "litellm_provider": "anthropic",
        "supports_function_calling": True, "supports_system_messages": True,
        "supports_tool_choice": True, "supports_vision": True,
        "supports_prompt_caching": True, "supports_assistant_prefill": True,
    },
    "anthropic/claude-opus-4-8": {
        "max_input_tokens": 1000000, "max_output_tokens": 128000, "max_tokens": 128000,
        "mode": "chat", "edit_format": "diff",
        "input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5,
        "cache_creation_input_token_cost": 6.25e-6, "cache_read_input_token_cost": 5e-7,
        "litellm_provider": "anthropic",
        "supports_function_calling": True, "supports_system_messages": True,
        "supports_tool_choice": True, "supports_vision": True,
        "supports_prompt_caching": True, "supports_assistant_prefill": True,
    },
    "anthropic/claude-sonnet-4-6": {
        "max_input_tokens": 200000, "max_output_tokens": 64000, "max_tokens": 64000,
        "mode": "chat", "edit_format": "diff",
        "input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5,
        "cache_creation_input_token_cost": 3.75e-6, "cache_read_input_token_cost": 3e-7,
        "litellm_provider": "anthropic",
        "supports_function_calling": True, "supports_system_messages": True,
        "supports_tool_choice": True, "supports_vision": True,
        "supports_prompt_caching": True, "supports_assistant_prefill": True,
    },
    "anthropic/claude-haiku-4-5-20251001": {
        "max_input_tokens": 200000, "max_output_tokens": 64000, "max_tokens": 64000,
        "mode": "chat", "edit_format": "diff",
        "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
        "cache_creation_input_token_cost": 1.25e-6, "cache_read_input_token_cost": 1e-7,
        "litellm_provider": "anthropic",
        "supports_function_calling": True, "supports_system_messages": True,
        "supports_tool_choice": True, "supports_vision": True,
        "supports_prompt_caching": True, "supports_assistant_prefill": True,
    },
    "anthropic/claude-haiku-4-5": {
        "max_input_tokens": 200000, "max_output_tokens": 64000, "max_tokens": 64000,
        "mode": "chat", "edit_format": "diff",
        "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
        "cache_creation_input_token_cost": 1.25e-6, "cache_read_input_token_cost": 1e-7,
        "litellm_provider": "anthropic",
        "supports_function_calling": True, "supports_system_messages": True,
        "supports_tool_choice": True, "supports_vision": True,
        "supports_prompt_caching": True, "supports_assistant_prefill": True,
    },
}
meta.update(_ANTHROPIC_DIRECT)

if os.environ.get("VERTEX_AI_API_KEY", "").strip() or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
    meta["vertex_ai/gemini-3.1-pro-preview"] = {
        "max_input_tokens": 1048576,
        "max_output_tokens": 65536,
        "max_tokens": 65536,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 2e-6,
        "output_cost_per_token": 1.2e-5,
        "cache_read_input_token_cost": 2e-7,
        "litellm_provider": "vertex_ai",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_prompt_caching": True,
        "supports_assistant_prefill": False,
    }
    meta["vertex_ai/gemini-2.5-pro"] = {
        "max_input_tokens": 1048576,
        "max_output_tokens": 65536,
        "max_tokens": 65536,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 1.25e-6,
        "output_cost_per_token": 1.0e-5,
        "cache_read_input_token_cost": 3.125e-7,
        "litellm_provider": "vertex_ai",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_prompt_caching": True,
        "supports_assistant_prefill": False,
    }
    meta["vertex_ai/gemini-2.5-flash"] = {
        "max_input_tokens": 1048576,
        "max_output_tokens": 65536,
        "max_tokens": 65536,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 3.0e-7,
        "output_cost_per_token": 2.5e-6,
        "cache_read_input_token_cost": 7.5e-8,
        "litellm_provider": "vertex_ai",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_prompt_caching": True,
        "supports_assistant_prefill": False,
    }
    meta["vertex_ai/claude-opus-4-7"] = {
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_read_input_token_cost": 5e-7,
        "input_cost_per_token": 5e-6,
        "litellm_provider": "vertex_ai-anthropic_models",
        "max_input_tokens": 1000000,
        "max_output_tokens": 128000,
        "max_tokens": 128000,
        "mode": "chat",
        "edit_format": "diff",
        "output_cost_per_token": 2.5e-5,
        "search_context_cost_per_query": {
            "search_context_size_high": 0.01,
            "search_context_size_low": 0.01,
            "search_context_size_medium": 0.01,
        },
        "supports_adaptive_thinking": True,
        "supports_assistant_prefill": False,
        "supports_computer_use": True,
        "supports_function_calling": True,
        "supports_pdf_input": True,
        "supports_prompt_caching": True,
        "supports_reasoning": True,
        "supports_response_schema": True,
        "supports_sampling_params": False,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_xhigh_reasoning_effort": True,
        "supports_max_reasoning_effort": True,
    }
    meta["vertex_ai/claude-opus-4-8"] = {
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_read_input_token_cost": 5e-7,
        "input_cost_per_token": 5e-6,
        "litellm_provider": "vertex_ai-anthropic_models",
        "max_input_tokens": 1000000,
        "max_output_tokens": 128000,
        "max_tokens": 128000,
        "mode": "chat",
        "edit_format": "diff",
        "output_cost_per_token": 2.5e-5,
        "search_context_cost_per_query": {
            "search_context_size_high": 0.01,
            "search_context_size_low": 0.01,
            "search_context_size_medium": 0.01,
        },
        "supports_adaptive_thinking": True,
        "supports_assistant_prefill": False,
        "supports_computer_use": True,
        "supports_function_calling": True,
        "supports_pdf_input": True,
        "supports_prompt_caching": True,
        "supports_reasoning": True,
        "supports_response_schema": True,
        "supports_sampling_params": False,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_xhigh_reasoning_effort": True,
        "supports_max_reasoning_effort": True,
    }
    meta["vertex_ai/claude-sonnet-4-6"] = {
        "cache_creation_input_token_cost": 3.75e-6,
        "cache_read_input_token_cost": 3e-7,
        "input_cost_per_token": 3e-6,
        "litellm_provider": "vertex_ai-anthropic_models",
        "max_input_tokens": 1000000,
        "max_output_tokens": 128000,
        "max_tokens": 128000,
        "mode": "chat",
        "edit_format": "diff",
        "output_cost_per_token": 1.5e-5,
        "search_context_cost_per_query": {
            "search_context_size_high": 0.01,
            "search_context_size_low": 0.01,
            "search_context_size_medium": 0.01,
        },
        "supports_adaptive_thinking": True,
        "supports_assistant_prefill": False,
        "supports_computer_use": True,
        "supports_function_calling": True,
        "supports_pdf_input": True,
        "supports_prompt_caching": True,
        "supports_reasoning": True,
        "supports_response_schema": True,
        "supports_sampling_params": False,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_max_reasoning_effort": True,
    }

meta["openai/gpt-5.5-2026-04-23"] = {
    "max_input_tokens": 1050000,
    "max_output_tokens": 128000,
    "max_tokens": 128000,
    "mode": "responses",
    "litellm_provider": "openai",
    "input_cost_per_token": 5e-6,
    "input_cost_per_token_above_272k_tokens": 1e-5,
    "input_cost_per_token_flex": 2.5e-6,
    "input_cost_per_token_batches": 2.5e-6,
    "input_cost_per_token_priority": 1e-5,
    "output_cost_per_token": 3e-5,
    "output_cost_per_token_above_272k_tokens": 4.5e-5,
    "output_cost_per_token_flex": 1.5e-5,
    "output_cost_per_token_batches": 1.5e-5,
    "output_cost_per_token_priority": 6e-5,
    "cache_read_input_token_cost": 5e-7,
    "cache_read_input_token_cost_above_272k_tokens": 1e-6,
    "cache_read_input_token_cost_flex": 2.5e-7,
    "cache_read_input_token_cost_priority": 1e-6,
    "supported_endpoints": ["/v1/chat/completions", "/v1/batch", "/v1/responses"],
    "supported_modalities": ["text", "image"],
    "supported_output_modalities": ["text"],
    "supports_function_calling": True,
    "supports_native_streaming": True,
    "supports_parallel_function_calling": True,
    "supports_pdf_input": True,
    "supports_prompt_caching": True,
    "supports_reasoning": True,
    "supports_response_schema": True,
    "supports_system_messages": True,
    "supports_tool_choice": True,
    "supports_service_tier": True,
    "supports_vision": True,
    "supports_web_search": True,
    "supports_none_reasoning_effort": True,
    "supports_xhigh_reasoning_effort": True,
    "supports_minimal_reasoning_effort": False,
}

# D7: the YAML settings file carries a GENERATED banner (a comment). We do NOT
# add a sentinel KEY to this metadata JSON — aider treats every top-level value
# as a model-info dict, so a non-dict marker could break parsing. The settings
# banner + this generator's provenance are sufficient.
print(json.dumps(meta, indent=2))
PYEOF

python3 - >"${ROOT}/.aider.model.settings.yml" <<'PYEOF'
import os

out = []

# Always included — no ARN required
out.append("""\
- name: bedrock/converse/global.anthropic.claude-opus-4-6-v1
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: false
  use_temperature: false
  extra_params:
    max_tokens: 32000
    thinking:
      type: enabled
      budget_tokens: 10000
  cache_control: true
  reasoning_tag: thinking
  remove_reasoning: thinking
  accepts_settings:
    - thinking_tokens""")

ARN_SETTINGS = {
    "BEDROCK_OPUS_ARN": {
        "use_repo_map": "true",
        "extra": """\
  extra_params:
    max_tokens: 32000
    thinking:
      type: enabled
      budget_tokens: 10000
  cache_control: true
  reasoning_tag: thinking
  remove_reasoning: thinking
  accepts_settings:
    - thinking_tokens""",
    },
    "BEDROCK_NOVA2_LITE_ARN": {
        "use_repo_map": "true",
        "extra": "  extra_params:\n    max_tokens: 65535",
    },
    "BEDROCK_NOVA_PREMIER_ARN": {
        "use_repo_map": "true",
        "extra": "  extra_params:\n    max_tokens: 25000",
    },
    "BEDROCK_GLM5_ARN": {
        "use_repo_map": "true",
        "extra": "  extra_params:\n    max_tokens: 32768",
    },
    "BEDROCK_KIMI_ARN": {
        "use_repo_map": "true",
        "extra": "  extra_params:\n    max_tokens: 16384",
    },
    "BEDROCK_MINIMAX_ARN": {
        "use_repo_map": "true",
        "extra": "  extra_params:\n    max_tokens: 8192",
    },
    "BEDROCK_OPUS47_ARN": {
        "use_repo_map": "false",
        "extra": """\
  extra_params:
    max_tokens: 128000
    thinking:
      type: adaptive
      display: summarized
    output_config:
      effort: high
  cache_control: true
  reasoning_tag: thinking
  remove_reasoning: thinking""",
    },
}

for env_var, cfg in ARN_SETTINGS.items():
    arn_raw = os.environ.get(env_var, "").strip()
    if arn_raw:
        arn = arn_raw if arn_raw.startswith("bedrock/") else f"bedrock/converse/{arn_raw}"
        entry = (
            f"- name: {arn}\n"
            f"  edit_format: diff\n"
            f"  use_repo_map: {cfg['use_repo_map']}\n"
            f"  examples_as_sys_msg: false\n"
            f"  use_temperature: false\n"
            + cfg["extra"]
        )
        out.append(entry)

# Anthropic-direct settings (always emitted; bridge users get cache_control + thinking).
# IMPORTANT: Opus 4.7/4.8 REMOVED `thinking: {type: enabled, budget_tokens: N}` — sending it
# returns HTTP 400. Adaptive thinking is the only valid on-mode; reasoning depth is controlled
# by output_config.effort (max = deepest). We also use the model's FULL 128K output budget
# rather than an arbitrary 32K cap, so the model isn't cut off mid-response.
#   - Opus 4.7/4.8 -> adaptive thinking + effort:max + max_tokens:128000 (full output)
#   - Sonnet 4.6   -> adaptive thinking + effort:high + max_tokens:64000 (Sonnet's output cap)
#   - Haiku 4.5    -> legacy enabled+budget (Haiku does not support adaptive/effort)
# Do NOT advertise `thinking_tokens` for adaptive models — it would let a budget be injected
# and re-trigger the 400.
# NOTE on effort: the installed litellm build only permits effort='max' for Opus 4.6
# (anthropic/chat/transformation.py) and rejects 'xhigh' as an invalid value, so for
# Opus 4.7/4.8 the accepted ceiling is 'high'. Adaptive thinking remains unbounded
# (no fixed budget_tokens), so reasoning depth is still high. Using 'max' here makes
# litellm raise before the request is sent (0 tokens, $0 cost, empty trajectory).
# num_retries (Option A): litellm re-issues the whole completion transparently on
# a transient failure — including a mid-stream drop ("peer closed connection /
# incomplete chunked read") — BEFORE aider ever sees an error. This is the
# primary handler for the connection-drop we hit on the large service_info turn:
# the module can't be left half-implemented because the failed call is retried at
# the call level. cache_control:true makes each retry cheap (the big context is
# served from the prompt cache).
_ANTHROPIC_OPUS_BLOCK = """\
  extra_params:
    max_tokens: 128000
    num_retries: 3
    thinking:
      type: adaptive
      display: summarized
    output_config:
      effort: high
  cache_control: true
  reasoning_tag: thinking
  remove_reasoning: thinking"""

_ANTHROPIC_SONNET_BLOCK = """\
  extra_params:
    max_tokens: 64000
    num_retries: 3
    thinking:
      type: adaptive
      display: summarized
    output_config:
      effort: high
  cache_control: true
  reasoning_tag: thinking
  remove_reasoning: thinking"""

_ANTHROPIC_HAIKU_BLOCK = """\
  extra_params:
    max_tokens: 32000
    thinking:
      type: enabled
      budget_tokens: 10000
  cache_control: true
  reasoning_tag: thinking
  remove_reasoning: thinking
  accepts_settings:
    - thinking_tokens"""

_ANTHROPIC_DIRECT_SETTINGS = {
    "anthropic/claude-opus-4-7": _ANTHROPIC_OPUS_BLOCK,
    "anthropic/claude-opus-4-8": _ANTHROPIC_OPUS_BLOCK,
    "anthropic/claude-sonnet-4-6": _ANTHROPIC_SONNET_BLOCK,
    "anthropic/claude-haiku-4-5-20251001": _ANTHROPIC_HAIKU_BLOCK,
    "anthropic/claude-haiku-4-5": _ANTHROPIC_HAIKU_BLOCK,
}
for _name, _block in _ANTHROPIC_DIRECT_SETTINGS.items():
    out.append(f"""\
- name: {_name}
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: false
  use_temperature: false
{_block}""")

if os.environ.get("VERTEX_AI_API_KEY", "").strip() or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
    out.append("""\
- name: vertex_ai/gemini-3.1-pro-preview
  edit_format: diff
  use_repo_map: false
  examples_as_sys_msg: false
  use_temperature: 1.0
  cache_control: false
  extra_params:
    max_tokens: 65536
    reasoning_effort: high""")
    out.append("""\
- name: vertex_ai/gemini-2.5-pro
  edit_format: diff
  use_repo_map: false
  examples_as_sys_msg: false
  use_temperature: 1.0
  cache_control: false
  extra_params:
    max_tokens: 65536
    reasoning_effort: high""")
    out.append("""\
- name: vertex_ai/gemini-2.5-flash
  edit_format: diff
  use_repo_map: false
  examples_as_sys_msg: false
  use_temperature: 1.0
  cache_control: false
  extra_params:
    max_tokens: 65536
    reasoning_effort: high""")
    out.append("""\
- name: vertex_ai/claude-opus-4-7
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: false
  use_temperature: false
  cache_control: true
  overeager: false
  reasoning_tag: thinking
  remove_reasoning: thinking
  extra_params:
    max_tokens: 128000
    thinking:
      type: adaptive
      display: summarized""")
    out.append("""\
- name: vertex_ai/claude-opus-4-8
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: false
  use_temperature: false
  cache_control: true
  overeager: false
  reasoning_tag: thinking
  remove_reasoning: thinking
  extra_params:
    max_tokens: 128000
    thinking:
      type: adaptive
      display: summarized""")
    out.append("""\
- name: vertex_ai/claude-sonnet-4-6
  edit_format: diff
  use_repo_map: true
  examples_as_sys_msg: false
  use_temperature: false
  cache_control: true
  overeager: false
  reasoning_tag: thinking
  remove_reasoning: thinking
  extra_params:
    max_tokens: 128000
    thinking:
      type: adaptive
      display: summarized""")

if os.environ.get("OPENAI_API_KEY", "").strip():
    out.append("""\
- name: openai/gpt-5.5-2026-04-23
  edit_format: diff
  use_repo_map: false
  examples_as_sys_msg: false
  use_temperature: 1.0
  cache_control: false
  streaming: false
  extra_params:
    reasoning_effort: high
    num_retries: 3""")

# D7: prepend a GENERATED banner so a human who hand-edits this file realizes it
# is machine-owned and regenerated on every pipeline launch (silently discarding
# manual hot-patches otherwise). Edits belong in scripts/generate_aider_config.sh.
_BANNER = (
    "# ===========================================================================\n"
    "# GENERATED FILE — DO NOT EDIT BY HAND.\n"
    "# Regenerated on every pipeline launch by scripts/generate_aider_config.sh.\n"
    "# Any manual edits here are SILENTLY OVERWRITTEN. Change the generator instead.\n"
    "# ===========================================================================\n"
)
print(_BANNER + "\n" + "\n\n".join(out))
PYEOF

# D2: validate the generated Anthropic settings against the INSTALLED litellm's
# transformation rules BEFORE any run. Both prior outages were a config that the
# installed litellm rejected (effort=max only on opus-4-6; thinking shapes). This
# fails config-generation loudly instead of 3 hours into a run with 0/N output.
python3 - "${ROOT}/.aider.model.settings.yml" <<'VALEOF' || { echo "FATAL: aider config failed litellm-invariant validation (see above)" >&2; exit 1; }
import sys, yaml
path = sys.argv[1]
try:
    import yaml as _y
    entries = _y.safe_load(open(path)) or []
except Exception as e:
    print(f"VALIDATION: could not read {path}: {e}", file=sys.stderr); sys.exit(0)
try:
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    cfg = AnthropicConfig()
except Exception as e:
    # Don't silently pass — this is the guard that prevents an outage. We can't
    # hard-fail (it would block every run on a litellm refactor), but make it LOUD
    # so it's visible; the representative preflight (resolve_model.sh) is the
    # runtime backstop that still catches a bad param set.
    print(f"VALIDATION WARNING: litellm AnthropicConfig unavailable ({e}); could NOT "
          f"validate effort/thinking at config-gen — relying on the preflight probe.",
          file=sys.stderr)
    sys.exit(0)
problems = []
# Validate every Claude entry, not just anthropic/ — bedrock/converse and
# vertex_ai claude entries also carry output_config.effort.
def _is_claude(n):
    nl = n.lower()
    return ("claude" in nl) or n.startswith("anthropic/")
for e in entries:
    if not isinstance(e, dict):
        continue
    name = e.get("name", "")
    if not _is_claude(name):
        continue
    ep = e.get("extra_params") or {}
    eff = (ep.get("output_config") or {}).get("effort")
    if eff is None:
        continue
    accepted = ["high", "medium", "low", "max"]
    if eff not in accepted:
        problems.append(f"{name}: effort={eff!r} not in {accepted} (this litellm build rejects it)")
    if eff == "max":
        is46 = getattr(cfg, "_is_opus_4_6_model", lambda m: "opus-4-6" in m or "opus-4.6" in m)(name)
        if not is46:
            problems.append(f"{name}: effort='max' only supported on Opus 4.6 by this litellm build")
if problems:
    print("VALIDATION FAILED:", file=sys.stderr)
    for p in problems:
        print("  -", p, file=sys.stderr)
    sys.exit(1)
print("VALIDATION: anthropic effort/thinking settings OK for installed litellm", file=sys.stderr)
VALEOF

echo "aider configs regenerated in ${ROOT}" >&2
