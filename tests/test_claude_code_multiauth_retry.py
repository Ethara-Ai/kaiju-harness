"""Targeted coverage for the multi-account + retry-on-limit blind spots.

These tests close the five gaps found while reviewing the "test the claude code
logic for multi auth + retry when limit hit" issue:

  1. Large pool > inline-retry budget must still fully fail over (no needless pause).
  2. Streaming path failover (the path aider actually uses) -- previously untested.
  3. Cross-layer seam: the bridge's /quota output feeds recovery's pause-and-resume.
  4. Ambiguous token-prefix attribution must NOT mark the wrong account.
  5. Error attribution survives a concurrent token rotation; serial-drain is thread-safe.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest

from agent.claude_code.credentials import (
    CredentialProvider,
    MultiAccountCredentialProvider,
    OAuthCredentials,
    _AccountSlot,
    _FileCredentialProvider,
    _KeychainCredentialProvider,
    load_account_pool,
)
from agent.claude_code.errors import ErrorKind

URL = "https://api.anthropic.com/v1/messages"


# --------------------------------------------------------------------------
# Shared helpers (kept self-contained so this file has no cross-test imports)
# --------------------------------------------------------------------------
def _err_body(kind: str, msg: str = "boom") -> bytes:
    return json.dumps(
        {"type": "error", "error": {"type": kind, "message": msg}, "request_id": "req_x"}
    ).encode()


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

    def get_access_token(self) -> str:
        assert self._creds is not None
        return self._creds.access_token

    def token_prefix(self) -> str | None:
        return self._creds.access_token[:20] if self._creds else None


def _pool(*tokens: str) -> MultiAccountCredentialProvider:
    slots = [_AccountSlot(provider=_StubProvider(t), label=f"acc{i}") for i, t in enumerate(tokens)]
    return MultiAccountCredentialProvider(slots)


class _UpstreamStub:
    """Mock non-streaming upstream: pop one response per request."""

    def __init__(self, *responses: tuple[int, dict[str, str], bytes]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"auth": request.headers.get("authorization", "")})
        if not self._responses:
            return httpx.Response(500, content=b"no more stubbed responses")
        status, headers, body = self._responses.pop(0)
        return httpx.Response(status, headers=headers, content=body)


def _install_upstream(monkeypatch, stub: _UpstreamStub) -> None:
    async def _fake_request(self, method, url, **kwargs):
        req = httpx.Request(
            method, url, **{k: v for k, v in kwargs.items() if k in {"headers", "content", "params"}}
        )
        return stub(req)

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)


# ---- streaming mock ----
class _FakeStreamResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _FakeStreamCM:
    def __init__(self, resp: _FakeStreamResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._resp

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _StreamUpstreamStub:
    """Mock streaming upstream: pop one response per client.stream() call."""

    def __init__(self, *responses: tuple[int, dict[str, str], bytes]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> _FakeStreamCM:
        headers = dict(kwargs.get("headers") or {})
        auth = headers.get("authorization") or headers.get("Authorization", "")
        self.calls.append({"auth": auth})
        if not self._responses:
            return _FakeStreamCM(_FakeStreamResponse(500, {}, b"no more"))
        status, hdrs, body = self._responses.pop(0)
        return _FakeStreamCM(_FakeStreamResponse(status, hdrs, body))


def _install_stream_upstream(monkeypatch, stub: _StreamUpstreamStub) -> None:
    def _fake_stream(self, method, url, **kwargs):  # sync: returns an async CM
        return stub(method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "stream", _fake_stream)


async def _drain(resp) -> bytes:
    out = b""
    async for chunk in resp.body_iterator:
        out += chunk if isinstance(chunk, bytes) else chunk.encode()
    return out


@pytest.fixture
def isolated_env(monkeypatch):
    for k in (
        "KAIJU_CC_ACCOUNT_POOL",
        "KAIJU_CC_SKIP_SYSTEM_PREFIX",
        "KAIJU_CC_MAX_INLINE_RETRIES",
        "KAIJU_CC_MAX_INLINE_WAIT",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


_CAP = (
    429,
    {"Retry-After": "3600", "anthropic-ratelimit-tokens-remaining": "0"},
    _err_body("rate_limit_error", "cap"),
)
_OK = (200, {"content-type": "application/json"}, b'{"ok": true}')


# ==========================================================================
# Finding 1 -- large pool must fully fail over even when it exceeds the
# small inline-retry budget (no needless recovery pause).
# ==========================================================================
def test_large_pool_fully_fails_over_beyond_retry_budget(isolated_env, monkeypatch):
    """5-account pool, first 4 capped, tiny inline-retry budget of 1.

    Before the fix (failover bounded by max_retries=1) the bridge gave up after
    ~2 accounts and bubbled a cap even though account 5 was free. Now failover is
    bounded by POOL SIZE, so it reaches account 5 in the same request.
    """
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "1")
    from agent.claude_code import bridge

    stub = _UpstreamStub(_CAP, _CAP, _CAP, _CAP, _OK)  # 4 caps then success
    _install_upstream(monkeypatch, stub)
    pool = _pool("t0", "t1", "t2", "t3", "t4")

    resp = asyncio.run(
        bridge._forward_non_streaming(
            pool, "POST", URL, b"{}", httpx.Headers({"x-api-key": "stub"}), {}
        )
    )
    assert resp.status_code == 200
    assert len(stub.calls) == 5, "must traverse the whole pool, not stop at the retry budget"
    assert stub.calls[-1]["auth"] == "Bearer t4"


def test_large_pool_all_capped_bubbles_after_trying_all(isolated_env, monkeypatch):
    """If EVERY account in a large pool is capped, bubble a structured cap error
    only after each account was actually tried once."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "1")
    from agent.claude_code import bridge

    stub = _UpstreamStub(_CAP, _CAP, _CAP)
    _install_upstream(monkeypatch, stub)
    pool = _pool("t0", "t1", "t2")

    resp = asyncio.run(
        bridge._forward_non_streaming(
            pool, "POST", URL, b"{}", httpx.Headers({"x-api-key": "stub"}), {}
        )
    )
    assert resp.status_code == 429
    assert resp.headers.get("X-Kaiju-Bridge-Error") == ErrorKind.SUBSCRIPTION_CAP.value
    assert len(stub.calls) == 3  # every account tried exactly once


# ==========================================================================
# Finding 2 -- streaming path failover (the path aider actually uses).
# ==========================================================================
def test_stream_failover_to_next_account(isolated_env, monkeypatch):
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "3")
    from agent.claude_code import bridge

    stub = _StreamUpstreamStub(_CAP, (200, {"content-type": "text/event-stream"}, b"data: hi\n\n"))
    _install_stream_upstream(monkeypatch, stub)
    pool = _pool("acc0_token", "acc1_token")

    async def _call():
        resp = await bridge._stream_with_failover(
            pool, "POST", URL, b'{"stream": true}', httpx.Headers({"x-api-key": "stub"}), {}
        )
        body = await _drain(resp)
        return resp, body

    resp, body = asyncio.run(_call())
    assert resp.status_code == 200
    assert body == b"data: hi\n\n"
    assert len(stub.calls) == 2
    assert stub.calls[0]["auth"] == "Bearer acc0_token"
    assert stub.calls[1]["auth"] == "Bearer acc1_token"
    assert any(s["label"] == "acc0" and not s["available"] for s in pool.snapshot())


def test_stream_oauth_invalid_triggers_failover(isolated_env, monkeypatch):
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "3")
    from agent.claude_code import bridge

    stub = _StreamUpstreamStub(
        (401, {}, _err_body("authentication_error", "bad token")),
        (200, {"content-type": "text/event-stream"}, b"data: ok\n\n"),
    )
    _install_stream_upstream(monkeypatch, stub)
    pool = _pool("acc0_token", "acc1_token")

    async def _call():
        resp = await bridge._stream_with_failover(
            pool, "POST", URL, b'{"stream": true}', httpx.Headers({"x-api-key": "stub"}), {}
        )
        await _drain(resp)
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 200
    assert any(s["label"] == "acc0" and s["invalid"] for s in pool.snapshot())


def test_stream_all_accounts_exhausted_bubbles_cap(isolated_env, monkeypatch):
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "5")
    from agent.claude_code import bridge

    stub = _StreamUpstreamStub(_CAP, _CAP)
    _install_stream_upstream(monkeypatch, stub)
    pool = _pool("acc0_token", "acc1_token")

    resp = asyncio.run(
        bridge._stream_with_failover(
            pool, "POST", URL, b'{"stream": true}', httpx.Headers({"x-api-key": "stub"}), {}
        )
    )
    assert resp.status_code == 429
    assert resp.headers.get("X-Kaiju-Bridge-Error") == ErrorKind.SUBSCRIPTION_CAP.value
    assert len(stub.calls) == 2


def test_stream_large_pool_fully_fails_over(isolated_env, monkeypatch):
    """Finding 1 also applies to the streaming path."""
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "1")
    from agent.claude_code import bridge

    stub = _StreamUpstreamStub(
        _CAP, _CAP, _CAP, _CAP, (200, {"content-type": "text/event-stream"}, b"data: e\n\n")
    )
    _install_stream_upstream(monkeypatch, stub)
    pool = _pool("t0", "t1", "t2", "t3", "t4")

    async def _call():
        resp = await bridge._stream_with_failover(
            pool, "POST", URL, b'{"stream": true}', httpx.Headers({"x-api-key": "stub"}), {}
        )
        await _drain(resp)
        return resp

    resp = asyncio.run(_call())
    assert resp.status_code == 200
    assert len(stub.calls) == 5
    assert stub.calls[-1]["auth"] == "Bearer t4"


# ==========================================================================
# Finding 3 -- cross-layer seam: the bridge's real /quota output must drive
# recovery's pause-and-resume correctly.
# ==========================================================================
def test_bridge_quota_drives_recovery_pause_and_resume(monkeypatch):
    """End-to-end seam test.

    The bridge (real /quota endpoint) reports the soonest reset when every
    account is capped. The agent-side recovery loop must read that exact shape,
    pause for the reported duration, then retry -- and succeed on resume.
    """
    from fastapi.testclient import TestClient

    from agent.claude_code import bridge, recovery

    # --- emit side: a real bridge with an all-exhausted pool ---
    pool = _pool("a", "b")
    reset_at = time.time() + 120
    pool.mark_account_exhausted("a", reset_at)
    pool.mark_account_exhausted("b", reset_at + 10)
    app = bridge.build_app(provider=pool)
    quota = TestClient(app).get("/quota").json()
    assert quota["multi_account"] is True
    assert quota["next_reset_at_unix"] is not None

    # --- consume side: recovery reads that exact /quota bytes ---
    monkeypatch.setenv("ANTHROPIC_API_BASE", "http://127.0.0.1:8765")
    monkeypatch.setattr(recovery, "_fetch_quota", lambda base_url: quota)

    slept: list[float] = []
    monkeypatch.setattr(
        recovery, "_sleep_with_heartbeat", lambda secs, log_dir=None, *a, **k: slept.append(secs)
    )

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            # what litellm raises when the bridge returns X-Kaiju-Bridge-Error: subscription_cap
            raise RuntimeError("upstream returned subscription_cap (rate_limit_error)")
        return "resumed"

    out = recovery.run_with_recovery(fn, max_retries=3)

    assert out == "resumed"
    assert calls["n"] == 2, "must pause once then retry"
    assert slept, "recovery must pause on a cap"
    # Paused ~ until the soonest reset the bridge reported (not a blind fallback).
    assert abs(slept[0] - (quota["next_reset_at_unix"] - time.time())) < 5


# ==========================================================================
# Finding 4 -- ambiguous prefix must NOT mis-attribute an account.
# ==========================================================================
def test_ambiguous_prefix_does_not_misattribute():
    """Two accounts whose tokens share the first 20 chars. Marking by that shared
    prefix (before any token was handed out) is ambiguous and must mark NEITHER,
    rather than guessing and disabling the wrong account."""
    shared20 = "sk-ant-oat01-SAMEPFX"  # exactly 20 chars -> both prefixes collide
    assert len(shared20) == 20
    p = _pool(shared20 + "-zero", shared20 + "-one")

    p.mark_account_exhausted(shared20, time.time() + 3600)

    snap = p.snapshot()
    assert all(s["available"] for s in snap), "ambiguous prefix must not mark any account"


def test_unique_prefix_still_attributes():
    """Sanity: a non-colliding prefix DOES attribute (the marks-before-use path)."""
    p = _pool("tokenAAA", "tokenBBB")
    p.mark_account_exhausted("tokenAAA", time.time() + 3600)
    by_label = {s["label"]: s for s in p.snapshot()}
    assert by_label["acc0"]["available"] is False
    assert by_label["acc1"]["available"] is True


# ==========================================================================
# Finding 5 -- attribution survives token rotation; serial-drain is thread-safe.
# ==========================================================================
def test_attribution_survives_token_rotation():
    """A cap that arrives attributed to a STALE token (rotated out by a later
    refresh) must still mark the correct slot, thanks to recent-token history."""
    slot0 = _AccountSlot(provider=_StubProvider("t2_new"), label="acc0")
    slot1 = _AccountSlot(provider=_StubProvider("other"), label="acc1")
    # slot0 handed out t1 first, then a refresh rotated it to t2.
    slot0.remember_token("t1_old")
    slot0.remember_token("t2_new")
    p = MultiAccountCredentialProvider([slot0, slot1])

    # The failing request used the now-stale t1_old.
    p.mark_account_exhausted("t1_old", time.time() + 3600)

    by_label = {s["label"]: s for s in p.snapshot()}
    assert by_label["acc0"]["available"] is False, "stale-token cap must still hit acc0"
    assert by_label["acc1"]["available"] is True


def test_recent_token_history_is_bounded():
    slot = _AccountSlot(provider=_StubProvider("x"), label="acc0")
    for i in range(50):
        slot.remember_token(f"tok{i}")
    assert len(slot.recent_tokens) <= 8
    assert slot.has_token("tok49")  # newest retained
    assert not slot.has_token("tok0")  # oldest evicted


def test_concurrent_get_access_token_serial_drain_and_thread_safe():
    """50 concurrent callers with no exhaustion all get the first account
    (documented serial-drain policy), and nothing crashes under the lock."""
    p = _pool("tok0", "tok1", "tok2")
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        tok = p.get_access_token()
        with lock:
            results.append(tok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 50
    assert all(r == "tok0" for r in results), "serial-drain: all pick the first available account"


# ==========================================================================
# NEW-1 -- has_available() disambiguates "a slot is free" from "all dead", so
# the bridge fails over correctly and /quota is not misleading.
# ==========================================================================
def test_has_available_false_when_all_invalid():
    p = _pool("a", "b")
    p.mark_account_invalid("a")
    p.mark_account_invalid("b")
    assert p.has_available() is False
    # next_reset_at() still (ambiguously) returns None here -- that is exactly
    # why has_available() exists.
    assert p.next_reset_at() is None


def test_has_available_false_when_all_capped():
    p = _pool("a", "b")
    future = time.time() + 3600
    p.mark_account_exhausted("a", future)
    p.mark_account_exhausted("b", future)
    assert p.has_available() is False


def test_has_available_true_when_one_free():
    p = _pool("a", "b")
    p.mark_account_exhausted("a", time.time() + 3600)
    assert p.has_available() is True  # b still free


def test_failover_into_all_dead_returns_real_error_not_401(isolated_env, monkeypatch):
    """Pool with a PRE-invalidated slot: the last live account then returns 403.

    Before Change 1, next_reset_at() returned None (all invalid) so the bridge
    failed over into a dead get_access_token() and returned a generic
    401 credentials_unavailable, hiding the real error. has_available() fixes it:
    the real 403 account_restricted is returned instead.
    """
    monkeypatch.setenv("KAIJU_CC_MAX_INLINE_RETRIES", "3")
    from agent.claude_code import bridge

    pool = _pool("acc0_token", "acc1_token")
    pool.mark_account_invalid("acc0_token")  # acc0 already dead from a prior request

    stub = _UpstreamStub(
        (403, {}, _err_body("permission_error", "restricted")),  # acc1 also restricted
    )
    _install_upstream(monkeypatch, stub)

    resp = asyncio.run(
        bridge._forward_non_streaming(
            pool, "POST", URL, b"{}", httpx.Headers({"x-api-key": "stub"}), {}
        )
    )
    assert resp.status_code == 403
    assert resp.headers.get("X-Kaiju-Bridge-Error") == ErrorKind.ACCOUNT_RESTRICTED.value
    assert len(stub.calls) == 1  # only acc1 was tried; no spin into a dead account


def test_quota_reports_any_available_false_when_all_invalid(isolated_env):
    from fastapi.testclient import TestClient

    from agent.claude_code import bridge

    pool = _pool("a", "b")
    pool.mark_account_invalid("a")
    pool.mark_account_invalid("b")
    body = TestClient(bridge.build_app(provider=pool)).get("/quota").json()

    assert body["any_available"] is False
    # next_reset_at_unix is null here too -- the new field is what disambiguates.
    assert body["next_reset_at_unix"] is None


def test_quota_reports_any_available_true_when_one_free(isolated_env):
    from fastapi.testclient import TestClient

    from agent.claude_code import bridge

    pool = _pool("a", "b")
    pool.mark_account_exhausted("a", time.time() + 3600)
    body = TestClient(bridge.build_app(provider=pool)).get("/quota").json()

    assert body["any_available"] is True


# ==========================================================================
# Keychain pool-spec parsing (Option A). `:` separates entries AND appears
# inside `keychain:<service>`, so the parser must re-glue them. This makes the
# value documented in MULTI_ACCOUNT_SETUP.md actually work.
# ==========================================================================
def test_pool_spec_single_keychain_parses():
    p = load_account_pool("keychain:Claude Code-credentials")
    assert p is not None
    assert p.pool_size == 1
    slot = p._slots[0]
    assert isinstance(slot.provider, _KeychainCredentialProvider)
    assert slot.label == "keychain:Claude Code-credentials"


def test_pool_spec_documented_keychain_pair_parses():
    """The EXACT value from MULTI_ACCOUNT_SETUP.md -> two keychain accounts,
    with the same labels the doc's /quota example shows."""
    p = load_account_pool(
        "keychain:Claude Code-credentials:keychain:Claude Code-credentials-acct2"
    )
    assert p is not None
    assert p.pool_size == 2
    assert all(isinstance(s.provider, _KeychainCredentialProvider) for s in p._slots)
    assert [s.label for s in p._slots] == [
        "keychain:Claude Code-credentials",
        "keychain:Claude Code-credentials-acct2",
    ]


def test_pool_spec_mixed_keychain_and_file(tmp_path):
    f = tmp_path / "acct.json"
    f.write_text("{}")
    p = load_account_pool(f"keychain:SvcOne:{f}")
    assert p is not None
    assert p.pool_size == 2
    assert isinstance(p._slots[0].provider, _KeychainCredentialProvider)
    assert p._slots[0].label == "keychain:SvcOne"
    assert isinstance(p._slots[1].provider, _FileCredentialProvider)
    assert p._slots[1].label == f"file:{f}"
