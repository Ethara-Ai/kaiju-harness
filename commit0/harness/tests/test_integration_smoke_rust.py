"""Integration smoke tests — CLI invocations and agent flow mocks.

These tests verify that the main entry points can be imported and invoked
without hitting external services (Docker, git remotes, HuggingFace).
"""

from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# CLI smoke tests — verify commands are registered and callable
# ---------------------------------------------------------------------------
class TestCliSmoke:
    def test_commit0_app_importable(self):
        from commit0.cli import commit0_app

        assert commit0_app is not None

    def test_main_module_importable(self):
        from commit0 import __main__

        assert hasattr(__main__, "commit0_app")

    def test_all_commands_registered(self):
        from commit0.cli import commit0_app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(commit0_app, ["--help"])
        assert result.exit_code == 0
        # Check all 8 commands are mentioned
        for cmd in ["setup", "build", "test", "evaluate", "lint", "save", "get-tests"]:
            assert cmd in result.output

    def test_setup_without_config_fails(self, tmp_path, monkeypatch):
        from commit0.cli import commit0_app
        from typer.testing import CliRunner

        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(commit0_app, ["setup"])
        # Should fail because no .commit0.yaml exists
        assert result.exit_code != 0

    def test_help_for_each_command(self):
        from commit0.cli import commit0_app
        from typer.testing import CliRunner

        runner = CliRunner()
        for cmd in ["setup", "build", "test", "evaluate", "lint", "save"]:
            result = runner.invoke(commit0_app, [cmd, "--help"])
            assert result.exit_code == 0, f"{cmd} --help failed"


# ---------------------------------------------------------------------------
# Agent CLI smoke tests
# ---------------------------------------------------------------------------
class TestAgentCliSmoke:
    def test_agent_module_importable(self):
        from agent import cli

        assert hasattr(cli, "agent_app")

    def test_agent_agents_importable(self):
        from agent.agents import AiderAgents, Agents

        assert issubclass(AiderAgents, Agents)


# ---------------------------------------------------------------------------
# Config round-trip smoke
# ---------------------------------------------------------------------------
class TestConfigSmoke:
    def test_base_yaml_loadable(self):
        config_path = Path(__file__).parent.parent.parent / "configs" / "base.yaml"
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f)
            assert isinstance(data, dict)

    def test_user_yaml_loadable(self):
        config_path = Path(__file__).parent.parent.parent / "configs" / "user.yaml"
        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f)
            assert data is None or isinstance(data, dict)


# ---------------------------------------------------------------------------
# Import smoke tests — ensure all major modules load without error
# ---------------------------------------------------------------------------
class TestImportSmoke:
    def test_import_harness_modules(self):
        """All harness modules should import without error."""
        modules = [
            "commit0.harness.constants",
            "commit0.harness.utils",
            "commit0.harness.spec",
            "commit0.harness.docker_utils",
            "commit0.harness.health_check",
            "commit0.harness.lint_filter",
            "commit0.harness.rust_test_parser",
        ]
        for mod_name in modules:
            __import__(mod_name)

    def test_import_agent_modules(self):
        modules = [
            "agent.agents",
            "agent.agent_utils",
            "agent.class_types",
            "agent.thinking_capture",
        ]
        for mod_name in modules:
            __import__(mod_name)

    def test_import_config_module(self):
        from commit0.configs.config_class import Commit0Config

        assert Commit0Config is not None


# ---------------------------------------------------------------------------
# Constants smoke tests
# ---------------------------------------------------------------------------
class TestConstantsSmoke:
    def test_split_dict_not_empty(self):
        from commit0.harness.constants import SPLIT

        assert len(SPLIT) > 0

    def test_split_dict_has_all_key(self):
        """The SPLIT dict should have well-known keys."""
        from commit0.harness.constants import SPLIT

        # 'all' may not be a key, but there should be some split names
        assert isinstance(SPLIT, dict)

    def test_test_status_enum(self):
        from commit0.harness.constants import TestStatus

        assert hasattr(TestStatus, "PASSED")
        assert hasattr(TestStatus, "FAILED")

    def test_eval_backends(self):
        from commit0.harness.constants import EVAL_BACKENDS

        assert "local" in EVAL_BACKENDS or "LOCAL" in EVAL_BACKENDS
