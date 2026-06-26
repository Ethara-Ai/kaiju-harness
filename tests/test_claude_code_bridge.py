"""Unit + opt-in integration tests for the Claude Code OAuth bridge.

Run unit tests::

    .venv/bin/python -m pytest tests/test_claude_code_bridge.py -v

Run the live integration test against api.anthropic.com (requires a real
Claude Code subscription)::

    KAIJU_CC_INTEGRATION=1 .venv/bin/python -m pytest \
        tests/test_claude_code_bridge.py::test_real_anthropic_via_bridge -v
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpx
import pytest

from agent.claude_code.bridge import (
    SYSTEM_PREFIX,
    _build_forward_headers,
    _is_streaming_payload,
    build_app,
    inject_system_prefix,
)
from agent.claude_code.credentials import (
    CredentialProvider,
    CredentialsError,
    OAuthCredentials,
    load_credentials,
)


# ---------------------------------------------------------------------------
# OAuthCredentials
# ---------------------------------------------------------------------------


def test_credentials_from_claude_payload():
    payload = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-X",
            "refreshToken": "sk-ant-ort01-Y",
            "expiresAt": 1782402066667,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }
    c = OAuthCredentials.from_claude_payload(payload)
    assert c.access_token == "sk-ant-oat01-X"
    assert c.refresh_token == "sk-ant-ort01-Y"
    assert c.expires_at_ms == 1782402066667
    assert c.scopes == ["user:inference"]
    assert c.subscription_type == "max"


def test_credentials_from_unwrapped_payload():
    # Some sources may store the inner dict directly.
    payload = {
        "accessToken": "tok",
        "refreshToken": "ref",
        "expiresAt": 1782402066667,
        "scopes": [],
    }
    c = OAuthCredentials.from_claude_payload(payload)
    assert c.access_token == "tok"


def test_credentials_malformed():
    with pytest.raises(CredentialsError):
        OAuthCredentials.from_claude_payload({"claudeAiOauth": {}})


def test_is_expired_far_future():
    c = OAuthCredentials("a", "b", int((time.time() + 3600) * 1000), [])
    assert not c.is_expired()


def test_is_expired_past():
    c = OAuthCredentials("a", "b", int((time.time() - 10) * 1000), [])
    assert c.is_expired()


def test_is_expired_within_leeway():
    # 30s in the future, but leeway is 60s -> should report expired.
    c = OAuthCredentials("a", "b", int((time.time() + 30) * 1000), [])
    assert c.is_expired()


# ---------------------------------------------------------------------------
# load_credentials priority
# ---------------------------------------------------------------------------


def _valid_creds_json(expires_offset_s: int = 3600) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "tok",
                "refreshToken": "ref",
                "expiresAt": int((time.time() + expires_offset_s) * 1000),
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
    )


def test_load_inline_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS", _valid_creds_json())
    monkeypatch.setenv("KAIJU_CC_CREDS_PATH", str(tmp_path / "missing.json"))
    c = load_credentials()
    assert c.access_token == "tok"


def test_load_file_path(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_CREDENTIALS", raising=False)
    p = tmp_path / "creds.json"
    p.write_text(_valid_creds_json())
    monkeypatch.setenv("KAIJU_CC_CREDS_PATH", str(p))
    # Disable lower-priority sources so we know file path was used.
    monkeypatch.setattr(
        "agent.claude_code.credentials._read_keychain_macos", lambda: None
    )
    monkeypatch.setattr(
        "agent.claude_code.credentials._read_cache_file", lambda: None
    )
    c = load_credentials()
    assert c.access_token == "tok"


def test_load_no_sources(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_CREDENTIALS", raising=False)
    monkeypatch.setenv("KAIJU_CC_CREDS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(
        "agent.claude_code.credentials._read_keychain_macos", lambda: None
    )
    monkeypatch.setattr(
        "agent.claude_code.credentials._read_cache_file", lambda: None
    )
    # also stub the linux fallback path away
    monkeypatch.setattr(
        "agent.claude_code.credentials._read_credentials_file", lambda: None
    )
    with pytest.raises(CredentialsError):
        load_credentials()


# ---------------------------------------------------------------------------
# System-prefix injection
# ---------------------------------------------------------------------------


def test_inject_absent():
    out = inject_system_prefix({"messages": []})
    assert out["system"] == [{"type": "text", "text": SYSTEM_PREFIX}]


def test_inject_string():
    out = inject_system_prefix({"system": "Be helpful.", "messages": []})
    assert out["system"].startswith(SYSTEM_PREFIX)
    assert "Be helpful." in out["system"]


def test_inject_list():
    out = inject_system_prefix(
        {"system": [{"type": "text", "text": "Be helpful."}], "messages": []}
    )
    assert out["system"][0] == {"type": "text", "text": SYSTEM_PREFIX}
    assert out["system"][1]["text"] == "Be helpful."


def test_inject_idempotent_string():
    body = {"system": SYSTEM_PREFIX + "\n\nExtra.", "messages": []}
    out = inject_system_prefix(body)
    assert out["system"].count(SYSTEM_PREFIX) == 1


def test_inject_idempotent_list():
    body = {
        "system": [
            {"type": "text", "text": SYSTEM_PREFIX},
            {"type": "text", "text": "Extra."},
        ],
        "messages": [],
    }
    before = json.dumps(body)
    out = inject_system_prefix(body)
    # No second prefix block was inserted.
    assert sum(
        1 for b in out["system"] if SYSTEM_PREFIX in b.get("text", "")
    ) == 1
    assert json.dumps(out) == before

def test_inject_preserves_cache_control_on_existing_block():
    """Prefix injection must NOT strip or mutate cache_control markers.

    Anthropic's prompt-cache hash is computed over the verbatim block
    structure. Two byte-identical bodies hit the same cache; any silent
    mutation of cache_control would break cache hits across calls.
    """
    cached_text = "x" * 8000  # comfortably over per-model minimums
    body = {
        "system": [
            {
                "type": "text",
                "text": cached_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = inject_system_prefix(body)
    # Injected prefix sits at position 0 with no cache_control.
    assert out["system"][0] == {"type": "text", "text": SYSTEM_PREFIX}
    assert "cache_control" not in out["system"][0]
    # Caller's block (now at position 1) keeps its text AND cache_control.
    assert out["system"][1]["text"] == cached_text
    assert out["system"][1]["cache_control"] == {"type": "ephemeral"}
    # Roundtripping through JSON preserves the structure byte-perfectly.
    assert json.loads(json.dumps(out)) == out



# ---------------------------------------------------------------------------
# Header construction
# ---------------------------------------------------------------------------


class _Hdrs(dict):
    """Minimal Starlette-like Headers replacement that returns ``items()``."""


def test_forward_headers_strips_auth_and_injects_bearer():
    hdrs = _Hdrs(
        {
            "x-api-key": "stub",
            "authorization": "Bearer leaked",
            "host": "ignored",
            "content-type": "application/json",
            "user-agent": "litellm",
        }
    )
    out = _build_forward_headers(hdrs, "OAUTH-TOKEN")
    assert out["Authorization"] == "Bearer OAUTH-TOKEN"
    assert "x-api-key" not in {k.lower() for k in out}
    assert "host" not in {k.lower() for k in out}
    assert out.get("content-type") == "application/json"
    assert "oauth-2025-04-20" in out["anthropic-beta"]
    assert out["anthropic-version"] == "2023-06-01"


def test_forward_headers_merges_existing_beta():
    hdrs = _Hdrs({"anthropic-beta": "prompt-caching-2024-07-31"})
    out = _build_forward_headers(hdrs, "TOK")
    betas = [b.strip() for b in out["anthropic-beta"].split(",")]
    assert "oauth-2025-04-20" in betas
    assert "prompt-caching-2024-07-31" in betas


def test_forward_headers_preserves_caller_version():
    hdrs = _Hdrs({"anthropic-version": "2024-10-01"})
    out = _build_forward_headers(hdrs, "TOK")
    assert out["anthropic-version"] == "2024-10-01"


# ---------------------------------------------------------------------------
# Streaming detection
# ---------------------------------------------------------------------------


def test_streaming_detection():
    assert _is_streaming_payload(b'{"stream":true,"model":"x"}') is True
    assert _is_streaming_payload(b'{"stream": true, "model":"x"}') is True
    assert _is_streaming_payload(b'{"stream":false}') is False
    assert _is_streaming_payload(b"") is False


# ---------------------------------------------------------------------------
# End-to-end bridge with mocked upstream
# ---------------------------------------------------------------------------


def test_bridge_proxies_with_oauth_and_prefix(monkeypatch):
    from fastapi.testclient import TestClient

    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["content"] = content

            class _Resp:
                status_code = 200
                headers = {"content-type": "application/json"}
                content = (
                    b'{"id":"msg_x","type":"message","role":"assistant",'
                    b'"content":[{"type":"text","text":"ok"}]}'
                )

            return _Resp()

    monkeypatch.setattr("agent.claude_code.bridge.httpx.AsyncClient", _FakeClient)

    # Stub credential provider so we never touch Keychain/network
    class _StubProvider:
        def get_access_token(self) -> str:
            return "sk-ant-oat01-MOCK"

    app = build_app(_StubProvider())
    with TestClient(app) as client:
        r = client.post(
            "/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"x-api-key": "stub"},
        )
    assert r.status_code == 200, r.text

    h = {k.lower(): v for k, v in captured["headers"].items()}
    assert h.get("authorization") == "Bearer sk-ant-oat01-MOCK"
    assert "x-api-key" not in h
    assert "oauth-2025-04-20" in h.get("anthropic-beta", "")
    assert h.get("anthropic-version") == "2023-06-01"
    assert captured["url"].endswith("/v1/messages")

    forwarded_body = json.loads(captured["content"])
    sys_blocks = forwarded_body.get("system")
    if isinstance(sys_blocks, list):
        assert any(
            SYSTEM_PREFIX in b.get("text", "")
            for b in sys_blocks
            if isinstance(b, dict)
        )
    else:
        assert SYSTEM_PREFIX in sys_blocks


def test_bridge_skip_prefix_env(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KAIJU_CC_SKIP_SYSTEM_PREFIX", "1")
    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            captured["content"] = content

            class _Resp:
                status_code = 200
                headers = {"content-type": "application/json"}
                content = b"{}"

            return _Resp()

    monkeypatch.setattr("agent.claude_code.bridge.httpx.AsyncClient", _FakeClient)

    class _StubProvider:
        def get_access_token(self) -> str:
            return "TOK"

    app = build_app(_StubProvider())
    with TestClient(app) as client:
        client.post(
            "/v1/messages",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    forwarded = json.loads(captured["content"])
    assert "system" not in forwarded


def test_bridge_healthz_returns_token_prefix():
    from fastapi.testclient import TestClient

    class _StubProvider:
        def get_access_token(self) -> str:
            return "sk-ant-oat01-ABCDEFGHIJK"

    app = build_app(_StubProvider())
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["token_prefix"].startswith("sk-ant-oat01-")


def test_bridge_healthz_credentials_error(monkeypatch):
    from fastapi.testclient import TestClient

    class _ErrProvider:
        def get_access_token(self) -> str:
            raise CredentialsError("no creds for you")

    app = build_app(_ErrProvider())
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert "no creds" in r.json()["error"]


# ---------------------------------------------------------------------------
# Integration: real /v1/messages call via the bridge.
# Gated on KAIJU_CC_INTEGRATION=1 because it consumes subscription quota.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("KAIJU_CC_INTEGRATION") != "1",
    reason="set KAIJU_CC_INTEGRATION=1 to hit real api.anthropic.com",
)
def test_real_anthropic_via_bridge():
    import uvicorn

    port = int(os.environ.get("KAIJU_CC_TEST_PORT", "18765"))
    app = build_app()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    # Wait for the server to come up.
    for _ in range(40):
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        server.should_exit = True
        raise AssertionError("bridge failed to start")

    try:
        r = httpx.post(
            f"http://127.0.0.1:{port}/v1/messages",
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 16,
                "messages": [
                    {
                        "role": "user",
                        "content": "Reply with exactly: BRIDGE_OK",
                    }
                ],
            },
            headers={"x-api-key": "stub", "anthropic-version": "2023-06-01"},
            timeout=60.0,
        )
        assert r.status_code == 200, r.text[:500]
        data = r.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", []) if isinstance(b, dict)
        )
        assert "BRIDGE_OK" in text, f"unexpected response: {text!r}"
    finally:
        server.should_exit = True
        t.join(timeout=5)
