"""Tests for agent.agent_utils_ts — file collection, stub detection, message
building, test output parsing, and spec doc injection.
"""

import bz2
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


MODULE = "agent.agent_utils_ts"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_config(**overrides: Any) -> MagicMock:
    defaults = {
        "user_prompt": "Implement the stubbed functions.",
        "use_unit_tests_info": True,
        "max_unit_tests_info_length": 5000,
        "use_repo_info": False,
        "max_repo_info_length": 2000,
        "use_spec_info": False,
        "max_spec_info_length": 3000,
        "model_name": "test-model",
        "spec_summary_max_tokens": 4000,
    }
    defaults.update(overrides)
    cfg = MagicMock()
    for k, v in defaults.items():
        setattr(cfg, k, v)
    return cfg


# ===================================================================
# collect_typescript_files
# ===================================================================


class TestCollectTypescriptFiles:
    def test_collects_ts_and_tsx(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("export const x = 1;")
        (tmp_path / "src" / "App.tsx").write_text("<div/>")
        (tmp_path / "src" / "types.d.ts").write_text("declare module 'x';")
        (tmp_path / "README.md").write_text("# hello")

        from agent.agent_utils_ts import collect_typescript_files

        files = collect_typescript_files(str(tmp_path / "src"))
        basenames = {os.path.basename(f) for f in files}
        assert "index.ts" in basenames
        assert "App.tsx" in basenames
        assert "types.d.ts" not in basenames

    def test_excludes_node_modules_and_dist(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "index.ts").write_text("")
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "bundle.ts").write_text("")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.ts").write_text("")

        from agent.agent_utils_ts import collect_typescript_files

        files = collect_typescript_files(str(tmp_path))
        assert len(files) == 1
        assert files[0].endswith("main.ts")

    def test_empty_directory(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import collect_typescript_files

        assert collect_typescript_files(str(tmp_path)) == []

    def test_nested_directories(self, tmp_path: Path) -> None:
        (tmp_path / "a" / "b" / "c").mkdir(parents=True)
        (tmp_path / "a" / "b" / "c" / "deep.ts").write_text("")
        (tmp_path / "a" / "top.tsx").write_text("")

        from agent.agent_utils_ts import collect_typescript_files

        files = collect_typescript_files(str(tmp_path))
        assert len(files) == 2


# ===================================================================
# collect_ts_test_files
# ===================================================================


class TestCollectTsTestFiles:
    def test_by_pattern(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "utils.test.ts").write_text("")
        (tmp_path / "src" / "utils.spec.tsx").write_text("")
        (tmp_path / "src" / "utils.ts").write_text("")

        from agent.agent_utils_ts import collect_ts_test_files

        files = collect_ts_test_files(str(tmp_path))
        basenames = {os.path.basename(f) for f in files}
        assert "utils.test.ts" in basenames
        assert "utils.spec.tsx" in basenames
        assert "utils.ts" not in basenames

    def test_by_test_dir(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "__tests__"
        test_dir.mkdir()
        (test_dir / "helper.ts").write_text("")

        from agent.agent_utils_ts import collect_ts_test_files

        files = collect_ts_test_files(str(tmp_path))
        basenames = {os.path.basename(f) for f in files}
        assert "helper.ts" in basenames

    def test_excludes_node_modules(self, tmp_path: Path) -> None:
        nm = tmp_path / "node_modules" / "__tests__"
        nm.mkdir(parents=True)
        (nm / "bad.test.ts").write_text("")

        from agent.agent_utils_ts import collect_ts_test_files

        assert collect_ts_test_files(str(tmp_path)) == []

    def test_excludes_d_ts(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "types.d.ts").write_text("")

        from agent.agent_utils_ts import collect_ts_test_files

        assert collect_ts_test_files(str(tmp_path)) == []


# ===================================================================
# has_ts_stubs / extract_ts_stubs
# ===================================================================


class TestStubDetection:
    def test_has_ts_stubs_true(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text('function foo() { throw new Error("STUB"); }')

        from agent.agent_utils_ts import has_ts_stubs

        assert has_ts_stubs(str(f)) is True

    def test_has_ts_stubs_false(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text("function foo() { return 42; }")

        from agent.agent_utils_ts import has_ts_stubs

        assert has_ts_stubs(str(f)) is False

    def test_has_ts_stubs_missing_file(self) -> None:
        from agent.agent_utils_ts import has_ts_stubs

        assert has_ts_stubs("/nonexistent/path.ts") is False

    def test_extract_ts_stubs_finds_function(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text(
            "export function calculate(a: number): number {\n"
            '  throw new Error("STUB");\n'
            "}\n"
        )

        from agent.agent_utils_ts import extract_ts_stubs

        stubs = extract_ts_stubs(str(f))
        assert len(stubs) == 1
        assert "calculate" in stubs[0]

    def test_extract_ts_stubs_multiple(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text(
            "export function foo() {\n"
            '  throw new Error("STUB");\n'
            "}\n"
            "export function bar() {\n"
            '  throw new Error("STUB");\n'
            "}\n"
        )

        from agent.agent_utils_ts import extract_ts_stubs

        stubs = extract_ts_stubs(str(f))
        assert len(stubs) == 2

    def test_extract_ts_stubs_no_stubs(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text("export function foo() { return 1; }\n")

        from agent.agent_utils_ts import extract_ts_stubs

        assert extract_ts_stubs(str(f)) == []

    def test_extract_ts_stubs_missing_file(self) -> None:
        from agent.agent_utils_ts import extract_ts_stubs

        assert extract_ts_stubs("/nonexistent/path.ts") == []

    def test_extract_ts_stubs_arrow_function(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text(
            "const greet = (name: string) => {\n" '  throw new Error("STUB");\n' "};\n"
        )

        from agent.agent_utils_ts import extract_ts_stubs

        stubs = extract_ts_stubs(str(f))
        assert len(stubs) == 1
        assert "greet" in stubs[0]

    def test_extract_ts_stubs_class_method(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text(
            "class Foo {\n"
            "  public doThing(x: number): void {\n"
            '    throw new Error("STUB");\n'
            "  }\n"
            "}\n"
        )

        from agent.agent_utils_ts import extract_ts_stubs

        stubs = extract_ts_stubs(str(f))
        assert len(stubs) == 1
        assert "doThing" in stubs[0]


# ===================================================================
# _find_enclosing_signature
# ===================================================================


class TestFindEnclosingSignature:
    def test_finds_export_function(self) -> None:
        from agent.agent_utils_ts import _find_enclosing_signature

        lines = [
            "export function process(data: string): string {",
            '  throw new Error("STUB");',
            "}",
        ]
        result = _find_enclosing_signature(lines, 1)
        assert result is not None
        assert "process" in result

    def test_finds_async_function(self) -> None:
        from agent.agent_utils_ts import _find_enclosing_signature

        lines = [
            "export async function fetchData(): Promise<void> {",
            '  throw new Error("STUB");',
            "}",
        ]
        result = _find_enclosing_signature(lines, 1)
        assert result is not None
        assert "fetchData" in result

    def test_returns_none_for_orphan_stub(self) -> None:
        from agent.agent_utils_ts import _find_enclosing_signature

        lines = [
            "// just a comment",
            "// another comment",
            '  throw new Error("STUB");',
        ]
        result = _find_enclosing_signature(lines, 2)
        assert result is None

    def test_lookback_limit(self) -> None:
        from agent.agent_utils_ts import _find_enclosing_signature

        lines = (
            ["function distant() {"] + ["// filler"] * 25 + ['throw new Error("STUB");']
        )
        result = _find_enclosing_signature(lines, len(lines) - 1)
        assert result is None


# ===================================================================
# _find_ts_files_to_edit
# ===================================================================


class TestFindTsFilesToEdit:
    def test_excludes_test_files(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")
        (src / "main.test.ts").write_text("")

        from agent.agent_utils_ts import _find_ts_files_to_edit

        files = _find_ts_files_to_edit(str(tmp_path), "src", "src")
        basenames = {os.path.basename(f) for f in files}
        assert "main.ts" in basenames
        assert "main.test.ts" not in basenames

    def test_excludes_config_files(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "index.ts").write_text("")
        (src / "jest.config.ts").write_text("")
        (src / "vitest.config.ts").write_text("")
        (src / "tsconfig.ts").write_text("")

        from agent.agent_utils_ts import _find_ts_files_to_edit

        files = _find_ts_files_to_edit(str(tmp_path), "src", "tests")
        basenames = {os.path.basename(f) for f in files}
        assert "index.ts" in basenames
        assert "jest.config.ts" not in basenames
        assert "vitest.config.ts" not in basenames

    def test_excludes_d_ts(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("")
        (src / "types.d.ts").write_text("")

        from agent.agent_utils_ts import _find_ts_files_to_edit

        files = _find_ts_files_to_edit(str(tmp_path), "src", "tests")
        basenames = {os.path.basename(f) for f in files}
        assert "main.ts" in basenames
        assert "types.d.ts" not in basenames

    def test_root_src_dir_excludes_special_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "index.ts").write_text("")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.ts").write_text("")
        (tmp_path / "coverage").mkdir()
        (tmp_path / "coverage" / "report.ts").write_text("")

        from agent.agent_utils_ts import _find_ts_files_to_edit

        files = _find_ts_files_to_edit(str(tmp_path), ".", "tests")
        basenames = {os.path.basename(f) for f in files}
        assert "index.ts" in basenames
        assert "dep.ts" not in basenames
        assert "report.ts" not in basenames


# ===================================================================
# _strip_ansi
# ===================================================================


class TestStripAnsi:
    def test_strips_color_codes(self) -> None:
        from agent.agent_utils_ts import _strip_ansi

        assert _strip_ansi("\x1b[31mFAIL\x1b[0m") == "FAIL"

    def test_strips_multiple_codes(self) -> None:
        from agent.agent_utils_ts import _strip_ansi

        text = "\x1b[1m\x1b[96mSome text\x1b[39m\x1b[22m"
        assert _strip_ansi(text) == "Some text"

    def test_no_codes(self) -> None:
        from agent.agent_utils_ts import _strip_ansi

        assert _strip_ansi("plain text") == "plain text"

    def test_empty(self) -> None:
        from agent.agent_utils_ts import _strip_ansi

        assert _strip_ansi("") == ""


# ===================================================================
# _deduplicate_ts_errors
# ===================================================================


class TestDeduplicateTsErrors:
    def test_deduplicates_identical_errors(self) -> None:
        from agent.agent_utils_ts import _deduplicate_ts_errors

        text = (
            "TS2339: Property 'x' does not exist on type 'Y'\n"
            "  at src/foo.ts:10\n"
            "\n"
            "TS2339: Property 'x' does not exist on type 'Y'\n"
            "  at src/bar.ts:20\n"
            "\n"
            "TS2345: Argument of type 'A' is not assignable to 'B'\n"
        )
        result = _deduplicate_ts_errors(text)
        assert result.count("TS2339") == 1
        assert "TS2345" in result
        assert "1 duplicate TS error(s) removed" in result

    def test_no_duplicates(self) -> None:
        from agent.agent_utils_ts import _deduplicate_ts_errors

        text = "TS2339: Property 'x'\nTS2345: Type 'A'\n"
        result = _deduplicate_ts_errors(text)
        assert "duplicate" not in result
        assert "TS2339" in result
        assert "TS2345" in result

    def test_empty_input(self) -> None:
        from agent.agent_utils_ts import _deduplicate_ts_errors

        assert _deduplicate_ts_errors("") == ""

    def test_multiple_duplicates(self) -> None:
        from agent.agent_utils_ts import _deduplicate_ts_errors

        lines = []
        for _ in range(5):
            lines.append("TS2339: Property 'x' does not exist")
            lines.append("")
        result = _deduplicate_ts_errors("\n".join(lines))
        assert result.count("TS2339") == 1
        assert "4 duplicate TS error(s) removed" in result


# ===================================================================
# _parse_jest_vitest_output
# ===================================================================


class TestParseJestVitestOutput:
    def test_extracts_fail_block(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = (
            "PASS src/ok.test.ts\n"
            "FAIL src/bad.test.ts\n"
            "  ● should work\n"
            "    expect(1).toBe(2)\n"
            "    Expected: 2\n"
            "    Received: 1\n"
            "\n\n"
            "Test Suites: 1 failed, 1 passed\n"
            "Tests: 1 failed, 1 passed\n"
        )
        result = _parse_jest_vitest_output(raw)
        assert "FAIL src/bad.test.ts" in result
        assert "Test Suites:" in result

    def test_extracts_assertion_lines(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = (
            "FAIL test.ts\n"
            "  expect(received).toBe(expected)\n"
            "  Expected: 42\n"
            "  Received: 0\n"
            "\n\n"
            "Test Suites: 1 failed\n"
        )
        result = _parse_jest_vitest_output(raw)
        assert "Expected:" in result
        assert "Received:" in result

    def test_extracts_summary(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = "Test Suites: 5 passed\nTests: 20 passed\nTime: 3.2s\n"
        result = _parse_jest_vitest_output(raw)
        assert "Test Suites:" in result
        assert "Tests:" in result
        assert "Time:" in result

    def test_strips_ansi_before_parsing(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = "\x1b[31mFAIL\x1b[0m src/bad.test.ts\nTest Suites: 1 failed\n"
        result = _parse_jest_vitest_output(raw)
        assert "\x1b[" not in result

    def test_returns_full_if_no_sections(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = "Some random output\nNo jest sections here\n"
        result = _parse_jest_vitest_output(raw)
        assert "Some random output" in result

    def test_deduplicates_ts_errors_in_output(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = (
            "FAIL src/test.ts\n"
            "TS2339: Property 'x' does not exist on type 'Y'\n"
            "  at file1.ts\n"
            "\n"
            "TS2339: Property 'x' does not exist on type 'Y'\n"
            "  at file2.ts\n"
            "\n\n"
            "Test Suites: 1 failed\n"
        )
        result = _parse_jest_vitest_output(raw)
        assert result.count("TS2339") == 1


# ===================================================================
# _count_tokens
# ===================================================================


class TestCountTokens:
    def test_fallback_without_litellm(self) -> None:
        from agent.agent_utils_ts import _count_tokens

        with patch.dict("sys.modules", {"litellm": None}):
            result = _count_tokens("a" * 100, "")
            assert result == 25

    def test_with_model(self) -> None:
        from agent.agent_utils_ts import _count_tokens

        mock_litellm = MagicMock()
        mock_litellm.token_counter.return_value = 42
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _count_tokens("hello world", "test-model")
            assert result == 42

    def test_litellm_exception_falls_back(self) -> None:
        from agent.agent_utils_ts import _count_tokens

        mock_litellm = MagicMock()
        mock_litellm.token_counter.side_effect = Exception("fail")
        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = _count_tokens("a" * 80, "test-model")
            assert result == 20


# ===================================================================
# summarize_test_output_ts
# ===================================================================


class TestSummarizeTestOutputTs:
    def test_short_output_returned_as_is(self) -> None:
        from agent.agent_utils_ts import summarize_test_output_ts

        raw = "PASS all tests\nTests: 5 passed\n"
        result, costs = summarize_test_output_ts(raw, max_length=15000)
        assert result == raw
        assert costs == []

    def test_tier1_deterministic_parse(self) -> None:
        from agent.agent_utils_ts import summarize_test_output_ts

        fail_section = "FAIL src/bad.test.ts\n  ● broken test\n"
        summary = "Test Suites: 1 failed\nTests: 1 failed\n"
        padding = "x\n" * 5000
        raw = fail_section + padding + summary

        result, costs = summarize_test_output_ts(raw, max_length=500)
        assert "FAIL" in result
        assert costs == []

    def test_tier3_truncation_fallback(self) -> None:
        from agent.agent_utils_ts import summarize_test_output_ts

        raw = "A" * 50000
        result, costs = summarize_test_output_ts(raw, max_length=15000, model="")
        assert "... [truncated] ..." in result
        assert len(result) < len(raw)

    def _mock_litellm_for_tier2(
        self, content="Summarized", cost=0.001, side_effect=None
    ):
        """Helper: returns a mock litellm module for tier 2 tests."""
        m = MagicMock()
        if side_effect:
            m.completion.side_effect = side_effect
        else:
            resp = MagicMock()
            resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = content
            m.completion.return_value = resp
            m.completion_cost.return_value = cost
        m.token_counter.return_value = 99999
        return m

    def _run_with_mock_litellm(self, mock_ll, **kwargs):
        """Run summarize_test_output_ts with litellm mocked and _count_tokens
        rigged so max_token_length is small (100) and raw/parsed tokens large (99999).
        """
        import agent.agent_utils_ts as mod

        call_num = [0]

        def _fake_count_tokens(text, model):
            call_num[0] += 1
            if call_num[0] == 1:
                return 100
            return 99999

        real_litellm = sys.modules.get("litellm")
        sys.modules["litellm"] = mock_ll
        try:
            with patch.object(mod, "_count_tokens", side_effect=_fake_count_tokens):
                return mod.summarize_test_output_ts(**kwargs)
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm
            else:
                sys.modules.pop("litellm", None)

    def test_tier2_llm_with_proxy_kwargs(self) -> None:
        mock_ll = self._mock_litellm_for_tier2(content="Summarized output", cost=0.001)

        result, costs = self._run_with_mock_litellm(
            mock_ll,
            raw_output="A" * 100000,
            max_length=1000,
            model="test-model",
            api_base="http://proxy:9090",
            api_key="pk-test",
        )

        assert result == "Summarized output"
        assert len(costs) == 1
        assert costs[0].cost == 0.001
        call_kwargs = mock_ll.completion.call_args
        assert call_kwargs.kwargs.get("api_base") == "http://proxy:9090"
        assert call_kwargs.kwargs.get("api_key") == "pk-test"

    def test_tier2_llm_no_proxy_kwargs_when_empty(self) -> None:
        mock_ll = self._mock_litellm_for_tier2(content="Summary")

        self._run_with_mock_litellm(
            mock_ll,
            raw_output="B" * 100000,
            max_length=1000,
            model="test-model",
            api_base="",
            api_key="",
        )

        call_kwargs = mock_ll.completion.call_args
        assert "api_base" not in call_kwargs.kwargs
        assert "api_key" not in call_kwargs.kwargs

    def test_tier2_llm_failure_falls_to_tier3(self) -> None:
        mock_ll = self._mock_litellm_for_tier2(side_effect=Exception("API down"))
        mock_ll.token_counter.return_value = 99999

        result, costs = self._run_with_mock_litellm(
            mock_ll,
            raw_output="C" * 100000,
            max_length=15000,
            model="test-model",
        )

        assert "... [truncated] ..." in result

    def test_tier2_empty_content_falls_to_tier3(self) -> None:
        mock_ll = self._mock_litellm_for_tier2(content="")

        result, costs = self._run_with_mock_litellm(
            mock_ll,
            raw_output="D" * 100000,
            max_length=15000,
            model="test-model",
        )

        assert "... [truncated] ..." in result

    def test_ansi_stripped_before_processing(self) -> None:
        from agent.agent_utils_ts import summarize_test_output_ts

        raw = "\x1b[31mFAIL\x1b[0m test\nTests: 1 failed\n"
        result, _ = summarize_test_output_ts(raw, max_length=50000)
        assert "\x1b[" not in result


# ===================================================================
# get_message_ts — spec PDF/bz2 injection
# ===================================================================


class TestGetMessageTsSpec:
    def test_spec_disabled(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import get_message_ts, SPEC_INFO_HEADER

        cfg = _make_agent_config(use_spec_info=False)
        msg, costs = get_message_ts(cfg, str(tmp_path))
        assert SPEC_INFO_HEADER not in msg
        assert costs == []

    def test_readme_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# My Library\nDoes stuff.")

        from agent.agent_utils_ts import get_message_ts, SPEC_INFO_HEADER

        cfg = _make_agent_config(use_spec_info=True)
        msg, costs = get_message_ts(cfg, str(tmp_path))
        assert SPEC_INFO_HEADER in msg
        assert "My Library" in msg
        assert costs == []

    def test_readme_priority_order(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("markdown content")
        (tmp_path / "README.rst").write_text("rst content")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        assert "markdown content" in msg
        assert "rst content" not in msg

    def test_readme_rst_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "README.rst").write_text("rst documentation")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        assert "rst documentation" in msg

    def test_readme_truncation(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("x" * 10000)

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True, max_spec_info_length=100)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        # Raw README is 10000 chars but spec info is truncated to 100 chars; the
        # remainder is fixed task boilerplate, so the total stays far below the
        # raw size. Bound accounts for the current (grown) message template.
        assert len(msg) < 700
        assert "x" * 200 not in msg

    def test_no_readme_no_spec(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import get_message_ts, SPEC_INFO_HEADER

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        assert SPEC_INFO_HEADER not in msg

    def test_bz2_decompression_and_pdf_extraction(self, tmp_path: Path) -> None:
        pdf_content = b"fake pdf content for testing"
        bz2_data = bz2.compress(pdf_content)
        (tmp_path / "spec.pdf.bz2").write_bytes(bz2_data)

        from agent.agent_utils_ts import get_message_ts, SPEC_INFO_HEADER

        cfg = _make_agent_config(use_spec_info=True)
        with patch(
            "agent.agent_utils.get_specification", return_value="Extracted spec text"
        ):
            msg, costs = get_message_ts(cfg, str(tmp_path))

        assert SPEC_INFO_HEADER in msg
        assert "Extracted spec text" in msg
        assert (tmp_path / "spec.pdf").exists()

    def test_pdf_already_exists_skips_decompression(self, tmp_path: Path) -> None:
        (tmp_path / "spec.pdf").write_bytes(b"existing pdf")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True)
        with patch("agent.agent_utils.get_specification", return_value="PDF text"):
            msg, _ = get_message_ts(cfg, str(tmp_path))

        assert "PDF text" in msg

    def test_bz2_decompression_failure_falls_to_readme(self, tmp_path: Path) -> None:
        (tmp_path / "spec.pdf.bz2").write_bytes(b"not valid bz2 data")
        (tmp_path / "README.md").write_text("Fallback readme")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        assert "Fallback readme" in msg
        assert not (tmp_path / "spec.pdf").exists()

    def test_spec_summarization_triggered(self, tmp_path: Path) -> None:
        (tmp_path / "spec.pdf").write_bytes(b"pdf")

        from agent.agent_utils_ts import get_message_ts
        from agent.thinking_capture import SummarizerCost

        long_text = "x" * 10000
        mock_cost = SummarizerCost()
        mock_cost.cost = 0.05

        cfg = _make_agent_config(
            use_spec_info=True,
            max_spec_info_length=3000,
        )
        with patch("agent.agent_utils.get_specification", return_value=long_text):
            with patch(
                "agent.agent_utils.summarize_specification",
                return_value=("Summarized", [mock_cost]),
            ) as mock_summarize:
                msg, costs = get_message_ts(cfg, str(tmp_path))

        mock_summarize.assert_called_once()
        assert len(costs) == 1
        assert costs[0].cost == 0.05
        assert "Summarized" in msg

    def test_spec_no_summarization_when_short(self, tmp_path: Path) -> None:
        (tmp_path / "spec.pdf").write_bytes(b"pdf")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True, max_spec_info_length=50000)
        with patch("agent.agent_utils.get_specification", return_value="Short spec"):
            with patch("agent.agent_utils.summarize_specification") as mock_summarize:
                msg, _ = get_message_ts(cfg, str(tmp_path))

        mock_summarize.assert_not_called()
        assert "Short spec" in msg

    def test_pdf_extraction_failure_falls_to_readme(self, tmp_path: Path) -> None:
        (tmp_path / "spec.pdf").write_bytes(b"pdf")
        (tmp_path / "README.md").write_text("Readme fallback")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_spec_info=True)
        with patch(
            "agent.agent_utils.get_specification", side_effect=Exception("fitz error")
        ):
            msg, _ = get_message_ts(cfg, str(tmp_path))

        assert "Readme fallback" in msg


# ===================================================================
# get_message_ts — unit tests info
# ===================================================================


class TestGetMessageTsUnitTests:
    def test_includes_test_file_contents(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test" / "foo.test.ts"
        test_file.parent.mkdir()
        test_file.write_text("describe('foo', () => { it('works', () => {}); });")

        from agent.agent_utils_ts import get_message_ts, UNIT_TESTS_INFO_HEADER

        cfg = _make_agent_config(use_unit_tests_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path), test_files=["test/foo.test.ts"])
        assert UNIT_TESTS_INFO_HEADER in msg
        assert "describe('foo'" in msg

    def test_no_test_files(self) -> None:
        from agent.agent_utils_ts import get_message_ts, UNIT_TESTS_INFO_HEADER

        cfg = _make_agent_config(use_unit_tests_info=True)
        msg, _ = get_message_ts(cfg, "/nonexistent", test_files=[])
        assert UNIT_TESTS_INFO_HEADER not in msg

    def test_disabled(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import get_message_ts, UNIT_TESTS_INFO_HEADER

        cfg = _make_agent_config(use_unit_tests_info=False)
        msg, _ = get_message_ts(cfg, str(tmp_path), test_files=["test/foo.ts"])
        assert UNIT_TESTS_INFO_HEADER not in msg

    def test_missing_test_file_skipped(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_unit_tests_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path), test_files=["nonexistent.test.ts"])
        assert "nonexistent" not in msg

    def test_truncation(self, tmp_path: Path) -> None:
        test_file = tmp_path / "big.test.ts"
        test_file.write_text("x" * 20000)

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(
            use_unit_tests_info=True,
            max_unit_tests_info_length=100,
        )
        msg, _ = get_message_ts(cfg, str(tmp_path), test_files=["big.test.ts"])
        # Raw test file is 20000 chars but unit-tests info is truncated to 100
        # chars; the remainder is fixed task boilerplate. Bound accounts for the
        # current (grown) message template.
        assert len(msg) < 700
        assert "x" * 200 not in msg


# ===================================================================
# get_message_ts — repo info
# ===================================================================


class TestGetMessageTsRepoInfo:
    def test_includes_tree(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")

        from agent.agent_utils_ts import get_message_ts, REPO_INFO_HEADER

        cfg = _make_agent_config(use_repo_info=True)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        assert REPO_INFO_HEADER in msg

    def test_disabled(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import get_message_ts, REPO_INFO_HEADER

        cfg = _make_agent_config(use_repo_info=False)
        msg, _ = get_message_ts(cfg, str(tmp_path))
        assert REPO_INFO_HEADER not in msg


# ===================================================================
# _get_ts_dir_tree
# ===================================================================


class TestGetTsDirTree:
    def test_basic_tree(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "index.ts").write_text("")
        (tmp_path / "package.json").write_text("{}")

        from agent.agent_utils_ts import _get_ts_dir_tree

        tree = _get_ts_dir_tree(tmp_path, max_depth=2)
        assert "src" in tree
        assert "package.json" in tree

    def test_excludes_hidden_and_node_modules(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "dist").mkdir()
        (tmp_path / "src").mkdir()

        from agent.agent_utils_ts import _get_ts_dir_tree

        tree = _get_ts_dir_tree(tmp_path)
        assert ".git" not in tree
        assert "node_modules" not in tree
        assert "dist" not in tree
        assert "src" in tree

    def test_max_depth(self, tmp_path: Path) -> None:
        (tmp_path / "a" / "b" / "c").mkdir(parents=True)
        (tmp_path / "a" / "b" / "c" / "deep.ts").write_text("")

        from agent.agent_utils_ts import _get_ts_dir_tree

        tree = _get_ts_dir_tree(tmp_path, max_depth=1)
        assert "deep.ts" not in tree

    def test_empty_dir(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import _get_ts_dir_tree

        assert _get_ts_dir_tree(tmp_path) == ""


# ===================================================================
# get_changed_ts_files_from_commits
# ===================================================================


class TestGetChangedTsFilesFromCommits:
    def test_returns_only_ts_files(self) -> None:
        from agent.agent_utils_ts import get_changed_ts_files_from_commits

        mock_repo = MagicMock()
        mock_diff_item_ts = MagicMock(a_path="src/index.ts")
        mock_diff_item_js = MagicMock(a_path="src/index.js")
        mock_diff_item_tsx = MagicMock(a_path="src/App.tsx")
        mock_repo.commit.return_value.diff.return_value = [
            mock_diff_item_ts,
            mock_diff_item_js,
            mock_diff_item_tsx,
        ]

        files = get_changed_ts_files_from_commits(mock_repo, "abc123", "def456")
        assert "src/index.ts" in files
        assert "src/App.tsx" in files
        assert "src/index.js" not in files

    def test_handles_exception(self) -> None:
        from agent.agent_utils_ts import get_changed_ts_files_from_commits

        mock_repo = MagicMock()
        mock_repo.commit.side_effect = Exception("bad commit")

        files = get_changed_ts_files_from_commits(mock_repo, "abc", "def")
        assert files == []

    def test_skips_none_paths(self) -> None:
        from agent.agent_utils_ts import get_changed_ts_files_from_commits

        mock_repo = MagicMock()
        mock_diff = [MagicMock(a_path=None), MagicMock(a_path="src/ok.ts")]
        mock_repo.commit.return_value.diff.return_value = mock_diff

        files = get_changed_ts_files_from_commits(mock_repo, "a", "b")
        assert files == ["src/ok.ts"]


# ===================================================================
# get_ts_lint_cmd
# ===================================================================


class TestGetTsLintCmd:
    def test_enabled(self) -> None:
        from agent.agent_utils_ts import get_ts_lint_cmd

        cmd = get_ts_lint_cmd("my-repo", True, "/path/to/config.yaml")
        assert "commit0.cli_ts lint" in cmd
        assert "my-repo" in cmd
        assert "/path/to/config.yaml" in cmd

    def test_disabled(self) -> None:
        from agent.agent_utils_ts import get_ts_lint_cmd

        assert get_ts_lint_cmd("my-repo", False, "/config.yaml") == ""


# ===================================================================
# get_target_edit_files_ts
# ===================================================================


class TestGetTargetEditFilesTs:
    def test_filters_by_stubs_and_diff(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        stubbed = src / "stubbed.ts"
        stubbed.write_text('function foo() { throw new Error("STUB"); }')
        clean = src / "clean.ts"
        clean.write_text("function bar() { return 1; }")

        from agent.agent_utils_ts import get_target_edit_files_ts

        mock_repo = MagicMock()
        mock_repo.working_dir = str(tmp_path)
        mock_repo.git.diff.side_effect = lambda commit, *args: (
            "diff output" if "stubbed" in args[-1] else ""
        )

        files, deps = get_target_edit_files_ts(
            mock_repo, "src", "tests", "branch", "ref_commit"
        )
        assert any("stubbed.ts" in f for f in files)
        assert not any("clean.ts" in f for f in files)
        assert deps == {}

    def test_no_stubs_returns_empty(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.ts").write_text("function foo() { return 1; }")

        from agent.agent_utils_ts import get_target_edit_files_ts

        mock_repo = MagicMock()
        mock_repo.working_dir = str(tmp_path)

        files, deps = get_target_edit_files_ts(
            mock_repo, "src", "tests", "branch", "ref_commit"
        )
        assert files == []
        assert deps == {}

    def test_no_diff_skips_file(self, tmp_path: Path) -> None:
        """Line 209: stub file with empty diff is skipped."""
        src = tmp_path / "src"
        src.mkdir()
        stubbed = src / "stubbed.ts"
        stubbed.write_text('function foo() { throw new Error("STUB"); }')

        from agent.agent_utils_ts import get_target_edit_files_ts

        mock_repo = MagicMock()
        mock_repo.working_dir = str(tmp_path)
        mock_repo.git.diff.return_value = ""

        files, deps = get_target_edit_files_ts(
            mock_repo, "src", "tests", "branch", "ref_commit"
        )
        assert files == []
        assert deps == {}


# ===================================================================
# Additional coverage tests
# ===================================================================


class TestCollectTsTestFilesNonTsSkip:
    def test_js_file_in_test_dir_skipped(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "__tests__"
        test_dir.mkdir()
        (test_dir / "helper.js").write_text("module.exports = {};")
        (test_dir / "helper.ts").write_text("export {};")

        from agent.agent_utils_ts import collect_ts_test_files

        files = collect_ts_test_files(str(tmp_path))
        basenames = {os.path.basename(f) for f in files}
        assert "helper.js" not in basenames
        assert "helper.ts" in basenames

    def test_json_file_in_test_dir_skipped(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        (test_dir / "fixtures.json").write_text("{}")

        from agent.agent_utils_ts import collect_ts_test_files

        assert collect_ts_test_files(str(tmp_path)) == []


class TestExtractTsStubsOrphanFallback:
    def test_orphan_stub_uses_raw_line(self, tmp_path: Path) -> None:
        f = tmp_path / "src.ts"
        f.write_text(
            "// top-level comment\n" "// another remark\n" 'throw new Error("STUB");\n'
        )

        from agent.agent_utils_ts import extract_ts_stubs

        stubs = extract_ts_stubs(str(f))
        assert len(stubs) == 1
        assert 'throw new Error("STUB")' in stubs[0]


class TestGetMessageTsOSErrors:
    def test_test_file_oserror_skipped(self, tmp_path: Path) -> None:
        test_file = tmp_path / "test" / "foo.test.ts"
        test_file.parent.mkdir()
        test_file.write_text("describe('foo', () => {});")

        from agent.agent_utils_ts import get_message_ts

        cfg = _make_agent_config(use_unit_tests_info=True)

        original_open = open

        def _failing_open(path, *args, **kwargs):
            if "foo.test.ts" in str(path):
                raise OSError("permission denied")
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=_failing_open):
            msg, _ = get_message_ts(cfg, str(tmp_path), test_files=["test/foo.test.ts"])

        assert "permission" not in msg

    def test_all_readmes_oserror_fallback(self, tmp_path: Path) -> None:
        for name in ["README.md", "README.rst", "README.txt", "README"]:
            (tmp_path / name).write_text("content")

        from agent.agent_utils_ts import get_message_ts, SPEC_INFO_HEADER

        cfg = _make_agent_config(use_spec_info=True)

        original_read_text = Path.read_text

        def _failing_read_text(self_path, *args, **kwargs):
            if self_path.name.startswith("README"):
                raise OSError("disk error")
            return original_read_text(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", _failing_read_text):
            msg, _ = get_message_ts(cfg, str(tmp_path))

        assert SPEC_INFO_HEADER not in msg


class TestGetTsDirTreeOSError:
    def test_permission_denied_returns_empty(self, tmp_path: Path) -> None:
        from agent.agent_utils_ts import _get_ts_dir_tree

        with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            result = _get_ts_dir_tree(tmp_path)

        assert result == ""


class TestParseJestVitestOutputAdditional:
    def test_assertionerror_grep_match(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = (
            "Some output\n"
            "AssertionError: expected true to be false\n"
            "Test Suites: 1 failed\n"
        )
        result = _parse_jest_vitest_output(raw)
        assert "AssertionError" in result

    def test_summary_failed_test_line(self) -> None:
        from agent.agent_utils_ts import _parse_jest_vitest_output

        raw = "Some random output\n" "3 failed test cases\n" "done\n"
        result = _parse_jest_vitest_output(raw)
        assert "3 failed test cases" in result


class TestCountTokensMaxTokenLengthZero:
    def test_zero_token_count_triggers_fallback(self) -> None:
        mock_litellm = MagicMock()
        mock_litellm.token_counter.return_value = 0

        import agent.agent_utils_ts as mod

        real_litellm = sys.modules.get("litellm")
        sys.modules["litellm"] = mock_litellm
        try:
            result, costs = mod.summarize_test_output_ts(
                raw_output="short output",
                max_length=100,
                model="test-model",
            )
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm
            else:
                sys.modules.pop("litellm", None)

        assert isinstance(result, str)


class TestSummarizeTestOutputTsTier1Success:
    def test_tier1_parse_fits_budget(self) -> None:
        import agent.agent_utils_ts as mod

        raw = (
            "FAIL src/bad.test.ts\n"
            "  ● should work\n"
            "    expect(1).toBe(2)\n"
            "    Expected: 2\n"
            "    Received: 1\n"
            "\n\n"
            "Test Suites: 1 failed, 1 passed\n"
            "Tests: 1 failed, 1 passed\n"
        )
        padding = "x\n" * 2000
        big_raw = raw + padding + raw

        call_num = [0]

        def _fake_count(text, model):
            call_num[0] += 1
            if call_num[0] == 1:
                return 500
            if call_num[0] == 2:
                return 1000
            return 100

        with patch.object(mod, "_count_tokens", side_effect=_fake_count):
            result, costs = mod.summarize_test_output_ts(
                raw_output=big_raw,
                max_length=2000,
                model="test-model",
            )

        assert "FAIL" in result
        assert costs == []


class TestSummarizeTestOutputTsCompletionCostException:
    def test_completion_cost_exception_handled(self) -> None:
        import agent.agent_utils_ts as mod

        mock_ll = MagicMock()
        resp = MagicMock()
        resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "Summarized"
        mock_ll.completion.return_value = resp
        mock_ll.completion_cost.side_effect = Exception("cost calc failed")
        mock_ll.token_counter.return_value = 99999

        call_num = [0]

        def _fake_count(text, model):
            call_num[0] += 1
            if call_num[0] == 1:
                return 100
            return 99999

        real_litellm = sys.modules.get("litellm")
        sys.modules["litellm"] = mock_ll
        try:
            with patch.object(mod, "_count_tokens", side_effect=_fake_count):
                result, costs = mod.summarize_test_output_ts(
                    raw_output="A" * 100000,
                    max_length=1000,
                    model="test-model",
                )
        finally:
            if real_litellm is not None:
                sys.modules["litellm"] = real_litellm
            else:
                sys.modules.pop("litellm", None)

        assert result == "Summarized"
        assert len(costs) == 1
        assert costs[0].cost == 0.0
        assert costs[0].prompt_tokens == 10
        assert costs[0].completion_tokens == 5
