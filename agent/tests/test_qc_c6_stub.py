"""QC cluster C6_stub regression tests.

Pins the fixes for:
  * QC-C6-002 — six core stub-implementation invariants present in EVERY
    language system prompt (cpp/rust are canonical; go/js/ts/java were missing
    some). Enforced as a cross-language parity assertion.
  * QC-C6-003 — the TS system prompt lives at agent/prompts/ (parity with the
    six siblings), not the orphan agent/ts_system_prompt.md, and the loader
    resolves the canonical path.
  * QC-C6-004 — JS agent stub-detection accepts EITHER the comment marker OR
    the throw signal (not requiring both), keyed on shared constants.
  * QC-C6-008 — Java stubber strips Javadoc from stubs by default
    (preserveJavadoc default -> false) to avoid answer leakage.

All checks are source-text / import assertions — no Docker, no network.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import agent.agent_utils_js as ajs
from commit0.harness.constants_js import JS_STUB_MARKER, JS_STUB_THROW

_AGENT_DIR = Path(__file__).resolve().parents[1]
_PROMPTS_DIR = _AGENT_DIR / "prompts"

# Every language whose agent is driven by a system prompt. cpp/rust are the
# canonical set that already carried all six invariants; the rest were brought
# up to parity by QC-C6-002.
_ALL_PROMPT_LANGS = ["c", "cpp", "rust", "go", "js", "ts", "java"]

# Languages that must carry the full six-invariant block. C's prompt is a
# thin marker-only prompt (its stubber contract differs); it is excluded from
# the full-invariant parity set but still must exist (C6-003 sibling check).
_INVARIANT_LANGS = ["cpp", "rust", "go", "js", "ts", "java"]


def _prompt_text(lang: str) -> str:
    return (_PROMPTS_DIR / f"{lang}_system_prompt.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# QC-C6-003: TS prompt location + loader parity
# ---------------------------------------------------------------------------


def test_ts_prompt_lives_in_prompts_dir_not_orphan() -> None:
    assert (_PROMPTS_DIR / "ts_system_prompt.md").exists(), (
        "TS system prompt must live under agent/prompts/ for sibling parity"
    )
    assert not (_AGENT_DIR / "ts_system_prompt.md").exists(), (
        "orphan agent/ts_system_prompt.md must be gone after the move"
    )


@pytest.mark.parametrize("lang", _ALL_PROMPT_LANGS)
def test_every_language_prompt_resolves_under_prompts(lang: str) -> None:
    assert (_PROMPTS_DIR / f"{lang}_system_prompt.md").exists(), (
        f"{lang} system prompt must resolve at agent/prompts/{lang}_system_prompt.md"
    )


def test_ts_loader_points_at_canonical_path_with_legacy_fallback() -> None:
    # Inspect source text (avoids importing agents_ts, which pulls aider).
    src = (_AGENT_DIR / "agents_ts.py").read_text(encoding="utf-8")
    assert '"prompts" / "ts_system_prompt.md"' in src, (
        "agents_ts loader must resolve the canonical agent/prompts/ location"
    )
    assert "_TS_SYSTEM_PROMPT_LEGACY_PATH" in src, (
        "agents_ts loader must retain a legacy-path fallback"
    )


# ---------------------------------------------------------------------------
# QC-C6-002: six core invariants present across every language prompt
# ---------------------------------------------------------------------------

# Each invariant -> a set of accepted substrings (lowercased). A prompt passes
# the invariant if ANY synonym is present, allowing documented per-language
# adaptations (e.g. JS has no visibility modifiers -> "export surface").
_INVARIANTS: dict[str, tuple[str, ...]] = {
    "match_style": ("code style", "existing style", "existing code"),
    "preserve_signature": ("signature",),
    "visibility": (
        "visibility",
        "access modifier",
        "export surface",
        "export names",
        "export pattern",
    ),
    "no_new_deps": ("dependenc",),
    "no_new_files": (
        "new file",
        "new source file",
        "new module",
        "new class",
        "new package",
    ),
}


@pytest.mark.parametrize("lang", _INVARIANT_LANGS)
@pytest.mark.parametrize("invariant", sorted(_INVARIANTS))
def test_prompt_carries_invariant(lang: str, invariant: str) -> None:
    text = _prompt_text(lang).lower()
    synonyms = _INVARIANTS[invariant]
    assert any(s in text for s in synonyms), (
        f"{lang}_system_prompt.md is missing the '{invariant}' core invariant "
        f"(expected one of {synonyms})"
    )


@pytest.mark.parametrize("lang", _INVARIANT_LANGS)
def test_prompt_forbids_leftover_stub_in_final(lang: str) -> None:
    text = _prompt_text(lang).lower()
    assert "final" in text, f"{lang} prompt must talk about the 'final' code"
    assert any(
        k in text for k in ("todo", "fixme", "stub", "panic", "placeholder")
    ), f"{lang} prompt must forbid leftover stub/placeholder markers in final code"


# ---------------------------------------------------------------------------
# QC-C6-004: JS stub-detection accepts EITHER signal
# ---------------------------------------------------------------------------


def test_js_stub_constants_distinct() -> None:
    assert JS_STUB_MARKER == "// __COMMIT0_STUB__"
    assert JS_STUB_THROW == 'throw new Error("STUB")'


@pytest.mark.parametrize(
    "content,expected",
    [
        (JS_STUB_MARKER, True),          # comment only
        (JS_STUB_THROW, True),           # throw only
        (f"{JS_STUB_MARKER}\n{JS_STUB_THROW}", True),  # both
        ("return 1;", False),            # neither
        ("// nothing here", False),
    ],
)
def test_js_content_has_stub_accepts_either(content: str, expected: bool) -> None:
    assert ajs.js_content_has_stub(content) is expected


def _write_js(src: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".js")
    with open(fd, "w", encoding="utf-8") as f:
        f.write(src)
    return path


def test_has_js_stubs_detects_throw_only_file() -> None:
    # A stub carrying only the throw (comment stripped) must still be detected,
    # otherwise the file is silently dropped from the target-edit set.
    path = _write_js("function f() {\n  throw new Error(\"STUB\");\n}\n")
    try:
        assert ajs.has_js_stubs(path) is True
    finally:
        Path(path).unlink()


def test_extract_js_stubs_dedupes_comment_and_throw() -> None:
    # foo has BOTH signals on adjacent lines -> exactly one signature entry.
    # bar has only the throw -> one entry. Total = 2, not 3.
    src = (
        "export function foo() {\n"
        f"  {JS_STUB_MARKER}\n"
        '  throw new Error("STUB");\n'
        "}\n"
        "function bar() {\n"
        '  throw new Error("STUB");\n'
        "}\n"
    )
    path = _write_js(src)
    try:
        stubs = ajs.extract_js_stubs(path)
    finally:
        Path(path).unlink()
    assert len(stubs) == 2, f"expected one entry per stub body, got {stubs}"
    assert any("foo" in s for s in stubs)
    assert any("bar" in s for s in stubs)


# ---------------------------------------------------------------------------
# QC-C6-008: Java stubber strips Javadoc by default
# ---------------------------------------------------------------------------


def test_java_preserve_javadoc_default_is_false() -> None:
    repo_root = _AGENT_DIR.parent
    stub_config = (
        repo_root
        / "tools"
        / "javastubber"
        / "src"
        / "main"
        / "java"
        / "com"
        / "commit0"
        / "stubber"
        / "StubConfig.java"
    )
    text = stub_config.read_text(encoding="utf-8")
    assert "public boolean preserveJavadoc = false;" in text, (
        "StubConfig.preserveJavadoc must default to false so Javadoc is stripped "
        "from stubs (answer-leak defense)"
    )
    assert "public boolean preserveJavadoc = true;" not in text

    # The Python wrapper writes preserveJavadoc into the config JSON and thus
    # OVERRIDES the Java default. Its default must ALSO be False or the Java flip
    # is inert in production (the two silently drifted before QC-C6-008).
    import inspect

    from tools.stub_java import stub_java_sources

    py_default = inspect.signature(stub_java_sources).parameters[
        "preserve_javadoc"
    ].default
    assert py_default is False, (
        "tools/stub_java.py stub_java_sources(preserve_javadoc=...) default must "
        "be False to match StubConfig.java; otherwise the Java default flip does "
        "nothing in production (the caller passes no explicit value)."
    )
