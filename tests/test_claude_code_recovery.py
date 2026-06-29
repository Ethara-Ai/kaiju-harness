"""Tests for the pause-and-resume helper used by the harness when the bridge
returns a 429 SUBSCRIPTION_CAP."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from agent.claude_code import recovery


class _FakeRateLimitError(Exception):
    """Looks enough like litellm.exceptions.RateLimitError for the detector."""
    __module__ = "litellm.exceptions"


# The class above is module-anchored; rename the type so MRO walks find it.
_FakeRateLimitError.__name__ = "RateLimitError"
_FakeRateLimitError.__qualname__ = "RateLimitError"


@pytest.fixture
def bridge_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_BASE", "http://127.0.0.1:8765")
    monkeypatch.delenv("KAIJU_CC_MAX_PAUSE_SEC", raising=False)
    yield


@pytest.fixture
def no_bridge_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_BASE", raising=False)
    yield


def test_bridge_base_url_detects_localhost(bridge_env):
    assert recovery._bridge_base_url() == "http://127.0.0.1:8765"


def test_bridge_base_url_returns_none_for_remote(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_BASE", "https://api.anthropic.com")
    assert recovery._bridge_base_url() is None


def test_bridge_base_url_unset_returns_none(no_bridge_env):
    assert recovery._bridge_base_url() is None


def test_run_without_bridge_passes_exceptions_through(no_bridge_env):
    """When not using the bridge, RateLimitError should propagate as-is."""
    calls = []

    def boom():
        calls.append(1)
        raise _FakeRateLimitError("rate_limit_error")

    with pytest.raises(_FakeRateLimitError):
        recovery.run_with_recovery(boom)
    assert len(calls) == 1  # no retry attempted


def test_run_with_bridge_retries_after_pause(bridge_env, tmp_path, monkeypatch):
    """RateLimitError -> /quota lookup -> short sleep -> retry succeeds."""
    quota_future = time.time() + 0.5  # very short pause for testing
    monkeypatch.setattr(
        recovery, "_fetch_quota",
        lambda url: {"multi_account": False, "next_reset_at_unix": quota_future},
    )
    # No-op the heartbeat sleep so the test is fast.
    sleep_calls = []
    monkeypatch.setattr(
        recovery, "_sleep_with_heartbeat",
        lambda total, log_dir, heartbeat_seconds=60: sleep_calls.append((total, log_dir)),
    )

    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise _FakeRateLimitError("rate_limit_error from upstream")
        return "ok"

    result = recovery.run_with_recovery(flaky, _kaiju_log_dir=tmp_path)
    assert result == "ok"
    assert len(attempts) == 2
    assert len(sleep_calls) == 1
    # Should have used the quota /next_reset hint, not the fallback.
    assert sleep_calls[0][0] >= 0


def test_run_with_bridge_respects_max_pause(bridge_env, tmp_path, monkeypatch):
    """Reset > KAIJU_CC_MAX_PAUSE_SEC -> give up + re-raise."""
    monkeypatch.setenv("KAIJU_CC_MAX_PAUSE_SEC", "60")
    monkeypatch.setattr(
        recovery, "_fetch_quota",
        lambda url: {"next_reset_at_unix": time.time() + 7200},  # 2h -> exceeds 60s cap
    )
    monkeypatch.setattr(recovery, "_sleep_with_heartbeat", lambda *a, **k: None)

    attempts = []

    def always_throttled():
        attempts.append(1)
        raise _FakeRateLimitError("rate_limit_error")

    with pytest.raises(_FakeRateLimitError):
        recovery.run_with_recovery(always_throttled, _kaiju_log_dir=tmp_path)
    assert len(attempts) == 1


def test_run_with_bridge_uses_retry_after_header_fallback(bridge_env, tmp_path, monkeypatch):
    """If /quota returns no hint, fall back to the error's Retry-After."""
    monkeypatch.setattr(recovery, "_fetch_quota", lambda url: {})
    sleeps = []
    monkeypatch.setattr(
        recovery, "_sleep_with_heartbeat",
        lambda total, log_dir, **kw: sleeps.append(total),
    )

    attempts = []

    def make_err():
        attempts.append(1)
        if len(attempts) == 1:
            err = _FakeRateLimitError("Hit rate limit. Retry-After: 12")
            raise err
        return "recovered"

    result = recovery.run_with_recovery(make_err, _kaiju_log_dir=tmp_path)
    assert result == "recovered"
    assert sleeps == [12]  # matched retry-after from string


def test_run_with_bridge_non_rate_limit_error_passes_through(bridge_env, tmp_path):
    """Non-RateLimitError exceptions should NOT trigger recovery."""

    class _ValueErr(ValueError):
        pass

    def bad_input():
        raise _ValueErr("garbage in")

    with pytest.raises(_ValueErr):
        recovery.run_with_recovery(bad_input, _kaiju_log_dir=tmp_path)


def test_run_with_bridge_max_retries_exhausted(bridge_env, tmp_path, monkeypatch):
    """After max_retries persistent errors, raise."""
    monkeypatch.setattr(recovery, "_fetch_quota", lambda url: {"next_reset_at_unix": time.time() + 1})
    monkeypatch.setattr(recovery, "_sleep_with_heartbeat", lambda *a, **k: None)

    attempts = []

    def always_throttled():
        attempts.append(1)
        raise _FakeRateLimitError("rate_limit_error")

    with pytest.raises(_FakeRateLimitError):
        recovery.run_with_recovery(always_throttled, _kaiju_log_dir=tmp_path, max_retries=2)
    assert len(attempts) == 3  # initial + 2 retries


def test_is_rate_limit_error_detects_litellm_class():
    err = _FakeRateLimitError("rate_limit_error")
    assert recovery._is_rate_limit_error(err)


def test_is_rate_limit_error_detects_string_only():
    err = RuntimeError("upstream returned X-Kaiju-Bridge-Error: subscription_cap")
    assert recovery._is_rate_limit_error(err)


def test_is_rate_limit_error_ignores_unrelated():
    assert not recovery._is_rate_limit_error(ValueError("not a rate limit"))


def test_extract_retry_after_from_string():
    err = _FakeRateLimitError("API call failed. retry-after: 42 seconds")
    assert recovery._extract_retry_after_from_error(err) == 42


def test_extract_retry_after_returns_none_when_absent():
    err = _FakeRateLimitError("no hint here")
    assert recovery._extract_retry_after_from_error(err) is None


def test_heartbeat_creates_marker(tmp_path):
    recovery._heartbeat(tmp_path)
    assert (tmp_path / ".rate_limit_paused").exists()


def test_sleep_with_heartbeat_touches_marker(tmp_path, monkeypatch):
    """Real sleep is short; verify heartbeat fires at least once."""
    # Force the heartbeat slice to be smaller than the total sleep.
    recovery._sleep_with_heartbeat(0.05, tmp_path, heartbeat_seconds=0.02)
    assert (tmp_path / ".rate_limit_paused").exists()



def test_heartbeat_touches_agent_run_log_when_present(tmp_path):
    """Heartbeat must walk UP to find agent_run.log -- that's what the harness watchdog polls."""
    stage_dir = tmp_path / "stage1_draft"
    module_dir = stage_dir / "add_module"
    module_dir.mkdir(parents=True)
    agent_log = stage_dir / "agent_run.log"
    agent_log.write_text("existing log\n")
    # Make it old enough to verify mtime advances
    old_mtime = agent_log.stat().st_mtime
    import time
    time.sleep(0.05)
    recovery._heartbeat(module_dir)
    assert agent_log.stat().st_mtime > old_mtime
    assert (module_dir / ".rate_limit_paused").exists()


def test_heartbeat_touches_aider_log_alongside_agent_run_log(tmp_path):
    """Heartbeat must also touch any aider.log in the discovered stage subtree."""
    stage_dir = tmp_path / "stage1_draft"
    module_dir = stage_dir / "add_module"
    module_dir.mkdir(parents=True)
    (stage_dir / "agent_run.log").write_text("x\n")
    aider_log = module_dir / "aider.log"
    aider_log.write_text("y\n")
    import time
    old = aider_log.stat().st_mtime
    time.sleep(0.05)
    recovery._heartbeat(module_dir)
    assert aider_log.stat().st_mtime > old


def test_heartbeat_does_not_crash_without_agent_run_log(tmp_path):
    """If walk fails to find agent_run.log, only the marker file gets touched -- never raise."""
    recovery._heartbeat(tmp_path)
    assert (tmp_path / ".rate_limit_paused").exists()


def test_effective_max_retries_single_account(monkeypatch):
    monkeypatch.setattr(recovery, "_fetch_quota", lambda url: {"multi_account": False, "accounts": []})
    assert recovery._effective_max_retries("http://x", 1) == 1
    assert recovery._effective_max_retries("http://x", 5) == 5


def test_effective_max_retries_multi_account_pool_size_3(monkeypatch):
    monkeypatch.setattr(
        recovery, "_fetch_quota",
        lambda url: {"multi_account": True, "accounts": [{"label": f"a{i}"} for i in range(3)]},
    )
    # User asks for 1 retry, but pool=3 -> auto-bumped to 3
    assert recovery._effective_max_retries("http://x", 1) == 3
    # User asks for 10, retains higher value
    assert recovery._effective_max_retries("http://x", 10) == 10


def test_effective_max_retries_unreachable_bridge_returns_user_value(monkeypatch):
    monkeypatch.setattr(recovery, "_fetch_quota", lambda url: {})
    assert recovery._effective_max_retries("http://x", 1) == 1


def test_is_rate_limit_error_uses_real_litellm_class_when_available():
    """#5 fix: if litellm is installed, real isinstance check should work."""
    if not recovery._RATE_LIMIT_EXC_CLASSES:
        import pytest as _pt
        _pt.skip("litellm/openai not installed")
    real_cls = recovery._RATE_LIMIT_EXC_CLASSES[0]
    # Construct a minimal instance. litellm.RateLimitError needs (message, model, llm_provider).
    try:
        err = real_cls("throttled", model="x", llm_provider="anthropic")
    except TypeError:
        # openai variant has a different signature; skip if neither works trivially
        import pytest as _pt
        _pt.skip("could not instantiate canonical RateLimitError in test")
    assert recovery._is_rate_limit_error(err)

# ---------------------------------------------------------------------------
# Fix #5: Transient network-error retry
# ---------------------------------------------------------------------------


class TestTransientNetworkErrorDetection:
    def test_httpx_read_timeout_is_transient(self):
        import httpx
        from agent.claude_code.recovery import _is_transient_network_error

        assert _is_transient_network_error(httpx.ReadTimeout("x")) is True

    def test_httpx_connect_error_is_transient(self):
        import httpx
        from agent.claude_code.recovery import _is_transient_network_error

        assert _is_transient_network_error(httpx.ConnectError("x")) is True

    def test_rate_limit_is_NOT_transient(self):
        """Rate-limit errors should go down the rate-limit branch, not the
        transient branch. Otherwise we'd retry them on a 5s/10s/20s schedule
        instead of waiting for the actual reset window."""
        from agent.claude_code.recovery import _is_transient_network_error

        assert _is_transient_network_error(Exception("rate_limit_error: cap reached")) is False

    def test_message_based_transient_detection(self):
        from agent.claude_code.recovery import _is_transient_network_error

        assert _is_transient_network_error(Exception("Connection reset by peer")) is True
        assert _is_transient_network_error(Exception("server disconnected")) is True
        assert _is_transient_network_error(Exception("midstream error")) is True

    def test_unknown_exception_is_not_transient(self):
        from agent.claude_code.recovery import _is_transient_network_error

        assert _is_transient_network_error(ValueError("bad value")) is False
        assert _is_transient_network_error(KeyError("missing")) is False


class TestTransientBackoffSchedule:
    def test_default_schedule(self, monkeypatch):
        monkeypatch.delenv("KAIJU_CC_TRANSIENT_BACKOFF", raising=False)
        from agent.claude_code.recovery import _transient_backoff_schedule

        assert _transient_backoff_schedule() == (5, 10, 20)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("KAIJU_CC_TRANSIENT_BACKOFF", "2,4,8,16")
        from agent.claude_code.recovery import _transient_backoff_schedule

        assert _transient_backoff_schedule() == (2, 4, 8, 16)

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("KAIJU_CC_TRANSIENT_BACKOFF", "not,a,number")
        from agent.claude_code.recovery import _transient_backoff_schedule

        assert _transient_backoff_schedule() == (5, 10, 20)

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("KAIJU_CC_TRANSIENT_BACKOFF", "")
        from agent.claude_code.recovery import _transient_backoff_schedule

        assert _transient_backoff_schedule() == (5, 10, 20)
