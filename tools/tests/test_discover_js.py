from __future__ import annotations

from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

import tools.discover_js as discover_js
from tools.discover_js import _safe_request


BAD_SCHEMES = [
    "file:///etc/passwd",
    "ftp://x.example/zzz",
    "data:text/plain,x",
    "javascript:alert(1)",
    "gopher://x/",
    "ssh://user@host/",
]


class TestS002SchemeAllowlist:
    @pytest.mark.parametrize("bad_url", BAD_SCHEMES)
    def test_rejects_non_http_schemes(self, bad_url: str) -> None:
        with pytest.raises(ValueError, match="Refusing non-http"):
            _safe_request(bad_url, headers={})

    def test_accepts_uppercase_host_after_normalisation(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"{}"
        with patch("tools.discover_js.urlopen", return_value=mock_resp):
            data = _safe_request("https://API.GitHub.com/foo", headers={})
        assert data == b"{}"

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.github.com:443/foo",
            "https://registry.npmjs.org:8080/x",
            "https://api.github.com:80/foo",
        ],
    )
    def test_rejects_explicit_port(self, url: str) -> None:
        with patch("tools.discover_js.urlopen") as mock_urlopen:
            with pytest.raises(ValueError, match="explicit port"):
                _safe_request(url, headers={})
        mock_urlopen.assert_not_called()


class TestHostAllowlist:
    def test_rejects_arbitrary_https_host(self) -> None:
        with pytest.raises(ValueError, match="Refusing unexpected host"):
            _safe_request("https://evil.com/x", headers={})

    def test_rejects_github_com_without_api_subdomain(self) -> None:
        with pytest.raises(ValueError, match="Refusing unexpected host"):
            _safe_request("https://github.com/anything", headers={})

    def test_accepts_api_github_com(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"items":[]}'
        with patch("tools.discover_js.urlopen", return_value=mock_resp):
            data = _safe_request(
                "https://api.github.com/search/repositories?q=x",
                headers={"Accept": "application/vnd.github+json"},
            )
        assert data == b'{"items":[]}'

    def test_accepts_registry_npmjs_org(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"dist-tags":{"latest":"1.0.0"}}'
        with patch("tools.discover_js.urlopen", return_value=mock_resp):
            data = _safe_request(
                "https://registry.npmjs.org/lodash", headers={}
            )
        assert b"latest" in data

    def test_accepts_api_npmjs_org(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"{}"
        with patch("tools.discover_js.urlopen", return_value=mock_resp):
            data = _safe_request(
                "https://api.npmjs.org/downloads/point/last-week/lodash",
                headers={},
            )
        assert data == b"{}"


class TestAllowlistInvariants:
    def test_only_http_and_https_allowed(self) -> None:
        assert discover_js._ALLOWED_SCHEMES == frozenset({"http", "https"})

    def test_github_hosts_does_not_include_bare_github_com(self) -> None:
        assert "github.com" not in discover_js._GITHUB_HOSTS

    def test_npm_hosts_includes_registry(self) -> None:
        assert "registry.npmjs.org" in discover_js._NPM_HOSTS


class TestGhRequestParsesJson:
    def test_gh_request_returns_dict(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"total_count":3,"items":[]}'
        with patch("tools.discover_js.urlopen", return_value=mock_resp):
            data = discover_js._gh_request(
                "https://api.github.com/search/repositories?q=x"
            )
        assert data == {"total_count": 3, "items": []}

    def test_gh_request_with_token_sets_authorization(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"{}"
        with patch("tools.discover_js.urlopen", return_value=mock_resp) as mock_urlopen:
            discover_js._gh_request(
                "https://api.github.com/x", token="ghp_test"
            )
        req = mock_urlopen.call_args.args[0]
        assert req.headers.get("Authorization") == "Bearer ghp_test"

    def test_gh_request_without_token_no_auth_header(self) -> None:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"{}"
        with patch("tools.discover_js.urlopen", return_value=mock_resp) as mock_urlopen:
            discover_js._gh_request("https://api.github.com/x")
        req = mock_urlopen.call_args.args[0]
        assert req.headers.get("Authorization") is None


class TestSearchSkipTopics:
    def test_awesome_lists_are_skipped(self) -> None:
        assert "awesome" in discover_js.SKIP_REPO_TOPICS
        assert "tutorial" in discover_js.SKIP_REPO_TOPICS
        assert "cheatsheet" in discover_js.SKIP_REPO_TOPICS


class TestSafeRequestNoNetworkOnBadInput:
    @pytest.mark.parametrize("bad_url", BAD_SCHEMES)
    def test_no_urlopen_call_on_bad_scheme(self, bad_url: str) -> None:
        with patch("tools.discover_js.urlopen") as mock_urlopen:
            with pytest.raises(ValueError):
                _safe_request(bad_url, headers={})
        mock_urlopen.assert_not_called()

    def test_no_urlopen_call_on_bad_host(self) -> None:
        with patch("tools.discover_js.urlopen") as mock_urlopen:
            with pytest.raises(ValueError):
                _safe_request("https://malicious.example.com/", headers={})
        mock_urlopen.assert_not_called()


class TestNoUrlBypassesSafeRequest:
    def test_gh_request_routes_through_safe_request(self) -> None:
        with patch("tools.discover_js._safe_request") as mock_safe:
            mock_safe.return_value = b"{}"
            discover_js._gh_request("https://api.github.com/x")
        mock_safe.assert_called_once()

    def test_npm_request_routes_through_safe_request(self) -> None:
        with patch("tools.discover_js._safe_request") as mock_safe:
            mock_safe.return_value = b"{}"
            discover_js._npm_request("https://registry.npmjs.org/x")
        mock_safe.assert_called_once()


class TestNpmRequestPropagatesValueError:
    def test_bad_scheme_raises_through_npm_request(self) -> None:
        with pytest.raises(ValueError, match="Refusing non-http"):
            discover_js._npm_request("file:///etc/passwd")

    def test_bad_host_raises_through_npm_request(self) -> None:
        with pytest.raises(ValueError, match="Refusing unexpected host"):
            discover_js._npm_request("https://evil.example.com/x")

    def test_enrich_npm_does_not_swallow_value_error(self) -> None:
        candidate: dict = {}
        with patch(
            "tools.discover_js._fetch_package_json",
            return_value={"name": "@bad/../traversal"},
        ):
            with patch(
                "tools.discover_js._npm_request",
                side_effect=ValueError("Refusing unexpected host"),
            ):
                with pytest.raises(ValueError, match="Refusing unexpected host"):
                    discover_js._enrich_npm(candidate, "x/y", "main", None)


def _make_http_error(code: int, url: str = "https://api.github.com/x") -> HTTPError:
    return HTTPError(url=url, code=code, msg="boom", hdrs=Message(), fp=None)


class TestRateLimitRetry:
    def test_403_retries_via_gh_request_then_succeeds(self) -> None:
        """QC-C8-001: rate-limit retry now lives INSIDE the bounded _gh_request
        wrapper (waits until X-RateLimit-Reset, capped at `retries`), not an
        unbounded fixed-60s loop in _search_js_repos. Drive it via the real HTTP
        seam (_safe_request) so the wrapper's retry actually runs."""
        rate_limited = _make_http_error(403)
        success_body = b'{"items": [], "total_count": 0}'
        call_count = {"n": 0}

        def _fake_safe_request(_url, _headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise rate_limited
            return success_body

        sleep_calls: list[float] = []
        with (
            patch.object(discover_js, "_safe_request", _fake_safe_request),
            patch.object(
                discover_js.time, "sleep", lambda secs: sleep_calls.append(secs)
            ),
        ):
            out = discover_js._gh_request("https://api.github.com/x", None)

        assert call_count["n"] == 2  # retried once after the 403
        assert sleep_calls  # waited (bounded) before retrying
        assert out == {"items": [], "total_count": 0}

    def test_non_403_httperror_propagates_without_sleep(self) -> None:
        unauthorised = _make_http_error(401)

        def _fake_gh(_url, _token=None):
            raise unauthorised

        with (
            patch.object(discover_js, "_gh_request", _fake_gh),
            patch.object(discover_js.time, "sleep") as mock_sleep,
            pytest.raises(HTTPError),
        ):
            discover_js._search_js_repos(
                min_stars=100, max_results=1, token=None
            )
        mock_sleep.assert_not_called()

    def test_gh_request_has_bounded_retry_cap(self) -> None:
        """QC-C8-001 CLOSED the old 'no max-retries cap' gap: _gh_request now
        bounds retries and raises RuntimeError rather than looping forever on a
        persistent 403."""
        import inspect

        source = inspect.getsource(discover_js._gh_request)
        assert "retries" in source
        assert "exhausted" in source and "RuntimeError" in source

        with (
            patch.object(
                discover_js, "_safe_request", side_effect=_make_http_error(403)
            ),
            patch.object(discover_js.time, "sleep", lambda secs: None),
        ):
            with pytest.raises(RuntimeError, match="exhausted"):
                discover_js._gh_request(
                    "https://api.github.com/x", None, retries=3
                )
        assert "max_retries" not in source.lower(), (
            "if a max-retries cap is added, update this test to assert the new "
            "bound rather than asserting the current unbounded loop"
        )
