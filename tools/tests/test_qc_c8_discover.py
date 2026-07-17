"""QC-C8-001 parity tests: SSRF allowlist + bounded GitHub rate-limit backoff.

discover_js.py was the only discovery tool with a scheme/host allowlist
(``_safe_request``) and a bounded, X-RateLimit-Reset-aware ``_gh_request``.
discover_c/go/java lacked it (SSRF + unbounded 60s retry loop). These tests
pin the invariant across EVERY discovery entrypoint so the defense cannot
regress or drift per-language again.
"""
from __future__ import annotations

import email.message
from urllib.error import HTTPError

import pytest

from tools import discover_c, discover_go, discover_js, discover_java

# The three urllib-based tools share the identical _safe_request/_gh_request
# defense. discover_java goes through requests + the gh CLI, so it is asserted
# separately via _validate_host / _owner_repo_from_url.
_URLLIB_MODULES = [discover_c, discover_go, discover_js]


@pytest.mark.parametrize("mod", _URLLIB_MODULES, ids=lambda m: m.__name__)
def test_safe_request_refuses_non_http_scheme(mod):
    with pytest.raises(ValueError, match="scheme"):
        mod._safe_request("ftp://api.github.com/x", headers={})
    with pytest.raises(ValueError, match="scheme"):
        mod._safe_request("file:///etc/passwd", headers={})


@pytest.mark.parametrize("mod", _URLLIB_MODULES, ids=lambda m: m.__name__)
def test_safe_request_refuses_unlisted_host(mod):
    with pytest.raises(ValueError, match="host"):
        mod._safe_request("https://evil.example.com/x", headers={})


@pytest.mark.parametrize("mod", _URLLIB_MODULES, ids=lambda m: m.__name__)
def test_safe_request_refuses_explicit_port(mod):
    with pytest.raises(ValueError, match="port"):
        mod._safe_request("https://api.github.com:8080/x", headers={})


@pytest.mark.parametrize("mod", _URLLIB_MODULES, ids=lambda m: m.__name__)
def test_safe_request_allows_github_host(mod):
    # api.github.com is allowlisted; the ValueError guards must NOT fire (we stub
    # urlopen so no real network call happens).
    calls = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            calls["read"] = True
            return b"{}"

    def _fake_urlopen(req, timeout=30):
        calls["url"] = req.full_url
        return _Resp()

    original = mod.urlopen
    mod.urlopen = _fake_urlopen
    try:
        body = mod._safe_request("https://api.github.com/x", headers={})
    finally:
        mod.urlopen = original
    assert body == b"{}"
    assert calls.get("read") is True


@pytest.mark.parametrize("mod", _URLLIB_MODULES, ids=lambda m: m.__name__)
def test_gh_request_bounds_persistent_403(mod, monkeypatch):
    """A persistent 403 must raise RuntimeError after a BOUNDED number of
    retries — never loop forever (the old fixed-60s ``continue`` loop)."""
    attempts = {"n": 0}

    def _always_403(url, headers, *a, **kw):
        attempts["n"] += 1
        hdrs = email.message.Message()
        hdrs["X-RateLimit-Reset"] = "0"
        raise HTTPError(url, 403, "rate limited", hdrs, None)

    monkeypatch.setattr(mod, "_safe_request", _always_403)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_kw: None)

    with pytest.raises(RuntimeError, match="exhausted"):
        mod._gh_request("https://api.github.com/x", token=None, retries=5)
    assert attempts["n"] == 5  # bounded, not infinite


def test_java_validate_host_refuses_bad_scheme_and_host():
    with pytest.raises(ValueError, match="scheme"):
        discover_java._validate_host("ftp://search.maven.org/x", discover_java._MAVEN_HOSTS)
    with pytest.raises(ValueError, match="host"):
        discover_java._validate_host("https://evil.example.com/x", discover_java._MAVEN_HOSTS)
    with pytest.raises(ValueError, match="port"):
        discover_java._validate_host("https://search.maven.org:9/x", discover_java._MAVEN_HOSTS)
    # A well-formed allowlisted URL passes.
    discover_java._validate_host(discover_java.MAVEN_SEARCH_URL, discover_java._MAVEN_HOSTS)


def test_java_owner_repo_rejects_injection():
    # Clean slug is accepted.
    assert discover_java._owner_repo_from_url("https://github.com/owner/repo") == "owner/repo"
    # Anything that could splice into a `gh api` path / CLI arg is refused.
    for bad in (
        "https://github.com/owner/repo; rm -rf /",
        "https://github.com/../../etc",
        "https://github.com/owner/repo?ref=x",
        "https://github.com/owner",  # no repo component
        "https://github.com/owner/repo/extra",
    ):
        assert discover_java._owner_repo_from_url(bad) is None
