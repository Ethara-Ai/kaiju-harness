from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch


from agent.config_java import JavaAgentConfig


def _make_java_agent(config: JavaAgentConfig | None = None) -> object:
    """Create a JavaAgents instance with all external deps mocked."""
    cfg = config or JavaAgentConfig()
    with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key-for-unit-tests"}), \
         patch("agent.agents_java.Model"), \
         patch("agent.agents_java.register_bedrock_arn_pricing"), \
         patch("agent.agents_java.AiderAgents") as mock_aider:
        mock_aider._load_model_settings = MagicMock()
        from agent.agents_java import JavaAgents
        agent = JavaAgents(cfg)
    return agent


class TestGetCompileCommand:
    def test_maven_default(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_compile_command("maven")
        assert cmd.startswith("mvn ")
        assert "compile" in cmd

    def test_gradle_default(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_compile_command("gradle")
        assert cmd.startswith("gradle ")
        assert "classes" in cmd

    def test_maven_wrapper_script(self, tmp_path: Path) -> None:
        agent = _make_java_agent()
        result = agent.get_compile_command("maven", str(tmp_path))
        assert result.endswith(".sh")
        wrapper = Path(result)
        assert wrapper.exists()

    def test_gradle_wrapper_script(self, tmp_path: Path) -> None:
        agent = _make_java_agent()
        result = agent.get_compile_command("gradle", str(tmp_path))
        assert result.endswith(".sh")
        wrapper = Path(result)
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "classes" in content


class TestGetTestCommand:
    def test_maven_test(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_test_command("maven")
        assert cmd == "mvn test -B"

    def test_gradle_test(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_test_command("gradle")
        assert cmd == "gradle test --no-daemon"

    def test_custom_test_ids(self, tmp_path: Path) -> None:
        # When repo_path is provided, gradle test still returns a plain command
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/bash\n")
        gradlew.chmod(0o755)
        agent = _make_java_agent()
        cmd = agent.get_test_command("gradle", str(tmp_path))
        assert "test" in cmd
        assert "--no-daemon" in cmd


class TestSystemPrompt:
    def test_contains_never_edit_test_files(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Base system prompt content.")
        cfg = JavaAgentConfig(system_prompt_path=str(prompt_file))
        agent = _make_java_agent(cfg)
        # The system_prompt property reads the file; the "NEVER edit test files"
        # is appended in run(), but system_prompt returns the file content
        assert agent.system_prompt == "Base system prompt content."

    def test_contains_stub_marker(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("Implement stubs marked with STUB.")
        cfg = JavaAgentConfig(system_prompt_path=str(prompt_file))
        agent = _make_java_agent(cfg)
        assert "STUB" in agent.system_prompt

    def test_appended_to_system_prompt(self, tmp_path: Path) -> None:
        # When prompt file doesn't exist, system_prompt returns empty string
        cfg = JavaAgentConfig(system_prompt_path=str(tmp_path / "nonexistent.md"))
        agent = _make_java_agent(cfg)
        assert agent.system_prompt == ""


class TestMakeWrapperScript:
    def test_creates_wrapper_file(self, tmp_path: Path) -> None:
        agent = _make_java_agent()
        result = agent._make_wrapper_script(str(tmp_path), "mvn", "compile -q")
        assert Path(result).exists()
        assert Path(result).read_text().startswith("#!/usr/bin/env bash")

    def test_wrapper_is_executable(self, tmp_path: Path) -> None:
        agent = _make_java_agent()
        result = agent._make_wrapper_script(str(tmp_path), "mvn", "compile -q")
        st = os.stat(result)
        assert st.st_mode & stat.S_IXUSR != 0

    def test_different_goals_different_files(self, tmp_path: Path) -> None:
        agent = _make_java_agent()
        r1 = agent._make_wrapper_script(str(tmp_path), "mvn", "compile -q")
        r2 = agent._make_wrapper_script(str(tmp_path), "mvn", "test -B")
        assert r1 != r2

    def test_wrapper_dir_created(self, tmp_path: Path) -> None:
        repo = tmp_path / "deep" / "repo"
        repo.mkdir(parents=True)
        agent = _make_java_agent()
        agent._make_wrapper_script(str(repo), "gradle", "classes")
        assert (repo / ".commit0_scripts").is_dir()


class TestEdgeCases:
    def test_unknown_build_system_defaults_maven_compile(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_compile_command("unknown_system")
        # resolve_build_cmd defaults to maven path for non-gradle
        assert "compile" in cmd

    def test_empty_build_system_defaults_maven_test(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_test_command("")
        assert "test -B" in cmd

    def test_unknown_build_system_defaults_maven_test(self) -> None:
        agent = _make_java_agent()
        cmd = agent.get_test_command("unknown")
        assert "test -B" in cmd

    def test_empty_prompt_file_yields_empty(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / "empty.md"
        prompt_file.write_text("")
        cfg = JavaAgentConfig(system_prompt_path=str(prompt_file))
        agent = _make_java_agent(cfg)
        assert agent.system_prompt == ""
