"""Guard: every language's agent system prompt must describe the SAME stub
marker its stubber actually emits.

QC-C6-001: the C++ prompt told the model to hunt
``throw std::runtime_error("STUB: not implemented")`` while the C++ stubber
writes ``__builtin_trap() /* STUB: not implemented */`` (and ``return {};`` for
constexpr) — so a model following the prompt literally never recognized a real
stub and the whole C++ trajectory/score was corrupted. This test pins the
prompt<->marker contract for ALL languages so the drift cannot recur in any of
them.
"""

from pathlib import Path

import pytest

_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_AGENT = Path(__file__).resolve().parents[1]


def _find_prompt(name: str) -> Path:
    """Locate a system prompt under agent/prompts/ or agent/ (TS lives in agent/)."""
    for cand in (_PROMPTS / name, _AGENT / name):
        if cand.exists():
            return cand
    raise FileNotFoundError(name)


# (prompt filename, substrings that MUST appear, substrings that must NOT appear)
_CASES = [
    ("c_system_prompt.md", ["STUB_PANIC"], []),
    (
        "cpp_system_prompt.md",
        ["__builtin_trap", "STUB: not implemented", "return {}"],
        ['std::runtime_error("STUB'],
    ),
    ("go_system_prompt.md", ["STUB: not implemented"], []),
    (
        "java_system_prompt.md",
        ["UnsupportedOperationException", "STUB: not implemented"],
        [],
    ),
    ("js_system_prompt.md", ["__COMMIT0_STUB__"], []),
    ("ts_system_prompt.md", ['Error("STUB")'], []),
    ("rust_system_prompt.md", ['panic!("STUB: not implemented")'], []),
]


@pytest.mark.parametrize("name,required,forbidden", _CASES, ids=[c[0] for c in _CASES])
def test_prompt_matches_actual_stub_marker(name, required, forbidden):
    text = _find_prompt(name).read_text(encoding="utf-8")
    for needle in required:
        assert needle in text, (
            f"{name} must reference the real stub marker substring {needle!r} "
            f"that its stubber emits (prompt<->marker drift)"
        )
    for needle in forbidden:
        assert needle not in text, (
            f"{name} references a stale/wrong stub marker {needle!r} that the "
            f"stubber does NOT emit — the model will never recognize real stubs"
        )
