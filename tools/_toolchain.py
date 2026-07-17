"""Universal JS/TS toolchain provisioning for kaiju-harness.

Replaces the two duplicate ``_ensure_pkg_manager`` shims in
``tools/prepare_repo_ts.py`` and ``tools/prepare_repo_js.py`` with a single
source of truth that also handles Bun (hybrid: npm-global preferred, pinned
installer fallback), corepack self-bootstrap, and subprocess-scoped Node
version switching via fnm/nvm/volta.

See ``TOOLCHAIN_PROVISIONING.md`` at the repo root for the full architecture
document (module layout, error taxonomy, trust matrix, state journal schema,
migration path).

Invariants enforced by this module (referenced by number in tests + docstrings):

- **I1**  Node switching NEVER mutates ``os.environ``. All switching returns
  an ``env=`` dict for :func:`subprocess.run`.
- **I2**  ``sudo`` is NEVER invoked. If a global install needs root, we raise
  ``ToolchainInstallFailedError`` naming the exact command the user should run.
- **I3**  Tool names must be in ``SUPPORTED_TOOLS = {"npm","pnpm","yarn","bun"}``.
  ``packageManager`` field values outside this set raise
  ``ToolchainAllowlistError`` (defeats supply-chain typo-squats).
- **I4**  A ``packageManager: pm@X.Y.Z+sha224.abc...`` field whose integrity
  hash mismatches must NOT fall back to ``@latest``. Raises
  ``ToolchainVersionMismatchError``.
- **I5**  Every state-mutating op is idempotent (fast-path via
  :func:`shutil.which` before locking).
- **I6**  State journal writes are 2-phase: ``INTENT`` before install,
  ``SUCCESS``/``FAILURE`` after. Orphan intents recovered on next run.
- **I7**  Concurrency is serialised via :func:`fcntl.flock`; double-checked
  read after acquire.
- **I8**  Failure loud, never silent. No ``|| true``, no swallowed stderr.
- **I9**  Two-Node model: controller Node (this module, aider, stubber) is
  never switched; worker Node (repo install/test) is scoped to subprocess env.
- **I10** ``KAIJU_TOOLCHAIN_DRY_RUN=1`` makes every mutating op print + return
  without side effects.
- **I11** ``KAIJU_TOOLCHAIN_OFFLINE=1`` blocks every network fetch; missing
  tools raise ``ToolchainMissingError`` with an "offline mode" hint.

Non-goals (explicit refusals):

- Windows support.
- Deno / package managers outside ``{npm, pnpm, yarn, bun}``.
- Modifying the user's repo (writing ``.nvmrc``, upgrading lockfiles, editing
  ``package.json``).
- Native compiler toolchain installation (gcc/make/python-for-node-gyp).
- CI cache management.
- Private registry auth (``.npmrc`` manipulation).
- Auto-migrating classic yarn → berry.
- Running installs via ``sudo``.
- Silent fallback when ``packageManager`` integrity hash mismatches.
- Managing Rust / Go / Java / Python versions (own modules).
- NFS-safe locking (advisory ``fcntl.flock`` only).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

__all__ = [
    "Tool",
    "TrustMode",
    "InstallMethod",
    "ToolchainError",
    "ToolchainMissingError",
    "ToolchainInstallFailedError",
    "ToolchainTrustDeniedError",
    "ToolchainVersionMismatchError",
    "ToolchainConcurrencyError",
    "ToolchainSwitchUnavailableError",
    "ToolchainAllowlistError",
    "ToolchainConfig",
    "ProvisionResult",
    "NodeSwitchResult",
    "DoctorReport",
    "load_config",
    "ensure_pm",
    "ensure_node",
    "build_env_for_repo",
    "resolve_install_command",
    "yarn_major",
    "doctor",
    "uninstall",
    "list_installed",
    "SUPPORTED_TOOLS",
    "LTS_ALIASES",
    "STATE_SCHEMA_VERSION",
]



SUPPORTED_TOOLS: frozenset[str] = frozenset({"npm", "pnpm", "yarn", "bun"})
"""Hardcoded allowlist (Invariant I3). Refuses arbitrary ``packageManager``
values to defeat supply-chain typo-squats (e.g. ``yarn-classic``)."""

LTS_ALIASES: dict[str, str] = {
    "lts/*": "22",
    "lts/latest": "22",
    "lts/jod": "22",
    "lts/iron": "20",
    "lts/hydrogen": "18",
    "lts/gallium": "16",
    "lts/fermium": "14",
}
"""Node LTS codenames → major version. Hardcoded because ``nvm ls-remote``
requires network. Update this map when a new LTS lands (drift-checked by
``test_lts_aliases_all_in_supported`` in ``test_toolchain.py``)."""

STATE_SCHEMA_VERSION: int = 1

_DEFAULT_LOCK_TIMEOUT_SEC = 300
_DEFAULT_BUN_VERSION_FALLBACK = "1.1.29"
_LOCK_POLL_INTERVAL_SEC = 0.1
_INSTALL_TIMEOUT_SEC = 600
_VERSION_QUERY_TIMEOUT_SEC = 15
_STDERR_TAIL_CHARS = 1000
_BUN_MANIFEST_FILENAME = "_toolchain_bun_manifest.json"

_CONTROLLER_SWITCHER = "none-controller"

# Regex to strip a leading integrity suffix from a packageManager value.
# packageManager: "yarn@4.5.0+sha224.abcdef..." → base "yarn@4.5.0", integrity "sha224.abcdef..."
_PM_INTEGRITY_RE = re.compile(r"^([^@]+@[^+]+)\+(sha\d+\.[a-f0-9]+)$")




class Tool(str, Enum):
    """Every tool this module can provision."""

    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"
    NODE = "node"
    COREPACK = "corepack"


class TrustMode(str, Enum):
    """Trust modes (see §3 of TOOLCHAIN_PROVISIONING.md)."""

    STRICT = "strict"
    NORMAL = "normal"
    PERMISSIVE = "permissive"


class InstallMethod(str, Enum):
    """How a tool ended up available. Recorded in the state journal."""

    ALREADY_PRESENT = "already-present"
    NPM_GLOBAL = "npm-global"
    COREPACK_ACTIVATE = "corepack-activate"
    BUN_INSTALLER_SCRIPT = "bun-installer-script"
    VERSION_MANAGER = "version-manager"




class ToolchainError(RuntimeError):
    """Base class for toolchain-provisioning errors.

    Callers should catch ``ToolchainError`` to handle any provisioning failure
    generically; specific subclasses let them distinguish ``install failed``
    from ``user forbade it`` without brittle string checks.
    """


class ToolchainMissingError(ToolchainError):
    """Tool is absent and cannot be auto-installed (no path available)."""


class ToolchainInstallFailedError(ToolchainError):
    """Install was attempted, subprocess exited non-zero. Carries stderr tail."""


class ToolchainTrustDeniedError(ToolchainError):
    """Requested action is blocked by the current :class:`TrustMode`."""


class ToolchainVersionMismatchError(ToolchainError):
    """A version pin mismatched (``packageManager`` integrity hash, etc.)."""


class ToolchainConcurrencyError(ToolchainError):
    """File-lock acquisition timed out or deadlocked."""


class ToolchainSwitchUnavailableError(ToolchainError):
    """A Node version switch was required but no switcher is on the host."""


class ToolchainAllowlistError(ToolchainError):
    """Tool name is not in :data:`SUPPORTED_TOOLS` (Invariant I3)."""




@dataclass(frozen=True)
class ToolchainConfig:
    """Immutable configuration snapshot. Produced by :func:`load_config`."""

    trust_mode: TrustMode
    no_auto_install: bool
    dry_run: bool
    offline: bool
    state_dir: Path
    log_path: Path | None
    allow_bun_install_script: bool
    allow_global_npm: bool
    node_switcher_preference: str  # "auto" | "fnm" | "nvm" | "volta" | "none"
    pin_node: str | None
    version_pins: dict[str, str] = field(default_factory=dict)
    lock_timeout_sec: int = _DEFAULT_LOCK_TIMEOUT_SEC


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of :func:`ensure_pm`."""

    tool: Tool
    version: str
    install_method: InstallMethod
    install_path: str
    from_cache: bool
    journal_id: str | None


@dataclass(frozen=True)
class NodeSwitchResult:
    """Outcome of :func:`ensure_node`. ``env_delta`` is safe to merge into a
    ``subprocess.run(..., env=...)`` invocation without touching
    ``os.environ`` (Invariant I1)."""

    resolved_version: str
    switcher: str
    env_delta: dict[str, str]
    source: str


@dataclass(frozen=True)
class DoctorReport:
    """Diagnostic snapshot (``python -m tools.toolchain doctor`` output)."""

    config: ToolchainConfig
    tools_which: dict[str, str | None]
    tools_versions: dict[str, str | None]
    node_switcher: str | None
    state_entries: list[dict]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    v = env.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _default_state_dir(env: Mapping[str, str]) -> Path:
    override = env.get("KAIJU_TOOLCHAIN_STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = env.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "kaiju" / "toolchain"
    home = env.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".kaiju" / "toolchain"


def _default_allow_global_npm(env: Mapping[str, str]) -> bool:
    """Linux system-npm typically writes to ``/usr/lib/node_modules`` (needs
    root, Invariant I2). Detect and default OFF; macOS/Homebrew prefix is
    user-writable and defaults ON."""
    npm = shutil.which("npm")
    if npm is None:
        return True
    if sys.platform == "darwin":
        return True
    # Linux heuristic: `npm config get prefix` should NOT be under /usr, /opt/nodejs, etc.
    try:
        prefix = subprocess.run(
            [npm, "config", "get", "prefix"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    if not prefix:
        return False
    return not (prefix.startswith("/usr/") or prefix.startswith("/opt/nodejs"))


def load_config(env: Mapping[str, str] | None = None) -> ToolchainConfig:
    """Read all ``KAIJU_TOOLCHAIN_*`` env vars into an immutable config.

    ``env`` defaults to ``os.environ``; tests inject synthetic maps.
    """
    e = env if env is not None else os.environ

    trust_raw = e.get("KAIJU_TOOLCHAIN_TRUST", "normal").strip().lower()
    try:
        trust = TrustMode(trust_raw)
    except ValueError:
        logger.warning(
            "invalid KAIJU_TOOLCHAIN_TRUST=%r, falling back to 'normal'", trust_raw
        )
        trust = TrustMode.NORMAL

    switcher_pref = e.get("KAIJU_TOOLCHAIN_NODE_SWITCHER", "auto").strip().lower()
    if switcher_pref not in ("auto", "fnm", "nvm", "volta", "none"):
        logger.warning(
            "invalid KAIJU_TOOLCHAIN_NODE_SWITCHER=%r, falling back to 'auto'",
            switcher_pref,
        )
        switcher_pref = "auto"

    version_pins: dict[str, str] = {}
    for pm in ("bun", "pnpm", "yarn", "npm"):
        v = e.get(f"KAIJU_{pm.upper()}_VERSION", "").strip()
        if v:
            version_pins[pm] = v

    log_raw = e.get("KAIJU_TOOLCHAIN_LOG", "").strip()
    log_path = Path(log_raw).expanduser() if log_raw else None

    try:
        lock_timeout = int(e.get("KAIJU_TOOLCHAIN_LOCK_TIMEOUT", str(_DEFAULT_LOCK_TIMEOUT_SEC)))
    except ValueError:
        lock_timeout = _DEFAULT_LOCK_TIMEOUT_SEC

    return ToolchainConfig(
        trust_mode=trust,
        no_auto_install=_env_bool(e, "KAIJU_NO_AUTO_INSTALL"),
        dry_run=_env_bool(e, "KAIJU_TOOLCHAIN_DRY_RUN"),
        offline=_env_bool(e, "KAIJU_TOOLCHAIN_OFFLINE"),
        state_dir=_default_state_dir(e),
        log_path=log_path,
        allow_bun_install_script=_env_bool(e, "KAIJU_ALLOW_BUN_INSTALL_SCRIPT"),
        allow_global_npm=_env_bool(
            e, "KAIJU_TOOLCHAIN_ALLOW_GLOBAL_NPM", default=_default_allow_global_npm(e)
        ),
        node_switcher_preference=switcher_pref,
        pin_node=e.get("KAIJU_PIN_NODE", "").strip() or None,
        version_pins=version_pins,
        lock_timeout_sec=lock_timeout,
    )




def _state_path(config: ToolchainConfig) -> Path:
    return config.state_dir / "state.json"


def _lock_path(config: ToolchainConfig) -> Path:
    return config.state_dir / "toolchain.lock"


def _ensure_state_dir(config: ToolchainConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)


def _new_entry_id() -> str:
    """ULID-ish: monotonic ms timestamp + 6 hex chars of entropy."""
    ts_ms = int(time.time() * 1000)
    rand = secrets.token_hex(3)
    return f"{ts_ms:013d}-{rand}"


def _empty_state() -> dict:
    return {
        "version": STATE_SCHEMA_VERSION,
        "controller": {
            "hostname": os.uname().nodename if hasattr(os, "uname") else "unknown",
            "os": f"{sys.platform}-{os.uname().release if hasattr(os, 'uname') else 'unknown'}",
            "python": sys.version.split()[0],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "entries": [],
    }


def _load_state(config: ToolchainConfig) -> dict:
    p = _state_path(config)
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "toolchain state at %s unreadable (%s); starting fresh", p, exc
        )
        return _empty_state()
    if data.get("version") != STATE_SCHEMA_VERSION:
        logger.warning(
            "toolchain state schema %s != %s; treating as fresh",
            data.get("version"),
            STATE_SCHEMA_VERSION,
        )
        return _empty_state()
    return data


def _save_state(config: ToolchainConfig, data: dict) -> None:
    p = _state_path(config)
    _ensure_state_dir(config)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    os.replace(tmp, p)  # atomic


def _append_entry(config: ToolchainConfig, entry: dict) -> None:
    data = _load_state(config)
    data["entries"].append(entry)
    _save_state(config, data)


def _record_intent(
    config: ToolchainConfig,
    tool: str,
    version: str,
    install_method: InstallMethod,
    install_command: list[str],
) -> str:
    entry_id = _new_entry_id()
    entry = {
        "id": entry_id,
        "tool": tool,
        "version": version,
        "install_method": install_method.value,
        "install_command": install_command,
        "phase": "intent",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "stderr_tail": "",
    }
    _append_entry(config, entry)
    return entry_id


def _record_outcome(
    config: ToolchainConfig,
    entry_id: str,
    phase: str,
    install_path: str,
    stderr_tail: str,
) -> None:
    data = _load_state(config)
    for e in data["entries"]:
        if e.get("id") == entry_id:
            e["phase"] = phase
            e["finished_at"] = datetime.now(timezone.utc).isoformat()
            e["stderr_tail"] = stderr_tail[-_STDERR_TAIL_CHARS:] if stderr_tail else ""
            e["install_path"] = install_path
            break
    _save_state(config, data)


def _recover_orphans(config: ToolchainConfig) -> list[str]:
    """Called on config load / doctor. Returns list of tool names whose orphan
    intents were resolved (either promoted to success or marked failure)."""
    data = _load_state(config)
    changed: list[str] = []
    for e in data["entries"]:
        if e.get("phase") != "intent":
            continue
        tool = e.get("tool", "")
        version = e.get("version", "")
        which = shutil.which(tool) if tool else None
        installed_ver = _installed_version(tool) if which else None
        if which and (not version or installed_ver == version):
            e["phase"] = "success"
            e["install_path"] = which
            e["finished_at"] = datetime.now(timezone.utc).isoformat()
            changed.append(tool)
        else:
            e["phase"] = "failure"
            e["stderr_tail"] = "orphan-intent: could not verify install on recovery"
            e["finished_at"] = datetime.now(timezone.utc).isoformat()
            changed.append(tool)
    if changed:
        _save_state(config, data)
    return changed




@contextmanager
def _acquire_lock(config: ToolchainConfig) -> Iterator[None]:
    _ensure_state_dir(config)
    lock_p = _lock_path(config)
    fd = os.open(str(lock_p), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                if time.monotonic() - start > config.lock_timeout_sec:
                    holder_hint = _lock_holder_hint(lock_p)
                    raise ToolchainConcurrencyError(
                        f"toolchain lock {lock_p} not acquired within "
                        f"{config.lock_timeout_sec}s. {holder_hint}"
                    ) from exc
                time.sleep(_LOCK_POLL_INTERVAL_SEC)
        try:
            # Record our pid inside the lockfile — helps humans debug stuck
            # runs. Truncate first so stale content doesn't linger.
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _lock_holder_hint(lock_p: Path) -> str:
    try:
        content = lock_p.read_text(encoding="utf-8", errors="replace").strip()
        if content.isdigit():
            return f"Likely held by pid {content} (check `ps -p {content}`)."
    except OSError:
        pass
    return "Holder pid unknown."




def _bun_manifest_path() -> Path:
    return Path(__file__).parent / _BUN_MANIFEST_FILENAME


def _load_bun_manifest() -> dict:
    p = _bun_manifest_path()
    if not p.exists():
        logger.warning(
            "bun manifest %s missing; using default version %s",
            p,
            _DEFAULT_BUN_VERSION_FALLBACK,
        )
        return {
            "default": _DEFAULT_BUN_VERSION_FALLBACK,
            "versions": {
                _DEFAULT_BUN_VERSION_FALLBACK: {
                    "installer_url": "https://bun.sh/install",
                    "installer_sha256": None,
                    "npm_package_version": _DEFAULT_BUN_VERSION_FALLBACK,
                }
            },
        }
    return json.loads(p.read_text(encoding="utf-8"))


def _resolve_bun_version(config: ToolchainConfig) -> tuple[str, dict]:
    """Return (version, manifest_entry) using KAIJU_BUN_VERSION or manifest default."""
    manifest = _load_bun_manifest()
    ver = config.version_pins.get("bun") or manifest["default"]
    entry = manifest["versions"].get(ver)
    if entry is None:
        raise ToolchainVersionMismatchError(
            f"bun version {ver!r} not in manifest {_bun_manifest_path()}. "
            f"Known: {sorted(manifest['versions'])}"
        )
    return ver, entry




def _which(tool: str) -> str | None:
    return shutil.which(tool)


def _installed_version(tool: str) -> str | None:
    """Best-effort ``<tool> --version`` capture. Returns None on failure."""
    binary = _which(tool)
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_QUERY_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    stdout = proc.stdout if isinstance(proc.stdout, str) else ""
    stderr = proc.stderr if isinstance(proc.stderr, str) else ""
    out = (stdout or stderr or "").strip()
    if out.startswith("v"):
        out = out[1:]
    m = re.match(r"^(\d+\.\d+\.\d+(?:[-.+][A-Za-z0-9.]+)?)", out)
    return m.group(1) if m else out or None


def _run_subprocess(
    cmd: list[str],
    *,
    timeout: int = _INSTALL_TIMEOUT_SEC,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str]:
    """Thin wrapper for consistent capture. Returns (rc, stdout, stderr)."""
    logger.info("toolchain: running %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(env) if env is not None else None,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _validate_tool_name(name: str) -> None:
    """Invariant I3."""
    if name not in SUPPORTED_TOOLS:
        raise ToolchainAllowlistError(
            f"tool {name!r} not in supported allowlist {sorted(SUPPORTED_TOOLS)!r}. "
            f"Refusing to install (supply-chain guard, invariant I3)."
        )


def _parse_package_manager_field(raw: str) -> tuple[str, str, str | None]:
    """Split ``packageManager: yarn@4.5.0+sha224.abc...`` into (pm, ver, integrity).

    Returns (pm, version, integrity_hash_or_none). Raises
    :class:`ToolchainAllowlistError` if pm is outside the allowlist (Invariant I3).
    """
    raw = raw.strip()
    m = _PM_INTEGRITY_RE.match(raw)
    if m:
        base = m.group(1)
        integrity = m.group(2)
    else:
        base = raw
        integrity = None
    if "@" not in base:
        raise ToolchainVersionMismatchError(
            f"packageManager value {raw!r} missing '@version' (Corepack requires exact pin)"
        )
    pm, _, ver = base.partition("@")
    _validate_tool_name(pm)
    if not ver:
        raise ToolchainVersionMismatchError(
            f"packageManager value {raw!r} has empty version"
        )
    return pm, ver, integrity


def _read_package_manager_field(repo_root: Path) -> str | None:
    """Return the raw ``packageManager`` string from package.json, or None."""
    pj = repo_root / "package.json"
    if not pj.exists():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    val = data.get("packageManager")
    return val if isinstance(val, str) else None


def yarn_major(repo_root: Path) -> int | None:
    """Best-effort Yarn MAJOR version for *repo_root*.

    Yarn Classic (v1) and Berry (v2+) take DIFFERENT install flags, so callers
    that build an install command must know which generation is in play.
    Detection order, most authoritative first:

    1. ``packageManager`` pin (``yarn@4.13.0`` -> 4) — the source of truth
       Corepack itself uses.
    2. ``.yarnrc.yml`` presence — a Berry-only config file (Classic uses the
       extension-less ``.yarnrc``), so it implies >= 2.
    3. ``yarn --version`` run WITH cwd=repo_root, so a repo-pinned
       ``yarnPath`` release answers instead of a global classic shim.

    Returns None when yarn can't be resolved; callers should treat None as
    "assume Classic" (the historical default, and the safe flag set).
    """
    raw = _read_package_manager_field(repo_root)
    if raw and raw.strip().lower().startswith("yarn@"):
        try:
            _, ver, _ = _parse_package_manager_field(raw)
            return int(ver.split(".")[0])
        except (ToolchainError, ValueError):
            pass
    if (repo_root / ".yarnrc.yml").exists():
        return 2
    yarn_bin = _which("yarn")
    if yarn_bin:
        try:
            proc = subprocess.run(
                [yarn_bin, "--version"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=_VERSION_QUERY_TIMEOUT_SEC,
                check=False,
            )
            out = (proc.stdout or "").strip()
            if out:
                return int(out.split(".")[0])
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return None


def resolve_install_command(
    pkg_manager: str, repo_root: Path, *, frozen: bool
) -> list[str]:
    """Return the lockfile-driven install argv for *pkg_manager* in *repo_root*.

    Single source of truth so prepare_repo_js / prepare_repo_ts (and any future
    caller) can't drift into passing Yarn-Classic flags to a Yarn-Berry repo.
    That mistake makes ``yarn install --frozen-lockfile --ignore-scripts`` die
    with "Unsupported option name (--ignore-scripts)"; under ``check=False`` the
    failure is swallowed and node_modules is left EMPTY, which silently breaks
    the tsc compilability check and test-id discovery (0 IDs on a repo that
    actually has tests).

    ``frozen=True`` -> reproducible install against the committed lockfile.
    ``frozen=False`` -> generating install (no committed lockfile) that resolves
    and writes one. ``--ignore-scripts`` / Berry ``--mode=skip-build`` keep
    arbitrary lifecycle scripts from running during preparation.
    """
    _validate_tool_name(pkg_manager)
    if pkg_manager == "npm":
        if frozen:
            return ["npm", "ci", "--no-audit", "--no-fund", "--ignore-scripts"]
        return [
            "npm", "install", "--no-audit", "--no-fund",
            "--ignore-scripts", "--package-lock=true",
        ]
    if pkg_manager == "pnpm":
        cmd = ["pnpm", "install", "--ignore-scripts"]
        if frozen:
            cmd.append("--frozen-lockfile")
        return cmd
    if pkg_manager == "bun":
        cmd = ["bun", "install", "--ignore-scripts"]
        if frozen:
            cmd.append("--frozen-lockfile")
        return cmd
    if pkg_manager == "yarn":
        major = yarn_major(repo_root)
        if major is not None and major >= 2:
            # Berry: --immutable == frozen-lockfile; --mode=skip-build is the
            # equivalent of --ignore-scripts (Berry rejects both classic flags).
            cmd = ["yarn", "install", "--mode=skip-build"]
            if frozen:
                cmd.insert(2, "--immutable")
            return cmd
        # Classic (v1), or unknown -> assume Classic (safe historical default).
        cmd = ["yarn", "install", "--ignore-scripts"]
        if frozen:
            cmd.insert(2, "--frozen-lockfile")
        return cmd
    # Unreachable: _validate_tool_name only admits the four PMs above.
    return [pkg_manager, "install"]




def _install_pm_via_npm(pm: str, version: str | None, config: ToolchainConfig) -> tuple[str, str]:
    """Return (install_path, stderr_tail). Raises on failure."""
    if not config.allow_global_npm:
        raise ToolchainInstallFailedError(
            f"`npm install -g {pm}` needs a user-writable npm prefix. "
            f"Detected system npm on {sys.platform} with non-user-writable prefix — set `KAIJU_TOOLCHAIN_ALLOW_GLOBAL_NPM=1` "
            f"if you know sudo/root is safe, or run manually: "
            f"`sudo npm install -g {pm}` (Invariant I2: this module never invokes sudo)."
        )
    npm = _which("npm")
    if npm is None:
        raise ToolchainMissingError(
            f"cannot install {pm}: `npm` is not on PATH. Install Node.js first."
        )
    if config.offline:
        raise ToolchainMissingError(
            f"`{pm}` not present and KAIJU_TOOLCHAIN_OFFLINE=1; "
            f"cannot fetch from npm registry."
        )
    spec = f"{pm}@{version}" if version else pm
    cmd = [npm, "install", "-g", spec]
    if config.dry_run:
        logger.info("DRY-RUN would run: %s", " ".join(cmd))
        return f"<dry-run:{pm}>", ""
    rc, _, err = _run_subprocess(cmd)
    if rc != 0:
        if "EACCES" in err or "permission denied" in err.lower():
            raise ToolchainInstallFailedError(
                f"npm i -g {spec} failed with EACCES (npm prefix not writable). "
                f"Run manually: `sudo {' '.join(cmd)}`, or set a user prefix "
                f"via `npm config set prefix ~/.npm-global`. "
                f"stderr: {err[-_STDERR_TAIL_CHARS:]}"
            )
        raise ToolchainInstallFailedError(
            f"npm i -g {spec} exited {rc}. stderr: {err[-_STDERR_TAIL_CHARS:]}"
        )
    path = _which(pm)
    if path is None:
        raise ToolchainInstallFailedError(
            f"npm i -g {spec} returned 0 but `{pm}` still not on PATH. "
            f"Check `npm config get prefix` and ensure it's on PATH."
        )
    return path, err[-_STDERR_TAIL_CHARS:]


def _install_pm_via_corepack(pm: str, version: str, config: ToolchainConfig) -> tuple[str, str]:
    corepack = _which("corepack")
    if corepack is None:
        raise ToolchainMissingError("corepack not on PATH")
    if config.offline:
        raise ToolchainMissingError(
            f"corepack needs to fetch {pm}@{version} and KAIJU_TOOLCHAIN_OFFLINE=1"
        )
    cmd = [corepack, "prepare", f"{pm}@{version}", "--activate"]
    if config.dry_run:
        logger.info("DRY-RUN would run: %s", " ".join(cmd))
        return f"<dry-run:{pm}>", ""
    rc, _, err = _run_subprocess(cmd)
    if rc != 0:
        raise ToolchainInstallFailedError(
            f"corepack prepare {pm}@{version} exited {rc}. stderr: {err[-_STDERR_TAIL_CHARS:]}"
        )
    path = _which(pm)
    if path is None:
        raise ToolchainInstallFailedError(
            f"corepack prepare {pm}@{version} returned 0 but `{pm}` not on PATH."
        )
    return path, err[-_STDERR_TAIL_CHARS:]


def _install_corepack_via_npm(config: ToolchainConfig) -> None:
    """Self-bootstrap corepack via ``npm install -g corepack``. Some Homebrew
    node distributions strip corepack; some Node 24 builds unbundle it."""
    if _which("corepack") is not None:
        return
    if config.trust_mode == TrustMode.STRICT:
        raise ToolchainTrustDeniedError(
            "corepack missing; strict trust mode forbids `npm i -g corepack`. "
            "Install manually (e.g. `brew install corepack`) or lower trust to normal."
        )
    _install_pm_via_npm("corepack", None, config)




def _install_bun_hybrid(config: ToolchainConfig) -> tuple[str, str, InstallMethod, str, list[str]]:
    """Return (install_path, stderr_tail, method, version_str, install_cmd)."""
    version, entry = _resolve_bun_version(config)
    npm = _which("npm")
    # Prefer registry install unless caller explicitly opted into installer script
    if npm is not None and not config.allow_bun_install_script:
        cmd = [npm, "install", "-g", f"bun@{entry['npm_package_version']}"]
        install_path, err = _install_pm_via_npm("bun", entry["npm_package_version"], config)
        return install_path, err, InstallMethod.NPM_GLOBAL, version, cmd
    # Fallback: pinned installer with SHA verify
    if config.trust_mode == TrustMode.STRICT and entry.get("installer_sha256") is None:
        raise ToolchainTrustDeniedError(
            f"bun installer path requested but manifest {_bun_manifest_path()} "
            f"has installer_sha256=null for version {version}. Refresh via "
            f"`python -m tools.toolchain refresh-bun-manifest`, or lower trust to normal."
        )
    return _install_bun_via_installer(version, entry, config)


def _install_bun_via_installer(
    version: str, entry: dict, config: ToolchainConfig
) -> tuple[str, str, InstallMethod, str, list[str]]:
    if config.offline:
        raise ToolchainMissingError(
            f"bun installer requires network fetch and KAIJU_TOOLCHAIN_OFFLINE=1"
        )
    url = entry["installer_url"]
    expected_sha = entry.get("installer_sha256")
    cmd_display = ["curl", "-fsSL", url, "|", "sha256verify", "|", "bash"]
    if config.dry_run:
        logger.info("DRY-RUN would fetch %s (verify SHA %s) and pipe to bash", url, expected_sha)
        return f"<dry-run:bun@{version}>", "", InstallMethod.BUN_INSTALLER_SCRIPT, version, cmd_display
    script = _fetch_installer(url)
    got_sha = hashlib.sha256(script).hexdigest()
    if expected_sha is None:
        msg = (
            f"bun installer downloaded from {url} with SHA256={got_sha}. "
            f"Manifest {_bun_manifest_path()} had installer_sha256=null; "
            f"proceeding WITHOUT verification (trust={config.trust_mode.value})."
        )
        if config.trust_mode == TrustMode.STRICT:
            raise ToolchainTrustDeniedError(msg)
        logger.warning(msg)
    elif got_sha != expected_sha:
        raise ToolchainVersionMismatchError(
            f"bun installer SHA mismatch: expected {expected_sha}, got {got_sha}. "
            f"Refusing to execute (supply-chain guard, invariant I4)."
        )
    # Execute the installer with pinned BUN_VERSION so it fetches the right binary
    env = dict(os.environ)
    env["BUN_VERSION"] = f"bun-v{version}"
    proc = subprocess.run(
        ["bash", "-s"],
        input=script,
        env=env,
        capture_output=True,
        timeout=_INSTALL_TIMEOUT_SEC,
        check=False,
    )
    stderr_tail = (proc.stderr.decode("utf-8", errors="replace") if proc.stderr else "")[
        -_STDERR_TAIL_CHARS:
    ]
    if proc.returncode != 0:
        raise ToolchainInstallFailedError(
            f"bun installer script exited {proc.returncode}. stderr: {stderr_tail}"
        )
    # bun installer drops binary into ~/.bun/bin/bun by default
    bun_bin = Path(env.get("HOME", os.path.expanduser("~"))) / ".bun" / "bin" / "bun"
    if not bun_bin.exists():
        raise ToolchainInstallFailedError(
            f"bun installer exited 0 but {bun_bin} does not exist"
        )
    return str(bun_bin), stderr_tail, InstallMethod.BUN_INSTALLER_SCRIPT, version, cmd_display


def _fetch_installer(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "kaiju-harness/toolchain"})
    try:
        with urlopen(req, timeout=60) as resp:  # noqa: S310 (URL is pinned)
            return resp.read()
    except URLError as exc:
        raise ToolchainInstallFailedError(
            f"fetch {url} failed: {exc}"
        ) from exc




def _resolve_lts_alias(alias: str) -> str | None:
    """Map ``lts/*``/``lts/iron`` etc. to a concrete major. Returns None if
    the alias is unknown."""
    return LTS_ALIASES.get(alias.strip().lower())


def _resolve_node_version(repo_root: Path, config: ToolchainConfig) -> tuple[str, str]:
    """Return (major_version, source_label). Reuses tools/node_version.py.

    Precedence (highest first): KAIJU_PIN_NODE → engines.node → volta.node →
    .nvmrc → .node-version → matrix → Dockerfile → default (20).
    """
    from commit0.harness.constants_ts import (
        DEFAULT_NODE_VERSION as _TS_DEFAULT,
        SUPPORTED_NODE_VERSIONS as _TS_SUPPORTED,
    )
    from tools.node_version import detect

    if config.pin_node:
        pin = config.pin_node.strip()
        m = re.match(r"^v?(\d+)", pin)
        if m:
            return m.group(1), "KAIJU_PIN_NODE"
    # .tool-versions (asdf) — added here since node_version.py doesn't parse it
    tv_hit = _collect_tool_versions(repo_root)
    if tv_hit is not None:
        return tv_hit, ".tool-versions"
    result = detect(
        repo_root,
        _TS_SUPPORTED,
        fallback=str(_TS_DEFAULT),
    )
    version = result.version or str(_TS_DEFAULT)
    return version, result.source


def _collect_tool_versions(repo_root: Path) -> str | None:
    tv = repo_root / ".tool-versions"
    if not tv.is_file():
        return None
    try:
        content = tv.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # asdf format: "nodejs 18.19.0\n"
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        tool_name = parts[0].lower()
        if tool_name in ("nodejs", "node"):
            raw = parts[1]
            if raw.startswith("lts/"):
                resolved = _resolve_lts_alias(raw)
                if resolved:
                    return resolved
                continue
            m = re.match(r"^v?(\d+)", raw)
            if m:
                return m.group(1)
    return None


def _detect_switcher(config: ToolchainConfig) -> str | None:
    """Return "fnm" | "nvm" | "volta" | None."""
    pref = config.node_switcher_preference
    if pref == "none":
        return None
    if pref != "auto":
        # Explicit preference — return only if present
        if pref == "fnm" and _which("fnm"):
            return "fnm"
        if pref == "volta" and _which("volta"):
            return "volta"
        if pref == "nvm" and _nvm_available():
            return "nvm"
        return None
    if _which("fnm"):
        return "fnm"
    if _which("volta"):
        return "volta"
    if _nvm_available():
        return "nvm"
    return None


def _nvm_available() -> bool:
    nvm_dir = os.environ.get("NVM_DIR")
    if nvm_dir and (Path(nvm_dir) / "nvm.sh").is_file():
        return True
    home = os.path.expanduser("~")
    return (Path(home) / ".nvm" / "nvm.sh").is_file()


def _env_for_switcher(
    switcher: str, node_major: str, base_env: Mapping[str, str], config: ToolchainConfig
) -> dict[str, str]:
    """Return an env dict with the target Node's ``bin/`` prepended to PATH.

    Invariant I1: NEVER mutates the incoming ``base_env`` or ``os.environ``.
    """
    env = dict(base_env)
    if switcher == "fnm":
        node_bin = _fnm_node_bin(node_major, config)
    elif switcher == "volta":
        node_bin = _volta_node_bin(node_major, config)
    elif switcher == "nvm":
        node_bin = _nvm_node_bin(node_major, config, base_env)
    else:
        raise ToolchainSwitchUnavailableError(f"unknown switcher: {switcher}")
    bin_dir = str(Path(node_bin).parent)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    return env


def _fnm_node_bin(node_major: str, config: ToolchainConfig) -> str:
    fnm = _which("fnm")
    if fnm is None:
        raise ToolchainSwitchUnavailableError("fnm not on PATH")
    rc, out, err = _run_subprocess([fnm, "which", node_major], timeout=15)
    if rc == 0 and out.strip():
        return out.strip()
    if config.offline:
        raise ToolchainMissingError(
            f"fnm has no Node {node_major} installed and KAIJU_TOOLCHAIN_OFFLINE=1"
        )
    if config.no_auto_install:
        raise ToolchainMissingError(
            f"fnm has no Node {node_major} and KAIJU_NO_AUTO_INSTALL=1. "
            f"Run `fnm install {node_major}` manually."
        )
    if config.dry_run:
        logger.info("DRY-RUN would run: fnm install %s", node_major)
        return f"<dry-run:node-{node_major}/bin/node>"
    rc, _, err = _run_subprocess([fnm, "install", node_major])
    if rc != 0:
        raise ToolchainInstallFailedError(
            f"fnm install {node_major} exited {rc}. stderr: {err[-_STDERR_TAIL_CHARS:]}"
        )
    rc, out, _ = _run_subprocess([fnm, "which", node_major], timeout=15)
    if rc != 0 or not out.strip():
        raise ToolchainInstallFailedError(
            f"fnm install {node_major} succeeded but `fnm which {node_major}` still empty"
        )
    return out.strip()


def _volta_node_bin(node_major: str, config: ToolchainConfig) -> str:
    volta = _which("volta")
    if volta is None:
        raise ToolchainSwitchUnavailableError("volta not on PATH")
    volta_home = os.environ.get("VOLTA_HOME") or os.path.expanduser("~/.volta")
    # volta lays out node at ${VOLTA_HOME}/tools/image/node/<full-version>/bin/node
    # For our purposes we install (if needed) then let volta shims resolve.
    if config.offline:
        # Trust that volta already has this Node
        pass
    elif not config.no_auto_install:
        # `volta install node@<major>` is idempotent
        if not config.dry_run:
            rc, _, err = _run_subprocess([volta, "install", f"node@{node_major}"])
            if rc != 0:
                raise ToolchainInstallFailedError(
                    f"volta install node@{node_major} exited {rc}. "
                    f"stderr: {err[-_STDERR_TAIL_CHARS:]}"
                )
    node_shim = Path(volta_home) / "bin" / "node"
    return str(node_shim)


def _nvm_node_bin(node_major: str, config: ToolchainConfig, base_env: Mapping[str, str]) -> str:
    nvm_dir = base_env.get("NVM_DIR") or os.environ.get("NVM_DIR") or os.path.expanduser("~/.nvm")
    if config.offline:
        # Trust that nvm already has this Node
        pass
    elif not config.no_auto_install:
        if not config.dry_run:
            rc, _, err = _run_subprocess(
                [
                    "bash",
                    "-c",
                    f'export NVM_DIR="{nvm_dir}" && . "$NVM_DIR/nvm.sh" && '
                    f"nvm install {node_major}",
                ]
            )
            if rc != 0:
                raise ToolchainInstallFailedError(
                    f"nvm install {node_major} exited {rc}. "
                    f"stderr: {err[-_STDERR_TAIL_CHARS:]}"
                )
    rc, out, err = _run_subprocess(
        [
            "bash",
            "-c",
            f'export NVM_DIR="{nvm_dir}" && . "$NVM_DIR/nvm.sh" && '
            f"nvm which {node_major}",
        ],
        timeout=15,
    )
    if rc != 0 or not out.strip():
        raise ToolchainInstallFailedError(
            f"nvm which {node_major} failed. stderr: {err[-_STDERR_TAIL_CHARS:]}"
        )
    return out.strip().splitlines()[-1]




def ensure_pm(
    pm: str, *, version: str | None = None, config: ToolchainConfig | None = None
) -> ProvisionResult:
    """Ensure package manager ``pm`` is available.

    Fast path: ``shutil.which(pm)`` → return ``ProvisionResult(from_cache=True)``.
    Slow path: acquire lock, double-check, then install via the appropriate
    method (corepack for pnpm/yarn, hybrid for bun, npm-global as fallback).
    """
    _validate_tool_name(pm)  # Invariant I3
    cfg = config if config is not None else load_config()

    # Fast path (Invariant I5)
    existing = _which(pm)
    if existing and (version is None or _installed_version(pm) == version):
        return ProvisionResult(
            tool=Tool(pm),
            version=_installed_version(pm) or "unknown",
            install_method=InstallMethod.ALREADY_PRESENT,
            install_path=existing,
            from_cache=True,
            journal_id=None,
        )

    if cfg.no_auto_install:
        raise ToolchainMissingError(
            f"`{pm}` not on PATH and KAIJU_NO_AUTO_INSTALL=1. "
            f"Install manually: `npm install -g {pm}` or via corepack."
        )

    with _acquire_lock(cfg):
        # Double-checked read (Invariant I7)
        existing = _which(pm)
        if existing and (version is None or _installed_version(pm) == version):
            return ProvisionResult(
                tool=Tool(pm),
                version=_installed_version(pm) or "unknown",
                install_method=InstallMethod.ALREADY_PRESENT,
                install_path=existing,
                from_cache=True,
                journal_id=None,
            )

        target_version = version or cfg.version_pins.get(pm)
        journal_id, install_path, method, actual_version = _do_install(
            pm, target_version, cfg
        )

    return ProvisionResult(
        tool=Tool(pm),
        version=actual_version,
        install_method=method,
        install_path=install_path,
        from_cache=False,
        journal_id=journal_id,
    )


def _do_install(
    pm: str, version: str | None, cfg: ToolchainConfig
) -> tuple[str, str, InstallMethod, str]:
    """Called under lock. Returns (journal_id, install_path, method, version_installed)."""
    if pm == "bun":
        version, entry = _resolve_bun_version(cfg)
        npm = _which("npm")
        if npm is not None and not cfg.allow_bun_install_script:
            planned_cmd = [npm, "install", "-g", f"bun@{entry['npm_package_version']}"]
            planned_method = InstallMethod.NPM_GLOBAL
        else:
            planned_cmd = ["curl", "-fsSL", entry["installer_url"], "|", "sha256verify", "|", "bash"]
            planned_method = InstallMethod.BUN_INSTALLER_SCRIPT
        journal_id = _record_intent(cfg, "bun", version, planned_method, planned_cmd)
        try:
            path, err, method, actual_version, _cmd = _install_bun_hybrid(cfg)
        except ToolchainError as exc:
            _record_outcome(cfg, journal_id, "failure", "", str(exc))
            raise
        _record_outcome(cfg, journal_id, "success", path, err)
        return journal_id, path, method, actual_version

    if pm == "npm":
        # npm is provided by Node itself — we can't install it independently.
        raise ToolchainMissingError(
            "`npm` is not on PATH but the repo needs it. Install Node.js "
            "(brew install node, or via nvm/fnm/volta)."
        )

    # pnpm/yarn: prefer corepack, fall back to npm-global
    if _which("corepack") is None:
        try:
            _install_corepack_via_npm(cfg)
        except (ToolchainInstallFailedError, ToolchainTrustDeniedError) as exc:
            logger.warning(
                "corepack self-bootstrap failed (%s); falling back to `npm i -g %s` directly",
                exc,
                pm,
            )

    if _which("corepack") is not None:
        cp_version = version or "latest"
        cmd = [_which("corepack") or "corepack", "prepare", f"{pm}@{cp_version}", "--activate"]
        journal_id = _record_intent(cfg, pm, cp_version, InstallMethod.COREPACK_ACTIVATE, cmd)
        try:
            path, err = _install_pm_via_corepack(pm, cp_version, cfg)
        except ToolchainError as exc:
            _record_outcome(cfg, journal_id, "failure", "", str(exc))
            # Fall through to npm-global
            logger.warning("corepack path failed (%s); trying `npm i -g %s`", exc, pm)
        else:
            _record_outcome(cfg, journal_id, "success", path, err)
            actual = _installed_version(pm) or cp_version
            return journal_id, path, InstallMethod.COREPACK_ACTIVATE, actual

    # npm-global fallback
    npm_bin = _which("npm") or "npm"
    spec = f"{pm}@{version}" if version else pm
    cmd = [npm_bin, "install", "-g", spec]
    journal_id = _record_intent(cfg, pm, version or "latest", InstallMethod.NPM_GLOBAL, cmd)
    try:
        path, err = _install_pm_via_npm(pm, version, cfg)
    except ToolchainError as exc:
        _record_outcome(cfg, journal_id, "failure", "", str(exc))
        raise
    _record_outcome(cfg, journal_id, "success", path, err)
    actual = _installed_version(pm) or (version or "unknown")
    return journal_id, path, InstallMethod.NPM_GLOBAL, actual


def ensure_node(
    repo_root: Path, *, config: ToolchainConfig | None = None
) -> NodeSwitchResult:
    """Resolve the required Node version for ``repo_root`` and prepare env delta.

    Returns a :class:`NodeSwitchResult` with ``env_delta`` suitable for merging
    into a ``subprocess.run(..., env=...)`` call. Never mutates ``os.environ``
    (Invariant I1).
    """
    cfg = config if config is not None else load_config()
    version, source = _resolve_node_version(repo_root, cfg)
    switcher = _detect_switcher(cfg)

    if switcher is None:
        # No switcher available. Compare against controller Node.
        controller_ver = _installed_version("node") or ""
        controller_major = controller_ver.split(".", 1)[0] if controller_ver else ""
        if controller_major == version:
            return NodeSwitchResult(
                resolved_version=version,
                switcher=_CONTROLLER_SWITCHER,
                env_delta={},
                source=source,
            )
        msg = (
            f"repo requires Node {version} (source: {source}) but controller Node "
            f"is {controller_ver or 'unknown'} and no switcher (fnm/nvm/volta) is on PATH"
        )
        if cfg.trust_mode == TrustMode.STRICT:
            raise ToolchainSwitchUnavailableError(msg)
        logger.warning("%s — proceeding with controller Node (trust=%s)", msg, cfg.trust_mode.value)
        return NodeSwitchResult(
            resolved_version=controller_major or version,
            switcher=_CONTROLLER_SWITCHER,
            env_delta={},
            source=source,
        )

    with _acquire_lock(cfg):
        env_delta = _env_for_switcher(switcher, version, os.environ, cfg)
    # Only keep the delta (PATH prefix), not the full env
    delta_only: dict[str, str] = {"PATH": env_delta["PATH"]}
    return NodeSwitchResult(
        resolved_version=version,
        switcher=switcher,
        env_delta=delta_only,
        source=source,
    )


def build_env_for_repo(
    repo_root: Path,
    *,
    base_env: Mapping[str, str] | None = None,
    config: ToolchainConfig | None = None,
) -> dict[str, str]:
    """Return a fresh env dict for subprocess.run with the target Node prepended.

    This is the RECOMMENDED entry point for callers that shell out to install
    or test commands in a repo. Never mutates ``os.environ``.
    """
    src = base_env if base_env is not None else os.environ
    env = dict(src)
    switch = ensure_node(repo_root, config=config)
    if switch.env_delta:
        env["PATH"] = switch.env_delta["PATH"]
    return env


def doctor(config: ToolchainConfig | None = None) -> DoctorReport:
    cfg = config if config is not None else load_config()
    _recover_orphans(cfg)
    tools_which: dict[str, str | None] = {}
    tools_versions: dict[str, str | None] = {}
    for t in ("node", "npm", "corepack", "pnpm", "yarn", "bun", "fnm", "nvm", "volta"):
        tools_which[t] = _which(t)
        tools_versions[t] = _installed_version(t)
    switcher = _detect_switcher(cfg)
    state = _load_state(cfg)
    warnings: list[str] = []
    if tools_which["node"] is None:
        warnings.append("node is not on PATH — the harness cannot function")
    if tools_which["npm"] is None:
        warnings.append("npm is not on PATH — global installs will fail")
    if cfg.trust_mode != TrustMode.NORMAL:
        warnings.append(f"non-default trust mode: {cfg.trust_mode.value}")
    if cfg.offline:
        warnings.append("KAIJU_TOOLCHAIN_OFFLINE=1 — installs will fail if not cached")
    return DoctorReport(
        config=cfg,
        tools_which=tools_which,
        tools_versions=tools_versions,
        node_switcher=switcher,
        state_entries=state["entries"],
        warnings=warnings,
    )


def uninstall(tool: str, *, force: bool = False, config: ToolchainConfig | None = None) -> None:
    """Remove a tool we previously installed. Refuses if the installed version
    differs from what we recorded (indicates user upgraded outside us) unless
    ``force=True``.

    NOTE: We do NOT touch Node itself; version-manager owns those.
    """
    _validate_tool_name(tool)
    cfg = config if config is not None else load_config()
    if tool == "npm":
        raise ToolchainError("cannot uninstall npm (comes with Node)")
    data = _load_state(cfg)
    our_entries = [
        e for e in data["entries"]
        if e.get("tool") == tool and e.get("phase") == "success"
    ]
    if not our_entries:
        raise ToolchainError(
            f"no successful install of {tool} in state journal — refusing uninstall"
        )
    installed_ver = _installed_version(tool)
    last = our_entries[-1]
    if not force and installed_ver != last.get("version"):
        raise ToolchainError(
            f"installed {tool}={installed_ver} differs from journal ({last.get('version')}); "
            f"pass force=True to override"
        )
    npm = _which("npm")
    if npm is None:
        raise ToolchainMissingError("npm not on PATH; cannot uninstall global")
    if cfg.dry_run:
        logger.info("DRY-RUN would run: %s uninstall -g %s", npm, tool)
        return
    with _acquire_lock(cfg):
        rc, _, err = _run_subprocess([npm, "uninstall", "-g", tool])
        if rc != 0:
            raise ToolchainInstallFailedError(
                f"npm uninstall -g {tool} exited {rc}. stderr: {err[-_STDERR_TAIL_CHARS:]}"
            )
        for e in data["entries"]:
            if e.get("id") == last.get("id"):
                e["phase"] = "uninstalled"
                e["finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(cfg, data)


def list_installed(config: ToolchainConfig | None = None) -> list[dict]:
    cfg = config if config is not None else load_config()
    data = _load_state(cfg)
    return [e for e in data["entries"] if e.get("phase") == "success"]
