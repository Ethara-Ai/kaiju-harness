"""Node.js version detection for TypeScript / JavaScript repos.

Replaces the silent hardcoded ``node_version = "20"`` in
``tools/prepare_repo_ts.py``. Same two-tier algorithm as
``tools/python_version.py`` but with Node-specific signal sources.

**Schema note:** the dataset entry key is ``setup["node_version"]`` (read by
``commit0/harness/spec_ts.py:_get_node_version`` and the kaiju build wrapper).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable

from tools._versioning import (
    DetectionResult,
    NoSignalsError,
    Signal,
    Tier,
    VersionConflictError,
    normalize_semver_range,
    parse_constraint_str,
    resolve_two_tier,
)

logger = logging.getLogger(__name__)

__all__ = [
    "collect_signals",
    "detect",
    "detect_from_signals",
]


# Source labels (used in DetectionResult.source)
_PACKAGE_ENGINES_NODE = "package.json[engines.node]"
_PACKAGE_VOLTA_NODE = "package.json[volta.node]"
_NVMRC = ".nvmrc"
_NODE_VERSION_FILE = ".node-version"
_GHA_NODE_MATRIX = ".github/workflows/*.yml[matrix.node-version]"
_DOCKERFILE_NODE_FROM = "Dockerfile[FROM node:X]"


def _collect_package_engines_volta(repo_root: Path) -> list[Signal]:
    pkg = repo_root / "package.json"
    if not pkg.is_file():
        return []
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []

    signals: list[Signal] = []

    engines = data.get("engines") or {}
    node_range = engines.get("node") if isinstance(engines, dict) else None
    if isinstance(node_range, str) and node_range.strip():
        spec = normalize_semver_range(node_range)
        if spec is not None:
            signals.append(
                Signal(
                    source=_PACKAGE_ENGINES_NODE,
                    tier=Tier.A_DECLARED,
                    constraint=spec,
                    raw=node_range,
                )
            )

    volta = data.get("volta") or {}
    volta_node = volta.get("node") if isinstance(volta, dict) else None
    if isinstance(volta_node, str) and volta_node.strip():
        # Volta pins are usually exact versions like "20.10.0"
        spec = parse_constraint_str(volta_node)
        if spec is not None:
            signals.append(
                Signal(
                    source=_PACKAGE_VOLTA_NODE,
                    tier=Tier.A_DECLARED,
                    constraint=spec,
                    raw=volta_node,
                )
            )

    return signals


def _collect_nvmrc(repo_root: Path) -> Signal | None:
    nvmrc = repo_root / ".nvmrc"
    if not nvmrc.is_file():
        return None
    raw = nvmrc.read_text(encoding="utf-8", errors="replace").strip()
    # Strip leading "v" / "node-" / etc.
    cleaned = re.sub(r"^(node-|v)", "", raw, flags=re.IGNORECASE)
    spec = parse_constraint_str(cleaned)
    if spec is None:
        # Bare "lts/iron" / "lts/*" — not actionable, skip
        return None
    return Signal(
        source=_NVMRC,
        tier=Tier.A_DECLARED,
        constraint=spec,
        raw=raw,
    )


def _collect_node_version_file(repo_root: Path) -> Signal | None:
    f = repo_root / ".node-version"
    if not f.is_file():
        return None
    raw = f.read_text(encoding="utf-8", errors="replace").strip()
    cleaned = re.sub(r"^v", "", raw)
    spec = parse_constraint_str(cleaned)
    if spec is None:
        return None
    return Signal(
        source=_NODE_VERSION_FILE,
        tier=Tier.A_DECLARED,
        constraint=spec,
        raw=raw,
    )


def _collect_gha_matrix(repo_root: Path) -> Signal | None:
    workflows = repo_root / ".github" / "workflows"
    if not workflows.is_dir():
        return None
    versions: set[str] = set()
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Match both `node-version:` and `nodejs-version:` (some repos use the latter)
        for m in re.finditer(
            r"(?:node|nodejs)-version\s*:\s*(\[[^\]]+\]|(?:\n[ \t]+-[ \t]+['\"]?[^\n]+['\"]?)+|['\"]?\d+(?:\.\d+)*['\"]?)",
            content,
        ):
            block = m.group(1)
            # Extract bare integers / X.Y / X.Y.Z (strip 'v' prefix if present)
            for v in re.findall(r"(?<![\d.])v?(\d+)(?:\.\d+){0,2}(?![\d.])", block):
                versions.add(v)
    if not versions:
        return None
    return Signal(
        source=_GHA_NODE_MATRIX,
        tier=Tier.B_MATRIX,
        versions=tuple(sorted(versions, key=int)),
        raw=", ".join(sorted(versions, key=int)),
    )


def _collect_dockerfile(repo_root: Path) -> Signal | None:
    candidates = [
        repo_root / "Dockerfile",
        repo_root / "Dockerfile.dev",
        repo_root / "docker" / "Dockerfile",
    ]
    versions: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(
            r"^\s*FROM\s+(?:[\w./-]+/)?node:(\d+)",
            content,
            re.MULTILINE | re.IGNORECASE,
        ):
            versions.add(m.group(1))
    if not versions:
        return None
    return Signal(
        source=_DOCKERFILE_NODE_FROM,
        tier=Tier.C_HINT,
        versions=tuple(sorted(versions, key=int)),
        raw=", ".join(sorted(versions, key=int)),
    )


def collect_signals(repo_root: Path) -> list[Signal]:
    """Scan ``repo_root`` for every Node-version signal kaiju knows about."""
    signals: list[Signal] = []
    collectors = [
        _collect_package_engines_volta,  # returns list[Signal]
        _collect_nvmrc,
        _collect_node_version_file,
        _collect_gha_matrix,
        _collect_dockerfile,
    ]
    for collector in collectors:
        try:
            result = collector(repo_root)
        except Exception:  # noqa: BLE001
            logger.exception("Node signal collector %s crashed", collector.__name__)
            continue
        if result is None:
            continue
        if isinstance(result, list):
            signals.extend(result)
        else:
            signals.append(result)
    return signals


def detect_from_signals(
    signals: list[Signal], supported: Iterable[str]
) -> DetectionResult:
    """Pure resolver — feed synthetic signal lists in tests."""
    # Node majors compared via ``X.0.0`` so semver ``<21`` excludes 21 cleanly
    return resolve_two_tier(signals, supported, version_template="{}.0.0", pick="min")


def detect(
    repo_root: Path,
    supported: Iterable[str],
    *,
    fallback: str | None = None,
    strict: bool = False,
) -> DetectionResult:
    """Detect node version for ``repo_root``. See ``python_version.detect`` for kwargs."""
    signals = collect_signals(repo_root)
    result = detect_from_signals(signals, supported)
    if result.version is None:
        if strict:
            raise NoSignalsError(
                f"No Node.js version signals found in {repo_root}. "
                f"Add `engines.node` to package.json or create a `.nvmrc` file."
            )
        if fallback is not None:
            return DetectionResult(
                version=fallback,
                source="default",
                conflicts=[],
                all_signals=result.all_signals,
            )
    return result
