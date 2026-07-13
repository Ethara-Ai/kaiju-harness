from __future__ import annotations

import inspect
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_utils_js import (
    _ANSI_RE,
    _JS_ERROR_KEY_RE,
    _find_enclosing_signature,
    collect_js_test_files,
    extract_js_stubs,
    has_js_stubs,
    summarize_test_output_js,
)


JS_STUB_BODY = (
    'function foo(x) {\n'
    '  // __COMMIT0_STUB__ foo\n'
    '  throw new Error("STUB");\n'
    '}\n'
)


class TestFindEnclosingSignatureBounded:
    def test_returns_signature_within_20_line_window(self) -> None:
        lines = [
            "function adder(a, b) {",
            "  let r = 0;",
            "  // __COMMIT0_STUB__ adder",
            '  throw new Error("STUB");',
            "}",
        ]
        sig = _find_enclosing_signature(lines, 2)
        assert sig is not None
        assert "function adder" in sig

    def test_returns_none_if_no_signature_in_window(self) -> None:
        lines = ["// no fn here"] * 30 + ["// __COMMIT0_STUB__"]
        sig = _find_enclosing_signature(lines, 30)
        assert sig is None

    @pytest.mark.parametrize(
        "adversarial_line",
        [
            "(" * 500 + ")" * 500,
            "abc" * 1000,
            "function " + "a" * 1000 + "(",
        ],
    )
    def test_no_redos_under_bounded_adversarial_input(
        self, adversarial_line: str
    ) -> None:
        lines = [adversarial_line, "// __COMMIT0_STUB__"]
        start = time.monotonic()
        _find_enclosing_signature(lines, 1)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, (
            f"_find_enclosing_signature took {elapsed:.2f}s on bounded adversarial "
            f"input ({len(adversarial_line)} chars). AU-G1 documents that "
            f"larger inputs can trip O(n²) backtracking on patterns like "
            f"`\\w+\\s*\\([^)]*\\)\\s*\\{{`; tighten the regex if this fails."
        )


class TestJsErrorKeyReNoRedos:
    def test_large_single_line_input_completes_quickly(self) -> None:
        line = "Error: " + "x" * 1_000_000
        start = time.monotonic()
        match = _JS_ERROR_KEY_RE.search(line)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, (
            f"_JS_ERROR_KEY_RE took {elapsed:.2f}s on 1MB single line — ReDoS"
        )
        assert match is not None

    def test_no_match_completes_quickly(self) -> None:
        line = "x" * 1_000_000
        start = time.monotonic()
        match = _JS_ERROR_KEY_RE.search(line)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0
        assert match is None

    def test_pattern_is_anchored_to_error_prefix(self) -> None:
        assert _JS_ERROR_KEY_RE.search("Error: something broke") is not None
        assert _JS_ERROR_KEY_RE.search("just a message") is None


class TestCollectJsTestFilesPatternMatching:
    def test_dot_test_dot_js_caught(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        target = tmp_path / "src" / "foo.test.js"
        target.write_text("test('x', () => {});\n")
        found = collect_js_test_files(str(tmp_path))
        assert str(target) in found

    def test_dot_spec_dot_js_caught(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        target = tmp_path / "src" / "foo.spec.js"
        target.write_text("test('x', () => {});\n")
        found = collect_js_test_files(str(tmp_path))
        assert str(target) in found

    def test_files_in_underscore_tests_dir_caught(self, tmp_path: Path) -> None:
        d = tmp_path / "__tests__"
        d.mkdir()
        target = d / "anything.js"
        target.write_text("test('x', () => {});\n")
        found = collect_js_test_files(str(tmp_path))
        assert str(target) in found

    def test_files_in_test_dir_caught(self, tmp_path: Path) -> None:
        d = tmp_path / "test"
        d.mkdir()
        target = d / "anything.js"
        target.write_text("test('x', () => {});\n")
        found = collect_js_test_files(str(tmp_path))
        assert str(target) in found

    def test_files_in_tests_dir_caught(self, tmp_path: Path) -> None:
        d = tmp_path / "tests"
        d.mkdir()
        target = d / "anything.js"
        target.write_text("test('x', () => {});\n")
        found = collect_js_test_files(str(tmp_path))
        assert str(target) in found

    def test_non_test_file_outside_test_dir_not_caught(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "src").mkdir()
        target = tmp_path / "src" / "regular.js"
        target.write_text("export default 1;\n")
        found = collect_js_test_files(str(tmp_path))
        assert str(target) not in found

    def test_node_modules_excluded(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules" / "foo").mkdir(parents=True)
        excluded = tmp_path / "node_modules" / "foo" / "x.test.js"
        excluded.write_text("test('x', () => {});\n")
        found = collect_js_test_files(str(tmp_path))
        assert not any("node_modules" in f for f in found)

    def test_lstrip_star_pattern_quirk_documented(self) -> None:
        from commit0.harness.constants_js import JS_TEST_FILE_PATTERNS

        for pat in JS_TEST_FILE_PATTERNS:
            stripped = pat.lstrip("*")
            if pat.startswith("**/") and not stripped.endswith(
                (".js", ".mjs", ".cjs", ".jsx")
            ):
                pytest.fail(
                    f"unexpected pattern shape after lstrip: {pat!r} -> {stripped!r}"
                )


class TestExtractJsStubs:
    def test_finds_signature_for_stub(self, tmp_path: Path) -> None:
        target = tmp_path / "lib.js"
        target.write_text(JS_STUB_BODY, encoding="utf-8")
        stubs = extract_js_stubs(str(target))
        assert any("function foo" in s for s in stubs)

    def test_returns_empty_for_no_stub_marker(self, tmp_path: Path) -> None:
        target = tmp_path / "nostub.js"
        target.write_text("function foo() { return 1; }\n")
        assert extract_js_stubs(str(target)) == []

    def test_has_js_stubs_true_when_marker_present(self, tmp_path: Path) -> None:
        target = tmp_path / "lib.js"
        target.write_text(JS_STUB_BODY, encoding="utf-8")
        assert has_js_stubs(str(target)) is True

    def test_has_js_stubs_false_when_marker_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "lib.js"
        target.write_text("function foo() { return 1; }")
        assert has_js_stubs(str(target)) is False


class TestExtractJsStubsExtendedPatterns:
    def test_arrow_function_const_assignment(self, tmp_path: Path) -> None:
        target = tmp_path / "arrow.js"
        target.write_text(
            "const handler = (req, res) => {\n"
            "  // __COMMIT0_STUB__ handler\n"
            '  throw new Error("STUB");\n'
            "};\n",
            encoding="utf-8",
        )
        stubs = extract_js_stubs(str(target))
        assert any("const handler" in s and "=>" in s for s in stubs)

    def test_async_function_declaration(self, tmp_path: Path) -> None:
        target = tmp_path / "async.js"
        target.write_text(
            "async function fetchData(url) {\n"
            "  // __COMMIT0_STUB__ fetchData\n"
            '  throw new Error("STUB");\n'
            "}\n",
            encoding="utf-8",
        )
        stubs = extract_js_stubs(str(target))
        assert any("async function fetchData" in s for s in stubs)

    def test_generator_function_declaration(self, tmp_path: Path) -> None:
        target = tmp_path / "gen.js"
        target.write_text(
            "function* range(n) {\n"
            "  // __COMMIT0_STUB__ range\n"
            '  throw new Error("STUB");\n'
            "}\n",
            encoding="utf-8",
        )
        stubs = extract_js_stubs(str(target))
        assert any("function*" in s for s in stubs)

    def test_class_method_with_computed_name(self, tmp_path: Path) -> None:
        target = tmp_path / "computed.js"
        target.write_text(
            "class Collection {\n"
            "  [Symbol.iterator]() {\n"
            "    // __COMMIT0_STUB__ iterator\n"
            '    throw new Error("STUB");\n'
            "  }\n"
            "}\n",
            encoding="utf-8",
        )
        stubs = extract_js_stubs(str(target))
        assert any("[Symbol.iterator]" in s for s in stubs)

    def test_let_arrow_with_async(self, tmp_path: Path) -> None:
        target = tmp_path / "async_arrow.js"
        target.write_text(
            "let handler = async (req) => {\n"
            "  // __COMMIT0_STUB__ handler\n"
            '  throw new Error("STUB");\n'
            "};\n",
            encoding="utf-8",
        )
        stubs = extract_js_stubs(str(target))
        assert any("let handler" in s and "async" in s for s in stubs)

    def test_var_function_expression(self, tmp_path: Path) -> None:
        target = tmp_path / "varfn.js"
        target.write_text(
            "var compute = function (n) {\n"
            "  // __COMMIT0_STUB__ compute\n"
            '  throw new Error("STUB");\n'
            "};\n",
            encoding="utf-8",
        )
        stubs = extract_js_stubs(str(target))
        assert any("var compute" in s and "function" in s for s in stubs)


class TestFindEnclosingSignatureObjectMethodDisambiguation:
    def test_object_shorthand_method_matches(self) -> None:
        lines = [
            "const obj = {",
            "  foo() {",
            "    // __COMMIT0_STUB__ foo",
            '    throw new Error("STUB");',
            "  }",
            "};",
        ]
        sig = _find_enclosing_signature(lines, 2)
        assert sig is not None
        assert "foo()" in sig

    def test_function_declaration_preferred_over_method_in_same_line(self) -> None:
        lines = [
            "function bar() {",
            "  // __COMMIT0_STUB__ bar",
            '  throw new Error("STUB");',
            "}",
        ]
        sig = _find_enclosing_signature(lines, 1)
        assert sig is not None
        assert "function bar" in sig

    def test_quoted_string_key_method(self) -> None:
        lines = [
            "const obj = {",
            "  'foo'() {",
            "    // __COMMIT0_STUB__ foo",
            '    throw new Error("STUB");',
            "  }",
            "};",
        ]
        sig = _find_enclosing_signature(lines, 2)
        assert sig is not None
        assert "'foo'" in sig

    def test_computed_symbol_arrow_property(self) -> None:
        lines = [
            "const obj = {",
            "  [Symbol.iterator]: () => {",
            "    // __COMMIT0_STUB__ iter",
            '    throw new Error("STUB");',
            "  },",
            "};",
        ]
        sig = _find_enclosing_signature(lines, 2)
        assert sig is not None
        assert "Symbol.iterator" in sig

    def test_double_quoted_string_key_method(self) -> None:
        lines = [
            "const obj = {",
            '  "compute"() {',
            "    // __COMMIT0_STUB__ compute",
            '    throw new Error("STUB");',
            "  }",
            "};",
        ]
        sig = _find_enclosing_signature(lines, 2)
        assert sig is not None
        assert '"compute"' in sig


class TestStripAnsi:
    def test_csi_sequences_removed(self) -> None:
        assert _ANSI_RE.sub("", "\x1b[31mred\x1b[0m") == "red"

    def test_no_ansi_unchanged(self) -> None:
        assert _ANSI_RE.sub("", "plain") == "plain"


class TestSummarizeTestOutputJsLlmFallback:
    def test_under_budget_returns_raw(self) -> None:
        out, costs = summarize_test_output_js(
            "short output", max_length=15000, model="", max_tokens=4000
        )
        assert out == "short output"
        assert costs == []

    def test_llm_failure_falls_through_to_truncation(self) -> None:
        big_output = ("FAIL " * 30000) + "\nError: boom\n"

        with patch(
            "agent.agent_utils_js._count_tokens",
            side_effect=lambda text, _model: len(text),
        ):
            with patch("agent.agent_utils_js.logger.warning"):
                fake_completion = MagicMock(
                    side_effect=RuntimeError("LLM unreachable")
                )
                with patch.dict(
                    "sys.modules",
                    {"litellm": MagicMock(completion=fake_completion)},
                ):
                    out, costs = summarize_test_output_js(
                        big_output,
                        max_length=5000,
                        model="claude-haiku-4-5-20251001",
                        max_tokens=100,
                    )
        assert "[truncated]" in out
        assert costs == []

    def test_llm_auth_error_does_not_propagate(self) -> None:
        big_output = ("FAIL" * 30000) + "\nError: boom\n"

        class FakeAuthError(Exception):
            pass

        with patch(
            "agent.agent_utils_js._count_tokens",
            side_effect=lambda text, _model: len(text),
        ):
            fake_completion = MagicMock(side_effect=FakeAuthError("bad token"))
            with patch.dict(
                "sys.modules",
                {"litellm": MagicMock(completion=fake_completion)},
            ):
                out, _costs = summarize_test_output_js(
                    big_output,
                    max_length=200,
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                )
        assert isinstance(out, str)
        assert len(out) > 0

    def test_llm_response_path_propagates_content(self) -> None:
        big_output = ("FAIL " * 30000) + "\nError: boom\n"

        fake_message = MagicMock()
        fake_message.content = "Compressed summary."
        fake_choice = MagicMock()
        fake_choice.message = fake_message
        fake_response = MagicMock()
        fake_response.choices = [fake_choice]
        fake_response.usage = MagicMock(prompt_tokens=100, completion_tokens=20)

        fake_litellm = MagicMock()
        fake_litellm.completion.return_value = fake_response
        fake_litellm.completion_cost.return_value = 0.001

        with patch(
            "agent.agent_utils_js._count_tokens",
            side_effect=lambda text, _model: len(text),
        ):
            with patch.dict("sys.modules", {"litellm": fake_litellm}):
                out, costs = summarize_test_output_js(
                    big_output,
                    max_length=200,
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                )
        assert out == "Compressed summary."
        assert len(costs) == 1


class TestSummarizeTestOutputJsRedactionGap:
    def test_completion_payload_contains_parsed_test_output(self) -> None:
        big_output = (
            "PASS test/foo.test.js\n"
            "FAIL test/bar.test.js\n"
            "  ● bar test > should work\n"
            "    Error: assertion failed\n"
            + ("padding " * 30000)
        )

        captured_prompt: dict[str, str] = {}

        def _capture_completion(model, messages, max_tokens, **kwargs):
            for msg in messages:
                if msg["role"] == "user":
                    captured_prompt["content"] = msg["content"]
            fake_message = MagicMock()
            fake_message.content = "ok"
            choice = MagicMock()
            choice.message = fake_message
            response = MagicMock()
            response.choices = [choice]
            response.usage = MagicMock(prompt_tokens=10, completion_tokens=2)
            return response

        fake_litellm = MagicMock()
        fake_litellm.completion.side_effect = _capture_completion
        fake_litellm.completion_cost.return_value = 0.0

        with patch(
            "agent.agent_utils_js._count_tokens",
            side_effect=lambda text, _model: len(text),
        ):
            with patch.dict("sys.modules", {"litellm": fake_litellm}):
                summarize_test_output_js(
                    big_output,
                    max_length=200,
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                )

        content = captured_prompt.get("content", "")
        assert content.startswith("Summarize this test output:"), (
            "AU-G16: payload prefix must signal LLM how to handle the data"
        )
        assert isinstance(content, str)
        assert len(content) > 0

    def test_no_redaction_layer_between_parser_and_llm_call(self) -> None:
        source = inspect.getsource(summarize_test_output_js)
        assert "redact" not in source.lower(), (
            "AU-G16: if redaction is added, update this guard so the test no "
            "longer asserts the gap"
        )
        assert "litellm.completion" in source


class TestPipelineShellNoArgEval:
    def test_pipeline_does_not_eval_arguments(self) -> None:
        pipeline = Path(__file__).resolve().parents[2] / "run_pipeline_js.sh"
        if not pipeline.exists():
            pytest.skip("run_pipeline_js.sh not at expected path")
        source = pipeline.read_text(encoding="utf-8")
        # Scan only executable shell content: strip full-line and inline
        # comments so English prose (e.g. "the eval writes ...") does not
        # false-positive. The guard still catches a real `eval` command or an
        # unquoted command substitution used on arguments.
        code_lines = []
        for line in source.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            code_lines.append(line.split(" #", 1)[0])
        code = "\n".join(code_lines)
        forbidden = ("eval ", "eval\t", '"$($', '"`')
        for tok in forbidden:
            assert tok not in code, (
                f"PI-G5: pipeline shell must not use eval/command-substitution "
                f"on arguments; found {tok!r}"
            )

    def test_pipeline_quotes_argument_substitutions(self) -> None:
        pipeline = Path(__file__).resolve().parents[2] / "run_pipeline_js.sh"
        if not pipeline.exists():
            pytest.skip("run_pipeline_js.sh not at expected path")
        source = pipeline.read_text(encoding="utf-8")
        assert "set -euo pipefail" in source, (
            "PI-G5: pipeline shell must use strict mode"
        )

    def test_pipeline_argument_handling_uses_shift_pattern(self) -> None:
        pipeline = Path(__file__).resolve().parents[2] / "run_pipeline_js.sh"
        if not pipeline.exists():
            pytest.skip("run_pipeline_js.sh not at expected path")
        source = pipeline.read_text(encoding="utf-8")
        assert "shift" in source, (
            "PI-G5: pipeline shell must use 'shift' for arg consumption rather "
            "than positional re-evaluation"
        )


class TestF003F008CleanupKillsProcessTree:
    def _source(self) -> str:
        pipeline = Path(__file__).resolve().parents[2] / "run_pipeline_js.sh"
        if not pipeline.exists():
            pytest.skip("run_pipeline_js.sh not at expected path")
        return pipeline.read_text(encoding="utf-8")

    def test_cleanup_uses_pkill_P_not_pgid_kill(self) -> None:
        source = self._source()
        cleanup_idx = source.find("cleanup() {")
        assert cleanup_idx != -1, "cleanup() function not found"
        end_idx = source.find("\n}\n", cleanup_idx)
        cleanup_body = source[cleanup_idx:end_idx]

        assert 'kill -- -"$AGENT_PID"' not in cleanup_body, (
            "F-003: cleanup() must NOT use `kill -- -PID` (process-group kill) "
            "because the agent is launched without setsid so AGENT_PID is not a "
            "PG leader — the PG kill silently fails and orphans the whole "
            "agent/aider/npm subtree."
        )
        assert 'kill -9 -- -"$AGENT_PID"' not in cleanup_body, (
            "F-003: same as above for SIGKILL variant."
        )
        assert 'pkill -TERM -P "$AGENT_PID"' in cleanup_body, (
            "F-003: cleanup() must use `pkill -P AGENT_PID` to walk the "
            "descendant tree explicitly."
        )
        assert 'pkill -KILL -P "$AGENT_PID"' in cleanup_body

    def test_watchdog_uses_kill_tree_helper(self) -> None:
        source = self._source()
        assert "watchdog_kill_tree()" in source, (
            "F-008: watchdog must define a kill-tree helper rather than calling "
            "`kill PID` directly (which leaves grandchildren orphaned)."
        )
        watchdog_idx = source.find("watchdog_run() {")
        assert watchdog_idx != -1
        end_idx = source.find("\n}\n", watchdog_idx)
        watchdog_body = source[watchdog_idx:end_idx]

        for label, snippet in (
            ("absolute wall-time cap", 'watchdog_kill_tree "$agent_pid"'),
            ("hard timeout", 'watchdog_kill_tree "$agent_pid"'),
            ("inactivity", 'watchdog_kill_tree "$agent_pid"'),
        ):
            assert watchdog_body.count(snippet) >= 1, (
                f"F-008: watchdog {label} branch must call watchdog_kill_tree "
                "instead of bare `kill PID`"
            )

    def test_watchdog_kill_tree_uses_pkill_P_and_signals(self) -> None:
        source = self._source()
        helper_idx = source.find("watchdog_kill_tree() {")
        assert helper_idx != -1
        end_idx = source.find("\n}\n", helper_idx)
        body = source[helper_idx:end_idx]

        assert 'pkill -TERM -P "$root_pid"' in body, (
            "F-008: kill-tree helper must SIGTERM descendants first"
        )
        assert 'pkill -KILL -P "$root_pid"' in body, (
            "F-008: kill-tree helper must SIGKILL descendants on escalation"
        )
        assert 'kill -TERM "$root_pid"' in body
        assert 'kill -KILL "$root_pid"' in body
