"""kaiju.verification.bridges — reuse must VALIDATE the secret, not just liveness.

Pins the fix for the stale-bridge 401: a bridge orphaned by a previous verify
run holds a secrets.token_hex() that died with its parent's environment, still
answers /healthz, and rejects every request — BUILD/JUDGE then fail with
"kaiju-bridge: missing/invalid bridge secret". ensure_bridges must (a) probe
candidate secrets before reusing (ported from run_trajectory.sh
_bridge_secret_matches, B2) with run_trajectory's well-known fixed secrets in
the chain, (b) persist the secret in use so the NEXT invocation can
authenticate, (c) force-restart a bridge only on a DEFINITIVE rejection (a
probe timeout is a slow proxied upstream, not a mismatch; a 401 without a
bridge-auth marker is an upstream credential failure), (d) never kill a bridge
that run_trajectory's users registry says a live run is using, and (e) gate
the post-start success path on the new bridge accepting OUR secret (a foreign
bridge surviving on the port answers /healthz too).

CI-safe: no sockets; subprocess and environ are isolated per test.
"""

from __future__ import annotations

import io
import os
import urllib.error

import pytest

from kaiju.verification import bridges as B


class _Recorder:
    def __init__(self, ret=None):
        self.calls = []
        self.ret = ret

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self.ret


@pytest.fixture
def state_files(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_SECRET_STATE", str(tmp_path / "secret_{}"))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_process_state(monkeypatch, tmp_path):
    """ensure_bridges mutates os.environ directly and cleanup() shells out to
    the real cc stop script — both must never leak out of a test (a real stop
    would kill a developer's live bridge)."""
    snapshot = dict(os.environ)
    monkeypatch.setattr(B.subprocess, "run", _Recorder(
        ret=type("R", (), {"stdout": "", "returncode": 0})()))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "no-users"))  # empty registry
    yield
    for k in set(os.environ) - set(snapshot):
        del os.environ[k]
    os.environ.update(snapshot)


def _http_err(code, body=b""):
    return urllib.error.HTTPError("u", code, "msg", {}, io.BytesIO(body))


# ---------------------------------------------------------------------------
# _secret_matches semantics (tri-state; mirrors the shell probe's contract
# plus the upstream-401 and timeout distinctions the review confirmed)
# ---------------------------------------------------------------------------
class TestSecretMatches:
    def _with_urlopen(self, monkeypatch, effect):
        def _urlopen(req, timeout=None):
            return effect(req)
        monkeypatch.setattr(B.urllib.request, "urlopen", _urlopen)

    def test_2xx_is_accepted(self, monkeypatch):
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        self._with_urlopen(monkeypatch, lambda req: _R())
        assert B._secret_matches(8765, "claude", "s") is True

    def test_non_401_http_error_is_accepted(self, monkeypatch):
        # 400 = auth OK, body invalid — the probe body is deliberately junk.
        self._with_urlopen(monkeypatch, lambda req: (_ for _ in ()).throw(_http_err(400)))
        assert B._secret_matches(8765, "claude", "s") is True

    def test_bridge_auth_401_is_hard_rejection(self, monkeypatch):
        for marker in (b'{"error":{"message":"bridge: unauthorized (bad OPENAI_API_KEY)"}}',
                       b'{"error":{"message":"kaiju-bridge: missing/invalid bridge secret"}}'):
            self._with_urlopen(monkeypatch,
                               lambda req, m=marker: (_ for _ in ()).throw(_http_err(401, m)))
            assert B._secret_matches(8788, "gpt", "s") is False

    def test_upstream_credential_401_is_accepted(self, monkeypatch):
        # A 401 WITHOUT a bridge-auth marker means the bridge accepted the
        # secret and the PROVIDER credentials are broken — restarting the
        # bridge cannot help and must not be triggered.
        body = (b'{"type":"error","error":{"type":"authentication_error",'
                b'"message":"OAuth access token has been revoked."}}')
        self._with_urlopen(monkeypatch, lambda req: (_ for _ in ()).throw(_http_err(401, body)))
        assert B._secret_matches(8765, "claude", "s") is True

    def test_no_response_is_inconclusive_not_mismatch(self, monkeypatch):
        # A valid secret's probe is proxied upstream and can outlive any
        # timeout (529 retries, refresh flock, slow connect) — None, never False.
        self._with_urlopen(monkeypatch, lambda req: (_ for _ in ()).throw(OSError("timeout")))
        assert B._secret_matches(8788, "gpt", "s") is None


# ---------------------------------------------------------------------------
# secret persistence
# ---------------------------------------------------------------------------
class TestPersistedSecret:
    def test_roundtrip_and_0600(self, state_files):
        B._persist_secret(8765, "tok-abc")
        assert B._read_persisted_secret(8765) == "tok-abc"
        mode = (state_files / "secret_8765").stat().st_mode & 0o777
        assert mode == 0o600

    def test_missing_or_empty_is_none(self, state_files):
        assert B._read_persisted_secret(9999) is None
        (state_files / "secret_8788").write_text("")
        assert B._read_persisted_secret(8788) is None


# ---------------------------------------------------------------------------
# users registry (run_trajectory.sh shared-bridge ref-count)
# ---------------------------------------------------------------------------
class TestLiveBridgeUsers:
    def test_live_and_dead_pids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        d = tmp_path / "kaiju_bridge_8765.users"
        d.mkdir()
        (d / str(os.getpid())).touch()      # this process: alive
        (d / "999999").touch()               # almost surely dead
        (d / "not-a-pid").touch()            # ignored
        assert B._live_bridge_users(8765) == [os.getpid()]

    def test_missing_dir_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert B._live_bridge_users(8788) == []


# ---------------------------------------------------------------------------
# ensure_bridges reuse / restart decisions
# ---------------------------------------------------------------------------
class TestEnsureBridges:
    def _setup(self, monkeypatch, *, healthy, verdicts, start_ok=True,
               post_start_verdict=True, port_held=False):
        """verdicts: dict secret -> True/False/None (default hard False).
        post_start_verdict applies to the freshly started bridge's own secret."""
        monkeypatch.setattr(B, "_healthy", lambda port, timeout=2.0: healthy)
        fresh: dict = {}

        def _matches(port, fam, secret, timeout=5.0):
            if fresh and secret == fresh.get("secret"):
                return post_start_verdict
            return verdicts.get(secret, False)
        monkeypatch.setattr(B, "_secret_matches", _matches)
        free = _Recorder()
        monkeypatch.setattr(B, "_force_free_port", free)
        monkeypatch.setattr(B, "_port_has_listener", lambda port: port_held)

        def _start(secret):
            fresh["secret"] = secret
            return object()
        monkeypatch.setattr(B, "_start_codex", lambda s: _start(s))
        monkeypatch.setattr(B, "_start_cc", lambda s: _start(s))
        monkeypatch.setattr(B, "_wait_healthy", lambda port, tries=40: start_ok)
        return free, fresh

    def test_reuse_with_validated_env_key_and_persists_it(self, monkeypatch, state_files):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "envkey")
        free, fresh = self._setup(monkeypatch, healthy=True, verdicts={"envkey": True})
        env, cleanup = B.ensure_bridges({"claude"})
        assert env["ANTHROPIC_API_KEY"] == "envkey"
        assert not free.calls and not fresh
        # the validated key is persisted so the NEXT invocation short-circuits
        assert B._read_persisted_secret(B.PORTS["claude"]) == "envkey"
        cleanup()

    def test_reuse_with_persisted_secret_when_env_unset(self, monkeypatch, state_files):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CC_BRIDGE_SECRET", raising=False)
        B._persist_secret(B.PORTS["claude"], "oldsecret")
        free, fresh = self._setup(monkeypatch, healthy=True, verdicts={"oldsecret": True})
        env, cleanup = B.ensure_bridges({"claude"})
        assert env["ANTHROPIC_API_KEY"] == "oldsecret"
        assert not fresh
        cleanup()

    def test_reuse_with_well_known_trajectory_secret(self, monkeypatch, state_files):
        # The NORMAL shared-bridge case: run_trajectory.sh started it with its
        # fixed default secret; verification must reuse, never kill it.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CODEX_BRIDGE_SECRET", raising=False)
        free, fresh = self._setup(monkeypatch, healthy=True,
                                  verdicts={"kaiju-trajectory-fixed": True})
        env, cleanup = B.ensure_bridges({"gpt"})
        assert env["OPENAI_API_KEY"] == "kaiju-trajectory-fixed"
        assert not free.calls and not fresh
        cleanup()

    def test_stale_bridge_is_restarted_not_reused(self, monkeypatch, state_files):
        # The user-hit failure: healthy /healthz, hard-401s EVERY candidate.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CC_BRIDGE_SECRET", raising=False)
        free, fresh = self._setup(monkeypatch, healthy=True, verdicts={})
        env, cleanup = B.ensure_bridges({"claude"})
        assert free.calls, "stale bridge must be force-freed"
        assert fresh, "a fresh bridge must be started"
        persisted = B._read_persisted_secret(B.PORTS["claude"])
        assert persisted and env["ANTHROPIC_API_KEY"] == persisted
        cleanup()

    def test_inconclusive_probe_fails_open_no_kill(self, monkeypatch, state_files):
        # Timeouts alone must NEVER trigger a force-restart: reuse with the
        # inconclusive candidate instead (wrong reuse fails only THIS run;
        # a kill severs a concurrent run's in-flight streams).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "maybe-valid")
        free, fresh = self._setup(monkeypatch, healthy=True,
                                  verdicts={"maybe-valid": None})
        env, cleanup = B.ensure_bridges({"claude"})
        assert env["ANTHROPIC_API_KEY"] == "maybe-valid"
        assert not free.calls and not fresh
        cleanup()

    def test_live_users_veto_the_restart(self, monkeypatch, state_files, tmp_path):
        # All candidates hard-rejected BUT run_trajectory's registry shows a
        # live run using the bridge -> do not kill it out from under the peer.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CC_BRIDGE_SECRET", raising=False)
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        d = tmp_path / f"kaiju_bridge_{B.PORTS['claude']}.users"
        d.mkdir()
        (d / str(os.getpid())).touch()
        free, fresh = self._setup(monkeypatch, healthy=True, verdicts={})
        env, cleanup = B.ensure_bridges({"claude"})
        assert not free.calls and not fresh
        cleanup()

    def test_post_start_foreign_bridge_not_registered(self, monkeypatch, state_files):
        # /healthz after "start" is answered by a surviving FOREIGN bridge that
        # hard-rejects our secret: must not persist/apply/register it as ours.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CODEX_BRIDGE_SECRET", raising=False)
        free, fresh = self._setup(monkeypatch, healthy=False, verdicts={},
                                  post_start_verdict=False)
        env, cleanup = B.ensure_bridges({"gpt"})
        assert fresh, "a start was attempted"
        assert "OPENAI_API_KEY" not in env, "foreign bridge must not be applied"
        assert B._read_persisted_secret(B.PORTS["gpt"]) is None, "no poisoned state file"
        cleanup()

    def test_post_start_inconclusive_probe_is_success(self, monkeypatch, state_files):
        # Our own fresh bridge validates the secret locally before proxying —
        # a slow upstream (None) must not fail the start.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CODEX_BRIDGE_SECRET", raising=False)
        free, fresh = self._setup(monkeypatch, healthy=False, verdicts={},
                                  post_start_verdict=None)
        env, cleanup = B.ensure_bridges({"gpt"})
        assert env["OPENAI_API_KEY"] == fresh["secret"]
        assert B._read_persisted_secret(B.PORTS["gpt"]) == fresh["secret"]
        cleanup()

    def test_unhealthy_held_port_is_freed_first(self, monkeypatch, state_files):
        # T-state/wedged holder: /healthz dead but port held — free before start.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("KAIJU_CODEX_BRIDGE_SECRET", raising=False)
        free, fresh = self._setup(monkeypatch, healthy=False, verdicts={},
                                  port_held=True)
        env, cleanup = B.ensure_bridges({"gpt"})
        assert free.calls, "held-but-unresponsive port must be force-freed"
        assert fresh
        cleanup()
