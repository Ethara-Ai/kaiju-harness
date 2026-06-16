"""Universal git auth + GitHub helper for kaiju prepare_repo pipelines.

This module is the SINGLE source of truth for:
- Reading the GitHub token (GITHUB_TOKEN / GH_TOKEN / GH_PAT)
- Configuring git for non-interactive HTTPS pushes (GIT_TERMINAL_PROMPT=0,
  GIT_ASKPASS, stdin=DEVNULL) so git NEVER falls back to the
  "could not read Username for 'https://github.com'" interactive prompt
- Forking a repo into an org via gh CLI with pre-flight token / org
  permission checks, rate-limit-aware exponential backoff, and clear
  diagnostics naming the missing scopes
- Pushing a branch to a fork with token injected into the remote URL,
  followed by an `ls-remote` verification to catch silent auth fallbacks
- A shared ``git()`` subprocess wrapper that all language pipelines use,
  guaranteeing the prompt-blocking env is applied uniformly

All ``tools/prepare_repo_*.py`` scripts MUST:
1. Call ``setup_git_credentials()`` once at the start of every ``main()``
   (or once per ``prepare_*_repo()`` call when no main exists).
2. Use ``fork_repo()`` from this module instead of inline ``gh repo fork``.
3. Use ``push_to_fork()`` from this module instead of inline ``git push``
   with hand-built remote URLs.

This eliminates the entire class of failure where one language's
prepare_repo file had a token-aware push but another did not.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "GitAuthError",
    "ForkError",
    "PushError",
    "get_github_token",
    "setup_git_credentials",
    "git",
    "verify_token_scopes",
    "verify_org_write_access",
    "fork_repo",
    "push_to_fork",
]


class GitAuthError(RuntimeError):
    """Base class for git-auth errors raised by this module."""


class ForkError(GitAuthError):
    """Raised when ``gh repo fork`` cannot succeed and retry is exhausted."""


class PushError(GitAuthError):
    """Raised when ``git push`` to a fork fails or its post-verify check fails."""


_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN", "GH_PAT", "GITHUB_PAT")


_cached_resolved_token: str | None = None


def _gh_cli_token() -> str | None:
    """Return the active gh CLI keyring token, or None if unavailable.

    NOTE: ``gh auth token`` prioritizes ``GITHUB_TOKEN``/``GH_TOKEN`` env vars
    over the keyring. If a stale .env GITHUB_TOKEN is set, it would mask the
    valid keyring login and trigger HTTP 401 fallback. We strip those vars
    from the subprocess env so the keyring value is always returned.
    """
    clean_env = {k: v for k, v in os.environ.items() if k not in _TOKEN_ENV_VARS}
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _token_authenticates(token: str) -> bool:
    """Cheap pre-flight: does *token* authenticate against api.github.com?"""
    if not token:
        return False
    try:
        env = {**os.environ, "GH_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}
        result = subprocess.run(
            ["gh", "api", "user"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_github_token(required: bool = True) -> str | None:
    """Resolve a GitHub token, with auto-fallback to gh CLI keyring.

    Priority order:
    1. Env vars (GITHUB_TOKEN, GH_TOKEN, GH_PAT, GITHUB_PAT). Each is validated
       via ``gh api user`` before being accepted, so a stale .env token cannot
       silently mask a working keyring login.
    2. ``gh auth token`` (active keyring account) if no env token authenticates.

    Self-healing: when ``.env`` GITHUB_TOKEN expires, the script falls through
    to the keyring instead of failing with HTTP 401. This eliminates the manual
    workaround of inline ``GITHUB_TOKEN=$(gh auth token) ./script.py``.

    The resolved token is cached per process to avoid hitting ``gh api user``
    on every call. Pass ``required=False`` to get ``None`` instead of an
    exception when nothing works.
    """
    global _cached_resolved_token
    if _cached_resolved_token:
        return _cached_resolved_token

    invalid_env_tokens: list[str] = []
    for var in _TOKEN_ENV_VARS:
        token = os.environ.get(var)
        if not token:
            continue
        if _token_authenticates(token):
            _cached_resolved_token = token
            return token
        invalid_env_tokens.append(var)

    if invalid_env_tokens:
        logger.warning(
            "Env GitHub token(s) %s failed validation; falling back to gh CLI keyring",
            ", ".join(invalid_env_tokens),
        )

    keyring_token = _gh_cli_token()
    if keyring_token and _token_authenticates(keyring_token):
        if invalid_env_tokens:
            logger.info("Using gh CLI keyring token (env token rejected)")
        _cached_resolved_token = keyring_token
        return keyring_token

    if required:
        raise GitAuthError(
            "No valid GitHub token available.\n"
            "  Tried env vars: " + ", ".join(_TOKEN_ENV_VARS) + "\n"
            "  Tried gh CLI keyring: empty or unauthenticated\n"
            "Fix one of:\n"
            "  - Run 'gh auth login' to authenticate the CLI keyring\n"
            "  - Export a valid Classic PAT (repo + admin:org scopes) from "
            "https://github.com/settings/tokens"
        )
    return None


_CREDENTIALS_CONFIGURED = False
_ASKPASS_PATH: Path | None = None


def setup_git_credentials(
    token: str | None = None,
    dry_run: bool = False,
) -> str | None:
    """Configure the current process for non-interactive git+gh operations.

    Sets:
    - ``GIT_TERMINAL_PROMPT=0`` so git refuses to prompt and fails fast
      instead of hanging on the "could not read Username" line that has
      been silently dropping Java / Rust / C / C++ pushes onto stdin.
    - ``GIT_ASKPASS`` pointing at a 0700 tempfile that echoes the token,
      so HTTPS operations git decides to authenticate against will get
      the token without writing it to ``~/.git-credentials`` (which is
      shared global state across all repos on the host).
    - ``GH_TOKEN`` and ``GITHUB_TOKEN`` env vars so the ``gh`` CLI picks
      up the token without prompting for a login flow.

    Idempotent. Safe to call from every ``prepare_repo_*`` entry point.
    The askpass tempfile is registered for cleanup at interpreter exit.

    If ``dry_run`` is True and no token is set, returns None without
    raising. The prompt-blocking env vars are still set so any git
    operation that does sneak through will fail loudly instead of
    hanging.
    """
    global _CREDENTIALS_CONFIGURED, _ASKPASS_PATH

    if _CREDENTIALS_CONFIGURED:
        return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if token is None:
        token = get_github_token(required=not dry_run)

    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    if not token:
        _CREDENTIALS_CONFIGURED = True
        logger.info(
            "Configured git in non-interactive mode (no token; dry-run only)"
        )
        return None

    os.environ["GH_TOKEN"] = token
    os.environ["GITHUB_TOKEN"] = token

    fd, name = tempfile.mkstemp(prefix="kaiju-askpass-", suffix=".sh", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(f'#!/bin/sh\nprintf "%s" "{token}"\n')
    askpass = Path(name)
    askpass.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    os.environ["GIT_ASKPASS"] = str(askpass)

    _ASKPASS_PATH = askpass

    def _cleanup_askpass() -> None:
        try:
            if _ASKPASS_PATH is not None:
                _ASKPASS_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    atexit.register(_cleanup_askpass)

    _CREDENTIALS_CONFIGURED = True
    logger.info(
        "Configured git credentials non-interactively "
        "(GIT_ASKPASS=%s, GIT_TERMINAL_PROMPT=0)",
        askpass,
    )
    return token


def git(
    repo_dir: Path | str,
    *args: str,
    check: bool = True,
    timeout: int = 120,
    capture_output: bool = True,
) -> str:
    """Run a ``git`` command in ``repo_dir`` and return stdout.

    Replacement for the local ``git()`` helper each prepare_repo file has
    been copying. Always closes stdin and inherits
    ``GIT_TERMINAL_PROMPT=0`` so the process can never deadlock on a
    credential prompt.

    Raises ``subprocess.CalledProcessError`` (when ``check=True``) with
    stdout / stderr populated, so callers can inspect ``e.stderr``.
    """
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=check,
    )
    return (result.stdout or "").strip()


def _gh(
    args: Sequence[str],
    *,
    timeout: int = 30,
    token: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command with token in env and stdin closed."""
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return subprocess.run(
        ["gh", *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def verify_token_scopes(token: str | None = None) -> dict:
    """Call ``gh api user`` to confirm the token authenticates.

    Returns the user JSON dict. Raises ``GitAuthError`` with a diagnostic
    message if the token is invalid, expired, or revoked. This is the
    cheapest available pre-flight check and must run before any fork
    work to avoid burning minutes on doomed pipelines.
    """
    token = token or get_github_token()
    result = _gh(["api", "user"], token=token)
    if result.returncode != 0:
        raise GitAuthError(
            "GitHub token validation failed.\n"
            f"  gh api user -> exit {result.returncode}\n"
            f"  stderr: {result.stderr.strip()}\n"
            "Check that GITHUB_TOKEN is valid and not expired. "
            "Regenerate at https://github.com/settings/tokens."
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitAuthError(
            f"gh api user returned non-JSON output: {result.stdout[:200]!r}"
        ) from exc

def _get_authenticated_login(token: str | None = None) -> str | None:
    """Return the authenticated user's GitHub login, or None on failure."""
    result = _gh(["api", "user", "--jq", ".login"], token=token)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_self_account(name: str, token: str | None = None) -> bool:
    """Return True if ``name`` is the authenticated user's own login."""
    own = _get_authenticated_login(token)
    return bool(own) and own.lower() == name.lower()

def verify_org_write_access(org: str, token: str | None = None) -> None:
    """Verify the authenticated user can fork into ``org``.

    Tries ``user/memberships/orgs/{org}`` first. If that 404s (which
    commonly happens when the token has ``repo`` but not ``read:org``)
    we fall back to probing ``orgs/{org}`` so we can at least
    distinguish "org doesn't exist" from "scope is missing".

    Raises ``ForkError`` only when the org is provably inaccessible.
    Logs a warning and lets the actual fork call surface the real
    error when the ambiguity is "scope missing but org exists".
    """
    token = token or get_github_token()
    result = _gh(["api", f"user/memberships/orgs/{org}"], token=token)
    if result.returncode == 0:
        try:
            membership = json.loads(result.stdout)
        except json.JSONDecodeError:
            membership = {}
        state = membership.get("state")
        if state and state != "active":
            raise ForkError(
                f"Membership in '{org}' is not active (state={state!r}). "
                f"Cannot fork repos there."
            )
        return

    org_probe = _gh(["api", f"orgs/{org}"], token=token)
    if org_probe.returncode != 0:
        if _is_self_account(org, token):
            logger.info(
                "Fork target '%s' is the authenticated user's own account; "
                "skipping org membership check.",
                org,
            )
            return
        raise ForkError(
            f"Cannot fork into '{org}': pre-flight check failed.\n"
            f"  user/memberships/orgs/{org} -> exit {result.returncode}: "
            f"{result.stderr.strip()}\n"
            f"  orgs/{org} -> exit {org_probe.returncode}: "
            f"{org_probe.stderr.strip()}\n"
            "Possible causes:\n"
            "  - Token lacks 'admin:org' or 'read:org' scope\n"
            "  - Token lacks 'repo' or 'public_repo' scope\n"
            f"  - Org '{org}' does not exist, is private, or is "
            "inaccessible to this token\n"
            f"  - You are not a member of '{org}'\n"
            "Generate a Classic PAT at "
            "https://github.com/settings/tokens with 'repo' and "
            "'admin:org' scopes."
        )
    logger.warning(
        "Cannot read membership in '%s' (token may lack read:org scope), "
        "but org exists; proceeding with fork attempt.",
        org,
    )


_TRANSIENT_PATTERNS = (
    "rate limit",
    "secondary rate limit",
    "api rate limit",
    "502 bad gateway",
    " 502",
    " 503",
    " 504",
    "connection reset",
    "connection refused",
    "i/o timeout",
    "request timeout",
    "temporary failure",
    "no route to host",
)


def _looks_transient(stderr: str) -> bool:
    s = stderr.lower()
    return any(pat in s for pat in _TRANSIENT_PATTERNS)


def fork_repo(
    upstream: str,
    org: str,
    token: str | None = None,
    *,
    max_retries: int = 5,
    backoff_base: float = 2.0,
    preflight_org_check: bool = True,
    wait_seconds: int = 60,
) -> str:
    """Fork ``upstream`` into ``org`` via gh CLI, with retry + clear errors.

    Returns the fork's full name (e.g. ``"Zahgon/fmt"``).

    Behaviors:
      * If the fork already exists and was forked from ``upstream``, the
        existing name is returned without re-forking.
      * If a repo named ``<org>/<name>`` exists but its parent is a
        different upstream, raises ``ForkError`` (name collision).
      * Pre-flight checks token validity + org write access (skip with
        ``preflight_org_check=False`` when the caller has already done it).
      * On rate-limit / 5xx / network errors, retries with exponential
        backoff (30s, 60s, 120s, 240s, 480s by default).
      * On permission errors (non-transient), raises ``ForkError`` with
        a diagnostic message naming likely missing scopes.

    This is the single fork entry point all prepare_repo_*.py files must
    use; the previous pattern of every file defining its own ``fork_repo``
    with subtly different retry counts and zero-scope diagnostics is what
    let Category A (9 Rust builds) silently fail with no actionable error.
    """
    setup_git_credentials(token)
    token = token or get_github_token()
    repo_name = upstream.split("/")[-1]
    fork_name = f"{org}/{repo_name}"
    is_self = _is_self_account(org, token)

    if preflight_org_check:
        verify_token_scopes(token)
        if not is_self:
            verify_org_write_access(org, token)

    check = _gh(["api", f"repos/{fork_name}"], token=token)
    if check.returncode == 0:
        try:
            data = json.loads(check.stdout)
            parent_full = (data.get("parent") or {}).get("full_name", "")
        except (json.JSONDecodeError, KeyError):
            parent_full = ""
        if parent_full and parent_full.lower() != upstream.lower():
            raise ForkError(
                f"Fork name collision: {fork_name} exists but is forked "
                f"from {parent_full!r}, not {upstream!r}. Refusing to "
                "push into an unrelated fork."
            )
        logger.info("Fork %s already exists", fork_name)
        return fork_name

    last_err = ""
    fork_args = ["repo", "fork", upstream, "--clone=false"]
    if not is_self:
        fork_args.extend(["--org", org])
    for attempt in range(max_retries):
        result = _gh(
            fork_args,
            timeout=60,
            token=token,
        )
        if result.returncode == 0:
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                v = _gh(["api", f"repos/{fork_name}"], token=token)
                if v.returncode == 0:
                    logger.info("Fork %s ready", fork_name)
                    return fork_name
                time.sleep(2)
            raise ForkError(
                f"Fork {fork_name} created by gh but not queryable after "
                f"{wait_seconds}s. The fork API may be lagging; retry the "
                "pipeline."
            )

        last_err = (
            f"exit {result.returncode}\n  stderr: {result.stderr.strip()}"
        )

        if _looks_transient(result.stderr):
            wait = backoff_base ** attempt * 30
            logger.warning(
                "Transient fork failure (%s); sleeping %.0fs before "
                "retry %d/%d",
                result.stderr.strip().splitlines()[0]
                if result.stderr.strip()
                else "unknown",
                wait,
                attempt + 1,
                max_retries,
            )
            time.sleep(wait)
            continue

        raise ForkError(
            f"gh repo fork {upstream} -> {org} failed (non-retryable):\n"
            f"  {last_err}\n"
            "Possible causes:\n"
            "  - Token lacks 'repo' or 'admin:org' scope\n"
            f"  - Org '{org}' policy blocks forks from non-members\n"
            f"  - Upstream '{upstream}' is private or returns 404\n"
            "  - Fork name collision with an unrelated repo in the org"
        )

    raise ForkError(
        f"gh repo fork {upstream} -> {org} exhausted retry budget after "
        f"{max_retries} attempts. Last error:\n  {last_err}"
    )


def push_to_fork(
    repo_dir: Path | str,
    fork_name: str,
    branch: str,
    *,
    token: str | None = None,
    remote_name: str = "fork",
    force_with_lease: bool = True,
    timeout: int = 300,
    verify: bool = True,
) -> None:
    """Push ``branch`` from ``repo_dir`` to the fork using a token-injected URL.

    Behavior:
      * Configures (or resets) ``remote_name`` to an HTTPS URL containing
        ``x-access-token:<token>`` so git authenticates without
        consulting ``~/.git-credentials`` or the askpass helper.
      * Pre-fetches the remote branch (best-effort) so
        ``--force-with-lease`` has a known reference; this lets the
        same call work both for first-push (branch doesn't exist on
        remote) and update-push (branch exists with a known SHA).
      * Verifies the branch landed on the remote via a separate
        ``ls-remote`` call. This catches the failure mode where a
        misconfigured credential helper silently returns success from
        the local push while never reaching the server.
      * Always resets ``remote_name`` to a token-free URL afterward, so
        the token never ends up in ``.git/config``.

    Args:
    ----
        repo_dir: Local repository directory.
        fork_name: ``"owner/repo"`` of the fork (e.g. ``"Zahgon/fmt"``).
        branch: Branch name to push.
        token: PAT; falls back to GITHUB_TOKEN env var.
        remote_name: Local remote name. Default ``"fork"`` matches what
            the existing prepare_repo.py / prepare_repo_go.py /
            prepare_repo_java.py call sites use; pass ``"origin"`` when
            replacing inline ``git push origin <branch>`` patterns
            (e.g. prepare_repo_rust.py, prepare_repo_cpp.py).
        force_with_lease: Default True. Set False only for first-push
            scenarios where you want plain ``--force``.
        timeout: Push timeout in seconds.
        verify: Default True. Skipping verify is supported for tests but
            must NOT be done in production pipelines - it's the
            mechanism that catches Category G "silent auth fallback".

    """
    setup_git_credentials(token)
    token = token or get_github_token()
    repo_dir = Path(repo_dir)

    fork_url = f"https://x-access-token:{token}@github.com/{fork_name}.git"
    clean_url = f"https://github.com/{fork_name}.git"

    existing = git(
        repo_dir, "remote", "get-url", remote_name, check=False
    ).strip()
    if existing:
        git(
            repo_dir, "remote", "set-url", remote_name, fork_url, check=False
        )
    else:
        git(
            repo_dir, "remote", "add", remote_name, fork_url, check=False
        )

    try:
        git(
            repo_dir,
            "fetch",
            remote_name,
            branch,
            "--no-tags",
            check=False,
            timeout=60,
        )

        push_args = ["push", "-u", remote_name, branch]
        push_args.append(
            "--force-with-lease" if force_with_lease else "--force"
        )

        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        result = subprocess.run(
            ["git", *push_args],
            cwd=str(repo_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            stderr_snippet = result.stderr.strip()[:1500]
            raise PushError(
                f"git push to {fork_name} (branch={branch}) failed:\n"
                f"  exit {result.returncode}\n"
                f"  stderr: {stderr_snippet}\n"
                "Possible causes:\n"
                f"  - Token lacks write access to {fork_name}\n"
                "  - Branch protection on the fork blocks force push\n"
                "  - --force-with-lease saw a remote update "
                "(re-fetch and retry)\n"
                "  - Network failure"
            )

        if verify:
            verify_result = subprocess.run(
                ["git", "ls-remote", "--heads", fork_url, branch],
                cwd=str(repo_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if (
                verify_result.returncode != 0
                or not verify_result.stdout.strip()
            ):
                raise PushError(
                    f"Push appeared to succeed but branch {branch!r} is "
                    f"not visible on {fork_name} after push "
                    f"(ls-remote exit {verify_result.returncode}).\n"
                    f"  ls-remote stderr: "
                    f"{verify_result.stderr.strip()}\n"
                    "This usually indicates a silent auth fallback or "
                    "branch protection rejecting the push."
                )

        logger.info(
            "Pushed %s -> %s (branch=%s, verified=%s)",
            branch,
            fork_name,
            branch,
            verify,
        )
    finally:
        git(
            repo_dir,
            "remote",
            "set-url",
            remote_name,
            clean_url,
            check=False,
        )
