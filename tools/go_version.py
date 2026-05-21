"""Go toolchain detection for kaiju.

Replaces the partial detection in ``tools/prepare_repo_go.py`` (which read
``go.mod`` ``go X.Y`` directive but had a hardcoded ``"1.23"`` fallback and
no awareness of the ``toolchain`` directive, GHA matrices, or Dockerfiles).

Detection priority (most → least authoritative):

  1. ``go.mod[toolchain]`` — Go 1.21+ exact toolchain pin (e.g. ``go1.22.5``)
  2. ``go.mod[go]`` — language version directive
  3. GHA matrix ``go-version`` values
  4. ``Dockerfile`` ``FROM golang:X.Y``
  5. caller-supplied fallback

Conflicts (lower-priority signals disagreeing with the chosen value) are
reported via ``conflicts`` for visibility but never raise — go.mod's
``go`` directive can lag the real minimum (Go is forgiving about this).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "GoDetectionResult",
    "collect_signals",
    "detect",
]


_GO_TOOLCHAIN = "go.mod[toolchain]"
_GO_DIRECTIVE = "go.mod[go]"
_GHA_GO_MATRIX = ".github/workflows/*.yml[matrix.go-version]"
_DOCKERFILE_GOLANG_FROM = "Dockerfile[FROM golang:X]"


@dataclass(frozen=True)
class GoDetectionResult:
    version: str | None
    source: str
    conflicts: list[str] = field(default_factory=list)
    all_signals: dict[str, str] = field(default_factory=dict)


def _read_go_mod(repo_root: Path) -> tuple[str | None, str | None]:
    """Return ``(go_version, toolchain)`` parsed from ``go.mod``."""
    f = repo_root / "go.mod"
    if not f.is_file():
        return None, None
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    go_ver: str | None = None
    toolchain: str | None = None
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("go ") and go_ver is None:
            parts = s.split(None, 1)
            if len(parts) == 2 and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", parts[1]):
                go_ver = parts[1]
        elif s.startswith("toolchain ") and toolchain is None:
            parts = s.split(None, 1)
            if len(parts) == 2:
                # toolchain go1.22.5 → 1.22.5
                tc = parts[1].lstrip("go")
                if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", tc):
                    toolchain = tc
    return go_ver, toolchain


def _read_gha_matrix(repo_root: Path) -> str | None:
    workflows = repo_root / ".github" / "workflows"
    if not workflows.is_dir():
        return None
    versions: set[str] = set()
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(
            r"go-version\s*:\s*(\[[^\]]+\]|(?:\n[ \t]+-[ \t]+['\"]?[^\n]+['\"]?)+|['\"]?[\d.x]+['\"]?)",
            content,
        ):
            block = m.group(1)
            for v in re.findall(r"['\"]?(\d+\.\d+(?:\.\d+)?)['\"]?", block):
                versions.add(v)
    if not versions:
        return None
    return ", ".join(sorted(versions, key=_vkey))


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


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
            r"^\s*FROM\s+(?:[\w./-]+/)?golang:(\d+\.\d+(?:\.\d+)?)",
            content,
            re.MULTILINE | re.IGNORECASE,
        )
        if m:
            return m.group(1)
    return None


def collect_signals(repo_root: Path) -> dict[str, str]:
    """Return a flat map of ``source_label → raw_version`` for every Go signal."""
    result: dict[str, str] = {}
    go_ver, toolchain = _read_go_mod(repo_root)
    if toolchain is not None:
        result[_GO_TOOLCHAIN] = toolchain
    if go_ver is not None:
        result[_GO_DIRECTIVE] = go_ver
    if (m := _read_gha_matrix(repo_root)) is not None:
        result[_GHA_GO_MATRIX] = m
    if (d := _read_dockerfile_from(repo_root)) is not None:
        result[_DOCKERFILE_GOLANG_FROM] = d
    return result


_PRIORITY: list[str] = [
    _GO_TOOLCHAIN,
    _GO_DIRECTIVE,
    _GHA_GO_MATRIX,
    _DOCKERFILE_GOLANG_FROM,
]


def detect(
    repo_root: Path,
    *,
    fallback: str = "1.22",
) -> GoDetectionResult:
    """Detect Go version for ``repo_root``."""
    signals = collect_signals(repo_root)
    if not signals:
        return GoDetectionResult(
            version=fallback,
            source="default",
            conflicts=[],
            all_signals={},
        )

    chosen: str | None = None
    chosen_source: str | None = None
    for source in _PRIORITY:
        if source in signals:
            raw = signals[source]
            # GHA matrix may contain multiple values; pick the minimum (lowest CI)
            if "," in raw:
                chosen = min(
                    [v.strip() for v in raw.split(",") if v.strip()],
                    key=_vkey,
                )
            else:
                chosen = raw
            chosen_source = source
            break

    if chosen is None:
        return GoDetectionResult(
            version=fallback,
            source="default",
            conflicts=[],
            all_signals=signals,
        )

    conflicts: list[str] = []
    for source, raw in signals.items():
        if source == chosen_source:
            continue
        # GHA matrix is multi-value; flag conflict if chosen not in the matrix
        if source == _GHA_GO_MATRIX:
            matrix_vs = [v.strip() for v in raw.split(",") if v.strip()]
            if chosen not in matrix_vs:
                conflicts.append(f"{source}({raw})")
            continue
        if raw != chosen:
            conflicts.append(f"{source}({raw})")

    return GoDetectionResult(
        version=chosen,
        source=chosen_source or "default",
        conflicts=conflicts,
        all_signals=signals,
    )
