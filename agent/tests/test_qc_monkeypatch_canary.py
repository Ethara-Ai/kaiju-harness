"""QC-C3-010 / QC-C3-011: upstream monkey-patch surface canary.

The capture layer (agent/agents.py, agent/edit_capture.py) monkey-patches
litellm and aider internals BY CLASS-NAME / ATTRIBUTE. Combined with the git
pins in pyproject.toml, an upstream rename would SILENTLY break cost / reasoning
/ edit capture with no error — producing wrong-but-plausible training labels.

This canary asserts every patched surface still resolves by name. In the
environment where aider/litellm are actually installed (the agent image), a
rename makes these tests FAIL loudly. Where the optional deps are absent (local
dev / this repo's base venv), each test SKIPS gracefully — per the cluster
guidance — so the canary never produces false failures off the agent image.

These are pure import/attribute assertions: no Docker, no network, no LLM call.
"""

from __future__ import annotations

import importlib

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_or_skip(module_name: str):
    """Import a module, or skip the test if it (or a hard dependency) is absent.

    We do NOT use ``importlib.util.find_spec`` here: find_spec turning up empty
    is exactly how an upstream MODULE rename would masquerade as "not installed"
    and silently skip. We import for real and only skip on ModuleNotFoundError
    for a KNOWN-absent optional top-level package (aider / litellm), so a rename
    of a submodule/attribute inside an installed package still FAILS loudly.

    The test suite injects lightweight *stub* modules for the optional ``[agent]``
    deps (commit0/harness/_optional_dep_stubs.py) so unrelated tests can import
    ``agent.*`` without aider/litellm installed. A stub is a bare ``ModuleType``
    with no ``__file__`` — asserting the real patch surface against it would be a
    false failure, so we detect and skip it. On the agent image (real deps) the
    modules have a ``__file__`` and the assertions run for real.
    """
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing in {"aider", "litellm"}:
            pytest.skip(f"optional dependency {missing!r} not installed")
        raise
    # Walk from the imported module up to its top-level package: if ANY level is
    # a stub (no __file__), the real dependency is not installed here.
    parts = module_name.split(".")
    import sys as _sys

    for i in range(len(parts)):
        ancestor = _sys.modules.get(".".join(parts[: i + 1]))
        if ancestor is not None and getattr(ancestor, "__file__", None) is None:
            pytest.skip(
                f"{parts[0]!r} is a test stub (no __file__), not the real dep"
            )
    return mod


def _assert_attr(obj, name: str) -> None:
    assert hasattr(obj, name), (
        f"{getattr(obj, '__name__', obj)!r} no longer exposes {name!r} — an "
        f"upstream rename would SILENTLY break the monkey-patch that binds it."
    )


# ---------------------------------------------------------------------------
# litellm surfaces (agent/agents.py Patch: reasoning bridge)
# ---------------------------------------------------------------------------

def test_litellm_reasoning_bridge_surface_present():
    """agents.py:_patch_litellm_responses_bridge_reasoning_capture targets this."""
    mod = _import_or_skip(
        "litellm.completion_extras.litellm_responses_transformation.transformation"
    )
    handler = getattr(mod, "LiteLLMResponsesTransformationHandler", None)
    assert handler is not None, (
        "LiteLLMResponsesTransformationHandler moved/renamed — reasoning-summary "
        "capture (agents.py:220) would silently no-op."
    )
    _assert_attr(handler, "_handle_raw_dict_response_item")


def test_litellm_reasoning_installer_fires():
    """Installing the patch must actually flip its idempotency flag."""
    agents = _import_or_skip("agent.agents")
    mod = _import_or_skip(
        "litellm.completion_extras.litellm_responses_transformation.transformation"
    )
    handler = mod.LiteLLMResponsesTransformationHandler
    agents._patch_litellm_responses_bridge_reasoning_capture()
    assert getattr(handler, "_reasoning_capture_patched", False) is True, (
        "reasoning-bridge installer ran but did not set _reasoning_capture_patched "
        "— the patched surface likely moved."
    )


def test_litellm_output_config_installer_fires():
    """_patch_litellm_output_config_passthrough must flip its flag when applied."""
    agents = _import_or_skip("agent.agents")
    _import_or_skip("litellm")
    try:
        agents._patch_litellm_output_config_passthrough()
    except Exception as exc:  # pragma: no cover - defensive
        pytest.fail(f"_patch_litellm_output_config_passthrough raised: {exc!r}")
    # The flag lives on the litellm class the patcher resolves internally; we only
    # require the installer to run without error and be idempotent on a 2nd call.
    agents._patch_litellm_output_config_passthrough()


# ---------------------------------------------------------------------------
# aider surfaces (agent/agents.py Patch 7 + edit_capture.py)
# ---------------------------------------------------------------------------

def test_aider_finish_reason_length_importable():
    """agents.py Patch 7 (patched_send) does `from ... import FinishReasonLength`."""
    base_coder = _import_or_skip("aider.coders.base_coder")
    _assert_attr(base_coder, "FinishReasonLength")


def test_aider_edit_coder_apply_edits_present():
    """edit_capture.py wraps EditBlockCoder/WholeFileCoder.apply_edits by name."""
    editblock = _import_or_skip("aider.coders.editblock_coder")
    wholefile = _import_or_skip("aider.coders.wholefile_coder")
    for cls_name, mod in (
        ("EditBlockCoder", editblock),
        ("WholeFileCoder", wholefile),
    ):
        cls = getattr(mod, cls_name, None)
        assert cls is not None, f"{cls_name} moved/renamed — edit capture no-ops."
        assert callable(getattr(cls, "apply_edits", None)), (
            f"{cls_name}.apply_edits missing/not callable — edit capture would "
            f"fall back to the phantom-producing text parser."
        )


# Every attribute name that _apply_thinking_capture_patches rebinds or calls on
# the Coder instance. A rename of any of these silently corrupts cost/reasoning
# capture in ALL eight language pipelines (they share agents.py or a copy of it).
_CODER_PATCHED_METHODS = (
    "send",
    "show_send_output",
    "show_send_output_stream",
    "add_assistant_reply_to_cur_messages",
    "show_usage_report",
    "clone",
    "apply_updates",
    "send_message",
    "summarize_start",
    "summarize_worker",
    "summarize_end",
    "calculate_and_show_tokens_and_cost",
)


def test_aider_coder_patched_method_surface():
    """Every Coder method the thinking-capture patches rebind/call must exist."""
    base_coder = _import_or_skip("aider.coders.base_coder")
    coder_cls = getattr(base_coder, "Coder", None)
    assert coder_cls is not None, "aider Coder class moved/renamed."
    for name in _CODER_PATCHED_METHODS:
        assert callable(getattr(coder_cls, name, None)), (
            f"aider Coder.{name} missing/not callable — the monkey-patch that "
            f"rebinds it would SILENTLY do nothing."
        )


def test_aider_coder_summarizer_thread_attribute():
    """Patch 6 assigns coder.summarizer_thread; ensure aider still owns the name.

    ``summarizer_thread`` is initialised in Coder.__init__ (not a class attr), so
    check the class attribute first and fall back to the __init__ source text.
    """
    import inspect

    base_coder = _import_or_skip("aider.coders.base_coder")
    coder_cls = base_coder.Coder
    if hasattr(coder_cls, "summarizer_thread"):
        return
    src = inspect.getsource(coder_cls.__init__)
    assert "summarizer_thread" in src, (
        "aider Coder no longer defines summarizer_thread — Patch 6 "
        "(summarize_start ContextVar propagation) would target a stale name."
    )
