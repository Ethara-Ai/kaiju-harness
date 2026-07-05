"""Tests for the OpenAI Codex (ChatGPT-auth) subscription bridge.

CI-safe: no live subscription or network — credentials come from inline JSON and
the upstream is a fake. Covers credential parsing/expiry/refresh and the bridge's
body normalization, SSE aggregation, header injection, and secret auth.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent.openai_codex import bridge as bridge_mod
from agent.openai_codex.bridge import (
    _aggregate_sse,
    _normalize_input,
    _prepare_body,
    build_app,
)
from agent.openai_codex.credentials import (
    CodexCredentials,
    CredentialProvider,
    CredentialsError,
    _decode_jwt_exp,
    load_credentials,
)


def _jwt(exp: int) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{hdr}.{payload}.sig"


def _auth_json(exp_offset: int = 100000, refresh: str | None = "rt.1.REFRESH") -> str:
    tokens = {"access_token": _jwt(int(time.time()) + exp_offset),
              "account_id": "acct-uuid-1234", "refresh_token": refresh, "id_token": "id"}
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens})


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------
class TestCredentials:
    def test_load_inline(self, monkeypatch):
        monkeypatch.setenv("CODEX_CREDENTIALS", _auth_json())
        c = load_credentials()
        assert c.account_id == "acct-uuid-1234"
        assert c.refresh_token == "rt.1.REFRESH"
        assert c.seconds_remaining() > 0 and not c.is_expired()

    def test_jwt_exp_decode(self):
        exp = int(time.time()) + 500
        assert abs(_decode_jwt_exp(_jwt(exp)) - exp) < 1

    def test_is_expired_within_skew(self):
        c = CodexCredentials("t", "a", expires_at=time.time() + 60)
        assert c.is_expired(skew=300)          # 60s left, 300s skew -> refresh
        assert not c.is_expired(skew=10)

    def test_missing_tokens_errors(self, monkeypatch):
        monkeypatch.setenv("CODEX_CREDENTIALS", json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
        with pytest.raises(CredentialsError):
            load_credentials()

    def test_api_key_mode_rejected(self, monkeypatch):
        monkeypatch.setenv("CODEX_CREDENTIALS",
                           json.dumps({"auth_mode": "apikey", "tokens": {"access_token": "x", "account_id": "y"}}))
        with pytest.raises(CredentialsError):
            load_credentials()

    def test_no_creds_anywhere_errors(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CODEX_CREDENTIALS", raising=False)
        monkeypatch.setenv("KAIJU_CODEX_AUTH_PATH", str(tmp_path / "nope.json"))
        # also make the ~/.codex default miss
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        with pytest.raises(CredentialsError):
            load_credentials()

    def test_refresh_called_when_expired(self, monkeypatch):
        monkeypatch.setenv("CODEX_CREDENTIALS", _auth_json(exp_offset=10))  # ~expired vs skew
        calls = {"n": 0}

        def fake_refresh(creds):
            calls["n"] += 1
            return CodexCredentials(_jwt(int(time.time()) + 100000), creds.account_id,
                                    refresh_token="rt.2.NEW", expires_at=time.time() + 100000)

        monkeypatch.setattr("agent.openai_codex.credentials.refresh_credentials", fake_refresh)
        p = CredentialProvider(persist_path=None)
        tok = p.get_access_token()
        assert calls["n"] == 1 and tok  # refreshed once


# --------------------------------------------------------------------------
# body normalization + SSE aggregation
# --------------------------------------------------------------------------
class TestBodyPrep:
    def test_string_input_normalized_to_list(self):
        body = {"input": "hello"}
        _normalize_input(body)
        assert body["input"] == [{"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": "hello"}]}]

    def test_list_input_untouched(self):
        body = {"input": [{"type": "message", "role": "user", "content": []}]}
        orig = json.loads(json.dumps(body))
        _normalize_input(body)
        assert body["input"] == orig["input"]

    def test_prepare_forces_stream_and_store(self, monkeypatch):
        monkeypatch.setenv("KAIJU_CODEX_FORCE_STORE_FALSE", "1")
        out, wanted = _prepare_body(json.dumps({"model": "gpt-5.5", "input": "x", "stream": False}).encode())
        d = json.loads(out)
        assert d["stream"] is True          # forced upstream (codex requires it)
        assert d["store"] is False          # forced
        assert wanted is False              # client did NOT want streaming
        assert isinstance(d["input"], list)  # normalized

    def test_prepare_remembers_client_stream(self):
        _, wanted = _prepare_body(json.dumps({"input": "x", "stream": True}).encode())
        assert wanted is True

    def test_aggregate_sse_assembles_output_from_item_done(self):
        sse = (
            b'data: {"type":"response.created","response":{"id":"r1"}}\n\n'
            b'data: {"type":"response.output_text.delta","delta":"AGG"}\n\n'
            b'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant",'
            b'"content":[{"type":"output_text","text":"AGG_OK"}]}}\n\n'
            b'data: {"type":"response.completed","response":{"id":"r1","status":"completed",'
            b'"output":[],"usage":{"input_tokens":5,"output_tokens":3}}}\n\n'
            b'data: [DONE]\n\n'
        )
        final, err = _aggregate_sse(sse)
        assert err is None
        assert final["status"] == "completed"
        assert final["usage"]["output_tokens"] == 3
        # output was empty in completed -> spliced from item.done
        assert final["output"][0]["content"][0]["text"] == "AGG_OK"

    def test_aggregate_sse_error_event(self):
        sse = b'data: {"type":"error","message":"boom"}\n\n'
        final, err = _aggregate_sse(sse)
        assert final is None and "boom" in err


# --------------------------------------------------------------------------
# bridge app: header injection, auth, aggregation — with a FAKE upstream
# --------------------------------------------------------------------------
class _FakeProvider:
    account_id = "acct-fake"

    def get_access_token(self):
        return "oauth-token-fake"

    def get_token_and_account(self):
        return self.get_access_token(), self.account_id


class _FakeStreamResponse:
    """Mimics httpx streaming response for the bridge's send(stream=True)."""

    def __init__(self, status_code, chunks, headers=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = headers or {"content-type": "text/event-stream"}

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b"".join(self._chunks)

    async def aclose(self):
        pass


@pytest.fixture
def captured():
    return {}


@pytest.fixture
def app_client(monkeypatch, captured):
    """Build the app with a fake httpx client that records the outgoing request."""
    completed = (
        b'data: {"type":"response.output_item.done","item":{"type":"message","role":"assistant",'
        b'"content":[{"type":"output_text","text":"HELLO"}]}}\n\n'
        b'data: {"type":"response.completed","response":{"id":"r","status":"completed","output":[],'
        b'"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    )

    class _FakeClient:
        def build_request(self, method, url, content=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json.loads(content) if content else {}
            return ("req", url, headers, content)

        async def send(self, req, stream=True):
            return _FakeStreamResponse(200, [completed])

        async def aclose(self):
            pass

    monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: _FakeClient())
    app = build_app(_FakeProvider())
    return TestClient(app)


class TestBridgeApp:
    def test_healthz(self, app_client):
        r = app_client.get("/healthz")
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_headers_injected_and_body_normalized(self, app_client, captured, monkeypatch):
        r = app_client.post("/v1/responses",
                            json={"model": "gpt-5.5", "input": "hi", "stream": False})
        assert r.status_code == 200
        h = captured["headers"]
        assert h["Authorization"] == "Bearer oauth-token-fake"
        assert h["ChatGPT-Account-Id"] == "acct-fake"
        assert h["OpenAI-Beta"] == "responses=experimental"
        assert h["originator"] == "codex_cli_rs"
        assert h["Accept"] == "text/event-stream"
        assert "session_id" in h
        # body: input normalized to list, stream forced true, store forced false
        assert isinstance(captured["body"]["input"], list)
        assert captured["body"]["stream"] is True
        assert captured["body"]["store"] is False
        assert captured["url"].endswith("/responses")

    def test_unary_client_gets_aggregated_json(self, app_client):
        r = app_client.post("/v1/responses", json={"input": "hi", "stream": False})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert body["output"][0]["content"][0]["text"] == "HELLO"

    def test_secret_auth_rejects_wrong_key(self, monkeypatch, captured):
        monkeypatch.setenv("KAIJU_CODEX_BRIDGE_SECRET", "sekret")

        completed = (
            b'data: {"type":"response.completed","response":{"id":"r","status":"completed",'
            b'"output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],'
            b'"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
        )

        class _FakeClient:
            def build_request(self, *a, **k):
                return None
            async def send(self, *a, **k):
                return _FakeStreamResponse(200, [completed])
            async def aclose(self):
                pass

        monkeypatch.setattr(bridge_mod.httpx, "AsyncClient", lambda **k: _FakeClient())
        client = TestClient(build_app(_FakeProvider()))
        bad = client.post("/v1/responses", headers={"Authorization": "Bearer WRONG"},
                          json={"input": "x"})
        assert bad.status_code == 401
        ok = client.post("/v1/responses", headers={"Authorization": "Bearer sekret"},
                         json={"input": "x", "stream": False})
        assert ok.status_code == 200


# --------------------------------------------------------------------------
# error classification
# --------------------------------------------------------------------------
class _StubProvider:
    def __init__(self, acct):
        self._acct = acct
    def get_access_token(self):
        return f"tok-{self._acct}"
    @property
    def account_id(self):
        return self._acct


class TestTranslation:
    def test_chat_to_responses_maps_roles(self):
        from agent.openai_codex.translate import chat_to_responses
        r = chat_to_responses({"model": "m", "max_tokens": 50, "stream": True, "messages": [
            {"role": "system", "content": "sys1"},
            {"role": "system", "content": "sys2"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "prev"},
        ]})
        assert r["instructions"] == "sys1\n\nsys2"
        assert r["input"][0] == {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "hi"}]}
        assert r["input"][1]["content"][0]["type"] == "output_text"
        assert r["max_output_tokens"] == 50 and r["stream"] is True

    def test_multipart_content_flattened(self):
        from agent.openai_codex.translate import chat_to_responses
        r = chat_to_responses({"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]})
        assert r["input"][0]["content"][0]["text"] == "ab"

    def test_responses_to_chat_extracts_text_and_usage(self):
        from agent.openai_codex.translate import responses_to_chat
        c = responses_to_chat({"id": "r", "status": "completed",
            "output": [{"type": "reasoning", "content": []},
                       {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 4,
                      "output_tokens_details": {"reasoning_tokens": 3}}}, "m", 100)
        assert c["choices"][0]["message"]["content"] == "hello"
        assert c["choices"][0]["finish_reason"] == "stop"
        assert c["usage"]["prompt_tokens"] == 10 and c["usage"]["completion_tokens"] == 4
        assert c["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3

    def test_sse_translation_emits_chat_chunks(self):
        from agent.openai_codex.translate import iter_responses_sse_as_chat
        sse = [
            b'data: {"type":"response.output_text.delta","delta":"AB"}',
            b'data: {"type":"response.output_text.delta","delta":"CD"}',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}',
        ]
        chunks = list(iter_responses_sse_as_chat(sse, "m", 1))
        joined = b"".join(chunks).decode()
        assert '"role": "assistant"' in joined
        assert '"content": "AB"' in joined and '"content": "CD"' in joined
        assert '"finish_reason": "stop"' in joined and joined.rstrip().endswith("[DONE]")


class TestBridgeStripsUnsupported:
    def test_prepare_strips_max_output_tokens_and_sampling(self):
        from agent.openai_codex.bridge import _prepare_body
        out, _ = _prepare_body(json.dumps({
            "model": "gpt-5.5", "input": "x", "max_output_tokens": 100,
            "temperature": 0.7, "top_p": 0.9}).encode())
        d = json.loads(out)
        assert "max_output_tokens" not in d and "temperature" not in d and "top_p" not in d

    def test_prepare_normalizes_dated_model(self):
        from agent.openai_codex.bridge import _prepare_body
        out, _ = _prepare_body(json.dumps({"model": "gpt-5.5-2026-04-23", "input": "x"}).encode())
        assert json.loads(out)["model"] == "gpt-5.5"


class TestDedupNormalization:
    """Regression: the codex bridge remaps the model upstream (openai/gpt-5.5-DATE
    -> gpt-5.5), so the cost-capture callback path (sees client model) and wrapper
    path (sees response model) must normalize to the SAME dedup key, or the same
    call is recorded twice (inflating cost / num_turns). Bug found on the bus run."""

    def test_client_and_response_model_normalize_equal(self):
        from agent.llm_cost_capture import _normalize_model
        assert _normalize_model("openai/gpt-5.5-2026-04-23") == _normalize_model("gpt-5.5")
        assert _normalize_model("openai/gpt-5.5") == "gpt-5.5"

    def test_other_providers_unaffected(self):
        from agent.llm_cost_capture import _normalize_model
        assert _normalize_model("anthropic/claude-opus-4-8") == "claude-opus-4-8"
        assert _normalize_model("vertex_ai/gemini-3.1-pro") == "gemini-3.1-pro"

    def test_dedup_keys_match_across_paths(self):
        # The exact key both paths build must be identical post-normalization.
        from agent.llm_cost_capture import _normalize_model
        client = f"call:{_normalize_model('openai/gpt-5.5-2026-04-23')}:100:20:0:0"
        server = f"call:{_normalize_model('gpt-5.5')}:100:20:0:0"
        assert client == server


class TestAsyncStreamTranslation:
    def test_incremental_chat_chunks(self):
        import asyncio
        from agent.openai_codex.translate import aiter_responses_sse_as_chat

        async def _src():
            for l in [
                b'data: {"type":"response.output_text.delta","delta":"AB"}',
                b'data: {"type":"response.output_text.delta","delta":"CD"}',
                b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}',
            ]:
                yield l

        async def _run():
            out = []
            async for c in aiter_responses_sse_as_chat(_src(), "m", 1):
                out.append(c)
            return b"".join(out).decode()

        joined = asyncio.get_event_loop().run_until_complete(_run())
        assert '"role": "assistant"' in joined
        assert '"content": "AB"' in joined and '"content": "CD"' in joined
        assert '"finish_reason": "stop"' in joined and joined.rstrip().endswith("[DONE]")
        assert '"usage"' in joined  # final chunk carries usage


class TestTranslationHardening:
    def test_tool_calls_extracted(self):
        from agent.openai_codex.translate import responses_to_chat
        c = responses_to_chat({"status": "completed", "output": [
            {"type": "function_call", "call_id": "c1", "name": "edit", "arguments": '{"x":1}'}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}, "m", 1)
        msg = c["choices"][0]["message"]
        assert msg["content"] is None                      # tool-call-only -> null content
        assert msg["tool_calls"][0]["function"]["name"] == "edit"
        assert c["choices"][0]["finish_reason"] == "tool_calls"

    def test_empty_response_content_is_empty_string(self):
        from agent.openai_codex.translate import responses_to_chat
        c = responses_to_chat({"status": "completed", "output": [
            {"type": "reasoning", "content": []}], "usage": {}}, "m", 1)
        assert c["choices"][0]["message"]["content"] == ""  # not None (no tool calls)

    def test_incomplete_maps_to_length(self):
        from agent.openai_codex.translate import responses_to_chat
        c = responses_to_chat({"status": "incomplete", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
            "usage": {}}, "m", 1)
        assert c["choices"][0]["finish_reason"] == "length"
        assert c["choices"][0]["message"]["content"] == "partial"

    def test_text_and_toolcall_both_present(self):
        from agent.openai_codex.translate import responses_to_chat
        c = responses_to_chat({"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}],
            "usage": {}}, "m", 1)
        msg = c["choices"][0]["message"]
        assert msg["content"] == "hi" and msg["tool_calls"][0]["function"]["name"] == "f"


class TestConcurrencyAndDedupFixes:
    def test_single_provider_token_and_account(self, monkeypatch):
        from agent.openai_codex.credentials import CredentialProvider
        monkeypatch.setenv("CODEX_CREDENTIALS", _auth_json())
        p = CredentialProvider(persist_path=None)
        tok, acct = p.get_token_and_account()
        assert tok and acct == "acct-uuid-1234"

    def test_constant_time_secret_compare(self):
        from agent.openai_codex.bridge import _secret_eq
        assert _secret_eq("abc", "abc") is True
        assert _secret_eq("abc", "abd") is False
        assert _secret_eq("", "abc") is False           # empty candidate rejected

    def test_per_log_dedup_not_cross_module(self):
        # The dedup set is per-LlmCallLog: two modules with identical-signature
        # calls both get counted (was a global-set under-count bug).
        from agent.llm_cost_capture import capture_module_calls, _record_response_object
        from agent.thinking_capture import ThinkingCapture

        class _U:
            prompt_tokens = 50; completion_tokens = 10
            input_tokens = 50; output_tokens = 10
        class _R:
            usage = _U(); choices = []

        tc = ThinkingCapture()
        with capture_module_calls(thinking_capture=tc, module="a", model_short="m") as la:
            _record_response_object("m", _R(), 1.0)
        with capture_module_calls(thinking_capture=tc, module="b", model_short="m") as lb:
            _record_response_object("m", _R(), 1.0)
        assert len(la.calls) == 1 and len(lb.calls) == 1     # both counted

    def test_within_module_dedup_still_works(self):
        from agent.llm_cost_capture import capture_module_calls, _record_response_object
        from agent.thinking_capture import ThinkingCapture

        class _U:
            prompt_tokens = 50; completion_tokens = 10
            input_tokens = 50; output_tokens = 10
        class _R:
            usage = _U(); choices = []

        tc = ThinkingCapture()
        with capture_module_calls(thinking_capture=tc, module="c", model_short="m") as lc:
            _record_response_object("m", _R(), 1.0)
            _record_response_object("m", _R(), 1.0)      # same sig -> deduped
        assert len(lc.calls) == 1


class TestErrorClassification:
    def _c(self, code, body="", headers=None):
        from agent.openai_codex.errors import classify_openai_error
        return classify_openai_error(code, body, headers)

    def test_200_ok(self):
        assert self._c(200).kind.value == "ok"

    def test_401_auth_triggers_refresh(self):
        e = self._c(401, '{"error":"invalid token"}')
        assert e.kind.value == "auth" and e.should_refresh_token

    def test_429_rate_limit_rotates(self):
        e = self._c(429, "Rate limit reached", {"retry-after": "12"})
        assert e.kind.value in ("rate_limit", "cap") and e.should_rotate_account
        assert e.retry_after == 12.0

    def test_429_usage_cap(self):
        e = self._c(429, "You exceeded your current quota")
        assert e.kind.value == "cap" and e.should_rotate_account

    def test_403_plan_cap_vs_auth(self):
        assert self._c(403, "weekly limit reached").kind.value == "cap"
        assert self._c(403, "forbidden").kind.value == "auth"

    def test_400_bad_request_fatal(self):
        e = self._c(400, "Unsupported parameter: max_output_tokens")
        assert e.kind.value == "bad_request" and not e.retryable

    def test_404_not_found(self):
        assert self._c(404, "Not Found").kind.value == "not_found"

    def test_5xx_transient(self):
        e = self._c(503, "service unavailable")
        assert e.kind.value == "transient" and e.retryable

    def test_none_status_transient(self):
        assert self._c(None, "conn reset").kind.value == "transient"

    def test_retry_after_from_body(self):
        from agent.openai_codex.errors import extract_retry_after
        assert extract_retry_after(None, "Please try again in 8s") == 8.0


# --------------------------------------------------------------------------
# multi-account pool
# --------------------------------------------------------------------------


class TestMultiAccountPool:
    def _pool(self, n=3):
        from agent.openai_codex.credentials import MultiAccountCredentialProvider
        return MultiAccountCredentialProvider([_StubProvider(f"acct{i}") for i in range(n)],
                                              state_path=None)

    def test_starts_on_first(self):
        p = self._pool()
        assert p.get_access_token() == "tok-acct0"
        assert p.account_id == "acct0"

    def test_penalize_rotates(self):
        p = self._pool()
        p.get_access_token()               # active = 0
        p.penalize(600)                    # cool 0, advance to 1
        assert p.get_access_token() == "tok-acct1"

    def test_skips_cooled_down(self):
        p = self._pool(2)
        p.get_access_token()               # active 0
        p.penalize(600)                    # cool 0 -> active 1
        p.get_access_token()               # active 1
        p.penalize(600)                    # cool 1 -> active 0 (still cooling) -> picks soonest
        # both cooled: _pick returns the soonest-free (0). Must still return a token.
        assert p.get_access_token() in ("tok-acct0", "tok-acct1")

    def test_status_shape(self):
        p = self._pool(2)
        s = p.status()
        assert s["active"] == 0 and len(s["accounts"]) == 2
        assert "cooldown_remaining" in s["accounts"][0]

    def test_load_pool_default_entry(self, monkeypatch):
        from agent.openai_codex.credentials import load_account_pool
        monkeypatch.setenv("CODEX_CREDENTIALS", _auth_json())
        pool = load_account_pool("default:default")
        assert pool is not None and len(pool._providers) == 2

    def test_load_pool_empty_returns_none(self):
        from agent.openai_codex.credentials import load_account_pool
        assert load_account_pool("") is None


# --------------------------------------------------------------------------
# recovery (transient retry)
# --------------------------------------------------------------------------


class TestRecovery:
    def test_retries_transient_then_succeeds(self):
        from agent.openai_codex.recovery import run_with_recovery
        calls = {"n": 0}
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("peer closed connection")
            return "ok"
        out = run_with_recovery(flaky, max_retries=5, sleep=lambda s: None)
        assert out == "ok" and calls["n"] == 3

    def test_non_transient_propagates_immediately(self):
        from agent.openai_codex.recovery import run_with_recovery
        calls = {"n": 0}
        def boom():
            calls["n"] += 1
            raise ValueError("bad request")
        with pytest.raises(ValueError):
            run_with_recovery(boom, sleep=lambda s: None)
        assert calls["n"] == 1               # not retried

    def test_gives_up_after_cap(self):
        from agent.openai_codex.recovery import run_with_recovery
        def always():
            raise RuntimeError("503 service unavailable")
        with pytest.raises(RuntimeError):
            run_with_recovery(always, max_retries=2, sleep=lambda s: None)


# --------------------------------------------------------------------------
# chat<->responses translation
# --------------------------------------------------------------------------


# (multi-account atomicity test, folded into the codex resilience commit)
class TestMultiAccountAtomic:
    def test_multi_account_token_and_id_atomic(self):
        # get_token_and_account must return token+id from the SAME slot.
        from agent.openai_codex.credentials import MultiAccountCredentialProvider

        class _P:
            def __init__(self, n): self._n = n
            def get_access_token(self): return f"tok-{self._n}"
            @property
            def account_id(self): return f"acct-{self._n}"

        pool = MultiAccountCredentialProvider([_P(0), _P(1)], state_path=None)
        tok, acct = pool.get_token_and_account()
        assert tok.split("-")[1] == acct.split("-")[1]     # same slot