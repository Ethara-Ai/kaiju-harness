from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


from agent.agent_utils import SPEC_INFO_HEADER, get_message


def _make_agent_config(**overrides: object) -> MagicMock:
    config = MagicMock()
    config.user_prompt = "Fix the bug"
    config.use_unit_tests_info = False
    config.use_repo_info = False
    config.use_spec_info = True
    config.max_spec_info_length = 10000
    config.spec_summary_max_tokens = 4000
    config.model_name = "test-model"
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


class TestReadmeFallback:
    def test_spec_exists_no_fallback(self, tmp_path: Path) -> None:
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"dummy")
        readme = tmp_path / "README.md"
        readme.write_text("# Project\nThis is the readme.")

        config = _make_agent_config()
        with patch(
            "agent.agent_utils.get_specification", return_value="Spec content here"
        ):
            message, costs = get_message(config, str(tmp_path))

        assert "Spec content here" in message
        assert "This is the readme" not in message

    def test_spec_missing_readme_md_used(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# My Library\nGreat docs here.")

        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert SPEC_INFO_HEADER in message
        assert "Great docs here" in message

    def test_spec_missing_readme_rst_used(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.rst"
        readme.write_text("My Library\n==========\nRST content.")

        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert "RST content" in message

    def test_spec_missing_readme_txt_used(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.txt"
        readme.write_text("Plain text readme.")

        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert "Plain text readme" in message

    def test_spec_missing_readme_priority_order(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("MD wins")
        (tmp_path / "README.rst").write_text("RST loses")

        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert "MD wins" in message
        assert "RST loses" not in message

    def test_neither_spec_nor_readme(self, tmp_path: Path) -> None:
        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert SPEC_INFO_HEADER not in message

    def test_use_spec_info_false_no_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("Should not appear")

        config = _make_agent_config(use_spec_info=False)
        message, costs = get_message(config, str(tmp_path))

        assert "Should not appear" not in message

    def test_readme_truncated_to_max_length(self, tmp_path: Path) -> None:
        long_text = "x" * 20000
        (tmp_path / "README.md").write_text(long_text)

        config = _make_agent_config(max_spec_info_length=500)
        message, costs = get_message(config, str(tmp_path))

        spec_start = message.find(SPEC_INFO_HEADER)
        assert spec_start != -1
        readme_content = message[spec_start + len(SPEC_INFO_HEADER) + 1 :]
        assert len(readme_content) <= 500

    def test_decompress_fails_falls_to_readme(self, tmp_path: Path) -> None:
        bz2_path = tmp_path / "spec.pdf.bz2"
        bz2_path.write_bytes(b"not valid bz2")
        (tmp_path / "README.md").write_text("Fallback content")

        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert "Fallback content" in message

    def test_broken_readme_md_falls_to_rst(self, tmp_path: Path) -> None:
        broken_md = tmp_path / "README.md"
        broken_md.symlink_to(tmp_path / "nonexistent_target")
        (tmp_path / "README.rst").write_text("RST fallback content")

        config = _make_agent_config()
        message, costs = get_message(config, str(tmp_path))

        assert "RST fallback content" in message
