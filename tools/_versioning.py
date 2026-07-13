"""Shared infrastructure for language-runtime version detection.

Consumed by ``tools/{python,node,rust,go,java,cpp}_version.py``.

Every language has the same problem: figure out which interpreter / toolchain
version to use for a repo, given a mix of declared constraints (manifest files),
test-matrix enumerations (CI config), and deployment hints (Dockerfile FROM).
The two-tier algorithm from ``python_version.py`` generalizes — only the
*sources* differ.

* **Tier A — declared constraints.** Manifest fields like
  ``[project] requires-python``, ``engines.node``, ``[package].rust-version``,
  ``go.mod`` ``go X.Y``, ``maven.compiler.release``,
  ``set(CMAKE_C_STANDARD ...)``. Combined as a PEP 440 ``SpecifierSet`` —
  numeric-only languages happen to fit the same algebra.
* **Tier B — test matrix.** GitHub Actions ``matrix.<lang>-version``, tox
  ``envlist``, Cargo matrices. Enum of versions actually exercised.
* **Tier C — deployment hint.** Dockerfile ``FROM`` directives, lockfile
  ``engines`` mirrors. Tie-breakers only.

Resolution:

  1. ``candidates = SUPPORTED ∩ (intersection of Tier A constraints)``
  2. If Tier B has overlap with ``candidates``: narrow to overlap.
  3. If Tier C has overlap and ``len(candidates) > 1``: narrow to overlap.
  4. ``return min(candidates)`` (lowest = max compat-bug exposure, matches
     maintainer CI floors, stable across releases).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

__all__ = [
    "DetectionResult",
    "NoSignalsError",
    "Signal",
    "Tier",
    "VersionConflictError",
    "normalize_semver_range",
    "parse_constraint_str",
    "resolve_two_tier",
    "version_sort_key",
]


class Tier(Enum):
    """Signal tier — see module docstring for semantics."""

    A_DECLARED = "A"
    B_MATRIX = "B"
    C_HINT = "C"


@dataclass(frozen=True)
class Signal:
    """One observation about runtime-version requirements.

    Attributes
    ----------
    source
        Human-readable identifier of the file/section that produced this
        signal. Used in :class:`DetectionResult.source` and conflict reports.
    tier
        Which authority tier this signal belongs to.
    constraint
        Parsed PEP 440 specifier (Tier A only).
    versions
        Explicit ``X[.Y[.Z]]`` strings (Tier B/C). Empty for Tier A.
    raw
        Verbatim text from the source. Kept for debugging / report mode.

    """

    source: str
    tier: Tier
    constraint: SpecifierSet | None = None
    versions: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class DetectionResult:
    """Outcome of running :func:`resolve_two_tier`."""

    version: str | None
    source: str
    conflicts: list[str] = field(default_factory=list)
    all_signals: dict[str, str] = field(default_factory=dict)


class VersionConflictError(ValueError):
    """Raised when signals together leave no candidate in ``supported``."""

    def __init__(
        self,
        candidates: set[str],
        rejecting_sources: dict[str, str],
    ):
        self.candidates = candidates
        self.rejecting_sources = rejecting_sources
        joined = "; ".join(f"{k}: {v}" for k, v in rejecting_sources.items())
        super().__init__(
            f"No version in SUPPORTED satisfies all signals. "
            f"Tier A candidates: {sorted(candidates) or '(empty)'}. "
            f"Rejecting sources: {joined or '(none)'}"
        )


class NoSignalsError(ValueError):
    """Raised when strict detection is requested but no signals exist."""


# ---------------------------------------------------------------------------
# Constraint parsing helpers
# ---------------------------------------------------------------------------


_PEP440_OP_RE = re.compile(r"^\s*(==|!=|<=|>=|<|>|~=|===)")
_BARE_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")


def _looks_like_version(s: str) -> bool:
    return bool(_BARE_VERSION_RE.match(s.strip()))


def parse_constraint_str(raw: str) -> SpecifierSet | None:
    """Parse a generic constraint string into a :class:`SpecifierSet`.

    Handles plain PEP 440 specs (``>=3.10,<3.13``), bare versions (``3.10`` →
    ``==3.10.*``), and empty / wildcard strings (``""``, ``*``). Returns
    ``None`` if the string can't be parsed meaningfully.
    """
    raw = raw.strip().strip("\"'")
    if not raw or raw in {"*", "any", "x", "X"}:
        return None
    try:
        return SpecifierSet(raw)
    except InvalidSpecifier:
        if _looks_like_version(raw):
            parts = raw.split(".")
            if len(parts) == 1:
                spec_str = f"=={parts[0]}.*"
            elif len(parts) == 2:
                spec_str = f"=={parts[0]}.{parts[1]}.*"
            else:
                spec_str = f"=={raw}"
            try:
                return SpecifierSet(spec_str)
            except InvalidSpecifier:
                return None
        return None


# ---------------------------------------------------------------------------
# Semver / npm-range conversion (for engines.node, ^/~ in package.json)
# ---------------------------------------------------------------------------


def normalize_semver_range(raw: str) -> SpecifierSet | None:
    """Convert an npm/semver range to a PEP 440 ``SpecifierSet``.

    Supports the common cases that appear in ``engines.node`` and similar
    fields. Returns ``None`` for unparseable input.

    Examples::

        >=18              → >=18
        >=18 <21          → >=18,<21
        >=18.0.0 <21.0.0  → >=18.0.0,<21.0.0
        ^18               → >=18,<19
        ~18.10            → >=18.10,<18.11
        18.x              → ==18.*
        18                → ==18.*
        ||                → None (disjunctions not modeled)

    Disjunctive ranges (``... || ...``) collapse to the first clause —
    callers needing full OR semantics should split before calling.
    """
    s = raw.strip().strip("\"'")
    if not s or s in {"*", "any", "latest"}:
        return None
    # Disjunction: take the first clause (caller can split if needed)
    if "||" in s:
        s = s.split("||", 1)[0].strip()

    parts = [p for p in re.split(r"[,\s]+", s) if p]
    converted: list[str] = []
    for part in parts:
        c = _convert_semver_part(part)
        if c is not None and c != "":
            converted.append(c)
    if not converted:
        return None
    try:
        return SpecifierSet(",".join(converted))
    except InvalidSpecifier:
        return None


def _convert_semver_part(part: str) -> str | None:
    part = part.strip()
    if not part or part in {"*", "x", "X"}:
        return ""
    if part.startswith("^"):
        return _semver_caret(part[1:])
    if part.startswith("~"):
        return _semver_tilde(part[1:])
    if _PEP440_OP_RE.match(part):
        return part
    # 18.x or 18.X
    if re.fullmatch(r"\d+\.[xX]", part):
        return f"=={part.split('.')[0]}.*"
    if re.fullmatch(r"\d+\.\d+\.[xX]", part):
        nums = part.split(".")
        return f"=={nums[0]}.{nums[1]}.*"
    if _looks_like_version(part):
        nums = part.split(".")
        if len(nums) == 1:
            return f"=={nums[0]}.*"
        if len(nums) == 2:
            return f"=={nums[0]}.{nums[1]}.*"
        return f"=={part}"
    return None


def _semver_caret(v: str) -> str | None:
    """``^X[.Y[.Z]]`` → ``>=X[.Y[.Z]],<(X+1)[.0[.0]]`` (npm semver)."""
    if not _looks_like_version(v):
        return None
    nums = v.split(".")
    major = int(nums[0])
    if major >= 1:
        return f">={v},<{major + 1}"
    # ^0.x semver: bump minor instead of major
    if len(nums) >= 2:
        minor = int(nums[1])
        if minor > 0:
            return f">={v},<0.{minor + 1}"
    if len(nums) >= 3:
        patch = int(nums[2])
        return f">={v},<0.0.{patch + 1}"
    return f">={v},<1"


def _semver_tilde(v: str) -> str | None:
    """``~X[.Y[.Z]]`` → ``>=X[.Y[.Z]],<X.(Y+1)`` (npm semver)."""
    if not _looks_like_version(v):
        return None
    nums = v.split(".")
    if len(nums) == 1:
        return f">={v},<{int(nums[0]) + 1}"
    return f">={v},<{nums[0]}.{int(nums[1]) + 1}"


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def version_sort_key(v: "str | int") -> tuple[int, ...]:
    """Numeric sort key — ``'3.10'`` sorts above ``'3.9'``.

    Coerce to str first: some callers pass integer major versions (Node's
    SUPPORTED_NODE_VERSIONS is ``{20, 22}``), and ``int.split`` raises
    AttributeError. This is a shared helper across all languages, so it must
    accept both str ("3.10.1") and int (20) forms. ``str(v)`` is a no-op for the
    existing string callers, so there's no behavior change for them.
    """
    return tuple(int(p) for p in str(v).split("."))


def _to_comparable_version(v: str, version_template: str) -> Version:
    """Turn a SUPPORTED-set entry into a fully-qualified :class:`Version`.

    ``version_template`` is a ``str.format`` template, e.g. ``"{}.0"`` to
    turn ``"3.10"`` into ``"3.10.0"`` for unambiguous comparison against
    specifiers like ``!=3.11.*``.
    """
    return Version(version_template.format(v))


def resolve_two_tier(
    signals: list[Signal],
    supported: Iterable[str],
    *,
    version_template: str = "{}.0",
    pick: str = "min",
) -> DetectionResult:
    """Generic two-tier signal resolver.

    Parameters
    ----------
    signals
        Output of a language-specific ``collect_signals`` function.
    supported
        Set of ``X[.Y]`` strings the harness has tooling for (Docker images,
        toolchains, etc.). The resolver only returns versions from this set.
    version_template
        How to format a ``supported`` entry into a parseable PEP 440 version
        for constraint checking. ``"{}.0"`` works for major-only languages
        like Java; ``"{}.0.0"`` works for ``Node`` ``18`` → ``18.0.0``.
    pick
        ``"min"`` (default — most likely to expose compat bugs) or ``"max"``
        when callers want newest-compatible.

    Raises
    ------
    VersionConflictError
        When Tier A intersection is empty across ``supported``.

    """
    supported_set = sorted(supported, key=version_sort_key)
    all_signals_map: dict[str, str] = {s.source: s.raw for s in signals}

    if not signals:
        return DetectionResult(
            version=None,
            source="default",
            conflicts=[],
            all_signals=all_signals_map,
        )

    tier_a = [s for s in signals if s.tier == Tier.A_DECLARED and s.constraint is not None]
    tier_b = [s for s in signals if s.tier == Tier.B_MATRIX and s.versions]
    tier_c = [s for s in signals if s.tier == Tier.C_HINT and s.versions]

    # ----- Tier A intersection -----
    if tier_a:
        candidates: set[str] = set()
        rejecting: dict[str, str] = {}
        for v in supported_set:
            try:
                version_obj = _to_comparable_version(v, version_template)
            except InvalidVersion:
                continue
            unmet: list[str] = []
            for sig in tier_a:
                assert sig.constraint is not None
                if not sig.constraint.contains(version_obj, prereleases=False):
                    unmet.append(f"{sig.source}({sig.raw})")
            if unmet:
                rejecting[v] = "; ".join(unmet)
            else:
                candidates.add(v)
        if not candidates:
            raise VersionConflictError(candidates=set(), rejecting_sources=rejecting)
    else:
        candidates = set(supported_set)

    # ----- Tier B narrowing -----
    tier_b_winner_source: str | None = None
    if tier_b:
        matrix_union: set[str] = set()
        for sig in tier_b:
            matrix_union.update(sig.versions)
        narrowed = candidates & matrix_union
        if narrowed:
            tier_b_winner_source = next(
                (sig.source for sig in tier_b if narrowed & set(sig.versions)),
                None,
            )
            candidates = narrowed

    if not candidates:
        raise VersionConflictError(
            candidates=set(),
            rejecting_sources={s.source: s.raw for s in tier_a + tier_b},
        )

    # ----- Tier C tie-breaker -----
    if tier_c and len(candidates) > 1:
        docker_versions: set[str] = set()
        for sig in tier_c:
            docker_versions.update(sig.versions)
        hint_pick = candidates & docker_versions
        if hint_pick:
            candidates = hint_pick

    chosen = (min if pick == "min" else max)(candidates, key=version_sort_key)

    # ----- Source attribution -----
    if tier_a:
        chosen_version = _to_comparable_version(chosen, version_template)
        winner = next(
            (
                s
                for s in tier_a
                if s.constraint is not None
                and s.constraint.contains(chosen_version, prereleases=False)
            ),
            tier_a[0],
        )
        winning_source = winner.source
    elif tier_b_winner_source is not None:
        winning_source = tier_b_winner_source
    elif tier_c:
        winning_source = tier_c[0].source
    else:
        winning_source = "default"

    # ----- Conflict report -----
    chosen_version_obj = _to_comparable_version(chosen, version_template)
    conflicts: list[str] = []
    for sig in signals:
        disagrees = False
        if sig.tier == Tier.A_DECLARED and sig.constraint is not None:
            disagrees = not sig.constraint.contains(chosen_version_obj, prereleases=False)
        elif sig.tier == Tier.B_MATRIX and sig.versions:
            disagrees = chosen not in sig.versions
        elif sig.tier == Tier.C_HINT and sig.versions:
            disagrees = chosen not in sig.versions
        if disagrees:
            conflicts.append(f"{sig.source}({sig.raw})")

    return DetectionResult(
        version=chosen,
        source=winning_source,
        conflicts=conflicts,
        all_signals=all_signals_map,
    )
