from __future__ import annotations

import inspect
from pathlib import Path

import yaml
from typer.testing import CliRunner

from agent.config_js import agent_js_app, config as config_cmd


runner = CliRunner()


class TestUseSpecInfoDefault:
    def test_signature_default_false(self) -> None:
        sig = inspect.signature(config_cmd)
        param = sig.parameters["use_spec_info"]
        typer_option = param.default
        assert hasattr(typer_option, "default"), (
            "use_spec_info parameter must be wrapped in typer.Option(...)"
        )
        assert typer_option.default is False, (
            "use_spec_info typer default must be False (JS-PLAN §11 item 7 "
            "binding: MVP skips PDF/README spec scraping)"
        )

    def test_yaml_emits_use_spec_info_false_by_default(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".agent.js.yaml"
        result = runner.invoke(
            agent_js_app,
            [
                "config",
                "aider",
                "--agent-config-file",
                str(cfg_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert cfg_path.exists(), "config command must write the YAML file"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data.get("use_spec_info") is False, (
            f"YAML must serialize use_spec_info as False; got {data.get('use_spec_info')!r}"
        )

    def test_use_spec_info_flag_flips_to_true(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".agent.js.yaml"
        result = runner.invoke(
            agent_js_app,
            [
                "config",
                "aider",
                "--use-spec-info",
                "--agent-config-file",
                str(cfg_path),
            ],
        )
        assert result.exit_code == 0, result.output
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data.get("use_spec_info") is True

    def test_use_spec_info_independent_of_other_fields(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".agent.js.yaml"
        result = runner.invoke(
            agent_js_app,
            [
                "config",
                "aider",
                "--use-spec-info",
                "--use-repo-info",
                "--agent-config-file",
                str(cfg_path),
            ],
        )
        assert result.exit_code == 0, result.output
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data.get("use_spec_info") is True
        assert data.get("use_repo_info") is True


class TestAgentNameValidation:
    def test_aider_writes_yaml_with_agent_name_aider(
        self, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / ".agent.js.yaml"
        result = runner.invoke(
            agent_js_app,
            ["config", "aider", "--agent-config-file", str(cfg_path)],
        )
        assert result.exit_code == 0, result.output
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["agent_name"] == "aider"

    def test_unknown_agent_name_currently_passes_through(
        self, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / ".agent.js.yaml"
        result = runner.invoke(
            agent_js_app,
            ["config", "adier", "--agent-config-file", str(cfg_path)],
        )
        if result.exit_code == 0:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            assert data["agent_name"] == "adier", (
                "CJ-G2: typo currently propagates; downstream "
                "run_agent_for_repo_js raises NotImplementedError when "
                "agent_name != 'aider'. If validation is added here, update "
                "this test to assert the CLI rejects unknown names."
            )
        else:
            assert "aider" in result.output.lower() or "agent" in result.output.lower()

    def test_downstream_rejects_non_aider_agent_name(self) -> None:
        from agent.run_agent_js import run_agent_for_repo_js

        source = inspect.getsource(run_agent_for_repo_js)
        assert (
            'agent_config.agent_name == "aider"' in source
            or "agent_config.agent_name != \"aider\"" in source
        ), (
            "CJ-G2: downstream must guard against non-aider agent_name; "
            "if config layer adds validation, update this test"
        )
        assert "NotImplementedError" in source
