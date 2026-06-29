"""Rust toolchain detection for kaiju.

Replaces the silent hardcoded ``rust_version="stable"`` /
``edition="2021"`` in ``tools/prepare_repo_rust.py``.

Rust differs from Python/Node in two ways:

1. **Channels and versions coexist.** A repo may pin to a channel
   (``stable``, ``beta``, ``nightly``) instead of a numeric version. We
   honor channel pins verbatim — there's no SpecifierSet for ``"stable"``.
2. **One Docker image, any toolchain.** The harness has a single
   ``Dockerfile.rust`` that uses ``rustup`` to install whatever toolchain
   the entry requests. So unlike Python, we don't clamp to a SUPPORTED
   set — anything ``rustup`` can install is fair game.

Detection priority (most → least authoritative):

  1. ``rust-toolchain.toml[toolchain.channel]``
  2. ``rust-toolchain`` (legacy plaintext file)
  3. ``Cargo.toml[package.rust-version]`` (MSRV — minimum, not exact)
  4. GitHub Actions matrix ``rust:`` / ``toolchain:`` values
  5. ``Dockerfile`` ``FROM rust:X``
  6. caller-supplied fallback (e.g. ``"stable"``)

Edition detection: ``Cargo.toml[package.edition]`` → ``"2015"|"2018"|"2021"|"2024"``.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "RUST_CHANNELS",
    "RustDetectionResult",
    "collect_signals",
    "detect",
    "detect_edition",
]


RUST_CHANNELS: frozenset[str] = frozenset({"stable", "beta", "nightly"})
_RUST_TOOLCHAIN_TOML = "rust-toolchain.toml[toolchain.channel]"
_RUST_TOOLCHAIN_FILE = "rust-toolchain"
_CARGO_RUST_VERSION = "Cargo.toml[package.rust-version]"
_GHA_RUST_MATRIX = ".github/workflows/*.yml[matrix.rust]"
_DOCKERFILE_RUST_FROM = "Dockerfile[FROM rust:X]"
_CARGO_EDITION = "Cargo.toml[package.edition]"


@dataclass(frozen=True)
class RustDetectionResult:
    """Outcome of ``detect()``.

    Attributes
    ----------
    version
        Either a channel (``"stable"``/``"beta"``/``"nightly"``) or a
        specific version like ``"1.70"`` / ``"1.70.0"``.
    edition
        Rust edition (``"2015"``/``"2018"``/``"2021"``/``"2024"``).
    source
        Which file produced the chosen version.
    edition_source
        Which file produced the chosen edition (``"Cargo.toml"`` or
        ``"default"``).
    conflicts
        Other signals that disagreed with the chosen value.
    all_signals
        Map of source → raw text, for debugging / report mode.

    """

    version: str | None
    edition: str
    source: str
    edition_source: str
    conflicts: list[str] = field(default_factory=list)
    all_signals: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Collectors — each returns a dict {source_label: raw_value} or {}.
# We keep things flat-dict rather than Signal objects because Rust
# resolution is priority-based (not SpecifierSet intersection).
# ---------------------------------------------------------------------------


def _read_rust_toolchain_toml(repo_root: Path) -> str | None:
    f = repo_root / "rust-toolchain.toml"
    if not f.is_file():
        return None
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    tc = data.get("toolchain")
    if not isinstance(tc, dict):
        return None
    chan = tc.get("channel")
    if isinstance(chan, str) and chan.strip():
        return chan.strip()
    return None


def _read_rust_toolchain_file(repo_root: Path) -> str | None:
    f = repo_root / "rust-toolchain"
    if not f.is_file():
        return None
    raw = f.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return None
    # The legacy file format is just the channel name on one line
    # (or sometimes TOML if someone gave it the wrong extension)
    first = raw.splitlines()[0].strip()
    if first.startswith("[toolchain]") or "channel" in first:
        # User put TOML in the wrong file — try parsing
        try:
            data = tomllib.loads(raw)
            tc = data.get("toolchain")
            if isinstance(tc, dict) and isinstance(tc.get("channel"), str):
                return tc["channel"].strip()
        except tomllib.TOMLDecodeError:
            return None
    return first


def _read_cargo_rust_version(repo_root: Path) -> str | None:
    cargo = repo_root / "Cargo.toml"
    if not cargo.is_file():
        return None
    try:
        data = tomllib.loads(cargo.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    pkg = data.get("package")
    if not isinstance(pkg, dict):
        return None
    rv = pkg.get("rust-version")
    if isinstance(rv, str) and rv.strip():
        return rv.strip()
    return None


def _read_cargo_edition(repo_root: Path) -> str | None:
    cargo = repo_root / "Cargo.toml"
    if not cargo.is_file():
        return None
    try:
        data = tomllib.loads(cargo.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    pkg = data.get("package")
    if not isinstance(pkg, dict):
        return None
    ed = pkg.get("edition")
    if isinstance(ed, str) and re.fullmatch(r"20\d{2}", ed):
        return ed
    return None


def _read_gha_matrix(repo_root: Path) -> tuple[list[str], str | None]:
    workflows = repo_root / ".github" / "workflows"
    if not workflows.is_dir():
        return [], None
    versions: set[str] = set()
    raw_seen = ""
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Match `rust:` or `toolchain:` matrix entries
        # NB: the list-block branch matches whole lines as `[^\n]+` with NO
        # optional quotes wrapped around it — the previous `['\"]?[^\n]+['\"]?`
        # created a nested-quantifier ambiguity that backtracks pathologically
        # (ReDoS) on crafted/malformed workflow YAML, which is repo-controlled.
        for m in re.finditer(
            r"(?:rust|toolchain)\s*:\s*(\[[^\]]+\]|(?:\n[ \t]+-[ \t]+[^\n]+)+|['\"]?[\w.\-]+['\"]?)",
            content,
        ):
            block = m.group(1)
            raw_seen = raw_seen or block
            # Channels (stable/beta/nightly) or numeric versions
            for tok in re.findall(r"['\"]?(stable|beta|nightly|\d+\.\d+(?:\.\d+)?)['\"]?", block):
                versions.add(tok)
    return sorted(versions), raw_seen or None


def _read_dockerfile_from(repo_root: Path) -> str | None:
    for path in [
        repo_root / "Dockerfile",
        repo_root / "Dockerfile.dev",
        repo_root / "docker" / "Dockerfile",
    ]:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(
            r"^\s*FROM\s+(?:[\w./-]+/)?rust:([\w.\-]+)",
            content,
            re.MULTILINE | re.IGNORECASE,
        )
        if m:
            return m.group(1)
    return None


def collect_signals(repo_root: Path) -> dict[str, str]:
    """Return a flat map of ``source_label → raw_value`` for every Rust signal."""
    result: dict[str, str] = {}
    if (v := _read_rust_toolchain_toml(repo_root)) is not None:
        result[_RUST_TOOLCHAIN_TOML] = v
    if (v := _read_rust_toolchain_file(repo_root)) is not None:
        result[_RUST_TOOLCHAIN_FILE] = v
    if (v := _read_cargo_rust_version(repo_root)) is not None:
        result[_CARGO_RUST_VERSION] = v
    versions, raw = _read_gha_matrix(repo_root)
    if versions:
        result[_GHA_RUST_MATRIX] = ", ".join(versions)
    if (v := _read_dockerfile_from(repo_root)) is not None:
        result[_DOCKERFILE_RUST_FROM] = v
    if (v := _read_cargo_edition(repo_root)) is not None:
        result[_CARGO_EDITION] = v
    return result


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


# Priority order (highest → lowest)
_PRIORITY: list[str] = [
    _RUST_TOOLCHAIN_TOML,
    _RUST_TOOLCHAIN_FILE,
    _CARGO_RUST_VERSION,
    _GHA_RUST_MATRIX,
    _DOCKERFILE_RUST_FROM,
]


def _normalize_toolchain(raw: str) -> str:
    """Strip MSRV operators (``>=1.70`` → ``1.70``), Docker image tags
    (``1.70-slim`` → ``1.70``), and uppercase noise.
    """
    s = raw.strip().lower()
    # Strip leading PEP440-like operators
    s = re.sub(r"^[<>=!~^]+\s*", "", s)
    # Docker tag suffix like "1.70-slim" or "1.70-bullseye"
    s = s.split("-", 1)[0] if re.match(r"^\d+(\.\d+){0,2}-", s) else s
    return s


def detect_edition(repo_root: Path, *, default: str = "2021") -> tuple[str, str]:
    """Return ``(edition, source)``. ``source`` is ``"Cargo.toml"`` or ``"default"``."""
    ed = _read_cargo_edition(repo_root)
    if ed is not None:
        return ed, _CARGO_EDITION
    return default, "default"


def detect(
    repo_root: Path,
    *,
    fallback: str = "stable",
) -> RustDetectionResult:
    """Detect Rust toolchain + edition for ``repo_root``.

    Returns a :class:`RustDetectionResult`. Unlike Python/Node, this function
    never raises — Rust has no notion of "conflict" because the resolver picks
    by priority, not intersection. Disagreeing lower-priority signals are
    reported via ``conflicts``.
    """
    signals = collect_signals(repo_root)
    all_signals = dict(signals)

    edition, edition_source = detect_edition(repo_root)

    # Pop edition from version-signals view
    signals_for_version = {k: v for k, v in signals.items() if k != _CARGO_EDITION}

    chosen_value: str | None = None
    chosen_source: str | None = None
    for source in _PRIORITY:
        if source in signals_for_version:
            chosen_value = _normalize_toolchain(signals_for_version[source])
            chosen_source = source
            break

    if chosen_value is None:
        return RustDetectionResult(
            version=fallback,
            edition=edition,
            source="default",
            edition_source=edition_source,
            conflicts=[],
            all_signals=all_signals,
        )

    # Build conflicts list: other Tier-A signals that disagree with chosen
    conflicts: list[str] = []
    for source, raw in signals_for_version.items():
        if source == chosen_source:
            continue
        normalized = _normalize_toolchain(raw)
        # Channel signals and version signals can co-exist (Cargo MSRV + GHA stable)
        # so only flag a conflict if both are numeric and don't match.
        if (
            chosen_value not in RUST_CHANNELS
            and normalized not in RUST_CHANNELS
            and normalized != chosen_value
        ):
            conflicts.append(f"{source}({raw})")

    return RustDetectionResult(
        version=chosen_value,
        edition=edition,
        source=chosen_source or "default",
        edition_source=edition_source,
        conflicts=conflicts,
        all_signals=all_signals,
    )
