"""Tests for Go spec_info injection in get_go_message.

Mirrors agent/tests/test_java_spec_info.py but targets the Go agent code
path (agent.agent_utils_go.get_go_message). Covers:
- spec.pdf direct injection
- long spec triggers summarize_specification
- spec.pdf.bz2 streamed decompression
- oversized bz2 aborts decompression and falls back
- README.md/rst fallback chain
- flag disabled short-circuits
"""
from __future__ import annotations

import bz2
from pathlib import Path
from unittest.mock import patch


from agent.agent_utils import MODULE_SCOPE_NOTE
from agent.agent_utils_go import SPEC_INFO_HEADER, get_go_message
from agent.class_types import AgentConfig


STUB_CONTENT = """\
package main

import "errors"

func Foo() error {
    return errors.New("STUB: not implemented")
}
"""

TEST_CONTENT = """\
package main

import "testing"

func TestFoo(t *testing.T) {
    if Foo() == nil {
        t.Error("expected error")
    }
}
"""


def _write_stubbed_repo(repo_path: Path) -> None:
    """Write minimal Go package: main.go (stub) + main_test.go."""
    (repo_path / "main.go").write_text(STUB_CONTENT)
    (repo_path / "main_test.go").write_text(TEST_CONTENT)


def _config(**overrides: object) -> AgentConfig:
    """Build an AgentConfig with defaults suitable for isolating spec behaviour."""
    defaults: dict[str, object] = dict(
        agent_name="aider",
        model_name="claude-3-5-sonnet-20240620",
        use_user_prompt=False,
        user_prompt="",
        use_topo_sort_dependencies=False,
        add_import_module_to_context=False,
        use_repo_info=False,
        max_repo_info_length=10000,
        use_unit_tests_info=False,
        max_unit_tests_info_length=10000,
        use_spec_info=True,
        max_spec_info_length=10000,
        use_lint_info=False,
        run_entire_dir_lint=False,
        max_lint_info_length=10000,
        pre_commit_config_path="",
        run_tests=False,
        max_iteration=3,
        record_test_for_each_commit=False,
        spec_summary_max_tokens=4000,
        max_test_output_length=15000,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


class TestGoSpecPdf:
    def test_spec_pdf_used(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"dummy")

        with patch(
            "agent.agent_utils_go.get_specification",
            return_value="Go spec content",
        ):
            message, costs = get_go_message(_config(), str(tmp_path), [])

        assert "Go spec content" in message
        assert SPEC_INFO_HEADER in message
        assert costs == []

    def test_spec_pdf_too_long_triggers_summarization(
        self, tmp_path: Path
    ) -> None:
        _write_stubbed_repo(tmp_path)
        spec_pdf = tmp_path / "spec.pdf"
        spec_pdf.write_bytes(b"dummy")

        long_spec = "x" * 20000
        config = _config(max_spec_info_length=500)
        with patch(
            "agent.agent_utils_go.get_specification",
            return_value=long_spec,
        ), patch(
            "agent.agent_utils_go.summarize_specification",
            return_value=("summarized", []),
        ) as mock_summarize:
            message, costs = get_go_message(config, str(tmp_path), [])

        mock_summarize.assert_called_once()
        assert "summarized" in message
        assert SPEC_INFO_HEADER in message

    def test_bz2_decompressed(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        raw = b"%PDF-1.4 fake content"
        bz2_path = tmp_path / "spec.pdf.bz2"
        bz2_path.write_bytes(bz2.compress(raw))

        with patch(
            "agent.agent_utils_go.get_specification",
            return_value="Decompressed Go spec",
        ):
            message, costs = get_go_message(_config(), str(tmp_path), [])

        assert (tmp_path / "spec.pdf").exists()
        assert "Decompressed Go spec" in message
        assert SPEC_INFO_HEADER in message

    def test_bz2_corrupt_falls_to_readme(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "spec.pdf.bz2").write_bytes(b"not valid bz2 data at all")
        (tmp_path / "README.md").write_text("Go readme fallback content")

        message, costs = get_go_message(_config(), str(tmp_path), [])

        assert "Go readme fallback content" in message
        assert not (tmp_path / "spec.pdf").exists()
        assert SPEC_INFO_HEADER in message


class TestGoReadmeFallback:
    def test_readme_md_used(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Go Lib\nDocs here.")

        message, costs = get_go_message(_config(), str(tmp_path), [])

        assert SPEC_INFO_HEADER in message
        assert "Docs here" in message

    def test_readme_rst_used(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "README.rst").write_text("Go RST content")

        message, costs = get_go_message(_config(), str(tmp_path), [])

        assert "Go RST content" in message
        assert SPEC_INFO_HEADER in message

    def test_readme_priority_md_over_rst(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "README.md").write_text("MD wins")
        (tmp_path / "README.rst").write_text("RST loses")

        message, costs = get_go_message(_config(), str(tmp_path), [])

        assert "MD wins" in message
        assert "RST loses" not in message

    def test_readme_truncated(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "README.md").write_text("y" * 20000)

        config = _config(max_spec_info_length=500)
        message, costs = get_go_message(config, str(tmp_path), [])

        spec_start = message.find(SPEC_INFO_HEADER)
        assert spec_start != -1
        # Header is followed by a newline in parts.append join, so we
        # measure the injected README region (capped at max_spec_info_length).
        # The message ends with MODULE_SCOPE_NOTE, so strip it first.
        readme_portion = message[spec_start + len(SPEC_INFO_HEADER):]
        assert readme_portion.endswith(MODULE_SCOPE_NOTE)
        readme_portion = readme_portion[: -len(MODULE_SCOPE_NOTE)]
        # Strip leading/trailing newline(s) that came from "\n".join of parts
        assert len(readme_portion.strip("\n")) <= 600  # 500 + small tolerance

    def test_neither_spec_nor_readme(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)

        message, costs = get_go_message(_config(), str(tmp_path), [])

        assert SPEC_INFO_HEADER not in message
        assert costs == []


class TestGoSpecInfoDisabled:
    def test_use_spec_info_false_skips_everything(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "README.md").write_text("Should not appear")
        (tmp_path / "spec.pdf").write_bytes(b"dummy")

        config = _config(use_spec_info=False)
        message, costs = get_go_message(config, str(tmp_path), [])

        assert "Should not appear" not in message
        assert SPEC_INFO_HEADER not in message
        assert costs == []


class TestGoMessageBasics:
    def test_returns_tuple(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)

        result = get_go_message(_config(use_spec_info=False), str(tmp_path), [])
        assert isinstance(result, tuple)
        assert len(result) == 2
        message, costs = result
        assert isinstance(message, str)
        assert isinstance(costs, list)

    def test_empty_costs_when_no_summarization(self, tmp_path: Path) -> None:
        _write_stubbed_repo(tmp_path)
        (tmp_path / "spec.pdf").write_bytes(b"dummy")

        with patch(
            "agent.agent_utils_go.get_specification",
            return_value="short spec",
        ):
            _, costs = get_go_message(_config(), str(tmp_path), [])

        assert costs == []
