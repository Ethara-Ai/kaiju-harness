"""Regression guard for GitHub token resolution robustness (tools/_git_auth.py).

Background: a real run aborted at [1/5] prepare with "No valid GitHub token
available … gh CLI keyring: empty or unauthenticated" even though `gh auth
status` showed a valid, active keyring login. The keyring token was fine; the
harness's *validation* probe (`gh api user`) had failed for a NON-auth reason
(network blip / slow-or-locked keychain), and the old code discarded the good
token and hard-failed the whole run.

The fix classifies a probe as "ok" | "rejected" | "transient" and:
  * uses a token on "ok";
  * on a "transient" keyring probe, PROCEEDS with the token gh vouched for
    (a later git push fails loudly if it is truly bad) instead of aborting;
  * only treats a definitive "rejected" (Bad credentials / 401) as a strike.
"""
import tools._git_auth as ga


def _reset():
    ga._cached_resolved_token = None


def test_ok_keyring_token_is_used(monkeypatch):
    _reset()
    for v in ga._TOKEN_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ga, "_gh_cli_token", lambda: "gho_ok")
    monkeypatch.setattr(ga, "_probe_token", lambda t: "ok")
    assert ga.get_github_token(required=True) == "gho_ok"


def test_transient_keyring_probe_proceeds_not_aborts(monkeypatch):
    """The core fix: a network/keychain blip must NOT kill the run."""
    _reset()
    for v in ga._TOKEN_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ga, "_gh_cli_token", lambda: "gho_vouched")
    monkeypatch.setattr(ga, "_probe_token", lambda t: "transient")
    assert ga.get_github_token(required=True) == "gho_vouched"


def test_rejected_keyring_token_raises_actionable(monkeypatch):
    _reset()
    for v in ga._TOKEN_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ga, "_gh_cli_token", lambda: "gho_bad")
    monkeypatch.setattr(ga, "_probe_token", lambda t: "rejected")
    try:
        ga.get_github_token(required=True)
        assert False, "should have raised on a rejected keyring token"
    except ga.GitAuthError as e:
        assert "REJECTED by GitHub" in str(e)


def test_no_auth_at_all_raises_no_token(monkeypatch):
    _reset()
    for v in ga._TOKEN_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(ga, "_gh_cli_token", lambda: None)
    monkeypatch.setattr(ga, "_probe_token", lambda t: "transient")
    try:
        ga.get_github_token(required=True)
        assert False, "should have raised when nothing is available"
    except ga.GitAuthError as e:
        assert "no token" in str(e)


def test_stale_env_token_self_heals_to_keyring(monkeypatch):
    """A rejected env token must fall through to a good keyring token."""
    _reset()
    for v in ga._TOKEN_ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_stale")
    monkeypatch.setattr(ga, "_gh_cli_token", lambda: "gho_good")
    monkeypatch.setattr(
        ga, "_probe_token",
        lambda t: "rejected" if t == "ghp_stale" else "ok",
    )
    assert ga.get_github_token(required=True) == "gho_good"


def test_probe_classifies_bad_credentials_as_rejected(monkeypatch):
    """A 'Bad credentials' response must be 'rejected', never 'transient'."""
    class _R:
        returncode = 1
        stdout = '{"message":"Bad credentials"}'
        stderr = ""

    monkeypatch.setattr(ga.subprocess, "run", lambda *a, **k: _R())
    assert ga._probe_token("ghp_whatever") == "rejected"


def test_probe_classifies_network_error_as_transient(monkeypatch):
    """A non-auth failure (5xx / connection reset) must be 'transient'."""
    class _R:
        returncode = 1
        stdout = ""
        stderr = "error connecting to api.github.com: connection reset by peer"

    monkeypatch.setattr(ga.subprocess, "run", lambda *a, **k: _R())
    assert ga._probe_token("gho_maybegood") == "transient"


def test_probe_gh_missing_is_transient(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(ga.subprocess, "run", _boom)
    assert ga._probe_token("gho_x") == "transient"
