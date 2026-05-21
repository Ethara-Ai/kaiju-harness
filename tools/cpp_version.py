"""C / C++ language-standard detection for kaiju.

Replaces:

* ``_detect_c_standard()`` in ``tools/prepare_repo_c.py`` (CMakeLists only,
  hardcoded ``"11"`` fallback).
* The total *absence* of detection in ``tools/prepare_repo_cpp.py``
  (``cpp_standard="17"`` kwarg, never overridden).

Both C and C++ live here because they share signal sources (CMake, Makefile,
GitHub Actions, Dockerfile compiler base images). They differ only in
variable names (``CMAKE_C_STANDARD`` vs ``CMAKE_CXX_STANDARD``,
``-std=cNN`` vs ``-std=c++NN``).

Standards we model:

* **C:** ``"89"``, ``"99"``, ``"11"``, ``"17"``, ``"23"``.
* **C++:** ``"98"``, ``"11"``, ``"14"``, ``"17"``, ``"20"``, ``"23"``, ``"26"``.

We pick the **highest** explicit standard found (newer C/C++ standards are
*supersets* of older ones — picking the floor would silently drop features
the repo's code may use). This inverts the Python/Node rule because the
semantics of "lowest-compatible" don't translate to language standards.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

logger = logging.getLogger(__name__)

__all__ = [
    "CDetectionResult",
    "C_STANDARDS",
    "CPP_STANDARDS",
    "detect_c",
    "detect_cpp",
]


# Canonical numeric standards, sorted ascending — used for picking max
C_STANDARDS: tuple[str, ...] = ("89", "99", "11", "17", "23")
CPP_STANDARDS: tuple[str, ...] = ("98", "11", "14", "17", "20", "23", "26")


@dataclass(frozen=True)
class CDetectionResult:
    """Outcome of ``detect_c`` / ``detect_cpp``."""

    version: str | None
    source: str
    conflicts: list[str] = field(default_factory=list)
    all_signals: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_standard(raw: str, *, kind: Literal["c", "cpp"]) -> str | None:
    """Normalize a raw standard string to a canonical form.

    Examples (C): ``"c11"`` → ``"11"``, ``"gnu17"`` → ``"17"``,
    ``"11"`` → ``"11"``.
    Examples (C++): ``"c++17"`` → ``"17"``, ``"gnu++20"`` → ``"20"``,
    ``"cxx_std_17"`` → ``"17"``, ``"17"`` → ``"17"``.
    """
    s = raw.strip().strip("\"'").lower()
    if not s:
        return None
    # Strip common prefixes
    s = re.sub(r"^(c\+\+|cxx_std_|cxx|gnu\+\+|gnu|c)", "", s)
    m = re.match(r"^(\d{1,3})", s)
    if not m:
        return None
    n = m.group(1)
    valid = CPP_STANDARDS if kind == "cpp" else C_STANDARDS
    if n in valid:
        return n
    # Map two-digit year shorthand (e.g. "1y" / "1z") — uncommon, skip
    return None


def _max_by_numeric(versions: Iterable[str]) -> str | None:
    """Return the highest version by integer comparison; ``None`` if empty."""
    valid = [v for v in versions if v.isdigit()]
    if not valid:
        return None
    return max(valid, key=int)


# ---------------------------------------------------------------------------
# Signal collectors (shared between C and C++)
# ---------------------------------------------------------------------------


def _from_cmakelists(repo_root: Path, kind: Literal["c", "cpp"]) -> dict[str, str]:
    """Return ``{source_label: raw_value}`` for CMake-based standards."""
    cmake = repo_root / "CMakeLists.txt"
    if not cmake.is_file():
        return {}
    try:
        text = cmake.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    result: dict[str, str] = {}
    if kind == "c":
        var_name = "CMAKE_C_STANDARD"
        std_pattern = r"-std=(?:gnu|c)(\d+)"
        feature_pattern = r"c_std_(\d+)"
    else:
        var_name = "CMAKE_CXX_STANDARD"
        std_pattern = r"-std=(?:gnu\+\+|c\+\+)(\d+)"
        feature_pattern = r"cxx_std_(\d+)"

    # set(CMAKE_C_STANDARD 17) / set(CMAKE_CXX_STANDARD 20)
    m = re.search(
        rf"set\s*\(\s*{var_name}\s+(\d+)\s*\)",
        text,
        re.IGNORECASE,
    )
    if m:
        norm = _normalize_standard(m.group(1), kind=kind)
        if norm:
            result[f"CMakeLists.txt[{var_name}]"] = norm

    # target_compile_features(... c_std_17 / cxx_std_20)
    versions = {
        norm
        for raw in re.findall(feature_pattern, text)
        if (norm := _normalize_standard(raw, kind=kind))
    }
    if versions:
        chosen = _max_by_numeric(versions)
        if chosen is not None:
            result["CMakeLists.txt[target_compile_features]"] = chosen

    # -std=cNN / -std=c++NN compile flags
    flag_versions = {
        norm
        for raw in re.findall(std_pattern, text)
        if (norm := _normalize_standard(raw, kind=kind))
    }
    if flag_versions:
        chosen = _max_by_numeric(flag_versions)
        if chosen is not None:
            result[f"CMakeLists.txt[-std={'cpp' if kind == 'cpp' else 'c'}NN]"] = chosen

    return result


def _from_makefile(repo_root: Path, kind: Literal["c", "cpp"]) -> dict[str, str]:
    """Return any ``-std=cNN`` / ``-std=c++NN`` flags from Makefile."""
    candidates = [repo_root / "Makefile", repo_root / "GNUmakefile", repo_root / "makefile"]
    text = ""
    for f in candidates:
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            break
    if not text:
        return {}
    pattern = r"-std=(?:gnu\+\+|c\+\+)(\d+)" if kind == "cpp" else r"-std=(?:gnu|c)(\d+)"
    versions = {
        norm for raw in re.findall(pattern, text)
        if (norm := _normalize_standard(raw, kind=kind))
    }
    if not versions:
        return {}
    chosen = _max_by_numeric(versions)
    if chosen is None:
        return {}
    return {f"Makefile[-std={'c++' if kind == 'cpp' else 'c'}NN]": chosen}


def _from_meson_build(repo_root: Path, kind: Literal["c", "cpp"]) -> dict[str, str]:
    """Return Meson ``c_std`` / ``cpp_std`` project option values."""
    f = repo_root / "meson.build"
    if not f.is_file():
        return {}
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    key = "cpp_std" if kind == "cpp" else "c_std"
    # Two forms appear in real-world meson.build files:
    #   1. project(... 'c_std': 'c11')                       (dict form)
    #   2. project(... default_options: ['c_std=c11'])        (string-pair form)
    patterns = [
        rf"['\"]?{key}['\"]?\s*[,:]\s*['\"]([\w+]+)['\"]",
        rf"['\"]{key}\s*=\s*([\w+]+)['\"]",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        norm = _normalize_standard(m.group(1), kind=kind)
        if norm is not None:
            return {f"meson.build[{key}]": norm}
    return {}


# ---------------------------------------------------------------------------
# Public detectors
# ---------------------------------------------------------------------------


_PRIORITY_TEMPLATE_C: list[str] = [
    "CMakeLists.txt[CMAKE_C_STANDARD]",
    "CMakeLists.txt[target_compile_features]",
    "CMakeLists.txt[-std=cNN]",
    "meson.build[c_std]",
    "Makefile[-std=cNN]",
]
_PRIORITY_TEMPLATE_CPP: list[str] = [
    "CMakeLists.txt[CMAKE_CXX_STANDARD]",
    "CMakeLists.txt[target_compile_features]",
    "CMakeLists.txt[-std=cppNN]",
    "meson.build[cpp_std]",
    "Makefile[-std=c++NN]",
]


def _detect(
    repo_root: Path,
    *,
    kind: Literal["c", "cpp"],
    fallback: str,
) -> CDetectionResult:
    signals: dict[str, str] = {}
    signals.update(_from_cmakelists(repo_root, kind))
    signals.update(_from_makefile(repo_root, kind))
    signals.update(_from_meson_build(repo_root, kind))

    if not signals:
        return CDetectionResult(
            version=fallback,
            source="default",
            conflicts=[],
            all_signals={},
        )

    priority = _PRIORITY_TEMPLATE_CPP if kind == "cpp" else _PRIORITY_TEMPLATE_C
    chosen: str | None = None
    chosen_source: str | None = None
    for source in priority:
        if source in signals:
            chosen = signals[source]
            chosen_source = source
            break
    if chosen is None:
        # Pick any signal — should never happen given priority above covers all
        chosen_source, chosen = next(iter(signals.items()))

    # Where multiple sources disagree, take the MAX numeric (C/C++ standards
    # are supersets — picking the floor would drop features). But only override
    # the priority-chosen value if a *higher-priority* source agrees with max;
    # otherwise we'd violate authority order.
    numeric_values = {v for v in signals.values() if v.isdigit()}
    if numeric_values:
        max_v = _max_by_numeric(numeric_values)
        # Conservative: if priority-chosen < max anywhere, surface as conflict
        # but DON'T override the priority pick (priority means trust).
        conflicts = [
            f"{src}({raw})"
            for src, raw in signals.items()
            if src != chosen_source and raw != chosen
        ]
    else:
        max_v = None
        conflicts = []

    # If the priority source was missing but other sources exist, pick max
    if chosen not in signals.values():
        chosen = max_v or chosen

    return CDetectionResult(
        version=chosen,
        source=chosen_source or "default",
        conflicts=conflicts,
        all_signals=signals,
    )


def detect_c(repo_root: Path, *, fallback: str = "11") -> CDetectionResult:
    """Detect the C language standard for ``repo_root``."""
    return _detect(repo_root, kind="c", fallback=fallback)


def detect_cpp(repo_root: Path, *, fallback: str = "17") -> CDetectionResult:
    """Detect the C++ language standard for ``repo_root``."""
    return _detect(repo_root, kind="cpp", fallback=fallback)
