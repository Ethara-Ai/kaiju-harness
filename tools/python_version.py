"""Canonical Python version detection for kaiju repos.

Replaces ad-hoc regex parsers in ``tools/prepare_repo.py`` and ``tools/validate.py``.

Single source of truth — every call site that needs to know "what Python version
should we use for repo X" goes through :func:`detect`.

Design (see plan in m0031):

  * **Tier A — declared constraints** (PEP 621 ``requires-python``, Poetry
    ``python``, setup.cfg ``python_requires``, setup.py regex,
    ``.python-version`` if pinned X.Y, ``runtime.txt``). All intersected as
    a single :class:`packaging.specifiers.SpecifierSet`.
  * **Tier B — test matrix** (tox ``envlist``, noxfile ``@session(python=...)``,
    GitHub Actions ``python-version`` matrix). Treated as an explicit enum
    that further narrows candidates.
  * **Tier C — deployment hint** (Dockerfile ``FROM python:X.Y``). Tie-breaker
    only; never used to reject otherwise-valid candidates.

Resolution:

  1. ``candidates = SUPPORTED ∩ tier_A_intersection``
  2. If Tier B is non-empty: ``candidates = candidates ∩ tier_B_enum``
  3. If empty: raise :class:`VersionConflictError` with source attribution.
  4. Else: return ``min(candidates)`` — lowest = most likely to expose real
     compat bugs in benchmark code, matches maintainer CI floors, and is
     stable across Python releases.

The module is intentionally I/O-free at the resolution layer
(:func:`detect_from_signals`). :func:`collect_signals` does all the file I/O
so tests can feed in synthetic signal dicts.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

__all__ = [
    "DetectionResult",
    "NoSignalsError",
    "Signal",
    "SignalSource",
    "TIER_A",
    "TIER_B",
    "TIER_C",
    "VersionConflictError",
    "collect_signals",
    "detect",
    "detect_from_signals",
    "poetry_constraint_to_pep440",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal model
# ---------------------------------------------------------------------------


class SignalSource(str, Enum):
    """Identifies which file/section produced a signal.

    String value is the human-readable name used in conflict reports.
    """

    # Tier A — declared constraints
    PEP621_REQUIRES_PYTHON = "pyproject.toml[project.requires-python]"
    POETRY_PYTHON = "pyproject.toml[tool.poetry.dependencies.python]"
    SETUP_CFG = "setup.cfg[options.python_requires]"
    SETUP_PY = "setup.py:python_requires"
    PYTHON_VERSION_FILE = ".python-version"
    RUNTIME_TXT = "runtime.txt"

    # Tier B — test matrix
    TOX_ENVLIST = "tox.ini[tox.envlist]"
    NOXFILE = "noxfile.py[@session.python]"
    GHA_MATRIX = ".github/workflows/*.yml[matrix.python-version]"

    # Tier C — deployment hint
    DOCKERFILE_FROM = "Dockerfile[FROM python:X.Y]"


TIER_A: frozenset[SignalSource] = frozenset(
    {
        SignalSource.PEP621_REQUIRES_PYTHON,
        SignalSource.POETRY_PYTHON,
        SignalSource.SETUP_CFG,
        SignalSource.SETUP_PY,
        SignalSource.PYTHON_VERSION_FILE,
        SignalSource.RUNTIME_TXT,
    }
)
TIER_B: frozenset[SignalSource] = frozenset(
    {
        SignalSource.TOX_ENVLIST,
        SignalSource.NOXFILE,
        SignalSource.GHA_MATRIX,
    }
)
TIER_C: frozenset[SignalSource] = frozenset({SignalSource.DOCKERFILE_FROM})


@dataclass(frozen=True)
class Signal:
    """One observation about Python version requirements for a repo.

    Exactly one of ``constraint`` (Tier A) or ``versions`` (Tier B/C) is set.

    Attributes
    ----------
    source
        Which file/section produced this signal.
    constraint
        PEP 440 specifier set for Tier A signals (e.g. ``>=3.9,<3.13``).
    versions
        Explicit ``X.Y`` strings for Tier B (test matrix) or Tier C (Dockerfile
        ``FROM``). Empty for Tier A.
    raw
        Original text from the file. Preserved for debugging and error messages.
    """

    source: SignalSource
    constraint: SpecifierSet | None = None
    versions: tuple[str, ...] = ()
    raw: str = ""


@dataclass(frozen=True)
class DetectionResult:
    """Outcome of running :func:`detect_from_signals`.

    Attributes
    ----------
    version
        Chosen ``X.Y`` version, or ``None`` if no signals were present and the
        caller hasn't supplied a fallback.
    source
        The winning signal source's string value (e.g.
        ``"pyproject.toml[project.requires-python]"``). ``"default"`` if no
        signals existed.
    conflicts
        List of ``"<source>: <reason>"`` strings describing signals that
        disagreed with the winner. Empty when there's no contention.
    all_signals
        Map of ``source.value → raw text``, preserved for debugging and
        ``--detect-only --report`` output.
    """

    version: str | None
    source: str
    conflicts: list[str] = field(default_factory=list)
    all_signals: dict[str, str] = field(default_factory=dict)


class VersionConflictError(ValueError):
    """Raised when Tier A/B signals leave no candidate version.

    Attributes
    ----------
    candidates
        Versions in ``supported`` that survived Tier A intersection (may be
        empty).
    rejecting_sources
        Map of ``source.value → reason`` describing why each signal eliminated
        the remaining candidates.
    """

    def __init__(
        self,
        candidates: set[str],
        rejecting_sources: dict[str, str],
    ):
        self.candidates = candidates
        self.rejecting_sources = rejecting_sources
        joined = "; ".join(f"{k}: {v}" for k, v in rejecting_sources.items())
        super().__init__(
            f"No Python version in SUPPORTED satisfies all signals. "
            f"Tier A candidates: {sorted(candidates) or '(empty)'}. "
            f"Rejecting sources: {joined or '(none)'}"
        )


class NoSignalsError(ValueError):
    """Raised when the caller asks for strict detection but no signals exist."""


# ---------------------------------------------------------------------------
# Poetry caret/tilde → PEP 440 conversion
# ---------------------------------------------------------------------------


_PEP440_OP_RE = re.compile(r"^\s*(==|!=|<=|>=|<|>|~=|===)")


def poetry_constraint_to_pep440(constraint: str) -> str:
    """Convert a Poetry version constraint into a PEP 440 specifier set string.

    Poetry uses ``^`` and ``~`` operators with semver-ish semantics that PEP
    440's ``SpecifierSet`` doesn't understand directly. This function handles
    the cases that appear in the wild for the ``python`` constraint:

    * ``^3.10``    → ``>=3.10.0,<4.0.0``  (caret: bump major)
    * ``^3.10.5``  → ``>=3.10.5,<4.0.0``
    * ``~3.10``    → ``>=3.10.0,<3.11.0`` (tilde: bump minor)
    * ``~3.10.5``  → ``>=3.10.5,<3.11.0``
    * ``3.10``     → ``==3.10.*``         (bare version = wildcard pin)
    * ``3.10.5``   → ``==3.10.5``
    * ``>=3.8,<3.13`` → unchanged
    * ``*``        → ``""``  (no constraint)
    * Comma-separated combinations are split and each part is converted.

    See Poetry docs:
    https://python-poetry.org/docs/dependency-specification/

    Raises
    ------
    ValueError
        If the constraint is malformed enough that no PEP 440 equivalent can
        be produced.
    """
    raw = constraint.strip()
    if not raw or raw in {"*", "any"}:
        return ""

    parts = [p.strip() for p in raw.split(",") if p.strip()]
    converted: list[str] = []
    for part in parts:
        converted.append(_convert_single_poetry_part(part))
    return ",".join(p for p in converted if p)


def _convert_single_poetry_part(part: str) -> str:
    """Convert one Poetry constraint segment to PEP 440."""
    part = part.strip()
    if part in {"*", "any"}:
        return ""

    if part.startswith("^"):
        return _expand_caret(part[1:].strip())
    if part.startswith("~"):
        return _expand_tilde(part[1:].strip())

    # Already PEP 440 operator?
    if _PEP440_OP_RE.match(part):
        return part

    # Bare version like "3.10" or "3.10.5" — Poetry treats as exact match,
    # but at the patch level "3.10" means "3.10.x" (wildcard). Mimic that.
    if _looks_like_version(part):
        nums = part.split(".")
        if len(nums) == 1:  # "3" → 3.*
            return f"=={nums[0]}.*"
        if len(nums) == 2:  # "3.10" → 3.10.*
            return f"=={nums[0]}.{nums[1]}.*"
        return f"=={part}"

    # Unknown form — surface to caller.
    raise ValueError(f"Cannot parse Poetry constraint segment: {part!r}")


def _expand_caret(ver: str) -> str:
    """``^X.Y[.Z]`` → ``>=X.Y[.Z],<(X+1).0.0`` (bumps major; for 0.Y semver
    bumps minor, but Python's ``python`` constraint never goes below 1.0).
    """
    if not _looks_like_version(ver):
        raise ValueError(f"Invalid caret constraint: ^{ver}")
    parts = ver.split(".")
    major = int(parts[0])
    # Pad to at least X.Y.Z for the lower bound
    lower = ".".join(parts + ["0"] * (3 - len(parts)))
    # Poetry: ^0.x.y bumps the leftmost non-zero digit. For Python (always 3.x)
    # we always bump major.
    if major == 0 and len(parts) >= 2:
        minor = int(parts[1])
        if minor == 0 and len(parts) >= 3:
            # ^0.0.z → >=0.0.z,<0.0.(z+1)
            patch = int(parts[2])
            return f">={lower},<0.0.{patch + 1}"
        # ^0.y[.z] → >=0.y[.z],<0.(y+1).0
        return f">={lower},<0.{minor + 1}.0"
    return f">={lower},<{major + 1}.0.0"


def _expand_tilde(ver: str) -> str:
    """``~X.Y[.Z]`` → ``>=X.Y[.Z],<X.(Y+1).0`` (bumps minor; ``~X`` bumps major)."""
    if not _looks_like_version(ver):
        raise ValueError(f"Invalid tilde constraint: ~{ver}")
    parts = ver.split(".")
    major = int(parts[0])
    if len(parts) == 1:
        # ~3 → >=3.0.0,<4.0.0
        return f">={major}.0.0,<{major + 1}.0.0"
    minor = int(parts[1])
    lower = ".".join(parts + ["0"] * (3 - len(parts)))
    return f">={lower},<{major}.{minor + 1}.0"


def _looks_like_version(s: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+){0,2}", s))


# ---------------------------------------------------------------------------
# Per-source signal collectors
# ---------------------------------------------------------------------------


def _parse_pep440_constraint(raw: str, source: SignalSource) -> Signal | None:
    """Build a Tier-A Signal from a PEP 440 constraint string."""
    raw = raw.strip().strip("\"'")
    if not raw:
        return None
    # Bare ">=3.10" / ">=3.10,<3.13" / "==3.10.*" / "~=3.10"
    try:
        spec = SpecifierSet(raw)
    except InvalidSpecifier:
        # Try treating bare "3.10" as "==3.10.*"
        if _looks_like_version(raw):
            parts = raw.split(".")
            if len(parts) >= 2:
                bare = f"=={parts[0]}.{parts[1]}.*"
            else:
                bare = f"=={parts[0]}.*"
            try:
                spec = SpecifierSet(bare)
            except InvalidSpecifier:
                logger.debug("Cannot parse %s constraint %r", source.value, raw)
                return None
        else:
            logger.debug("Cannot parse %s constraint %r", source.value, raw)
            return None
    return Signal(source=source, constraint=spec, raw=raw)


def _collect_pep621(repo_root: Path) -> Signal | None:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    requires = (data.get("project") or {}).get("requires-python")
    if not isinstance(requires, str):
        return None
    return _parse_pep440_constraint(requires, SignalSource.PEP621_REQUIRES_PYTHON)


def _collect_poetry(repo_root: Path) -> Signal | None:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    deps = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies")
    if not isinstance(deps, dict):
        return None
    py = deps.get("python")
    if isinstance(py, dict):  # {version = "^3.10", ...}
        py = py.get("version")
    if not isinstance(py, str):
        return None
    try:
        pep440 = poetry_constraint_to_pep440(py)
    except ValueError:
        logger.debug("Cannot convert Poetry constraint %r to PEP 440", py)
        return None
    if not pep440:
        # ``*`` / ``any`` — no useful constraint
        return None
    try:
        spec = SpecifierSet(pep440)
    except InvalidSpecifier:
        return None
    return Signal(source=SignalSource.POETRY_PYTHON, constraint=spec, raw=py)


def _collect_setup_cfg(repo_root: Path) -> Signal | None:
    cfg = repo_root / "setup.cfg"
    if not cfg.is_file():
        return None
    content = cfg.read_text(encoding="utf-8", errors="replace")
    # Match: python_requires = >=3.8
    m = re.search(r"^\s*python_requires\s*=\s*(.+)$", content, re.MULTILINE)
    if not m:
        return None
    return _parse_pep440_constraint(m.group(1).strip(), SignalSource.SETUP_CFG)


def _collect_setup_py(repo_root: Path) -> Signal | None:
    setup_py = repo_root / "setup.py"
    if not setup_py.is_file():
        return None
    content = setup_py.read_text(encoding="utf-8", errors="replace")
    # python_requires="..." or python_requires='...' (quote-agnostic, allows whitespace/newlines)
    m = re.search(
        r"""python_requires\s*=\s*(['"])([^'"]+)\1""",
        content,
    )
    if not m:
        return None
    return _parse_pep440_constraint(m.group(2), SignalSource.SETUP_PY)


def _collect_python_version_file(repo_root: Path) -> Signal | None:
    """``.python-version`` is pyenv format — usually exact ``X.Y.Z`` but
    can be a short ``X.Y``. Treat as an exact pin via wildcard match.
    """
    pyver = repo_root / ".python-version"
    if not pyver.is_file():
        return None
    raw = pyver.read_text(encoding="utf-8", errors="replace").strip()
    # File may have multiple lines (pyenv-virtualenv); first non-comment line is canonical
    first = next(
        (
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ),
        None,
    )
    if not first:
        return None
    # Only treat as Tier A if it's a normal CPython X.Y[.Z], not "pypy3.10" or "3.10/envs/..."
    if not _looks_like_version(first):
        return None
    parts = first.split(".")
    if len(parts) == 1:
        spec_str = f"=={parts[0]}.*"
    elif len(parts) == 2:
        spec_str = f"=={parts[0]}.{parts[1]}.*"
    else:
        spec_str = f"=={parts[0]}.{parts[1]}.*"  # Pin to minor — patch is dev preference
    try:
        spec = SpecifierSet(spec_str)
    except InvalidSpecifier:
        return None
    return Signal(
        source=SignalSource.PYTHON_VERSION_FILE,
        constraint=spec,
        raw=first,
    )


def _collect_runtime_txt(repo_root: Path) -> Signal | None:
    rt = repo_root / "runtime.txt"
    if not rt.is_file():
        return None
    raw = rt.read_text(encoding="utf-8", errors="replace").strip()
    # Heroku/buildpack format: "python-3.10.5"
    m = re.match(r"python-(\d+\.\d+(?:\.\d+)?)", raw)
    if not m:
        return None
    parts = m.group(1).split(".")
    spec_str = f"=={parts[0]}.{parts[1]}.*"
    try:
        spec = SpecifierSet(spec_str)
    except InvalidSpecifier:
        return None
    return Signal(source=SignalSource.RUNTIME_TXT, constraint=spec, raw=raw)


def _collect_tox(repo_root: Path) -> Signal | None:
    tox = repo_root / "tox.ini"
    if not tox.is_file():
        return None
    content = tox.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*envlist\s*=\s*(.+(?:\n[ \t]+.+)*)", content, re.MULTILINE)
    if not m:
        return None
    envlist = m.group(1).replace("\n", ",").replace(" ", "").replace("\t", "")
    versions = sorted(set(_parse_tox_envlist(envlist)))
    if not versions:
        return None
    return Signal(
        source=SignalSource.TOX_ENVLIST,
        versions=tuple(versions),
        raw=envlist,
    )


def _parse_tox_envlist(envlist: str) -> Iterable[str]:
    """Extract Python versions from a tox envlist string.

    Handles ``py310``, ``py3.10``, ``{py310,py311}-{lint,test}`` style envs.
    """
    # Expand {a,b}-{c,d} → a-c, a-d, b-c, b-d (simple, only handles common cases)
    expanded: list[str] = []
    tokens = [envlist]
    while tokens:
        tok = tokens.pop()
        m = re.search(r"\{([^{}]+)\}", tok)
        if not m:
            expanded.append(tok)
            continue
        options = m.group(1).split(",")
        for opt in options:
            tokens.append(tok[: m.start()] + opt + tok[m.end() :])

    for env in (e.strip() for e in ",".join(expanded).split(",") if e.strip()):
        # Match py310, py3.10 at the start of an env name (before "-" or end)
        m = re.match(r"py(\d)\.?(\d{1,2})", env)
        if m:
            major, minor = m.group(1), m.group(2)
            yield f"{major}.{minor}"


def _collect_noxfile(repo_root: Path) -> Signal | None:
    """Parse ``noxfile.py`` for ``@nox.session(python=[...])`` versions.

    Uses regex (not AST) because noxfile contents can use complex constructs;
    we only care about literal lists. Misses dynamic versions — that's fine.
    """
    nox = repo_root / "noxfile.py"
    if not nox.is_file():
        return None
    content = nox.read_text(encoding="utf-8", errors="replace")
    versions: set[str] = set()
    # @session(python="3.10") or @session(python=["3.10","3.11"])
    for m in re.finditer(r"python\s*=\s*(\[[^\]]+\]|\(['\"][^'\"]+['\"]\)|['\"][^'\"]+['\"])", content):
        block = m.group(1)
        for v in re.findall(r"['\"](\d+\.\d+)['\"]", block):
            versions.add(v)
    if not versions:
        return None
    return Signal(
        source=SignalSource.NOXFILE,
        versions=tuple(sorted(versions)),
        raw=", ".join(sorted(versions)),
    )


def _collect_gha_matrix(repo_root: Path) -> Signal | None:
    """Parse ``.github/workflows/*.yml`` for ``matrix.python-version`` lists."""
    workflows = repo_root / ".github" / "workflows"
    if not workflows.is_dir():
        return None
    versions: set[str] = set()
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # YAML-light: match `python-version:` followed by inline list or block list.
        # Inline:  python-version: ["3.10", "3.11"]
        # Block:   python-version:\n      - "3.10"\n      - "3.11"
        # Scalar:  python-version: "3.10"
        for m in re.finditer(
            r"python-version\s*:\s*(\[[^\]]+\]|(?:\n[ \t]+-[ \t]+['\"]?[^\n]+['\"]?)+|['\"]?\d+(?:\.\d+)+['\"]?)",
            content,
        ):
            block = m.group(1)
            for v in re.findall(r"(?<![\d.])(\d+\.\d+)(?:\.\d+)?(?![\d.])", block):
                versions.add(v)
    if not versions:
        return None
    return Signal(
        source=SignalSource.GHA_MATRIX,
        versions=tuple(sorted(versions)),
        raw=", ".join(sorted(versions)),
    )


def _collect_dockerfile(repo_root: Path) -> Signal | None:
    """Parse ``Dockerfile`` (and common variants) for ``FROM python:X.Y...``."""
    candidates = [
        repo_root / "Dockerfile",
        repo_root / "Dockerfile.dev",
        repo_root / "docker" / "Dockerfile",
    ]
    versions: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
            r"^\s*FROM\s+(?:[\w./-]+/)?python:(\d+\.\d+)",
            content,
            re.MULTILINE | re.IGNORECASE,
        ):
            versions.add(m.group(1))
    if not versions:
        return None
    return Signal(
        source=SignalSource.DOCKERFILE_FROM,
        versions=tuple(sorted(versions)),
        raw=", ".join(sorted(versions)),
    )


# ---------------------------------------------------------------------------
# Public collector + resolver
# ---------------------------------------------------------------------------


_COLLECTORS = [
    _collect_pep621,
    _collect_poetry,
    _collect_setup_cfg,
    _collect_setup_py,
    _collect_python_version_file,
    _collect_runtime_txt,
    _collect_tox,
    _collect_noxfile,
    _collect_gha_matrix,
    _collect_dockerfile,
]


def collect_signals(repo_root: Path) -> list[Signal]:
    """Scan ``repo_root`` for every Python version signal kaiju knows about.

    Returns one :class:`Signal` per source that produced a parseable value.
    Sources that don't exist, can't be parsed, or produce empty constraints
    are silently skipped.
    """
    signals: list[Signal] = []
    for collector in _COLLECTORS:
        try:
            sig = collector(repo_root)
        except Exception:  # noqa: BLE001  - collectors must never crash detection
            logger.exception(
                "Signal collector %s crashed on %s", collector.__name__, repo_root
            )
            continue
        if sig is not None:
            signals.append(sig)
    return signals


def detect_from_signals(
    signals: list[Signal],
    supported: Iterable[str],
) -> DetectionResult:
    """Resolve a chosen Python version from collected signals.

    Pure function — no file I/O. Feed synthetic signal lists in tests.

    Parameters
    ----------
    signals
        Output of :func:`collect_signals` (or hand-built for tests).
    supported
        The set of ``X.Y`` versions kaiju has Docker base images for. The
        resolver will only return a value from this set.

    Returns
    -------
    DetectionResult
        ``version`` is ``None`` if no signals were present.

    Raises
    ------
    VersionConflictError
        If Tier A/B signals together leave no candidate in ``supported``.
    """
    supported_set = sorted(supported, key=_version_sort_key)
    all_signals_map: dict[str, str] = {s.source.value: s.raw for s in signals}

    if not signals:
        return DetectionResult(
            version=None,
            source="default",
            conflicts=[],
            all_signals=all_signals_map,
        )

    tier_a = [s for s in signals if s.source in TIER_A and s.constraint is not None]
    tier_b = [s for s in signals if s.source in TIER_B and s.versions]
    tier_c = [s for s in signals if s.source in TIER_C and s.versions]

    # ----- Tier A: intersect all constraints -----
    if tier_a:
        candidates: set[str] = set()
        rejecting: dict[str, str] = {}
        for v in supported_set:
            # Compare against X.Y.0 so specifiers like "!=3.11.*" work and
            # specifiers like ">=3.10" pass without prerelease confusion.
            try:
                version_obj = Version(f"{v}.0")
            except InvalidVersion:
                continue
            unmet: list[str] = []
            for sig in tier_a:
                assert sig.constraint is not None  # for type checker
                if not sig.constraint.contains(version_obj, prereleases=False):
                    unmet.append(f"{sig.source.value}({sig.raw})")
            if unmet:
                rejecting[v] = "; ".join(unmet)
            else:
                candidates.add(v)
        if not candidates:
            raise VersionConflictError(candidates=set(), rejecting_sources=rejecting)
    else:
        candidates = set(supported_set)

    # ----- Tier B: narrow by test matrix (only if it overlaps) -----
    tier_b_winner: SignalSource | None = None
    if tier_b:
        matrix_union = set()
        for sig in tier_b:
            matrix_union.update(sig.versions)
        narrowed = candidates & matrix_union
        if narrowed:
            # Pick the source that contributed the chosen version (for reporting)
            tier_b_winner = next(
                (sig.source for sig in tier_b if narrowed & set(sig.versions)),
                None,
            )
            candidates = narrowed
        # If matrix doesn't overlap Tier A candidates, keep Tier A and report
        # the mismatch as a conflict — matrix may just be outdated.

    if not candidates:
        # Shouldn't reach here (we only narrow when overlap exists) but be defensive.
        raise VersionConflictError(
            candidates=set(),
            rejecting_sources={s.source.value: s.raw for s in tier_a + tier_b},
        )

    # ----- Tier C: tie-breaker only -----
    if tier_c and len(candidates) > 1:
        dockerfile_versions: set[str] = set()
        for sig in tier_c:
            dockerfile_versions.update(sig.versions)
        if hint_pick := (candidates & dockerfile_versions):
            # Honor Dockerfile only if it picks something we'd otherwise consider.
            # Still use min() within the hint set for stability.
            candidates = hint_pick

    chosen = min(candidates, key=_version_sort_key)

    # Build source attribution
    if tier_a:
        # Find the most restrictive Tier A signal that includes `chosen`
        chosen_version = Version(f"{chosen}.0")
        winner = next(
            (
                s
                for s in tier_a
                if s.constraint is not None
                and s.constraint.contains(chosen_version, prereleases=False)
            ),
            tier_a[0],
        )
        winning_source = winner.source.value
    elif tier_b_winner is not None:
        winning_source = tier_b_winner.value
    elif tier_c:
        winning_source = tier_c[0].source.value
    else:
        winning_source = "default"

    # Build conflict list (signals that disagreed with `chosen`)
    chosen_version = Version(f"{chosen}.0")
    conflicts: list[str] = []
    for sig in signals:
        disagrees = False
        if sig.source in TIER_A and sig.constraint is not None:
            disagrees = not sig.constraint.contains(chosen_version, prereleases=False)
        elif sig.source in TIER_B and sig.versions:
            disagrees = chosen not in sig.versions
        elif sig.source in TIER_C and sig.versions:
            disagrees = chosen not in sig.versions
        if disagrees:
            conflicts.append(f"{sig.source.value}({sig.raw})")

    return DetectionResult(
        version=chosen,
        source=winning_source,
        conflicts=conflicts,
        all_signals=all_signals_map,
    )


def detect(
    repo_root: Path,
    supported: Iterable[str],
    *,
    fallback: str | None = None,
    strict: bool = False,
) -> DetectionResult:
    """Run :func:`collect_signals` + :func:`detect_from_signals` on ``repo_root``.

    Parameters
    ----------
    repo_root
        Repository directory to scan.
    supported
        Set of ``X.Y`` strings kaiju has Docker base images for.
    fallback
        Version to return when no signals are found. If ``None`` and
        ``strict`` is ``False``, returns ``version=None`` (caller decides).
    strict
        If ``True`` and no signals are found, raise :class:`NoSignalsError`
        instead of returning a fallback.

    Returns
    -------
    DetectionResult

    Raises
    ------
    NoSignalsError
        When ``strict=True`` and the repo has no detectable signals.
    VersionConflictError
        When signals contradict each other beyond reconciliation.
    """
    signals = collect_signals(repo_root)
    result = detect_from_signals(signals, supported)
    if result.version is None:
        if strict:
            raise NoSignalsError(
                f"No Python version signals found in {repo_root}. "
                f"Add `requires-python` to pyproject.toml or a `.python-version` file."
            )
        if fallback is not None:
            return DetectionResult(
                version=fallback,
                source="default",
                conflicts=[],
                all_signals=result.all_signals,
            )
    return result


def _version_sort_key(v: str) -> tuple[int, ...]:
    """Sort ``"3.10"`` correctly relative to ``"3.9"`` (numeric, not lex)."""
    return tuple(int(part) for part in v.split("."))
