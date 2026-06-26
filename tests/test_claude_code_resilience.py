"""Tests for the bridge resilience layer: error classification, multi-account
failover, and bridge retry/failover behavior under simulated upstream failures.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from agent.claude_code.credentials import (
    CredentialProvider,
    CredentialsError,
    MultiAccountCredentialProvider,
    OAuthCredentials,
    _AccountSlot,
    load_account_pool,
)
from agent.claude_code.errors import (
    ErrorKind,
    classify_anthropic_error,
    extract_retry_after,
)


# ============================================================================
# Error classification
# ============================================================================


def _err_body(kind: str, msg: str = "boom") -> bytes:
    return json.dumps(
        {"type": "error", "error": {"type": kind, "message": msg}, "request_id": "req_x"}
    ).encode()


def test_classify_429_transient_throttle():
    c = classify_anthropic_error(
        429,
        _err_body("rate_limit_error"),
        {"Retry-After": "3", "anthropic-ratelimit-tokens-remaining": "500"},
    )
    assert c.kind is ErrorKind.TRANSIENT_THROTTLE
    assert c.retry_after_seconds == 3
    assert c.kind.is_retryable
    assert not c.kind.is_account_problem
    assert c.request_id == "req_x"


def test_classify_429_subscription_cap_long_retry_after():
    c = classify_anthropic_error(
        429,
        _err_body("rate_limit_error"),
        {"Retry-After": "18000"},  # 5h
    )
    assert c.kind is ErrorKind.SUBSCRIPTION_CAP
    assert c.retry_after_seconds == 18000
    assert c.kind.is_account_problem
    assert not c.kind.is_retryable


def test_classify_429_subscription_cap_zero_tokens():
    c = classify_anthropic_error(
        429,
        _err_body("rate_limit_error"),
        {"Retry-After": "5", "anthropic-ratelimit-tokens-remaining": "0"},
    )
    # Zero remaining tokens wins over short retry-after.
    assert c.kind is ErrorKind.SUBSCRIPTION_CAP


def test_classify_401_token_invalid():
    c = classify_anthropic_error(401, _err_body("authentication_error"), {})
    assert c.kind is ErrorKind.OAUTH_TOKEN_INVALID
    assert c.kind.is_account_problem


def test_classify_403_account_restricted():
    c = classify_anthropic_error(403, _err_body("permission_error"), {})
    assert c.kind is ErrorKind.ACCOUNT_RESTRICTED


def test_classify_402_billing():
    c = classify_anthropic_error(402, _err_body("billing_error"), {})
    assert c.kind is ErrorKind.BILLING_ERROR


def test_classify_529_overloaded():
    c = classify_anthropic_error(529, _err_body("overloaded_error"), {"Retry-After": "10"})
    assert c.kind is ErrorKind.OVERLOADED
    assert c.kind.is_retryable
    assert c.retry_after_seconds == 10


def test_classify_500_upstream_5xx():
    c = classify_anthropic_error(500, _err_body("api_error"), {})
    assert c.kind is ErrorKind.UPSTREAM_5XX
    assert c.kind.is_retryable


def test_classify_400_invalid_request():
    c = classify_anthropic_error(400, _err_body("invalid_request_error"), {})
    assert c.kind is ErrorKind.INVALID_REQUEST
    assert not c.kind.is_retryable


def test_classify_unknown_status():
    c = classify_anthropic_error(418, b"i'm a teapot", {})
    assert c.kind is ErrorKind.UNKNOWN


def test_extract_retry_after_prefers_header():
    assert extract_retry_after({"Retry-After": "42"}) == 42


def test_extract_retry_after_falls_back_to_ratelimit_reset():
    future = time.time() + 7
    assert extract_retry_after(
        {"anthropic-ratelimit-tokens-reset": str(future)}
    ) in (6, 7)  # account for sub-second skew


def test_extract_retry_after_returns_none_when_no_signal():
    assert extract_retry_after({}) is None


def test_classify_handles_non_json_body():
    c = classify_anthropic_error(500, b"<html>internal server error</html>", {})
    assert c.kind is ErrorKind.UPSTREAM_5XX
    # Should not crash; message is best-effort.
    assert c.message


# ============================================================================
# MultiAccountCredentialProvider state machine
# ============================================================================


def _make_creds(token: str, expires_in_s: float = 3600) -> OAuthCredentials:
    return OAuthCredentials(
        access_token=token,
        refresh_token="rt_" + token,
        expires_at_ms=int((time.time() + expires_in_s) * 1000),
        scopes=["user:inference"],
        subscription_type="max",
    )


class _StubProvider(CredentialProvider):
    """In-memory provider that hands out a fixed token; no Keychain/network."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._creds = _make_creds(token)
        self._fail = False

    def get_access_token(self) -> str:
        if self._fail:
            raise CredentialsError("stubbed failure")
        assert self._creds is not None
        return self._creds.access_token

    def token_prefix(self) -> str | None:
        return self._creds.access_token[:20] if self._creds else None


def _pool(*tokens: str) -> MultiAccountCredentialProvider:
    slots = [_AccountSlot(provider=_StubProvider(t), label=f"acc{i}") for i, t in enumerate(tokens)]
    return MultiAccountCredentialProvider(slots)


def test_pool_picks_first_available():
    p = _pool("aaa", "bbb")
    assert p.get_access_token() == "aaa"


def test_pool_skips_exhausted_account():
    p = _pool("aaa", "bbb")
    p.mark_account_exhausted("aaa", time.time() + 3600)
    assert p.get_access_token() == "bbb"


def test_pool_skips_invalid_account():
    p = _pool("aaa", "bbb")
    p.mark_account_invalid("aaa")
    assert p.get_access_token() == "bbb"


def test_pool_all_exhausted_raises():
    p = _pool("aaa", "bbb")
    future = time.time() + 600
    p.mark_account_exhausted("aaa", future)
    p.mark_account_exhausted("bbb", future)
    with pytest.raises(CredentialsError) as ei:
        p.get_access_token()
    assert "exhausted" in str(ei.value)


def test_pool_exhaustion_clears_after_reset_time():
    p = _pool("aaa")
    past = time.time() - 5
    p.mark_account_exhausted("aaa", past)
    # Reset already passed -> account available again.
    assert p.get_access_token() == "aaa"


def test_pool_next_reset_at_none_when_account_available():
    p = _pool("aaa", "bbb")
    p.mark_account_exhausted("aaa", time.time() + 3600)
    assert p.next_reset_at() is None  # bbb still available


def test_pool_next_reset_at_returns_soonest():
    p = _pool("aaa", "bbb")
    soon = time.time() + 60
    later = time.time() + 3600
    p.mark_account_exhausted("aaa", later)
    p.mark_account_exhausted("bbb", soon)
    assert abs(p.next_reset_at() - soon) < 1


def test_pool_snapshot_shape():
    p = _pool("aaa", "bbb")
    snap = p.snapshot()
    assert len(snap) == 2
    assert {s["label"] for s in snap} == {"acc0", "acc1"}
    assert all("token_prefix" in s for s in snap)
    assert all("available" in s for s in snap)


def test_pool_force_reload_clears_state():
    p = _pool("aaa")
    p.mark_account_exhausted("aaa", time.time() + 3600)
    assert p.snapshot()[0]["available"] is False
    p.force_reload()
    assert p.snapshot()[0]["available"] is True


def test_pool_requires_at_least_one_slot():
    with pytest.raises(CredentialsError):
        MultiAccountCredentialProvider([])


# ============================================================================
# Pool loader from env-style spec
# ============================================================================


def test_load_account_pool_empty_returns_none():
    assert load_account_pool("") is None
    assert load_account_pool("   ") is None


def test_load_account_pool_with_file_paths(tmp_path):
    cred = _make_creds("file_token")
    f = tmp_path / "creds.json"
    f.write_text(json.dumps(cred.to_claude_payload()))
    pool = load_account_pool(str(f))
    assert pool is not None
    snap = pool.snapshot()
    assert len(snap) == 1
    assert snap[0]["label"].startswith("file:")
    assert pool.get_access_token() == "file_token"


def test_load_account_pool_mixed_file_and_default(tmp_path, monkeypatch):
    cred = _make_creds("file_token")
    f = tmp_path / "creds.json"
    f.write_text(json.dumps(cred.to_claude_payload()))
    # Default slot reads from CLAUDE_CODE_CREDENTIALS inline env.
    monkeypatch.setenv("CLAUDE_CODE_CREDENTIALS", json.dumps(
        _make_creds("default_token").to_claude_payload()
    ))
    pool = load_account_pool(f"{f}:default")
    assert pool is not None
    snap = pool.snapshot()
    assert len(snap) == 2
    assert snap[0]["label"].startswith("file:")
    assert snap[1]["label"] == "default"


# ============================================================================
# Bridge integration: retry-on-transient + failover-on-cap
# ============================================================================


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip env that would otherwise contaminate bridge config."""
    for k in (
        "KAIJU_CC_ACCOUNT_POOL",
        "KAIJU_CC_SKIP_SYSTEM_PREFIX",
        "KAIJU_CC_MAX_INLINE_RETRIES",
        "KAIJU_CC_MAX_INLINE_WAIT",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


class _UpstreamStub:
    """Mock httpx upstream: pop one response per request."""

    def __init__(self, *responses: tuple[int, dict[str, str], bytes]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "auth": request.headers.get("authorization", ""),
        })
        if not self._responses:
            return httpx.Response(500, content=b"no more stubbed responses")
        status, headers, body = self._responses.pop(0)
        return httpx.Response(status, headers=headers, content=body)


def _install_upstream(monkeypatch, stub: _UpstreamStub):
    """Patch httpx.AsyncClient.request to route through the stub."""
    async def _fake_request(self, method, url, **kwargs):
        req = httpx.Request(method, url, **{k: v for k, v in kwargs.items() if k in {"headers", "content", "params"}})
        return stub(req)
    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.get_event_loop().is_running() else asyncio.run(coro)


def test_forward_non_streaming_success(isolated_env, monkeypatch):
    """Happy path: 200 from upstream is passed through."""
    from agent.claude_code import bridge

    stub = _UpstreamStub((200, {"content-type": "application/json"}, b'{"ok": true}'))
    _install_upstream(monkeypatch, stub)
    prov = _StubProvider("acc_token")

    headers = httpx.Headers({"x-api-key": "stub", "content-type": "application/json"})
    resp = asyncio.run(bridge._forward_non_streaming(
        prov, "POST", "https://api.anthropic.com/v1/messages", b'{"x": 1}', headers, {}
    ))
    assert resp.status_code == 200
    assert len(stub.calls) == 1
    # x-api-key stripped; Bearer added.
    assert "x-api-key" not in {k.lower() for k in stub.calls[0]["headers"]}
    assert stub.calls[0]["auth"] == "Bearer acc_token"


def test_forward_non_streaming_retries_transient_429(isolated_env, monkeypatch):
    """Bridge retries on transient 429 with short Retry-After."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "3")
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_WAIT", "30")
    from agent.claude_code import bridge

    stub = _UpstreamStub(
        (429, {"Retry-After": "0", "anthropic-ratelimit-tokens-remaining": "100"},
         _err_body("rate_limit_error", "throttle")),
        (200, {"content-type": "application/json"}, b'{"ok": true}'),
    )
    _install_upstream(monkeypatch, stub)
    prov = _StubProvider("acc_token")

    headers = httpx.Headers({"x-api-key": "stub"})
    resp = asyncio.run(bridge._forward_non_streaming(
        prov, "POST", "https://api.anthropic.com/v1/messages", b'{"x": 1}', headers, {}
    ))
    assert resp.status_code == 200
    assert len(stub.calls) == 2  # transient + retry


def test_forward_non_streaming_subscription_cap_returns_error(isolated_env, monkeypatch):
    """Subscription cap with no other accounts bubbles up with X-Kaiju-Bridge-Error."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "0")  # no retry budget
    from agent.claude_code import bridge

    stub = _UpstreamStub(
        (429, {"Retry-After": "18000", "anthropic-ratelimit-tokens-remaining": "0"},
         _err_body("rate_limit_error", "5h cap")),
    )
    _install_upstream(monkeypatch, stub)
    prov = _StubProvider("acc_token")

    headers = httpx.Headers({"x-api-key": "stub"})
    resp = asyncio.run(bridge._forward_non_streaming(
        prov, "POST", "https://api.anthropic.com/v1/messages", b'{"x": 1}', headers, {}
    ))
    assert resp.status_code == 429
    assert resp.headers.get("X-Kaiju-Bridge-Error") == ErrorKind.SUBSCRIPTION_CAP.value
    assert resp.headers.get("Retry-After") == "18000"


def test_forward_non_streaming_failover_to_next_account(isolated_env, monkeypatch):
    """Multi-account: 429-cap on acc0 -> automatic retry with acc1."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "3")
    from agent.claude_code import bridge

    stub = _UpstreamStub(
        (429, {"Retry-After": "18000", "anthropic-ratelimit-tokens-remaining": "0"},
         _err_body("rate_limit_error", "5h cap")),
        (200, {"content-type": "application/json"}, b'{"ok": "switched"}'),
    )
    _install_upstream(monkeypatch, stub)
    pool = _pool("acc0_token", "acc1_token")

    headers = httpx.Headers({"x-api-key": "stub"})
    resp = asyncio.run(bridge._forward_non_streaming(
        pool, "POST", "https://api.anthropic.com/v1/messages", b'{"x": 1}', headers, {}
    ))
    assert resp.status_code == 200
    assert len(stub.calls) == 2
    assert stub.calls[0]["auth"] == "Bearer acc0_token"
    assert stub.calls[1]["auth"] == "Bearer acc1_token"
    # acc0 should now be marked exhausted in the pool.
    snap = pool.snapshot()
    assert any(s["label"] == "acc0" and not s["available"] for s in snap)


def test_forward_non_streaming_oauth_invalid_triggers_failover(isolated_env, monkeypatch):
    """Multi-account: 401 on acc0 -> mark invalid, retry with acc1."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "3")
    from agent.claude_code import bridge

    stub = _UpstreamStub(
        (401, {}, _err_body("authentication_error", "bad token")),
        (200, {"content-type": "application/json"}, b'{"ok": true}'),
    )
    _install_upstream(monkeypatch, stub)
    pool = _pool("acc0_token", "acc1_token")

    headers = httpx.Headers({"x-api-key": "stub"})
    resp = asyncio.run(bridge._forward_non_streaming(
        pool, "POST", "https://api.anthropic.com/v1/messages", b'{"x": 1}', headers, {}
    ))
    assert resp.status_code == 200
    snap = pool.snapshot()
    assert any(s["label"] == "acc0" and s["invalid"] for s in snap)


def test_forward_non_streaming_all_accounts_exhausted(isolated_env, monkeypatch):
    """Multi-account: every slot returns 429-cap -> bubble up structured error."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "5")
    from agent.claude_code import bridge

    cap_resp = (
        429,
        {"Retry-After": "3600", "anthropic-ratelimit-tokens-remaining": "0"},
        _err_body("rate_limit_error", "cap"),
    )
    stub = _UpstreamStub(cap_resp, cap_resp)
    _install_upstream(monkeypatch, stub)
    pool = _pool("acc0_token", "acc1_token")

    headers = httpx.Headers({"x-api-key": "stub"})
    resp = asyncio.run(bridge._forward_non_streaming(
        pool, "POST", "https://api.anthropic.com/v1/messages", b'{"x": 1}', headers, {}
    ))
    assert resp.status_code == 429
    assert resp.headers.get("X-Kaiju-Bridge-Error") == ErrorKind.SUBSCRIPTION_CAP.value
    assert len(stub.calls) == 2  # one per account


def test_quota_endpoint_single_account(isolated_env):
    """Single-account: /quota reports multi_account=false."""
    from agent.claude_code import bridge
    from fastapi.testclient import TestClient

    prov = _StubProvider("acc_token")
    app = bridge.build_app(provider=prov)
    client = TestClient(app)

    r = client.get("/quota")
    assert r.status_code == 200
    body = r.json()
    assert body["multi_account"] is False
    assert body["accounts"] == []
    assert body["next_reset_at_unix"] is None


def test_quota_endpoint_multi_account_with_exhaustion(isolated_env):
    """Multi-account: /quota reports per-slot state and soonest reset."""
    from agent.claude_code import bridge
    from fastapi.testclient import TestClient

    pool = _pool("acc0_token", "acc1_token")
    future = time.time() + 3600
    pool.mark_account_exhausted("acc0_token", future)

    app = bridge.build_app(provider=pool)
    client = TestClient(app)

    r = client.get("/quota")
    assert r.status_code == 200
    body = r.json()
    assert body["multi_account"] is True
    assert len(body["accounts"]) == 2
    # acc0 exhausted -> not available; acc1 available.
    by_label = {a["label"]: a for a in body["accounts"]}
    assert by_label["acc0"]["available"] is False
    assert by_label["acc1"]["available"] is True
    # At least one slot available -> next_reset_at is None.
    assert body["next_reset_at_unix"] is None


def test_quota_endpoint_all_exhausted_reports_reset(isolated_env):
    from agent.claude_code import bridge
    from fastapi.testclient import TestClient

    pool = _pool("acc0_token", "acc1_token")
    future = time.time() + 600
    pool.mark_account_exhausted("acc0_token", future)
    pool.mark_account_exhausted("acc1_token", future + 10)

    app = bridge.build_app(provider=pool)
    client = TestClient(app)

    body = client.get("/quota").json()
    assert body["next_reset_at_unix"] is not None
    # Reports the SOONEST reset (acc0).
    assert abs(body["next_reset_at_unix"] - future) < 1


def test_healthz_multi_account_includes_snapshot(isolated_env):
    from agent.claude_code import bridge
    from fastapi.testclient import TestClient

    pool = _pool("acc0_token", "acc1_token")
    app = bridge.build_app(provider=pool)
    client = TestClient(app)

    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert "accounts" in body
    assert len(body["accounts"]) == 2
