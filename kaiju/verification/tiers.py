"""#6 — coverage-tier utilities the container drivers call.

Pure, testable logic for:
  * flaky classification (rerun-N: mixed pass/fail == flaky);
  * transient-vs-genuine failure triage (stderr/exit-code signatures — an infra
    hiccup is code-independent, a genuine failure reproduces);
  * checksum tamper detection (frozen files hashed pre/post; a changed hash = the
    agent neutered a frozen test/config file — the stub-floor + checksum tier).
The re-run / hashing execution is container-bound; these decide from its outputs.
"""
from __future__ import annotations

import hashlib
import re

# Code-INDEPENDENT failure signatures: retry, do not count as a model failure.
_TRANSIENT_MARKERS = (
    "econnreset", "etimedout", "connection reset", "connection refused",
    "temporary failure in name resolution", "read timed out", "429 too many requests",
    "503 service unavailable", "no space left on device", "oomkilled",
    "gateway timeout", "tls handshake timeout", "i/o timeout",
)
_TRANSIENT_EXIT = {124, 137, 143}   # timeout / SIGKILL(OOM) / SIGTERM


def classify_flaky(outcomes: list[bool]) -> str:
    """From N rerun outcomes (True == passed): 'flaky' if both seen, else stable."""
    if not outcomes:
        return "unknown"
    if all(outcomes):
        return "stable_pass"
    if not any(outcomes):
        return "stable_fail"
    return "flaky"


def rerun_classify(rerun_fn, n: int) -> tuple[str, list[bool]]:
    """Run ``rerun_fn() -> passed`` up to n times (stop early once flaky is proven)."""
    outcomes: list[bool] = []
    for _ in range(max(1, n)):
        outcomes.append(bool(rerun_fn()))
        if len(set(outcomes)) > 1:      # mixed already -> flaky, stop
            break
    return classify_flaky(outcomes), outcomes


def classify_failure(stderr: str, exit_code: int | None) -> str:
    """'infra_transient' (retry, not a model failure) | 'genuine' (reproduces)."""
    if exit_code in _TRANSIENT_EXIT:
        return "infra_transient"
    low = (stderr or "").lower()
    if any(mk in low for mk in _TRANSIENT_MARKERS):
        return "infra_transient"
    return "genuine"


def checksum_manifest(files: dict[str, bytes | str]) -> dict[str, str]:
    """sha256 of each frozen file's content (path -> hex)."""
    out: dict[str, str] = {}
    for path, content in files.items():
        b = content.encode() if isinstance(content, str) else content
        out[path] = hashlib.sha256(b).hexdigest()
    return out


def compare_checksums(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Frozen files whose hash changed or which vanished -> tampered (test-neutering)."""
    tampered = []
    for path, h in before.items():
        if after.get(path) != h:
            tampered.append(path)
    return sorted(tampered)


def stub_floor_ok(stub_passes: bool) -> bool:
    """The frozen suite MUST fail on the unmodified stub. If the stub already passes,
    the suite is a weak oracle (or the stub wasn't really stubbed) -> not OK."""
    return not stub_passes


# --------------------------------------------------------------------------- #
# #7 Good-Turing / STADS residual-risk stop for verifier sizing.
#   Model verifier generation as species discovery: each distinct behavior a check
#   catches is a "species". The Good-Turing estimate of the mass of UNSEEN species
#   is N1/N (N1 = species seen exactly once, N = total observations). Stop growing
#   the suite when that upper bound on "a behavior we haven't covered yet" drops
#   below a threshold — a principled stop that quantifies remaining risk, unlike a
#   fixed cap or a single saturated proxy.
# --------------------------------------------------------------------------- #
def good_turing_unseen_mass(species_counts: list[int]) -> float:
    """N1/N from per-species observation counts. 1.0 when no observations (all risk)."""
    total = sum(c for c in species_counts if c > 0)
    if total <= 0:
        return 1.0
    singletons = sum(1 for c in species_counts if c == 1)
    return singletons / total


def residual_risk_saturated(species_counts: list[int], *, threshold: float = 0.05,
                            min_observations: int = 20) -> bool:
    """Stop iff enough has been observed AND the unseen-mass bound is below threshold."""
    total = sum(c for c in species_counts if c > 0)
    if total < min_observations:
        return False
    return good_turing_unseen_mass(species_counts) < threshold
