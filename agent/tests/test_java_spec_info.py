from __future__ import annotations

import bz2
from pathlib import Path
from unittest.mock import patch


from agent.agent_utils_java import SPEC_INFO_HEADER
from agent.config_java import JavaAgentConfig
from agent.run_agent_java import _get_java_message


STUB_CONTENT = """\
public class Foo {
    public void bar() {
        throw new UnsupportedOperationException("STUB: not implemented");
    }
}
"""


def _write_stubbed_file(repo_path: Path, rel: str = "src/main/java/Foo.java") -> str:
    f = repo_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(STUB_CONTENT)
    return str(f)


def _config(**overrides: object) -> JavaAgentConfig:
    defaults = dict(use_spec_info=True, use_unit_tests_info=False)
    defaults.update(overrides)
    return JavaAgentConfig(**defaults)


class TestJavaSpecPdf:
    def test_spec_pdf_used(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"dummy")

        with patch(
            "agent.run_agent_java.get_specification", return_value="Java spec content"
        ):
            message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert "Java spec content" in message
        assert SPEC_INFO_HEADER in message

    def test_spec_pdf_too_long_triggers_summarization(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"dummy")

        long_spec = "x" * 20000
        config = _config(max_spec_info_length=500)
        with patch(
            "agent.run_agent_java.get_specification", return_value=long_spec
        ), patch(
            "agent.run_agent_java.summarize_specification_java",
            return_value=("summarized", []),
        ) as mock_summarize:
            message, costs = _get_java_message(config, str(tmp_path), stubbed)

        mock_summarize.assert_called_once()
        assert "summarized" in message

    def test_bz2_decompressed(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        raw = b"%PDF-1.4 fake content"
        bz2_path = tmp_path / "spec.pdf.bz2"
        bz2_path.write_bytes(bz2.compress(raw))

        with patch(
            "agent.run_agent_java.get_specification", return_value="Decompressed spec"
        ):
            message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert (tmp_path / "spec.pdf").exists()
        assert "Decompressed spec" in message

    def test_bz2_corrupt_falls_to_readme(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        (tmp_path / "spec.pdf.bz2").write_bytes(b"not valid bz2")
        (tmp_path / "README.md").write_text("Java readme fallback")

        message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert "Java readme fallback" in message
        assert not (tmp_path / "spec.pdf").exists()


class TestJavaReadmeFallback:
    def test_readme_md_used(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        (tmp_path / "README.md").write_text("# Java Lib\nDocs here.")

        message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert SPEC_INFO_HEADER in message
        assert "Docs here" in message

    def test_readme_rst_used(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        (tmp_path / "README.rst").write_text("Java RST content")

        message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert "Java RST content" in message

    def test_readme_priority_md_over_rst(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        (tmp_path / "README.md").write_text("MD wins")
        (tmp_path / "README.rst").write_text("RST loses")

        message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert "MD wins" in message
        assert "RST loses" not in message

    def test_readme_truncated(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        (tmp_path / "README.md").write_text("y" * 20000)

        config = _config(max_spec_info_length=500)
        message, costs = _get_java_message(config, str(tmp_path), stubbed)

        spec_start = message.find(SPEC_INFO_HEADER)
        assert spec_start != -1
        readme_portion = message[spec_start + len(SPEC_INFO_HEADER) + 1:]
        assert len(readme_portion) <= 500

    def test_neither_spec_nor_readme(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)

        message, costs = _get_java_message(_config(), str(tmp_path), stubbed)

        assert SPEC_INFO_HEADER not in message


class TestJavaSpecInfoDisabled:
    def test_use_spec_info_false_skips_everything(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)
        (tmp_path / "README.md").write_text("Should not appear")

        config = _config(use_spec_info=False)
        message, costs = _get_java_message(config, str(tmp_path), stubbed)

        assert "Should not appear" not in message
        assert SPEC_INFO_HEADER not in message
        assert costs == []


class TestJavaMessageBasics:
    def test_contains_target_file(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)

        config = _config(use_spec_info=False)
        message, costs = _get_java_message(config, str(tmp_path), stubbed)

        assert "src/main/java/Foo.java" in message

    def test_contains_stub_count(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)

        config = _config(use_spec_info=False)
        message, costs = _get_java_message(config, str(tmp_path), stubbed)

        assert "1 stub(s)" in message

    def test_returns_empty_costs_when_no_summarization(self, tmp_path: Path) -> None:
        stubbed = _write_stubbed_file(tmp_path)

        config = _config(use_spec_info=False)
        message, costs = _get_java_message(config, str(tmp_path), stubbed)

        assert costs == []
