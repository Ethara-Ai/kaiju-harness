"""Claude Code OAuth credentials: read from system stores + refresh.

The official ``claude`` CLI stores credentials under the keychain service
``Claude Code-credentials`` on macOS (and ``~/.claude/.credentials.json`` on
Linux). The payload shape is::

    {"claudeAiOauth": {
        "accessToken":     "sk-ant-oat01-...",
        "refreshToken":    "sk-ant-ort01-...",
        "expiresAt":       1782402066667,    # Unix ms
        "scopes":          ["user:inference", ...],
        "subscriptionType": "max"
    }}

Sources are tried in this priority order:

  1. ``CLAUDE_CODE_CREDENTIALS`` env var (inline JSON, for tests/CI).
  2. ``KAIJU_CC_CREDS_PATH`` env var (path to JSON file, for overrides).
  3. macOS Keychain (``security find-generic-password -s ...``).
  4. ``~/.claude/.credentials.json`` (Linux fallback).
  5. ``~/.cache/kaiju-harness/claude_creds.json`` (bridge refresh cache, last).

When a refresh happens, we write the rotated token to the bridge cache (5) by
default. IMPORTANT: Anthropic rotates the *refresh* token on every grant, so a
bridge self-refresh invalidates the refresh token still stored in Keychain and
used by the real ``claude`` CLI -- the next ``claude`` invocation would 401 and
log the user out. To avoid that, set ``KAIJU_CC_WRITE_BACK_KEYCHAIN=1`` so the
bridge pushes the rotated credential back into Keychain (best-effort, macOS
only); otherwise the bridge logs a loud warning on each self-refresh. The most
robust setup is to give the bridge its own dedicated account so it never shares
a rotating grant with the interactive CLI.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

_LOG = logging.getLogger(__name__)

# Public Claude Code client identifier (same value ships in every release of
# the `claude` CLI; required for the OAuth refresh-token grant).
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_ENDPOINT = "https://console.anthropic.com/v1/oauth/token"
REFRESH_LEEWAY_SECONDS = 60

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CACHE_PATH = Path.home() / ".cache" / "kaiju-harness" / "claude_creds.json"


class CredentialsError(RuntimeError):
    """Raised when credentials cannot be loaded or refreshed."""


@dataclass
class OAuthCredentials:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: list[str]
    subscription_type: Optional[str] = None

    @classmethod
    def from_claude_payload(cls, payload: dict) -> "OAuthCredentials":
        cc = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        cc = cc or payload
        try:
            return cls(
                access_token=cc["accessToken"],
                refresh_token=cc["refreshToken"],
                expires_at_ms=int(cc["expiresAt"]),
                scopes=list(cc.get("scopes") or []),
                subscription_type=cc.get("subscriptionType"),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise CredentialsError(f"Malformed Claude Code credentials: {e}") from e

    def to_claude_payload(self) -> dict:
        return {
            "claudeAiOauth": {
                "accessToken": self.access_token,
                "refreshToken": self.refresh_token,
                "expiresAt": self.expires_at_ms,
                "scopes": self.scopes,
                "subscriptionType": self.subscription_type,
            }
        }

    def is_expired(self, leeway_seconds: int = REFRESH_LEEWAY_SECONDS) -> bool:
        return time.time() >= (self.expires_at_ms / 1000.0) - leeway_seconds


def _read_inline_env() -> Optional[str]:
    raw = os.environ.get("CLAUDE_CODE_CREDENTIALS")
    return raw if raw else None


def _read_credentials_file() -> Optional[str]:
    candidates: list[str] = []
    env_path = os.environ.get("KAIJU_CC_CREDS_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.append(str(Path.home() / ".claude" / ".credentials.json"))
    for c in candidates:
        p = Path(c).expanduser()
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8")
            except OSError as e:
                _LOG.debug("credentials file %s read failed: %s", p, e)
    return None


def _read_keychain_macos() -> Optional[str]:
    if platform.system() != "Darwin":
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.debug("keychain read failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def _read_cache_file() -> Optional[str]:
    if _CACHE_PATH.is_file():
        try:
            return _CACHE_PATH.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def load_credentials() -> OAuthCredentials:
    raw = (
        _read_inline_env()
        or _read_credentials_file()
        or _read_keychain_macos()
        or _read_cache_file()
    )
    if not raw:
        raise CredentialsError(
            "No Claude Code credentials found. Sign in via the `claude` CLI "
            "first, then verify with:\n"
            "  security find-generic-password -s 'Claude Code-credentials' -w"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialsError(f"Credentials are not valid JSON: {e}") from e
    return OAuthCredentials.from_claude_payload(payload)


def refresh_credentials(
    creds: OAuthCredentials,
    *,
    timeout: float = 30.0,
    max_attempts: int = 3,
    backoff_base: float = 1.0,
) -> OAuthCredentials:
    """Exchange ``refresh_token`` for a new ``access_token`` (and rotated refresh).

    Anthropic returns ``{access_token, refresh_token, expires_in, ...}`` --
    ``refresh_token`` is rotated on every call, so if we don't write the new
    one back somewhere durable the next refresh will 401.

    Retries up to ``max_attempts`` on transient network errors and on 5xx
    responses. A 4xx response (typically 401 = refresh_token revoked) is
    raised immediately -- retrying won't help.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(
                    REFRESH_ENDPOINT,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": creds.refresh_token,
                        "client_id": CLAUDE_CODE_CLIENT_ID,
                    },
                    headers={"content-type": "application/json"},
                )
        except (httpx.HTTPError, OSError) as e:
            last_error = e
            if attempt >= max_attempts:
                raise CredentialsError(
                    f"OAuth refresh network error after {attempt} attempts: {e}"
                ) from e
            sleep_s = backoff_base * (2 ** (attempt - 1))
            _LOG.warning(
                "OAuth refresh attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, max_attempts, e, sleep_s,
            )
            time.sleep(sleep_s)
            continue

        if r.status_code == 200:
            break
        if 400 <= r.status_code < 500:
            raise CredentialsError(
                f"OAuth refresh failed (non-retryable): HTTP {r.status_code} {r.text[:200]}"
            )
        last_error = CredentialsError(
            f"OAuth refresh failed: HTTP {r.status_code} {r.text[:200]}"
        )
        if attempt >= max_attempts:
            raise last_error
        sleep_s = backoff_base * (2 ** (attempt - 1))
        _LOG.warning(
            "OAuth refresh attempt %d/%d got HTTP %d; retrying in %.1fs",
            attempt, max_attempts, r.status_code, sleep_s,
        )
        time.sleep(sleep_s)
    else:  # pragma: no cover - exhausted loop with no break
        raise CredentialsError(
            f"OAuth refresh failed after {max_attempts} attempts: {last_error}"
        )

    try:
        body = r.json()
    except ValueError as e:
        raise CredentialsError(f"OAuth refresh returned non-JSON: {e}") from e

    access_token = body.get("access_token")
    if not access_token:
        raise CredentialsError(f"OAuth refresh missing access_token: {body}")
    refresh_token = body.get("refresh_token") or creds.refresh_token
    expires_in = int(body.get("expires_in", 3600))
    return OAuthCredentials(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=int(time.time() * 1000) + expires_in * 1000,
        scopes=creds.scopes,
        subscription_type=creds.subscription_type,
    )


def _atomic_write_creds(path: Path, creds: OAuthCredentials) -> None:
    """Write credentials JSON to *path* with 0600 perms and no TOCTOU window.

    Creates the file 0600 from the start via os.open (rather than write_text
    then chmod, which briefly exposes the token at the umask default), writes to
    a temp sibling, then atomically renames into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(creds.to_claude_payload()))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def write_cache(creds: OAuthCredentials) -> None:
    _atomic_write_creds(_CACHE_PATH, creds)


def _service_cache_path(service: str) -> Path:
    """Per-Keychain-service cache path so pooled accounts don't clobber each
    other in the single shared cache file."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", service)
    return _CACHE_PATH.parent / f"claude_creds_kc_{safe}.json"


def _keychain_write_back_enabled() -> bool:
    """Whether to push rotated tokens back into Keychain (opt-in, default off)."""
    return os.environ.get("KAIJU_CC_WRITE_BACK_KEYCHAIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _keychain_account_for_service(service: str) -> Optional[str]:
    """Read the ``acct`` attribute of a Keychain item so we can update it."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-g"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    # `security ... -g` prints attributes on stderr: `"acct"<blob>="user@x"`.
    m = re.search(r'"acct"<blob>="([^"]*)"', r.stderr or "")
    return m.group(1) if m else None


def _keychain_write_back(service: str, creds: OAuthCredentials) -> bool:
    """Best-effort: update the Keychain item for *service* with rotated creds.

    Anthropic rotates the refresh token on every grant. If the bridge refreshes
    a Keychain-sourced token and does NOT write the new one back, the refresh
    token still stored in Keychain (and used by the real ``claude`` CLI) is now
    dead — the next ``claude`` invocation 401s and the user is logged out. This
    keeps the canonical store in sync. Opt-in via KAIJU_CC_WRITE_BACK_KEYCHAIN.
    """
    if platform.system() != "Darwin":
        return False
    account = _keychain_account_for_service(service)
    if account is None:
        return False
    payload = json.dumps(creds.to_claude_payload())
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-a", account, "-s", service, "-w", payload],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        _LOG.warning("keychain write-back failed for %s: %s", service, e)
        return False
    return r.returncode == 0


def _warn_refresh_rotation(source: str, wrote_back: bool) -> None:
    """Warn that a self-refresh may have invalidated the claude CLI's login."""
    if wrote_back:
        _LOG.info("Refreshed token written back to %s; claude CLI stays in sync", source)
        return
    _LOG.warning(
        "Bridge refreshed the OAuth token from %s but did NOT write the rotated "
        "refresh token back. The `claude` CLI sharing this credential may now be "
        "logged out (refresh tokens rotate on every use). Set "
        "KAIJU_CC_WRITE_BACK_KEYCHAIN=1 to keep Keychain in sync, or give the "
        "bridge its own dedicated account.",
        source,
    )


class CredentialProvider:
    """Thread-safe lazy credential cache with auto-refresh.

    The bridge keeps one of these per process. Callers ask for an access
    token via ``get_access_token()``; the provider reads from disk/keychain
    on first call, then refreshes proactively whenever the token is within
    ``REFRESH_LEEWAY_SECONDS`` of expiry.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # B14: a SEPARATE lock for the slow refresh (network + backoff sleeps).
        # Holding `_lock` across `refresh_credentials` would serialize every
        # token read — even threads whose cached token is still valid — behind
        # one in-flight refresh. We hold `_lock` only for the brief field
        # read/write and `_refresh_lock` (which dedupes concurrent refreshes)
        # during the network call.
        self._refresh_lock = threading.Lock()
        self._creds: Optional[OAuthCredentials] = None

    def _load(self) -> OAuthCredentials:
        """Load creds from this provider's source. Overridden by subclasses."""
        return load_credentials()

    def _persist_after_refresh(self, creds: OAuthCredentials) -> None:
        """Persist a freshly-refreshed token. Overridden by subclasses."""
        try:
            write_cache(creds)
        except OSError as e:
            _LOG.warning("Could not persist refreshed creds to cache: %s", e)
        # The default provider's canonical source is the Keychain item; keep it
        # in sync (or warn) so the claude CLI isn't logged out.
        wrote_back = (
            _keychain_write_back(_KEYCHAIN_SERVICE, creds)
            if _keychain_write_back_enabled()
            else False
        )
        _warn_refresh_rotation(f"keychain {_KEYCHAIN_SERVICE!r}", wrote_back)

    def _refresh_flock_path(self) -> Optional[Path]:
        """B4: cross-process lock file path so multiple bridge processes don't
        refresh (and rotate the refresh token) simultaneously. Default provider
        guards the shared cache; subclasses override per-account."""
        return _CACHE_PATH.with_suffix(".lock")

    def _reload_from_persist(self) -> Optional[OAuthCredentials]:
        """B4: re-read creds from the durable post-refresh store so a peer
        process's refresh is picked up instead of refreshing again."""
        raw = _read_cache_file()
        if not raw:
            return None
        try:
            return OAuthCredentials.from_claude_payload(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None

    def _refresh_locked(self, creds: OAuthCredentials) -> str:
        """Perform the refresh while holding ``_refresh_lock`` (in-process dedupe)
        and, if configured, a cross-process flock (B4). Stores + returns token."""
        flock_path = self._refresh_flock_path()
        if flock_path is None:
            new_creds = refresh_credentials(creds)
            self._persist_after_refresh(new_creds)
            with self._lock:
                self._creds = new_creds
            return new_creds.access_token

        import fcntl
        try:
            flock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        with open(flock_path, "w") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                _LOG.warning("flock failed on %s: %s; proceeding unlocked", flock_path, e)
            # A peer process may have refreshed while we waited on the flock.
            peer = self._reload_from_persist()
            if peer is not None and not peer.is_expired():
                _LOG.info("picked up peer-refreshed token (no re-refresh)")
                with self._lock:
                    self._creds = peer
                return peer.access_token
            new_creds = refresh_credentials(creds)
            self._persist_after_refresh(new_creds)
            with self._lock:
                self._creds = new_creds
            return new_creds.access_token

    def get_access_token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds = self._load()
            creds = self._creds
        if not creds.is_expired():
            return creds.access_token
        # Refresh OUTSIDE `_lock` so valid-token readers don't block (B14).
        with self._refresh_lock:
            # Double-check: another thread may have refreshed while we waited.
            with self._lock:
                creds = self._creds
            if creds is not None and not creds.is_expired():
                return creds.access_token
            # A concurrent force_reload() may have nulled _creds in the window
            # between the outer read and here. Reload before refreshing so we
            # never call refresh_credentials(None) -> AttributeError (which would
            # escape as an unhandled 500 rather than a clean CredentialsError).
            if creds is None:
                with self._lock:
                    if self._creds is None:
                        self._creds = self._load()
                    creds = self._creds
                if not creds.is_expired():
                    return creds.access_token
            _LOG.info("Refreshing Claude Code OAuth token")
            return self._refresh_locked(creds)

    def force_reload(self) -> None:
        with self._lock:
            self._creds = None



# ----------------------------------------------------------------------------
# Multi-account pool support
# ----------------------------------------------------------------------------


class _FileCredentialProvider(CredentialProvider):
    """CredentialProvider that always loads from a specific file path."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self._path = Path(path).expanduser()

    def _load(self) -> OAuthCredentials:
        if not self._path.is_file():
            raise CredentialsError(f"credentials file not found: {self._path}")
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as e:
            raise CredentialsError(f"could not read {self._path}: {e}") from e
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CredentialsError(f"invalid JSON in {self._path}: {e}") from e
        return OAuthCredentials.from_claude_payload(payload)

    def get_access_token(self) -> str:
        with self._lock:
            if self._creds is None:
                self._creds = self._load()
            creds = self._creds
        if not creds.is_expired():
            return creds.access_token
        # B14: refresh OUTSIDE `_lock` so valid-token readers don't block on the
        # flock-wait + network call. `_refresh_lock` dedupes in-process refreshes;
        # the flock serializes across processes.
        with self._refresh_lock:
            with self._lock:
                creds = self._creds
            if creds is not None and not creds.is_expired():
                return creds.access_token
            # Cross-process serialization: only one bridge process should hit
            # the refresh endpoint; others wait, then re-read the rotated token.
            # Without this, concurrent harness runs sharing the same pool file
            # race on refresh and lose tokens (last-writer-wins).
            import fcntl
            lock_path = self._path.with_suffix(self._path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_fh:
                try:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                except OSError as e:
                    _LOG.warning("flock failed on %s: %s; proceeding unlocked", lock_path, e)
                # Re-load -- another process may have refreshed while we waited.
                try:
                    fresh = self._load()
                    if not fresh.is_expired():
                        with self._lock:
                            self._creds = fresh
                        return fresh.access_token
                    # Not expired-check passed but stale: use the re-loaded creds
                    # as the refresh source so we never refresh from None.
                    creds = fresh
                except CredentialsError:
                    pass
                # A concurrent force_reload() could have left creds None while the
                # disk re-load also failed; refuse rather than refresh_credentials(None).
                if creds is None:
                    raise CredentialsError(
                        f"credentials unavailable for {self._path} (nulled + re-load failed)"
                    )
                _LOG.info("Refreshing OAuth token from %s", self._path)
                new_creds = refresh_credentials(creds)
                try:
                    _atomic_write_creds(self._path, new_creds)
                except OSError as e:
                    _LOG.warning("Could not persist refreshed creds to %s: %s", self._path, e)
            with self._lock:
                self._creds = new_creds
            return new_creds.access_token

    def token_prefix(self) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None


class _KeychainCredentialProvider(CredentialProvider):
    """CredentialProvider that loads from a specific macOS Keychain service."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def _load(self) -> OAuthCredentials:
        if platform.system() != "Darwin":
            raise CredentialsError("Keychain accounts only supported on macOS")
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", self._service, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            raise CredentialsError(f"keychain read failed for {self._service}: {e}") from e
        if r.returncode != 0 or not r.stdout.strip():
            raise CredentialsError(
                f"no keychain entry for service {self._service!r}: {r.stderr[:200]}"
            )
        try:
            payload = json.loads(r.stdout.strip())
        except json.JSONDecodeError as e:
            raise CredentialsError(f"invalid JSON in keychain {self._service}: {e}") from e
        return OAuthCredentials.from_claude_payload(payload)

    def _persist_after_refresh(self, creds: OAuthCredentials) -> None:
        # B14: runs OUTSIDE `_lock` (held only by the base's `_refresh_lock`).
        try:
            # Per-service cache, NOT the shared write_cache() — otherwise
            # multiple pooled keychain accounts clobber one another (and a
            # `default` slot reading the shared cache could load the wrong
            # account's token).
            _atomic_write_creds(_service_cache_path(self._service), creds)
        except OSError as e:
            _LOG.warning("Could not persist refreshed creds to cache: %s", e)
        wrote_back = (
            _keychain_write_back(self._service, creds)
            if _keychain_write_back_enabled()
            else False
        )
        _warn_refresh_rotation(f"keychain {self._service!r}", wrote_back)

    def _refresh_flock_path(self) -> Optional[Path]:
        # B4: per-service cross-process lock so pooled keychain accounts each
        # serialize their own refresh independently.
        return _service_cache_path(self._service).with_suffix(".lock")

    def _reload_from_persist(self) -> Optional[OAuthCredentials]:
        # B4: a peer process writes the rotated token to this per-service cache.
        p = _service_cache_path(self._service)
        try:
            raw = p.read_text(encoding="utf-8")
            return OAuthCredentials.from_claude_payload(json.loads(raw))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # get_access_token inherited from CredentialProvider (uses our _load +
    # _persist_after_refresh, refreshing without holding the read lock).

    def token_prefix(self) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None


# How many recently-handed-out tokens to remember per slot for error
# attribution. Keeping a small history (not just the single latest token)
# survives a concurrent OAuth refresh: a request that was handed token T1 can
# still be attributed to this slot after a later request rotated the slot to T2.
_RECENT_TOKEN_HISTORY = 8


@dataclass
class _AccountSlot:
    provider: CredentialProvider
    label: str
    exhausted_until: float = 0.0
    invalid: bool = False
    # The access tokens most recently handed out from this slot (newest last).
    # Used to attribute an upstream error back to the exact account that
    # produced it, without relying on a low-entropy 20-char prefix (all OAuth
    # tokens share the `sk-ant-oat01-` prefix). Bounded so a long-lived slot
    # doesn't grow unbounded; a refresh race still can't lose attribution.
    recent_tokens: list[str] = field(default_factory=list)

    def remember_token(self, token: str) -> None:
        """Record a token handed out from this slot (bounded history)."""
        if not token or token in self.recent_tokens:
            return
        self.recent_tokens.append(token)
        if len(self.recent_tokens) > _RECENT_TOKEN_HISTORY:
            del self.recent_tokens[0]

    def has_token(self, token: str) -> bool:
        """True if ``token`` was recently handed out from this slot."""
        return bool(token) and token in self.recent_tokens

    def is_available(self, now: Optional[float] = None) -> bool:
        if self.invalid:
            return False
        now = now if now is not None else time.time()
        return now >= self.exhausted_until


class MultiAccountCredentialProvider:
    """Pool of ``CredentialProvider``s with rotation, exhaustion tracking, and failover.

    Drop-in replacement for ``CredentialProvider`` at the bridge layer: exposes
    ``get_access_token() -> str`` and ``force_reload()``. The bridge calls
    ``mark_account_exhausted`` / ``mark_account_invalid`` to record state from
    upstream classification (see ``agent.claude_code.errors``).

    Selection policy: first available slot in insertion order. This makes the
    behavior predictable and lets a user put their "primary" account first.

    Concurrency note: selection is intentionally a serial drain, NOT a
    round-robin load balancer. Under many concurrent requests they all pick the
    same first-available account and drain it before moving to the next. This is
    deliberate -- it keeps the "primary first" contract and the /quota picture
    easy to reason about. Error attribution is made concurrency-safe by
    per-slot recent-token history (see ``_AccountSlot.remember_token``) rather
    than a single shared "last used" pointer.
    """

    def __init__(
        self, slots: list[_AccountSlot], state_path: Optional[Path] = None
    ) -> None:
        if not slots:
            raise CredentialsError("MultiAccountCredentialProvider needs >= 1 slot")
        self._slots = slots
        self._lock = threading.Lock()

    @property
    def pool_size(self) -> int:
        """Total number of account slots (available or not)."""
        return len(self._slots)

    def get_access_token(self) -> str:
        with self._lock:
            slot, _idx = self._select_slot_locked()
        try:
            token = slot.provider.get_access_token()
        except CredentialsError:
            with self._lock:
                slot.invalid = True
            return self.get_access_token()
        with self._lock:
            slot.remember_token(token)
        return token

    def _select_slot_locked(self) -> tuple[_AccountSlot, int]:
        now = time.time()
        for idx, slot in enumerate(self._slots):
            if slot.is_available(now):
                return slot, idx
        # All accounts exhausted/invalid -- raise with earliest reset hint.
        soonest = min(
            (s.exhausted_until for s in self._slots if not s.invalid),
            default=0.0,
        )
        delta = max(0.0, soonest - now)
        raise CredentialsError(
            f"all {len(self._slots)} accounts exhausted; soonest reset in {delta:.0f}s"
        )

    def force_reload(self) -> None:
        # B8: ONLY drop cached creds (force a token re-read). Do NOT clear
        # exhausted_until / invalid — wiping cap timers makes the bridge
        # immediately re-hammer accounts that are still capped. Use
        # reset_account_state() explicitly if you really want to clear it.
        with self._lock:
            for slot in self._slots:
                slot.provider.force_reload()

    def reset_account_state(self) -> None:
        """Explicitly clear all exhaustion/invalid flags (operator action only)."""
        with self._lock:
            for slot in self._slots:
                slot.provider.force_reload()
                slot.exhausted_until = 0.0
                slot.invalid = False
            self._persist_state_locked()

    def mark_account_exhausted(self, token_prefix: str, until_unix: float) -> None:
        with self._lock:
            slot = self._find_slot_by_prefix_locked(token_prefix)
            if slot is None:
                return
            slot.exhausted_until = max(slot.exhausted_until, until_unix)
            self._persist_state_locked()
            _LOG.info(
                "account %s marked exhausted until %s (in %.0fs)",
                slot.label, until_unix, max(0.0, until_unix - time.time()),
            )

    def mark_account_invalid(self, token_prefix: str) -> None:
        with self._lock:
            slot = self._find_slot_by_prefix_locked(token_prefix)
            if slot is None:
                return
            slot.invalid = True
            self._persist_state_locked()
            _LOG.warning("account %s marked invalid (will not be retried)", slot.label)

    def next_reset_at(self) -> Optional[float]:
        """Soonest Unix-time at which any exhausted account becomes available.

        Returns ``None`` if at least one account is currently available.
        """
        with self._lock:
            now = time.time()
            if any(s.is_available(now) for s in self._slots):
                return None
            future = [s.exhausted_until for s in self._slots if not s.invalid]
            return min(future) if future else None

    def has_available(self) -> bool:
        """True iff at least one account is usable right now.

        Distinct from ``next_reset_at() is None``: when every account is
        *invalid* (not merely capped) there is no reset coming, yet
        ``next_reset_at()`` still returns ``None``. Callers deciding whether a
        failover target exists must use this, not the reset time.
        """
        with self._lock:
            now = time.time()
            return any(s.is_available(now) for s in self._slots)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "label": s.label,
                    "token_prefix": getattr(s.provider, "token_prefix", lambda: None)(),
                    "invalid": s.invalid,
                    "exhausted_until": s.exhausted_until,
                    "available": s.is_available(),
                }
                for s in self._slots
            ]

    def _find_slot_by_prefix_locked(self, token: str) -> Optional[_AccountSlot]:
        if not token:
            return None
        # Prefer an exact match against a token actually handed out from a slot
        # (recorded in get_access_token). This stays correct even after a
        # concurrent refresh rotated the slot to a newer token, because each
        # slot remembers its recent tokens, not just the latest one.
        for slot in self._slots:
            if slot.has_token(token):
                return slot
        # Fallback: low-entropy prefix match, for callers that mark a slot
        # before any token has been handed out (e.g. an error attributed before
        # first use, or the unit tests). Because all OAuth tokens share the
        # `sk-ant-oat01-` prefix, a prefix can be ambiguous -- so attribute ONLY
        # when exactly one slot matches, and refuse to guess (return None) when
        # two or more match. Guessing here would mark the WRONG account.
        matches = [
            slot
            for slot in self._slots
            if (sp := getattr(slot.provider, "token_prefix", lambda: None)())
            and (sp.startswith(token) or token.startswith(sp))
        ]
        return matches[0] if len(matches) == 1 else None


def _add_token_prefix_to_provider(p: CredentialProvider) -> CredentialProvider:
    """Monkey-patch a single-account ``CredentialProvider`` so it exposes ``token_prefix()``."""
    if hasattr(p, "token_prefix"):
        return p

    def _tp(self: CredentialProvider) -> Optional[str]:
        with self._lock:
            return self._creds.access_token[:20] if self._creds else None

    p.token_prefix = _tp.__get__(p, CredentialProvider)  # type: ignore[attr-defined]
    return p


def load_account_pool(spec: str) -> Optional[MultiAccountCredentialProvider]:
    """Parse a ``KAIJU_CC_ACCOUNT_POOL`` spec into a multi-account provider.

    Spec format: colon-separated entries, each one of:
      - ``keychain:<service-name>``               -> ``_KeychainCredentialProvider``
      - A file path (absolute or ``~``-relative)  -> ``_FileCredentialProvider``
      - ``default``                               -> default ``CredentialProvider``
                                                     (Keychain -> ~/.claude/.credentials.json -> cache)

    Note that ``:`` separates entries AND appears inside ``keychain:<service>``.
    We therefore parse by walking the colon-split tokens: a bare ``keychain``
    token consumes the following token as its service name. This lets the
    documented value
    ``keychain:Claude Code-credentials:keychain:Claude Code-credentials-acct2``
    parse into two keychain accounts. (A service name that itself contains a
    ``:`` is not supported.)

    Empty entries are skipped. Returns ``None`` if the spec yields no slots.
    """
    if not spec:
        return None
    tokens = spec.split(":")
    slots: list[_AccountSlot] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i].strip()
        if not entry:
            i += 1
            continue
        if entry == "default":
            slots.append(_AccountSlot(
                provider=_add_token_prefix_to_provider(CredentialProvider()),
                label="default",
            ))
            i += 1
            continue
        if entry == "keychain" and i + 1 < len(tokens):
            # Re-glue: the next token is this keychain entry's service name.
            service = tokens[i + 1].strip()
            if service:
                slots.append(_AccountSlot(
                    provider=_KeychainCredentialProvider(service),
                    label=f"keychain:{service}",
                ))
                i += 2
                continue
            # Bare "keychain" with an empty service name -> drop the marker.
            i += 1
            continue
        if entry.startswith("keychain:"):
            # Defensive: an already-glued single token (won't arise from a
            # ':'-split, but harmless to accept).
            service = entry[len("keychain:"):]
            slots.append(_AccountSlot(
                provider=_KeychainCredentialProvider(service),
                label=f"keychain:{service}",
            ))
            i += 1
            continue
        # Treat as a file path.
        slots.append(_AccountSlot(
            provider=_FileCredentialProvider(Path(entry)),
            label=f"file:{entry}",
        ))
        i += 1
    if not slots:
        return None
    # B13: production pools persist cap state across bridge restarts. An explicit
    # KAIJU_CC_POOL_STATE_PATH overrides the default cache location.
    env_path = os.environ.get("KAIJU_CC_POOL_STATE_PATH")
    state_path = Path(env_path) if env_path else (
        _CACHE_PATH.parent / "account_pool_state.json"
    )
    return MultiAccountCredentialProvider(slots, state_path=state_path)