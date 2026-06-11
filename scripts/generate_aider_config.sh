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
        "max_input_tokens": 200000,
        "max_output_tokens": 128000,
        "max_tokens": 128000,
        "mode": "chat",
        "edit_format": "diff",
        "input_cost_per_token": 5e-6,
        "output_cost_per_token": 2.5e-5,
        "cache_read_input_token_cost": 5e-7,
        "cache_creation_input_token_cost": 6.25e-6,
        "litellm_provider": "vertex_ai",
        "supports_function_calling": True,
        "supports_system_messages": True,
        "supports_tool_choice": True,
        "supports_vision": True,
        "supports_prompt_caching": True,
        "supports_assistant_prefill": True,
    }
    meta["vertex_ai/claude-opus-4-8"] = {
        "cache_creation_input_token_cost": 6.25e-6,
        "cache_creation_input_token_cost_above_1hr": 1e-5,
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
  overeager: true
  extra_params:
    max_tokens: 128000""")
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

print("\n\n".join(out))
PYEOF

echo "aider configs regenerated in ${ROOT}" >&2
