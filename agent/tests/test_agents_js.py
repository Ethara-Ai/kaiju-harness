from __future__ import annotations

import ast
import importlib.util
import inspect
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.agents_js as agents_js_module
from agent.agents_js import (
    AiderJsAgents,
    AiderJsReturn,
    JsAgentReturn,
    JsAgents,
    _load_js_system_prompt,
    handle_logging,
)


AGENTS_JS_PATH = Path(agents_js_module.__file__)
AGENTS_JS_SOURCE = AGENTS_JS_PATH.read_text(encoding="utf-8")

_AIDER_AVAILABLE = importlib.util.find_spec("aider") is not None
_requires_aider = pytest.mark.skipif(
    not _AIDER_AVAILABLE,
    reason="aider-chat optional dependency not installed",
)


@pytest.fixture(autouse=True)
def _no_real_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("API_KEY", "test-key")


class TestLintCmdsUsesJavascriptKey:
    def test_javascript_key_in_run_method(self) -> None:
        run_src = inspect.getsource(AiderJsAgents.run)
        assert '"javascript"' in run_src

    def test_typescript_key_not_in_run_method(self) -> None:
        run_src = inspect.getsource(AiderJsAgents.run)
        assert '"typescript"' not in run_src

    @_requires_aider
    def test_run_method_calls_coder_create_with_javascript_lint_key(self) -> None:
        with (
            patch("aider.models.Model") as MockModel,
            patch("aider.coders.Coder") as MockCoder,
            patch("aider.io.InputOutput"),
            patch.object(AiderJsAgents, "_load_model_settings"),
        ):
            MockModel.return_value = MagicMock(info={"max_input_tokens": 100000})
            mock_coder_instance = MagicMock()
            mock_coder_instance.gpt_prompts = MagicMock()
            mock_coder_instance.gpt_prompts.main_system = ""
            mock_coder_instance.abs_fnames = []
            mock_coder_instance.partial_response_content = ""
            MockCoder.create.return_value = mock_coder_instance

            agent = AiderJsAgents(max_iteration=1, model_name="anthropic/claude")
            agent.run(
                message="m",
                test_cmd="",
                lint_cmd="eslint .",
                fnames=[],
                log_dir=Path(os.environ.get("PYTEST_TMP", "/tmp")) / "jslog",
            )

        kwargs = MockCoder.create.call_args.kwargs
        assert kwargs["lint_cmds"] == {"javascript": "eslint ."}


class TestSourceForbiddenSurface:
    def test_no_language_kwarg_in_call(self) -> None:
        tree = ast.parse(AGENTS_JS_SOURCE)
        offenders: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "language":
                        offenders.append((node.lineno, ast.dump(node.func)))
        assert offenders == [], (
            "agents_js.py must not invent `language=` kwargs (C3 binding); "
            f"found at: {offenders}"
        )

    def test_no_executable_setup_specification_reference(self) -> None:
        tree = ast.parse(AGENTS_JS_SOURCE)
        violations: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "specification":
                if isinstance(node.value, ast.Attribute) and node.value.attr == "setup":
                    violations.append((node.lineno, ast.unparse(node)))
            if isinstance(node, ast.Subscript):
                if (
                    isinstance(node.slice, ast.Constant)
                    and node.slice.value == "specification"
                ):
                    parent_text = ast.unparse(node)
                    if "setup" in parent_text:
                        violations.append((node.lineno, parent_text))
        assert violations == [], (
            "agents_js.py must not reference setup.specification "
            f"(C3 Phase D binding); found at: {violations}"
        )

    def test_no_pdf_or_fitz_import(self) -> None:
        tree = ast.parse(AGENTS_JS_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = getattr(node, "names", [])
                for n in names:
                    assert n.name not in {"fitz", "pymupdf"}, (
                        f"agents_js must not import PDF deps (line {node.lineno})"
                    )


class TestJsAgentReturn:
    def test_is_abc(self) -> None:
        from abc import ABC as _ABC

        assert issubclass(JsAgentReturn, _ABC)

    def test_aider_js_return_subclass(self) -> None:
        assert issubclass(AiderJsReturn, JsAgentReturn)

    def test_aider_js_return_parses_cost(self, tmp_path: Path) -> None:
        log_file = tmp_path / "aider.log"
        log_file.write_text("...cost $0.12\nanother $0.34 line", encoding="utf-8")
        ret = AiderJsReturn(str(log_file))
        assert ret.last_cost == pytest.approx(0.34)

    def test_aider_js_return_no_log_returns_zero_cost(self) -> None:
        ret = AiderJsReturn(None)
        assert ret.last_cost == 0.0

    def test_aider_js_return_missing_file_returns_zero(self, tmp_path: Path) -> None:
        ret = AiderJsReturn(str(tmp_path / "nope.log"))
        assert ret.last_cost == 0.0


class TestJsAgentsAbstract:
    def test_js_agents_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            JsAgents(max_iteration=1)


class TestSystemPromptLoader:
    def test_prompt_returns_string(self) -> None:
        text = _load_js_system_prompt()
        assert isinstance(text, str)

    def test_prompt_path_under_agent_prompts_dir(self) -> None:
        prompt_path = AGENTS_JS_PATH.parent / "prompts" / "js_system_prompt.md"
        assert prompt_path.exists(), "js_system_prompt.md must ship with the package"


class TestHandleLogging:
    def test_handle_logging_attaches_file_handler(self, tmp_path: Path) -> None:
        log_file = tmp_path / "x.log"
        handle_logging("test_js_logger", log_file)
        import logging

        log = logging.getLogger("test_js_logger")
        assert any(getattr(h, "baseFilename", "") == str(log_file) for h in log.handlers)


class TestAiderJsAgentsInit:
    @_requires_aider
    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with (
            patch("aider.models.Model") as MockModel,
            patch.object(AiderJsAgents, "_load_model_settings"),
        ):
            MockModel.return_value = MagicMock(info={})
            with pytest.raises(ValueError, match="API Key Error"):
                AiderJsAgents(max_iteration=1, model_name="anthropic/claude")

    @_requires_aider
    def test_constructor_assigns_attrs(self) -> None:
        with (
            patch("aider.models.Model") as MockModel,
            patch.object(AiderJsAgents, "_load_model_settings"),
        ):
            MockModel.return_value = MagicMock(info={})
            agent = AiderJsAgents(
                max_iteration=5, model_name="anthropic/claude-x", cache_prompts=False
            )
        assert agent.max_iteration == 5
        assert agent.model_name == "anthropic/claude-x"
        assert agent.cache_prompts is False


class TestRunMethodAddendum:
    def test_addendum_forbids_typescript_syntax(self) -> None:
        run_src = inspect.getsource(AiderJsAgents.run)
        assert "Do NOT add TypeScript syntax" in run_src

    def test_addendum_mentions_stub_marker(self) -> None:
        run_src = inspect.getsource(AiderJsAgents.run)
        assert "__COMMIT0_STUB__" in run_src

    def test_addendum_forbids_esm_cjs_conversion(self) -> None:
        run_src = inspect.getsource(AiderJsAgents.run)
        assert "Do NOT convert between ESM and CJS" in run_src


class TestPipelineGitInvariant:
    def test_system_prompt_has_no_git_token(self) -> None:
        prompt_path = AGENTS_JS_PATH.parent / "prompts" / "js_system_prompt.md"
        assert prompt_path.exists(), f"missing {prompt_path}"
        text = prompt_path.read_text(encoding="utf-8")
        assert re.search(r"\bgit\b", text, flags=re.IGNORECASE) is None, (
            "js_system_prompt.md must not mention git "
            "(harness owns VCS; agent must not be coached to run git)"
        )

    def test_run_pipeline_js_has_no_git_command(self) -> None:
        repo_root = AGENTS_JS_PATH.parents[1]
        pipeline = repo_root / "run_pipeline_js.sh"
        assert pipeline.exists(), f"missing {pipeline}"
        offenders = [
            (lineno, line)
            for lineno, line in enumerate(
                pipeline.read_text(encoding="utf-8").splitlines(), start=1
            )
            if re.match(r"^\s*git\s", line)
        ]
        assert offenders == [], (
            "run_pipeline_js.sh must not execute git directly "
            f"(B2 invariant); found at: {offenders[:5]}"
        )


class _FakeCoder:
    def __init__(self) -> None:
        self.partial_response_content = ""
        self.message_tokens_sent = 0
        self.message_tokens_received = 0
        self.message_cost = 0.0
        self.reflected_message = None

    def show_send_output(self, completion: object) -> None: ...
    def show_send_output_stream(self, completion: object) -> object:
        return completion
    def send_message(self, message: object, *args: object, **kwargs: object) -> None:
        ...
    def add_assistant_reply_to_cur_messages(self) -> None: ...
    def show_usage_report(self) -> None: ...
    def apply_updates(self) -> set:
        return set()

    def clone(self) -> "_FakeCoder":
        sibling = _FakeCoder()
        for attr in (
            "_thinking_capture",
            "_current_stage",
            "_current_module",
            "_turn_counter_ref",
            "_last_reasoning_content",
            "_last_completion_usage",
        ):
            if hasattr(self, attr):
                setattr(sibling, attr, getattr(self, attr))
        return sibling


class TestPatchedCloneSharesTurnCounter:
    def _patched_coder(self) -> _FakeCoder:
        from agent.agents_js import _apply_thinking_capture_patches
        from agent.thinking_capture import ThinkingCapture

        coder = _FakeCoder()
        tc = ThinkingCapture()
        _apply_thinking_capture_patches(
            coder, tc, current_stage="draft", current_module="mod"
        )
        return coder

    def test_clone_shares_counter_ref_object(self) -> None:
        coder = self._patched_coder()
        clone = coder.clone()
        assert coder._turn_counter_ref is clone._turn_counter_ref

    def test_parent_increment_visible_to_clone(self) -> None:
        coder = self._patched_coder()
        coder.send_message("hello")
        assert coder._turn_counter_ref[0] == 1
        clone = coder.clone()
        coder.send_message("again")
        assert clone._turn_counter_ref[0] == 2

    def test_two_clones_share_one_counter(self) -> None:
        coder = self._patched_coder()
        a = coder.clone()
        b = coder.clone()
        a.send_message("x")
        b.send_message("y")
        assert a._turn_counter_ref[0] == 2
        assert b._turn_counter_ref[0] == 2
        assert coder._turn_counter_ref[0] == 2

    def test_no_by_value_snapshot_assignment_in_clone(self) -> None:
        clone_src = ""
        tree = ast.parse(AGENTS_JS_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "patched_clone":
                clone_src = ast.unparse(node)
        assert clone_src, "patched_clone closure not found"
        assert "_turn_counter_ref" in clone_src
        assert "_turn_counter = coder._turn_counter" not in clone_src
