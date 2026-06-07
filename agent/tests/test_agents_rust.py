"""Tests for agent/agents.py — pricing, logging, AiderReturn, AiderAgents."""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

MODULE = "agent.agents"


# ---------------------------------------------------------------------------
# _resolve_base_model_from_arn + env-driven profile map
# ---------------------------------------------------------------------------
class TestResolveBaseModelFromArn:
    """The profile-ID -> base-model map is built from BEDROCK_*_ARN env vars.

    Each test monkeypatches the env, rebuilds the module-level cache, then
    asserts the resolver returns the expected base model.
    """

    @staticmethod
    def _rebuild_map_with(monkeypatch, env):
        """Clear all BEDROCK_*_ARN vars, set the ones provided, rebuild cache."""
        import agent.agents as ag
        for key in list(os.environ):
            if key.startswith("BEDROCK_") and key.endswith("_ARN"):
                monkeypatch.delenv(key, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        ag._ARN_PROFILE_TO_BASE_MODEL = ag._build_arn_profile_map()

    def test_known_profile_id(self, monkeypatch):
        from agent.agents import _resolve_base_model_from_arn

        self._rebuild_map_with(monkeypatch, {
            "BEDROCK_OPUS_ARN": "arn:aws:bedrock:us-east-1:123:application-inference-profile/4w7tmk1iplxi",
        })
        # boto3 resolution fails silently under bearer-auth; static path wins
        result = _resolve_base_model_from_arn(
            "arn:aws:bedrock:us-east-1:123:application-inference-profile/4w7tmk1iplxi"
        )
        assert result == "anthropic.claude-opus-4-6-v1"

    def test_kimi_profile(self, monkeypatch):
        from agent.agents import _resolve_base_model_from_arn

        self._rebuild_map_with(monkeypatch, {
            "BEDROCK_KIMI_ARN": "arn:aws:bedrock:us-east-1:123:application-inference-profile/5m69567zugvx",
        })
        result = _resolve_base_model_from_arn("something-5m69567zugvx-something")
        assert result == "moonshotai.kimi-k2.5"

    def test_nova_lite_profile(self, monkeypatch):
        from agent.agents import _resolve_base_model_from_arn

        self._rebuild_map_with(monkeypatch, {
            "BEDROCK_NOVA2_LITE_ARN": "arn:aws:bedrock:us-east-1:123:application-inference-profile/lv09a6pe7jzv",
        })
        result = _resolve_base_model_from_arn("prefix-lv09a6pe7jzv-suffix")
        assert result == "amazon.nova-2-lite-v1:0"

    def test_nova_premier_profile(self, monkeypatch):
        from agent.agents import _resolve_base_model_from_arn

        self._rebuild_map_with(monkeypatch, {
            "BEDROCK_NOVA_PREMIER_ARN": "arn:aws:bedrock:us-east-1:123:application-inference-profile/td6kwwwp7q0e",
        })
        result = _resolve_base_model_from_arn("prefix-td6kwwwp7q0e-suffix")
        assert result == "amazon.nova-premier-v1:0"

    def test_unknown_profile_returns_none(self, monkeypatch):
        from agent.agents import _resolve_base_model_from_arn

        # Empty env -> empty map, unknown ARN -> boto3 path fails -> None
        self._rebuild_map_with(monkeypatch, {})
        # Use an ARN that won't match any real AWS profile so boto3 returns nothing
        result = _resolve_base_model_from_arn(
            "arn:aws:bedrock:us-east-1:000:application-inference-profile/completely-unknown-xxxxxx"
        )
        assert result is None

    def test_unset_alias_not_in_map(self, monkeypatch):
        """If BEDROCK_GLM5_ARN is unset, glm-5's profile ID must not appear in the map."""
        from agent.agents import _build_arn_profile_map

        self._rebuild_map_with(monkeypatch, {
            "BEDROCK_OPUS_ARN": "arn:aws:bedrock:us-east-1:123:application-inference-profile/opusxxxxxxxx",
        })
        rebuilt = _build_arn_profile_map()
        assert "zai.glm-5" not in rebuilt.values()
        assert "anthropic.claude-opus-4-6-v1" in rebuilt.values()


class TestExtractProfileId:
    def test_bare_arn(self):
        from agent.agents import _extract_profile_id
        assert _extract_profile_id(
            "arn:aws:bedrock:us-east-1:123:application-inference-profile/abc123"
        ) == "abc123"

    def test_routed_arn(self):
        from agent.agents import _extract_profile_id
        assert _extract_profile_id(
            "bedrock/converse/arn:aws:bedrock:us-east-1:123:application-inference-profile/xyz789"
        ) == "xyz789"

    def test_non_arn_rejected(self):
        from agent.agents import _extract_profile_id
        assert _extract_profile_id("/onlyslash") is None
        assert _extract_profile_id("foo/bar") is None
        assert _extract_profile_id("") is None
        assert _extract_profile_id("not-an-arn") is None


# ---------------------------------------------------------------------------
# register_bedrock_arn_pricing
# ---------------------------------------------------------------------------
class TestRegisterBedrockArnPricing:
    """Tests for register_bedrock_arn_pricing.

    Uses a mock litellm module injected via sys.modules. After the refactor,
    pricing lookup now reads ``litellm.model_cost[base_model_id]`` instead
    of an in-repo pricing dict, so mocks must seed base-model entries.
    """

    # Prices chosen to match real GLM-5 / Kimi so the assertions stay meaningful.
    _KIMI_PRICE = {
        "input_cost_per_token": 6e-07,
        "output_cost_per_token": 3e-06,
        "litellm_provider": "bedrock",
    }
    _GLM5_PRICE = {
        "input_cost_per_token": 1e-06,
        "output_cost_per_token": 3.2e-06,
        "litellm_provider": "bedrock",
    }

    @staticmethod
    def _make_mock_litellm(initial_cost=None):
        import types
        m = types.ModuleType('litellm')
        m.model_cost = dict(initial_cost or {})
        return m

    def _run_with_mock_litellm(self, mock_litellm, extra_modules=None):
        """Context-manage sys.modules so agent.agents sees our mock litellm."""
        modules = {'litellm': mock_litellm}
        if extra_modules:
            modules.update(extra_modules)
        return patch.dict('sys.modules', modules)

    @staticmethod
    def _rebuild_profile_map(monkeypatch, env):
        """Set BEDROCK_*_ARN vars and rebuild the module-level cache."""
        import agent.agents as ag
        for key in list(os.environ):
            if key.startswith("BEDROCK_") and key.endswith("_ARN"):
                monkeypatch.delenv(key, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        ag._ARN_PROFILE_TO_BASE_MODEL = ag._build_arn_profile_map()

    def test_non_arn_returns_early(self):
        mock_litellm = self._make_mock_litellm()
        with self._run_with_mock_litellm(mock_litellm):
            from agent.agents import register_bedrock_arn_pricing
            register_bedrock_arn_pricing('openai/gpt-4')
        assert len(mock_litellm.model_cost) == 0

    def test_boto3_resolution_success(self, monkeypatch):
        # Seed mock litellm.model_cost with kimi pricing so the lookup finds it.
        mock_litellm = self._make_mock_litellm({
            'moonshotai.kimi-k2.5': self._KIMI_PRICE.copy(),
        })
        model_name = 'bedrock/converse/arn:aws:bedrock:us-east-1:123:inference-profile/test-boto3-success'

        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_inference_profile.return_value = {
            'models': [{'modelArn': 'arn:aws:bedrock:us-east-1::foundation-model/moonshotai.kimi-k2.5'}]
        }

        with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
            from agent.agents import register_bedrock_arn_pricing
            register_bedrock_arn_pricing(model_name)

        assert model_name in mock_litellm.model_cost
        assert mock_litellm.model_cost[model_name]['litellm_provider'] == 'bedrock'

    def test_static_map_fallback(self, monkeypatch):
        # Seed litellm.model_cost + env so the profile-ID -> kimi map is built.
        mock_litellm = self._make_mock_litellm({
            'moonshotai.kimi-k2.5': self._KIMI_PRICE.copy(),
        })
        model_name = 'bedrock/converse/arn:aws:bedrock:us-east-1:999:application-inference-profile/5m69567zugvx-staticfallback'
        self._rebuild_profile_map(monkeypatch, {
            'BEDROCK_KIMI_ARN': 'arn:aws:bedrock:us-east-1:999:application-inference-profile/5m69567zugvx-staticfallback',
        })

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception('boto3 not configured')
        with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
            from agent.agents import register_bedrock_arn_pricing
            register_bedrock_arn_pricing(model_name)

        assert model_name in mock_litellm.model_cost
        assert mock_litellm.model_cost[model_name]['litellm_provider'] == 'bedrock'

    def test_already_registered_skips(self):
        model_name = 'bedrock/converse/arn:aws:bedrock:us-east-1:999:application-inference-profile/5m69567zugvx-alreadyreg'
        sentinel = {
            'test_sentinel': True,
            'litellm_provider': 'bedrock',
            'input_cost_per_token': 0.00001,
        }
        mock_litellm = self._make_mock_litellm({model_name: sentinel.copy()})

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception('boto3 not configured')
        with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
            from agent.agents import register_bedrock_arn_pricing
            register_bedrock_arn_pricing(model_name)

        assert mock_litellm.model_cost[model_name].get('test_sentinel') is True

    def test_region_extraction_from_arn(self, monkeypatch):
        for region in ('us-east-1', 'eu-west-1', 'ap-south-1', 'sa-east-1'):
            mock_litellm = self._make_mock_litellm({
                'moonshotai.kimi-k2.5': self._KIMI_PRICE.copy(),
            })
            model_name = f'bedrock/converse/arn:aws:bedrock:{region}:123:application-inference-profile/5m69567zugvx-{region}'
            self._rebuild_profile_map(monkeypatch, {
                'BEDROCK_KIMI_ARN': f'arn:aws:bedrock:{region}:123:application-inference-profile/5m69567zugvx-{region}',
            })

            mock_boto3 = MagicMock()
            mock_boto3.client.side_effect = Exception('force static fallback')
            with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
                from agent.agents import register_bedrock_arn_pricing
                register_bedrock_arn_pricing(model_name)

            assert model_name in mock_litellm.model_cost, f'Failed for region {region}'

    def test_unresolvable_arn_logs_warning(self, caplog, monkeypatch):
        # Empty env + no boto3 -> no base model resolvable -> warning emitted.
        mock_litellm = self._make_mock_litellm()
        model_name = 'bedrock/converse/arn:aws:bedrock:us-east-1:123:application-inference-profile/zzz-totally-unknown-zzz'
        self._rebuild_profile_map(monkeypatch, {})  # clear all BEDROCK_*_ARN

        mock_boto3 = MagicMock()
        mock_boto3.client.side_effect = Exception('force static fallback')
        import logging
        with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
            from agent.agents import register_bedrock_arn_pricing
            with caplog.at_level(logging.WARNING):
                register_bedrock_arn_pricing(model_name)

        assert model_name not in mock_litellm.model_cost
        assert any(
            'Could not resolve base model' in r.message
            or 'costs will report' in r.message
            for r in caplog.records
        )

    def test_boto3_resolves_via_substring_scan(self):
        # After refactor, the substring scan in _litellm_pricing_for_base_model
        # finds entries containing the base-model name anywhere in the key.
        mock_litellm = self._make_mock_litellm({
            'bedrock/us-east-1/some-unknown-model-v1': {
                'input_cost_per_token': 0.001,
                'output_cost_per_token': 0.002,
                'litellm_provider': 'bedrock',
            },
        })
        model_name = 'bedrock/converse/arn:aws:bedrock:us-east-1:123:inference-profile/regionkey-test'

        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_inference_profile.return_value = {
            'models': [{'modelArn': 'arn:aws:bedrock:us-east-1::foundation-model/some-unknown-model-v1'}]
        }

        with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
            from agent.agents import register_bedrock_arn_pricing
            register_bedrock_arn_pricing(model_name)

        assert model_name in mock_litellm.model_cost
        assert mock_litellm.model_cost[model_name]['input_cost_per_token'] == 0.001

    def test_converse_prefix_stripped_from_arn(self):
        mock_litellm = self._make_mock_litellm()
        model_name = 'bedrock/converse/arn:aws:bedrock:us-east-1:123:inference-profile/converse-strip-test'

        mock_client = MagicMock()
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_inference_profile.return_value = {'models': []}

        with self._run_with_mock_litellm(mock_litellm, {'boto3': mock_boto3}):
            from agent.agents import register_bedrock_arn_pricing
            register_bedrock_arn_pricing(model_name)

        call_args = mock_client.get_inference_profile.call_args
        arn_passed = call_args[1]['inferenceProfileIdentifier']
        assert not arn_passed.startswith('converse/')

# ---------------------------------------------------------------------------
# handle_logging
# ---------------------------------------------------------------------------
class TestHandleLogging:
    def test_creates_file_handler(self, tmp_path):
        from agent.agents import handle_logging

        log_file = tmp_path / "test.log"
        log_file.touch()
        handle_logging("test_logger", log_file)

        logger = logging.getLogger("test_logger")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.FileHandler)
        assert logger.level == logging.INFO
        assert logger.propagate is False

        # cleanup
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)

    def test_clears_existing_handlers(self, tmp_path):
        from agent.agents import handle_logging

        log_file = tmp_path / "test2.log"
        log_file.touch()
        logger = logging.getLogger("test_logger_clear")
        logger.addHandler(logging.StreamHandler())
        logger.addHandler(logging.StreamHandler())
        assert len(logger.handlers) == 2

        handle_logging("test_logger_clear", log_file)
        assert len(logger.handlers) == 1

        # cleanup
        for h in logger.handlers[:]:
            h.close()
            logger.removeHandler(h)


# ---------------------------------------------------------------------------
# AgentReturn / AiderReturn
# ---------------------------------------------------------------------------
class TestAgentReturn:
    def test_init(self, tmp_path):

        # AgentReturn is ABC so we can't instantiate directly,
        # but AiderReturn inherits from it
        log_file = tmp_path / "test.log"
        log_file.write_text("")
        from agent.agents import AiderReturn

        ret = AiderReturn(log_file)
        assert ret.log_file == log_file

    def test_aider_return_cost_parsing(self, tmp_path):
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text(
            "Some output\n"
            "Tokens: 1000 sent, 500 received. Cost: $0.05 message, $0.15 session.\n"
            "More output\n"
            "Tokens: 2000 sent, 1000 received. Cost: $0.10 message, $0.25 session.\n"
        )
        ret = AiderReturn(log_file)
        assert ret.last_cost == 0.25

    def test_aider_return_no_cost_lines(self, tmp_path):
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text("No cost info here\nJust regular output\n")
        ret = AiderReturn(log_file)
        assert ret.last_cost == 0.0

    def test_aider_return_empty_log(self, tmp_path):
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text("")
        ret = AiderReturn(log_file)
        assert ret.last_cost == 0.0

    def test_aider_return_test_summarizer_cost_default(self, tmp_path):
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text("")
        ret = AiderReturn(log_file)
        assert ret.test_summarizer_cost == 0.0


# ---------------------------------------------------------------------------
# Agents ABC
# ---------------------------------------------------------------------------
class TestAgentsABC:
    def test_cannot_instantiate(self):
        from agent.agents import Agents

        with pytest.raises(TypeError):
            Agents(max_iteration=5)

    def test_max_iteration(self):
        from agent.agents import Agents

        class ConcreteAgent(Agents):
            def run(self):
                return None

        agent = ConcreteAgent(max_iteration=10)
        assert agent.max_iteration == 10


# ---------------------------------------------------------------------------
# AiderAgents.__init__
# ---------------------------------------------------------------------------
class TestAiderAgentsInit:
    @patch(f"{MODULE}.Model")
    @patch(f"{MODULE}.AiderAgents._load_model_settings")
    @patch(f"{MODULE}.register_bedrock_arn_pricing")
    def test_openai_model(self, mock_register, mock_load, mock_model):
        os.environ["OPENAI_API_KEY"] = "test-key"
        try:
            from agent.agents import AiderAgents

            agent = AiderAgents(max_iteration=5, model_name="openai/gpt-4")
            assert agent.max_iteration == 5
            assert agent.model_name == "openai/gpt-4"
            mock_register.assert_called_once_with("openai/gpt-4")
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    @patch(f"{MODULE}.Model")
    @patch(f"{MODULE}.AiderAgents._load_model_settings")
    @patch(f"{MODULE}.register_bedrock_arn_pricing")
    def test_anthropic_model(self, mock_register, mock_load, mock_model):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        try:
            from agent.agents import AiderAgents

            agent = AiderAgents(max_iteration=3, model_name="claude-3-opus")
            assert agent.model_name == "claude-3-opus"
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    @patch(f"{MODULE}.Model")
    @patch(f"{MODULE}.AiderAgents._load_model_settings")
    @patch(f"{MODULE}.register_bedrock_arn_pricing")
    def test_missing_api_key_raises(
        self, mock_register, mock_load, mock_model, monkeypatch
    ):
        # Clear all relevant env vars
        for key in [
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_BEARER_TOKEN_BEDROCK",
        ]:
            monkeypatch.delenv(key, raising=False)

        from agent.agents import AiderAgents

        with pytest.raises(ValueError, match="API Key Error"):
            AiderAgents(max_iteration=5, model_name="openai/gpt-4")

    @patch(f"{MODULE}.Model")
    @patch(f"{MODULE}.AiderAgents._load_model_settings")
    @patch(f"{MODULE}.register_bedrock_arn_pricing")
    def test_bedrock_model_with_aws_key(
        self, mock_register, mock_load, mock_model, monkeypatch
    ):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA...")

        from agent.agents import AiderAgents

        agent = AiderAgents(max_iteration=5, model_name="bedrock/some-model")
        assert agent.model_name == "bedrock/some-model"

    @patch(f"{MODULE}.Model")
    @patch(f"{MODULE}.AiderAgents._load_model_settings")
    @patch(f"{MODULE}.register_bedrock_arn_pricing")
    def test_gemini_model(self, mock_register, mock_load, mock_model, monkeypatch):
        monkeypatch.setenv("API_KEY", "gemini-key")

        from agent.agents import AiderAgents

        agent = AiderAgents(max_iteration=5, model_name="gemini-pro")
        assert agent.model_name == "gemini-pro"

    @patch(f"{MODULE}.Model")
    @patch(f"{MODULE}.AiderAgents._load_model_settings")
    @patch(f"{MODULE}.register_bedrock_arn_pricing")
    def test_unknown_model_assumes_key_present(
        self, mock_register, mock_load, mock_model, monkeypatch
    ):
        # Unknown model provider should log warning but not raise
        from agent.agents import AiderAgents

        agent = AiderAgents(max_iteration=5, model_name="some-unknown-provider/model")
        assert agent.model_name == "some-unknown-provider/model"


# ---------------------------------------------------------------------------
# _BEDROCK_ENV_TO_BASE_MODEL structure
# ---------------------------------------------------------------------------
class TestPricingConstants:
    def test_env_map_has_all_six_aliases(self):
        from agent.agents import _BEDROCK_ENV_TO_BASE_MODEL

        expected = {
            "BEDROCK_OPUS_ARN",
            "BEDROCK_GLM5_ARN",
            "BEDROCK_KIMI_ARN",
            "BEDROCK_MINIMAX_ARN",
            "BEDROCK_NOVA2_LITE_ARN",
            "BEDROCK_NOVA_PREMIER_ARN",
        }
        assert set(_BEDROCK_ENV_TO_BASE_MODEL.keys()) == expected

    def test_base_models_are_strings(self):
        from agent.agents import _BEDROCK_ENV_TO_BASE_MODEL

        for env_key, base in _BEDROCK_ENV_TO_BASE_MODEL.items():
            assert isinstance(base, str) and base, f"{env_key} has non-string base"


# ---------------------------------------------------------------------------
# AiderReturn edge cases
# ---------------------------------------------------------------------------
class TestAiderReturnEdgeCases:
    def test_partial_cost_match(self, tmp_path):
        """Log has 'Cost:' but regex doesn't match format -> returns 0.0"""
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text("Tokens: 100 sent. Cost: unknown format here\n")
        ret = AiderReturn(log_file)
        assert ret.last_cost == 0.0

    def test_single_session_line(self, tmp_path):
        """Only one cost line -> returns that session cost"""
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text(
            "Tokens: 500 sent, 200 received. Cost: $0.03 message, $0.03 session.\n"
        )
        ret = AiderReturn(log_file)
        assert ret.last_cost == 0.03

    def test_cost_with_large_numbers(self, tmp_path):
        """Cost like $123.45 session -> parses correctly"""
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text(
            "Tokens: 9999 sent, 5000 received. Cost: $50.00 message, $123.45 session.\n"
        )
        ret = AiderReturn(log_file)
        assert ret.last_cost == 123.45

    def test_log_file_with_non_utf8(self, tmp_path):
        """Binary content in log file should raise or be handled"""
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_bytes(b"\x80\x81\x82 binary content\n")
        # UnicodeDecodeError is expected since open() uses 'r' mode
        with pytest.raises(UnicodeDecodeError):
            AiderReturn(log_file)

    def test_cost_accumulates_last(self, tmp_path):
        """Multiple cost lines, returns the LAST session cost (not sum)"""
        from agent.agents import AiderReturn

        log_file = tmp_path / "aider.log"
        log_file.write_text(
            "Tokens: 100 sent, 50 received. Cost: $0.01 message, $0.01 session.\n"
            "Tokens: 200 sent, 100 received. Cost: $0.02 message, $0.05 session.\n"
            "Tokens: 300 sent, 150 received. Cost: $0.03 message, $0.10 session.\n"
        )
        ret = AiderReturn(log_file)
        # Should be 0.10, NOT 0.01 + 0.05 + 0.10 = 0.16
        assert ret.last_cost == 0.10


class TestApplyThinkingCapturePatches:
    def _make_coder_mock(self):
        coder = MagicMock()
        del coder._turn_counter
        del coder._last_reasoning_content
        del coder._last_completion_usage
        return coder

    def test_patches_are_applied(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = self._make_coder_mock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "test_stage", "test_module")

        assert coder._thinking_capture is tc
        assert coder._current_stage == "test_stage"
        assert coder._current_module == "test_module"
        assert coder._turn_counter == 0
        assert coder._last_reasoning_content is None

    def test_patched_show_send_output_captures_reasoning(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = MagicMock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.reasoning_content = "I think therefore I am"
        completion.usage = MagicMock()

        coder.show_send_output(completion)
        assert coder._last_reasoning_content == "I think therefore I am"

    def test_patched_show_send_output_fallback_reasoning(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = MagicMock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        completion = MagicMock()
        completion.choices = [MagicMock()]
        msg = completion.choices[0].message
        del msg.reasoning_content
        msg.reasoning = "fallback reasoning"
        completion.usage = MagicMock()

        coder.show_send_output(completion)
        assert coder._last_reasoning_content == "fallback reasoning"

    def test_patched_show_send_output_no_reasoning(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = MagicMock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        completion = MagicMock()
        completion.choices = [MagicMock()]
        msg = completion.choices[0].message
        del msg.reasoning_content
        del msg.reasoning
        completion.usage = MagicMock()

        coder.show_send_output(completion)
        assert coder._last_reasoning_content is None

    def test_patched_send_message_increments_turn_counter(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = self._make_coder_mock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        assert coder._turn_counter == 0
        coder.send_message("hello")
        assert coder._turn_counter == 1
        coder.send_message("world")
        assert coder._turn_counter == 2

    def test_patched_send_message_adds_user_turn(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = self._make_coder_mock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        coder.send_message("fix the bug")
        assert len(tc.turns) == 1
        assert tc.turns[0].role == "user"
        assert tc.turns[0].content == "fix the bug"
        assert tc.turns[0].stage == "draft"
        assert tc.turns[0].module == "mod"
        assert tc.turns[0].turn_number == 1

    def test_patched_clone_propagates_patches(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = MagicMock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "lint", "mod")

        cloned_coder = MagicMock()
        coder.clone.side_effect = None

        _apply_thinking_capture_patches(coder, tc, "lint", "mod")

        mock_original_clone = MagicMock(return_value=cloned_coder)
        coder._original_clone_for_test = mock_original_clone

        cloned = coder.clone()
        assert cloned is not None

    def test_patched_apply_updates_records_edit_error(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture, Turn

        coder = MagicMock()
        coder.apply_updates = MagicMock(return_value=set())
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "test_mod")

        tc.turns.append(
            Turn(
                role="assistant", content="edit code", stage="draft", module="test_mod"
            )
        )
        coder.reflected_message = "SEARCH/REPLACE block failed"
        coder.apply_updates()

        assert tc.turns[0].edit_error == "SEARCH/REPLACE block failed"

    def test_patched_show_usage_report_snapshots_tokens(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = self._make_coder_mock()
        coder.message_tokens_sent = 150
        coder.message_tokens_received = 75
        coder.message_cost = 0.005
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        coder.show_usage_report()
        assert coder._snapshot_prompt_tokens == 150
        assert coder._snapshot_completion_tokens == 75
        assert coder._snapshot_cost == 0.005

    def test_patched_show_send_output_stores_usage(self):
        from agent.agents import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = self._make_coder_mock()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(coder, tc, "draft", "mod")

        completion = MagicMock()
        completion.choices = [MagicMock()]
        completion.choices[0].message.reasoning_content = "think"
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        completion.usage = mock_usage

        coder.show_send_output(completion)
        assert coder._last_completion_usage is mock_usage


class TestAiderAgentsRun:
    def _make_agent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        from agent.agents import AiderAgents

        with (
            patch(f"{MODULE}.Model"),
            patch(f"{MODULE}.AiderAgents._load_model_settings"),
            patch(f"{MODULE}.register_bedrock_arn_pricing"),
        ):
            agent = AiderAgents(max_iteration=3, model_name="openai/gpt-4")
        agent.model = MagicMock()
        agent.model.info = {"max_input_tokens": 100000}
        return agent

    def _make_mock_coder(self):
        coder = MagicMock()
        coder.run = MagicMock()
        coder.commands = MagicMock()
        coder.commands.cmd_test = MagicMock(return_value=None)
        coder.commands.cmd_lint = MagicMock(return_value=None)
        coder.max_reflections = 3
        coder.stream = False
        coder.gpt_prompts = MagicMock()
        coder.gpt_prompts.main_system = "You are a helpful assistant."
        coder.abs_fnames = set()
        coder.get_inchat_relative_files = MagicMock(return_value=[])
        return coder

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_stdout_stderr_redirect(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        mock_coder_cls.create.return_value = coder

        saved_stdout = sys.stdout
        saved_stderr = sys.stderr

        agent.run(
            message="fix it",
            test_cmd="",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
        )

        assert sys.stdout is saved_stdout
        assert sys.stderr is saved_stderr

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_test_first_mode(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        coder.commands.cmd_test.return_value = "test errors found"
        mock_coder_cls.create.return_value = coder

        agent.run(
            message="fix it",
            test_cmd="pytest",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
            test_first=True,
        )

        coder.commands.cmd_test.assert_called_with("pytest")
        coder.run.assert_called_once_with("test errors found")

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_lint_first_mode(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        mock_coder_cls.create.return_value = coder

        agent.run(
            message="fix it",
            test_cmd="",
            lint_cmd="ruff check",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
            lint_first=True,
        )

        coder.commands.cmd_lint.assert_called_once_with(fnames=["test.py"])

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_default_mode_runs_message(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        mock_coder_cls.create.return_value = coder

        agent.run(
            message="implement feature X",
            test_cmd="",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
        )

        coder.run.assert_called_once_with("implement feature X")

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_token_estimation_skips_large(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path, caplog
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        agent.model.info = {"max_input_tokens": 100}
        coder = self._make_mock_coder()
        mock_coder_cls.create.return_value = coder

        large_message = "x" * 2000

        with caplog.at_level(logging.WARNING):
            result = agent.run(
                message=large_message,
                test_cmd="",
                lint_cmd="",
                fnames=["test.py"],
                log_dir=tmp_path / "logs",
            )

        coder.run.assert_not_called()
        from agent.agents import AiderReturn

        assert isinstance(result, AiderReturn)

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_returns_aider_return_with_cost(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        mock_coder_cls.create.return_value = coder

        result = agent.run(
            message="fix it",
            test_cmd="",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
        )

        from agent.agents import AiderReturn

        assert isinstance(result, AiderReturn)
        assert result.last_cost >= 0.0

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_log_dir_created(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        mock_coder_cls.create.return_value = coder

        log_dir = tmp_path / "nested" / "log" / "dir"
        assert not log_dir.exists()

        agent.run(
            message="fix it",
            test_cmd="",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=log_dir,
        )

        assert log_dir.exists()

    @patch(f"{MODULE}.summarize_test_output")
    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_max_test_output_wraps_cmd_test(
        self,
        mock_logging,
        mock_io,
        mock_coder_cls,
        mock_summarize,
        monkeypatch,
        tmp_path,
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        coder.commands.cmd_test.return_value = "x" * 5000
        mock_summarize.return_value = ("summarized", [])
        mock_coder_cls.create.return_value = coder

        agent.run(
            message="fix it",
            test_cmd="pytest",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
            test_first=True,
            max_test_output_length=100,
        )

        mock_summarize.assert_called_once()

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_test_first_no_errors_skips_coder_run(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        coder.commands.cmd_test.return_value = ""
        mock_coder_cls.create.return_value = coder

        agent.run(
            message="fix it",
            test_cmd="pytest",
            lint_cmd="",
            fnames=["test.py"],
            log_dir=tmp_path / "logs",
            test_first=True,
        )

        coder.run.assert_not_called()

    @patch(f"{MODULE}.Coder")
    @patch(f"{MODULE}.InputOutput")
    @patch(f"{MODULE}.handle_logging")
    def test_thinking_capture_records_files_read(
        self, mock_logging, mock_io, mock_coder_cls, monkeypatch, tmp_path
    ):
        from agent.thinking_capture import ThinkingCapture

        agent = self._make_agent(monkeypatch, tmp_path)
        coder = self._make_mock_coder()
        coder.abs_fnames = {"/tmp/foo.py"}
        coder.get_inchat_relative_files.return_value = ["foo.py"]
        mock_coder_cls.create.return_value = coder

        tc = ThinkingCapture()
        agent.run(
            message="fix it",
            test_cmd="",
            lint_cmd="",
            fnames=["foo.py"],
            log_dir=tmp_path / "logs",
            thinking_capture=tc,
            current_stage="draft",
            current_module="test_mod",
        )

        file_read_turns = [t for t in tc.turns if "[files:read]" in t.content]
        assert len(file_read_turns) == 1
        assert "foo.py" in file_read_turns[0].content


class TestLoadModelSettings:
    def test_settings_file_exists(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        settings_file = tmp_path / ".aider.model.settings.yml"
        settings_file.write_text("- name: test\n")

        with patch(f"{MODULE}.Model"), patch(f"{MODULE}.register_bedrock_arn_pricing"):
            monkeypatch.setenv("OPENAI_API_KEY", "test-key")

            with patch("aider.models.register_models") as mock_register:
                from agent.agents import AiderAgents

                AiderAgents(max_iteration=3, model_name="openai/gpt-4")
                mock_register.assert_called_once()

    def test_settings_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        with patch(f"{MODULE}.Model"), patch(f"{MODULE}.register_bedrock_arn_pricing"):
            monkeypatch.setenv("OPENAI_API_KEY", "test-key")

            with patch("aider.models.register_models") as mock_register:
                from agent.agents import AiderAgents

                AiderAgents(max_iteration=3, model_name="openai/gpt-4")
                mock_register.assert_not_called()
