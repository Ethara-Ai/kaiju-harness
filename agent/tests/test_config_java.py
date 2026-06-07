from __future__ import annotations

import dataclasses

import pytest

from agent.config_java import JavaAgentConfig


class TestJavaAgentConfigDefaults:
    def test_default_values(self) -> None:
        cfg = JavaAgentConfig()
        assert cfg.model == "gpt-4"
        assert cfg.run_tests is True
        assert cfg.max_iteration == 3

    def test_custom_values(self) -> None:
        cfg = JavaAgentConfig(model="claude-3", max_iteration=10, timeout=600)
        assert cfg.model == "claude-3"
        assert cfg.max_iteration == 10
        assert cfg.timeout == 600

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(JavaAgentConfig) is True
        cfg = JavaAgentConfig()
        assert dataclasses.is_dataclass(cfg) is True


class TestEdgeCases:
    def test_zero_max_iteration(self) -> None:
        with pytest.raises(ValueError, match="max_iteration must be a positive integer"):
            JavaAgentConfig(max_iteration=0)

    def test_negative_timeout(self) -> None:
        cfg = JavaAgentConfig(timeout=-1)
        assert cfg.timeout == -1

    def test_empty_skip_dirs(self) -> None:
        cfg = JavaAgentConfig(skip_dirs=[])
        assert cfg.skip_dirs == []

    def test_empty_model_string(self) -> None:
        with pytest.raises(ValueError, match="model must be a non-empty string"):
            JavaAgentConfig(model="")

    def test_all_booleans_false(self) -> None:
        cfg = JavaAgentConfig(
            compile_check=False,
            run_tests=False,
            cache_prompts=False,
            use_repo_info=False,
            use_unit_tests_info=False,
            use_spec_info=False,
            capture_thinking=False,
            trajectory_md=False,
            output_jsonl=False,
            record_test_for_each_commit=False,
        )
        assert cfg.compile_check is False
        assert cfg.run_tests is False
        assert cfg.cache_prompts is False
        assert cfg.use_repo_info is False
        assert cfg.use_unit_tests_info is False
        assert cfg.use_spec_info is False
        assert cfg.capture_thinking is False
        assert cfg.trajectory_md is False
        assert cfg.output_jsonl is False
        assert cfg.record_test_for_each_commit is False


class TestNewConfigFields:
    def test_user_prompt_default(self) -> None:
        cfg = JavaAgentConfig()
        assert "UnsupportedOperationException" in cfg.user_prompt

    def test_output_jsonl_default(self) -> None:
        cfg = JavaAgentConfig()
        assert cfg.output_jsonl is False

    def test_model_short_default(self) -> None:
        cfg = JavaAgentConfig()
        assert cfg.model_short == ""

    def test_spec_summary_max_tokens_default(self) -> None:
        cfg = JavaAgentConfig()
        assert cfg.spec_summary_max_tokens == 4000

    def test_max_test_output_length_default(self) -> None:
        cfg = JavaAgentConfig()
        assert cfg.max_test_output_length == 15000

    def test_custom_user_prompt(self) -> None:
        custom = "My custom prompt"
        cfg = JavaAgentConfig(user_prompt=custom)
        assert cfg.user_prompt == custom
