"""QC cluster C9_bridges — regression + cross-bridge parity tests.

Pins the fixes for:
  C9-001  openai_codex credential file must be written 0600 atomically.
  C9-002  CredentialProvider must not hold the read lock across a network refresh;
          the bridge must run token acquisition off the event loop.
  C9-003  openai_codex must NOT forward a truncated stream as a clean finish=stop —
          both the native keepalive path and the chat-translated path must signal
          an error when the upstream ends without a Responses terminal event.
  C9-004  openai_codex bridge must fail over to the next pooled account in-request
          (with a _tried_tokens spin guard) instead of forwarding the first cap.
  C9-005  the multi-account pool must permanently invalidate an AUTH/401 slot and
          never re-hand it out; drain (raise) when all slots are invalid.
  C9-006  both bridges warn loudly when unauthenticated and bind 127.0.0.1 (parity).
  C9-007  openai_codex has a buffer-and-retry path (Option D) mirroring claude_code.

CI-safe: no live subscription or network (inline creds + fake upstream).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.openai_codex import bridge as bridge_mod
from agent.openai_codex import credentials as credmod
from agent.openai_codex import translate as xlate
from agent.openai_codex.bridge import build_app
from agent.openai_codex.credentials import (
    CodexCredentials,
    CredentialProvider,
    MultiAccountCredentialProvider,
    _atomic_write_json,
)


def _jwt(exp: int) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{hdr}.{payload}.sig"


def _auth_json(exp_offset: int = 100000, refresh: str | None = "rt.1.REFRESH") -> str:
    tokens = {"access_token": _jwt(int(time.time()) + exp_offset),
              "account_id": "acct-uuid-1234", "refresh_token": refresh, "id_token": "id"}
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens})


# ---------------------------------------------------------------------------
# C9-001 — credential file 0600, atomic, umask-independent
# ---------------------------------------------------------------------------
class TestC9_001_CredentialFilePerms:
    def test_atomic_write_json_is_0600_under_loose_umask(self, tmp_path):
        old = os.umask(0o022)  # world-readable default; the write must override it
        try:
            p = tmp_path / "auth.json"
            _atomic_write_json(p, {"tokens": {"refresh_token": "rt.secret"}})
            assert (p.stat().st_mode & 0o777) == 0o600
            # no leftover temp file
            assert not (tmp_path / "auth.json.tmp").exists()
        finally:
            os.umask(old)

    def test_persist_writes_secret_0600(self, tmp_path, monkeypatch):
        # Real persistence path: provider bound to an auth.json, _persist writes
        # the rotated token back — it must land 0600 even under a loose umask.
        authp = tmp_path / "auth.json"
        authp.write_text(_auth_json())
        monkeypatch.delenv("CODEX_CREDENTIALS", raising=False)
        monkeypatch.setenv("KAIJU_CODEX_AUTH_PATH", str(authp))
        prov = CredentialProvider()
        old = os.umask(0o022)
        try:
            prov._persist(prov._creds)
            assert (authp.stat().st_mode & 0o777) == 0o600
        finally:
            os.umask(old)

    def test_pool_state_write_0600(self, tmp_path):
        statep = tmp_path / "pool_state.json"
        pool = MultiAccountCredentialProvider([_StubProvider("a"), _StubProvider("b")],
                                              state_path=str(statep))
        old = os.umask(0o022)
        try:
            pool.mark_exhausted("nope", 10)  # no-op attribution but still saves state
            pool._save_state()
            assert statep.exists() and (statep.stat().st_mode & 0o777) == 0o600
        finally:
            os.umask(old)

    def test_parity_both_credential_writers_use_0600_os_open(self):
        # Both bridges must create the temp file 0600 from the start (os.open),
        # never write_text-then-chmod (which briefly exposes the token).
        cc = Path(credmod.__file__).read_text()
        from agent.claude_code import credentials as cc_cred
        anth = Path(cc_cred.__file__).read_text()
        for src in (cc, anth):
            assert "os.open(" in src and "0o600" in src
            assert "O_CREAT" in src


# ---------------------------------------------------------------------------
# C9-002 — no process-wide lock held across a network refresh
# ---------------------------------------------------------------------------
class TestC9_002_RefreshLock:
    def test_both_providers_have_separate_refresh_lock(self, monkeypatch):
        monkeypatch.setenv("CODEX_CREDENTIALS", _auth_json())
        prov = CredentialProvider()
        assert hasattr(prov, "_refresh_lock") and prov._refresh_lock is not prov._lock
        from agent.claude_code.credentials import CredentialProvider as CC
        assert hasattr(CC(), "_refresh_lock")

    def test_account_id_not_blocked_by_inflight_refresh(self, monkeypatch):
        # An expired token forces a refresh; a concurrent account_id read must NOT
        # block on the slow refresh (it would if the single lock were held across it).
        monkeypatch.setenv("CODEX_CREDENTIALS", _auth_json(exp_offset=-100))
        prov = CredentialProvider()
        entered = threading.Event()
        release = threading.Event()

        def _slow_refresh(creds):
            entered.set()
            release.wait(2.0)
            return CodexCredentials(access_token=_jwt(int(time.time()) + 9999),
                                    account_id=creds.account_id,
                                    refresh_token=creds.refresh_token,
                                    expires_at=int(time.time()) + 9999)

        monkeypatch.setattr(credmod, "refresh_credentials", _slow_refresh)
        t = threading.Thread(target=prov.get_access_token, daemon=True)
        t.start()
        assert entered.wait(1.0), "refresh never started"
        t0 = time.time()
        aid = prov.account_id  # must return immediately, refresh still in flight
        assert aid == "acct-uuid-1234"
        assert time.time() - t0 < 0.5, "account_id blocked on the in-flight refresh"
        release.set()
        t.join(2.0)

    def test_bridge_runs_token_acquisition_off_event_loop(self):
        src = Path(bridge_mod.__file__).read_text()
        # healthz and the failover helper must offload the (possibly blocking)
        # token fetch so one refresh can't freeze the FastAPI event loop.
        assert "asyncio.to_thread(provider.get_access_token)" in src
        assert "asyncio.to_thread(provider.get_token_and_account)" in src


# ---------------------------------------------------------------------------
# C9-003 — truncated stream must NOT be forwarded as a clean completion
# ---------------------------------------------------------------------------
async def _agen(chunks):
    for c in chunks:
        yield c


def _run_keepalive(chunks, interval=10.0):
    async def _run():
        out = []
        async for b in bridge_mod._stream_with_keepalive(_agen(chunks), _noop_aclose, interval):
            out.append(b)
        return out
    return asyncio.run(_run())


async def _noop_aclose():
    pass


class TestC9_003_TruncationGuard:
    def test_keepalive_truncated_stream_emits_error(self):
        # Stream ends WITHOUT response.completed/incomplete/failed -> truncated.
        chunks = [b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n']
        out = _run_keepalive(chunks)
        joined = b"".join(out)
        assert b"response.failed" in joined
        assert b"truncated" in joined

    def test_keepalive_complete_stream_no_synthetic_error(self):
        chunks = [
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
            b'data: {"type":"response.completed","response":{"id":"r"}}\n\n',
        ]
        out = _run_keepalive(chunks)
        # every chunk forwarded verbatim, NO synthetic failure appended
        assert out == chunks
        assert not any(b"response.failed" in b and b"truncated" in b for b in out)

    def test_tail_terminal_detection_compact_and_spaced_and_event(self):
        assert xlate.tail_has_terminal_event(b'{"type":"response.completed"}')
        assert xlate.tail_has_terminal_event(b'{"type": "response.incomplete"}')
        assert xlate.tail_has_terminal_event(b'event: response.failed\n')
        assert not xlate.tail_has_terminal_event(b'{"type":"response.output_text.delta"}')

    def test_event_marker_must_be_line_anchored_not_false_latched_by_model_text(self):
        """QC-C9-003 bypass guard: the bare string 'event: response.completed'
        appears verbatim inside a model's own output_text.delta (it needs no JSON
        escaping), so an UNanchored substring match would false-latch the terminal
        flag and let a truncated turn ship as a clean stop. The claude_code twin
        anchors 'message_stop' to a line boundary; this must too."""
        # Real SSE: a model newline is JSON-escaped to backslash-n (0x5c 0x6e),
        # never a raw 0x0a, so the marker inside delta text is NOT line-anchored.
        poison_escaped = (
            b'data: {"type":"response.output_text.delta",'
            b'"delta":"see:\\nevent: response.completed\\n"}\n\n'
        )
        assert b"\nevent:" not in poison_escaped  # sanity: no raw framing newline
        assert not xlate.tail_has_terminal_event(poison_escaped)
        # marker at the START of a delta string value (quote-preceded, mid-buffer)
        poison_quote = (
            b'data: {"type":"response.output_text.delta",'
            b'"delta":"event: response.completed now"}\n\n'
        )
        assert not xlate.tail_has_terminal_event(poison_quote)
        # genuine SSE framing (leading newline OR tail start) still detected
        assert xlate.tail_has_terminal_event(b"prev\nevent: response.completed\n")
        assert xlate.tail_has_terminal_event(b"event: response.failed\ndata: {}\n")

    def test_async_chat_truncated_no_clean_stop(self):
        async def _src():
            yield b'data: {"type":"response.output_text.delta","delta":"AB"}'
            # no terminal event -> truncated

        async def _run():
            out = []
            async for c in xlate.aiter_responses_sse_as_chat(_src(), "m", 1):
                out.append(c)
            return b"".join(out).decode()

        joined = asyncio.run(_run())
        assert '"error"' in joined
        assert '"finish_reason": "stop"' not in joined
        assert joined.rstrip().endswith("[DONE]")

    def test_async_chat_complete_is_clean_stop(self):
        async def _src():
            yield b'data: {"type":"response.output_text.delta","delta":"AB"}'
            yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}'

        async def _run():
            out = []
            async for c in xlate.aiter_responses_sse_as_chat(_src(), "m", 1):
                out.append(c)
            return b"".join(out).decode()

        joined = asyncio.run(_run())
        assert '"finish_reason": "stop"' in joined
        assert '"error"' not in joined

    def test_sync_chat_truncated_no_clean_stop(self):
        lines = [b'data: {"type":"response.output_text.delta","delta":"AB"}']
        joined = b"".join(xlate.iter_responses_sse_as_chat(lines, "m", 1)).decode()
        assert '"error"' in joined
        assert '"finish_reason": "stop"' not in joined

    def test_parity_both_bridges_guard_truncation(self):
        codex = Path(bridge_mod.__file__).read_text()
        from agent.claude_code import bridge as cc_bridge
        anth = Path(cc_bridge.__file__).read_text()
        # claude_code guards on message_stop; codex on the Responses terminal events.
        assert "message_stop" in anth and "truncat" in anth.lower()
        assert "tail_has_terminal_event" in codex and "responses_truncation_error_sse" in codex


# ---------------------------------------------------------------------------
# shared stub + fake upstream for bridge-level failover tests
# ---------------------------------------------------------------------------
class _StubProvider:
    def __init__(self, acct):
        self._acct = acct

    def get_access_token(self):
        return f"tok-{self._acct}"

    def get_token_and_account(self):
        return self.get_access_token(), self._acct

    @property
    def account_id(self):
        return self._acct


class _FakeStreamResponse:
    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {"content-type": "text/event-stream"}

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aiter_lines(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b"".join(self._chunks)

    async def aclose(self):
        pass


_COMPLETE = (
    b'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant",'
    b'"content":[{"type":"output_text","text":"OK"}]}}\n\n'
    b'data: {"type":"response.completed","response":{"id":"r","status":"completed","output":[],'
    b'"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
)


def _fake_client_factory(script):
    """script: list of (status_code, body_bytes) returned per successive send()."""
    calls = {"n": 0}

    class _FakeClient:
        def build_request(self, method, url, content=None, headers=None):
            return ("req", url, headers, content)

        async def send(self, req, stream=True):
            i = min(calls["n"], len(script) - 1)
            calls["n"] += 1
            status, body = script[i]
            return _FakeStreamResponse(status, [body])

        async def aclose(self):
            pass

    return _FakeClient, calls


# ---------------------------------------------------------------------------
# C9-004 — in-request failover + spin guard
# ---------------------------------------------------------------------------
class TestC9_004_InRequestFailover:
    def _pool(self, n=2):
        return MultiAccountCredentialProvider([_StubProvider(f"a{i}") for i in range(n)],
                                              state_path=None)

    def test_cap_on_first_account_fails_over_inrequest(self, monkeypatch):
        script = [(429, b'{"error":{"message":"rate limit"}}'), (200, _COMPLETE)]
        FakeClient, calls = _fake_client_factory(script)
        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: FakeClient())
        client = TestClient(build_app(self._pool()))
        r = client.post("/v1/responses", json={"input": "hi", "stream": False})
        # First account 429 -> failover to second in the SAME request -> 200.
        assert r.status_code == 200
        assert calls["n"] == 2

    def test_all_capped_returns_error_no_spin(self, monkeypatch):
        script = [(429, b'{"error":{"message":"rate limit"}}')]  # every send 429
        FakeClient, calls = _fake_client_factory(script)
        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: FakeClient())
        client = TestClient(build_app(self._pool()))
        r = client.post("/v1/responses", json={"input": "hi", "stream": False})
        assert r.status_code == 429
        # Bounded: both slots tried once, then the pool is fully cooled -> stop.
        assert calls["n"] <= 4

    def test_bad_request_does_not_failover(self, monkeypatch):
        script = [(400, b'{"error":{"message":"bad"}}'), (200, _COMPLETE)]
        FakeClient, calls = _fake_client_factory(script)
        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: FakeClient())
        client = TestClient(build_app(self._pool()))
        r = client.post("/v1/responses", json={"input": "hi", "stream": False})
        assert r.status_code == 400
        assert calls["n"] == 1  # returned immediately, no rotation

    def test_all_auth_401_terminates(self, monkeypatch):
        script = [(401, b'{"error":{"message":"invalid token"}}')]
        FakeClient, calls = _fake_client_factory(script)
        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: FakeClient())
        client = TestClient(build_app(self._pool()))
        r = client.post("/v1/responses", json={"input": "hi", "stream": False})
        # Both accounts 401 -> both invalidated -> pool drained -> terminates.
        assert r.status_code in (401, 503)
        assert calls["n"] <= 3


# ---------------------------------------------------------------------------
# C9-005 — permanent invalidation of a revoked account
# ---------------------------------------------------------------------------
class TestC9_005_Invalidation:
    def _pool(self, n=2):
        return MultiAccountCredentialProvider([_StubProvider(f"a{i}") for i in range(n)],
                                              state_path=None)

    def test_invalid_account_never_rehanded(self):
        pool = self._pool()
        t0, _ = pool.get_token_and_account()          # slot 0
        assert t0 == "tok-a0"
        pool.mark_invalid(t0)                          # 401 -> permanently drop slot 0
        for _ in range(5):
            tok, acct = pool.get_token_and_account()
            assert tok == "tok-a1" and acct == "a1"    # only the healthy slot

    def test_all_invalid_drains(self):
        pool = self._pool()
        t0, _ = pool.get_token_and_account()
        pool.mark_invalid(t0)
        t1, _ = pool.get_token_and_account()
        pool.mark_invalid(t1)
        with pytest.raises(credmod.CredentialsError):
            pool.get_token_and_account()

    def test_next_reset_at_none_when_healthy_slot_exists(self):
        pool = self._pool()
        assert pool.next_reset_at() is None
        t0, _ = pool.get_token_and_account()
        pool.mark_exhausted(t0, 600)
        assert pool.next_reset_at() is None            # slot 1 still healthy
        t1, _ = pool.get_token_and_account()
        pool.mark_exhausted(t1, 600)
        assert pool.next_reset_at() is not None         # both cooling

    def test_parity_claude_code_pool_has_invalidation(self):
        from agent.claude_code.credentials import MultiAccountCredentialProvider as CCPool
        assert hasattr(CCPool, "mark_account_invalid")
        # codex reached parity with an equivalent invalidation entry point.
        assert hasattr(MultiAccountCredentialProvider, "mark_invalid")


# ---------------------------------------------------------------------------
# C9-006 — unauthenticated warning parity (accepted-risk item)
# ---------------------------------------------------------------------------
class TestC9_006_AuthParity:
    def test_both_bridges_warn_and_support_secret(self):
        codex = Path(bridge_mod.__file__).read_text()
        from agent.claude_code import bridge as cc_bridge
        anth = Path(cc_bridge.__file__).read_text()
        for src in (codex, anth):
            assert "UNAUTHENTICATED" in src
            assert "hmac.compare_digest" in src

    def test_both_bind_localhost_by_default(self):
        from agent.openai_codex import __main__ as codex_main
        from agent.claude_code import __main__ as cc_main
        for m in (codex_main, cc_main):
            assert '127.0.0.1' in Path(m.__file__).read_text()


# ---------------------------------------------------------------------------
# C9-007 — buffer-and-retry (Option D) parity
# ---------------------------------------------------------------------------
class TestC9_007_BufferAndRetry:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("KAIJU_CODEX_BUFFER_AND_RETRY", raising=False)
        assert bridge_mod._buffer_and_retry_enabled() is False
        monkeypatch.setenv("KAIJU_CODEX_BUFFER_AND_RETRY", "1")
        assert bridge_mod._buffer_and_retry_enabled() is True

    def test_buffered_complete_stream_replayed(self, monkeypatch):
        monkeypatch.setenv("KAIJU_CODEX_BUFFER_AND_RETRY", "1")
        FakeClient, calls = _fake_client_factory([(200, _COMPLETE)])
        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: FakeClient())
        client = TestClient(build_app(_StubProvider("solo")))
        r = client.post("/v1/responses", json={"input": "hi", "stream": True})
        assert r.status_code == 200
        assert b"response.completed" in r.content

    def test_buffered_incomplete_retries_then_errors(self, monkeypatch):
        monkeypatch.setenv("KAIJU_CODEX_BUFFER_AND_RETRY", "1")
        monkeypatch.setenv("KAIJU_CODEX_STREAM_BUFFER_RETRIES", "1")
        # Upstream always returns a stream WITHOUT a terminal event.
        truncated = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
        FakeClient, calls = _fake_client_factory([(200, truncated)])
        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: FakeClient())
        client = TestClient(build_app(_StubProvider("solo")))
        r = client.post("/v1/responses", json={"input": "hi", "stream": True})
        assert r.status_code == 200
        # Never a truncated clean stream: ends with a synthetic failure frame.
        assert b"response.failed" in r.content
        assert calls["n"] >= 2  # re-issued at least once before giving up

    def test_parity_both_bridges_have_buffered_path(self):
        codex = Path(bridge_mod.__file__).read_text()
        from agent.claude_code import bridge as cc_bridge
        anth = Path(cc_bridge.__file__).read_text()
        assert "_stream_buffered_with_retry" in codex
        assert "_stream_buffered_with_retry" in anth
