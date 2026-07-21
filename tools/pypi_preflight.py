"""Pre-flight validation of extracted pip dependencies against PyPI.

A dataset repo's extracted dependency list can reference packages that will
NEVER install on public PyPI:

  * private / test-only names not published anywhere public — e.g. textX's
    ``data-dsl``, ``flow-codegen`` (extracted from a dev/test dep list); or
  * an exact pin to a version that was deleted / never published — e.g.
    ``pypandoc_binary==1.12`` (only 1.13+ exist).

Left unchecked these surface 60-180 s into an expensive docker build as a cryptic
``No matching distribution found`` and get miscategorised as a build failure.
This module validates the list up front against PyPI's JSON API and:

  * **DROPS** names that do not exist on PyPI at all (HTTP 404). They cannot
    install (our generated Dockerfiles use only public PyPI), so keeping them
    only guarantees a build failure; they are almost always private/test-only or
    typos. Every drop is reported.
  * **WARNS but KEEPS** a package whose pinned version-range no published version
    satisfies. Auto-loosening a pin could silently install an *incompatible*
    version, so this needs a human/dataset decision — the warning attributes the
    failure BEFORE the build instead of after.

Design guarantees:
  * **Fail-open** — any network error, timeout, HTTP 5xx, or unparsable spec
    leaves that package untouched. A PyPI blip must never block prepare.
  * **Bounded** — queries run concurrently with a per-request timeout and a total
    wall-clock budget; on budget exhaustion the remaining packages are kept
    unchecked.
  * **Off-switch** — ``KAIJU_SKIP_PYPI_PREFLIGHT=1`` disables it entirely.
  * **Index-aware caveat** — only public PyPI is consulted. A package that lives
    on a private index the build can actually reach would be a false drop; our
    Dockerfiles configure no such index, so a 404 here == unbuildable there.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

try:  # packaging is a hard dep of the harness; guard only for exotic envs.
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
    _HAVE_PACKAGING = True
except Exception:  # pragma: no cover - packaging always present in practice
    _HAVE_PACKAGING = False

_logger = logging.getLogger(__name__)

# Sentinel: the query could not be completed (network error / timeout / 5xx).
# Distinct from ``None`` (a definitive 404 = package does not exist).
_UNKNOWN: frozenset = frozenset({"__kaiju_pypi_unknown__"})

_PYPI_JSON_URL = "https://pypi.org/pypi/{name}/json"

# Spec forms we cannot meaningfully validate against a PyPI name+version — leave
# them untouched (a local path, VCS/URL install, or editable target).
_UNVALIDATABLE_PREFIXES = ("-", ".", "/")
_UNVALIDATABLE_SUBSTRINGS = ("://", "@ ", " @", "file:", "git+", "http:", "https:")


@dataclass
class PreflightReport:
    """Outcome of a pre-flight pass (for logging / tests)."""

    dropped: list[tuple[str, str]] = field(default_factory=list)   # (spec, reason)
    warned: list[tuple[str, str]] = field(default_factory=list)    # (spec, reason)
    checked: int = 0
    skipped_network: int = 0

    @property
    def clean(self) -> bool:
        return not self.dropped and not self.warned


def _is_validatable(spec: str) -> bool:
    s = spec.strip()
    if not s or s.startswith(_UNVALIDATABLE_PREFIXES):
        return False
    low = s.lower()
    return not any(sub in low for sub in _UNVALIDATABLE_SUBSTRINGS)


def _query_pypi_versions(
    name: str, timeout: float
) -> Optional[frozenset]:
    """Return the set of published version strings for *name*.

    ``None``      -> PyPI returned 404 (package does not exist).
    ``_UNKNOWN``  -> the query failed for a transient/ambiguous reason; caller
                     must treat the package as unverified (fail-open).
    """
    url = _PYPI_JSON_URL.format(name=urllib.parse.quote(name, safe=""))
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return _UNKNOWN
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None if e.code == 404 else _UNKNOWN
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return _UNKNOWN
    releases = data.get("releases")
    if not isinstance(releases, dict):
        return _UNKNOWN
    # Count a version only if it has at least one NON-yanked file — a version whose
    # files were all yanked or removed is not installable, mirroring what pip's
    # resolver reports in its "from versions: …" list. (A version can still be
    # incompatible with a specific python/platform even when installable in
    # general; that env-specific filtering is out of scope for a name+version
    # pre-flight.)
    installable = {
        ver
        for ver, files in releases.items()
        if isinstance(files, list)
        and any(not f.get("yanked", False) for f in files if isinstance(f, dict))
    }
    return frozenset(installable)


def _pin_is_satisfiable(spec_obj: "Requirement", versions: frozenset) -> bool:
    """True if ANY published version satisfies the requirement's specifier.

    An empty specifier (unpinned) is always satisfiable when the package exists.
    Prereleases are allowed so a repo pinned to an rc/dev version isn't falsely
    flagged.
    """
    if not spec_obj.specifier:
        return True
    try:
        return any(
            spec_obj.specifier.contains(v, prereleases=True) for v in versions
        )
    except Exception:  # noqa: BLE001 - a weird version string must not crash prepare
        return True  # fail-open: don't warn if we can't evaluate the pin


def check_pip_packages(
    pip_packages: list[str],
    *,
    timeout: float = 5.0,
    total_budget_s: float = 20.0,
    max_workers: int = 8,
    logger: Optional[logging.Logger] = None,
) -> tuple[list[str], PreflightReport]:
    """Validate *pip_packages* against PyPI; return (kept, report).

    Non-existent packages (404) are removed from ``kept``; version-pin
    mismatches are kept but recorded in ``report.warned``. Best-effort and
    fail-open (see module docstring).
    """
    log = logger or _logger
    report = PreflightReport()
    if not pip_packages:
        return [], report
    if os.environ.get("KAIJU_SKIP_PYPI_PREFLIGHT") == "1" or not _HAVE_PACKAGING:
        return list(pip_packages), report

    # Parse once; dedupe network work by canonical name.
    parsed: dict[str, Optional["Requirement"]] = {}
    name_by_canon: dict[str, str] = {}
    for spec in pip_packages:
        if not _is_validatable(spec):
            parsed[spec] = None
            continue
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            parsed[spec] = None  # unparsable -> leave it alone
            continue
        parsed[spec] = req
        name_by_canon.setdefault(canonicalize_name(req.name), req.name)

    # Concurrent PyPI lookups, one per distinct package, under a time budget.
    versions_by_canon: dict[str, frozenset] = {}
    deadline = time.monotonic() + total_budget_s
    if name_by_canon:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_workers, len(name_by_canon))
        ) as ex:
            futs = {
                ex.submit(_query_pypi_versions, raw, timeout): canon
                for canon, raw in name_by_canon.items()
            }
            for fut in concurrent.futures.as_completed(futs):
                canon = futs[fut]
                remaining = deadline - time.monotonic()
                try:
                    versions_by_canon[canon] = fut.result(
                        timeout=max(0.0, remaining)
                    )
                except Exception:  # noqa: BLE001 - timeout/budget -> unverified
                    versions_by_canon[canon] = _UNKNOWN

    kept: list[str] = []
    for spec in pip_packages:
        req = parsed.get(spec)
        if req is None:
            kept.append(spec)
            continue
        versions = versions_by_canon.get(canonicalize_name(req.name), _UNKNOWN)
        if versions is _UNKNOWN:
            report.skipped_network += 1
            kept.append(spec)  # fail-open
            continue
        report.checked += 1
        if versions is None:  # 404 — does not exist on PyPI
            report.dropped.append((spec, "not found on public PyPI"))
            log.warning(
                "PyPI pre-flight: dropping %r — not found on public PyPI "
                "(private/test-only dep or typo; it cannot install and would "
                "fail the build).", spec,
            )
            continue
        if not _pin_is_satisfiable(req, versions):
            newest = _max_version(versions)
            reason = (
                f"no published version satisfies '{req.specifier}'"
                + (f" (newest on PyPI: {newest})" if newest else "")
            )
            report.warned.append((spec, reason))
            log.warning(
                "PyPI pre-flight: %r %s — the build WILL fail on this pin. "
                "Loosen/re-pick it in the dataset spec.", spec, reason,
            )
        kept.append(spec)

    if report.dropped or report.warned:
        log.info(
            "PyPI pre-flight: checked %d dep(s); dropped %d non-existent, "
            "warned %d unsatisfiable pin(s), skipped %d (network).",
            report.checked, len(report.dropped), len(report.warned),
            report.skipped_network,
        )
    return kept, report


def _max_version(versions: frozenset) -> Optional[str]:
    if not _HAVE_PACKAGING:
        return None
    try:
        from packaging.version import Version

        parsed = []
        for v in versions:
            try:
                parsed.append(Version(v))
            except Exception:  # noqa: BLE001
                continue
        return str(max(parsed)) if parsed else None
    except Exception:  # noqa: BLE001
        return None
