"""Guard against the transient-error FALSE POSITIVE that burned 900s/module.

Root cause (QC): the swallowed-transient backstop scanned llm_history.txt (the
PROMPT) and the model's chat response for phrases like "timed out"/"timeout".
Any module whose CODE deals with timeouts (HTTP clients, middleware, retries,
contexts) put those words in the prompt/response, so raise_if_transient_llm_error
mis-fired -> a 900s in-line retry loop x3 with ZERO progress. The real symptom:
llm_history.txt had 2310 "timeout" matches while aider.log/chat had 0.

Fix (caller-level, so genuine-error forms in aider.log still detect normally):
  1. no runner scans llm_history.txt (it is the prompt);
  2. the chat history is filtered to aider's OWN `> ` output lines before scanning
     (the model's SEARCH/REPLACE code — which may contain "timeout" — is dropped),
     via transient_scan_lines_from_chat_history.
"""
from pathlib import Path

from agent.agents import (
    raise_if_transient_llm_error,
    transient_scan_lines_from_chat_history,
    TransientLLMError,
)

AGENT = Path(__file__).resolve().parents[1]
_AGENTS = [
    "agents.py", "agents_c.py", "agents_cpp.py", "agents_go.py",
    "agents_ts.py", "agents_rust.py", "agents_js.py", "agents_java.py",
]


def _fires(text: str) -> bool:
    try:
        raise_if_transient_llm_error(text, context="test")
        return False
    except TransientLLMError:
        return True


def test_chat_history_filter_drops_model_code_keeps_aider_errors():
    chat = (
        '#### fix the middleware so the request is timed out at 3s\n'
        'I will use context.WithTimeout so the call is timed out.\n'  # model prose
        '```go\n'
        '\tt.Fatal("timeout waiting for finalizer")\n'                # model code
        '```\n'
        '> litellm.APITimeoutError: APITimeoutError - Request timed out.\n'  # aider error
    )
    filtered = transient_scan_lines_from_chat_history(chat)
    # only aider's own `> ` line survives
    assert filtered.strip() == (
        "> litellm.APITimeoutError: APITimeoutError - Request timed out."
    )
    # a genuine swallowed transient in the filtered text STILL fires
    assert _fires(filtered)
    # ...but the same chat WITHOUT the aider error line does NOT fire once filtered
    chat_no_error = "\n".join(chat.splitlines()[:-1])
    assert not _fires(transient_scan_lines_from_chat_history(chat_no_error))


def test_genuine_transient_forms_still_fire_directly():
    # forms that appear in aider.log (scanned in full) must still detect
    assert _fires("> MidStreamFallbackError: peer closed connection without "
                  "sending complete message body (incomplete chunked read)")
    assert _fires("openai.APIConnectionError: Connection reset by peer")


def test_no_runner_scans_llm_history_in_transient_loop():
    for f in _AGENTS:
        src = (AGENT / f).read_text(encoding="utf-8")
        assert 'chat_history_file, log_dir / "llm_history.txt")' not in src, (
            f"agent/{f}: transient scan still includes llm_history.txt (the prompt)"
        )


def test_every_runner_filters_chat_history():
    for f in _AGENTS:
        src = (AGENT / f).read_text(encoding="utf-8")
        assert "transient_scan_lines_from_chat_history(" in src, (
            f"agent/{f}: chat history must be filtered to aider's `> ` lines "
            f"before the transient scan (else model timeout-code false-fires)"
        )
