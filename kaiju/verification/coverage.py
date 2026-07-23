"""The Concern Registry as a machine-checkable artifact (plan §4.4).

Emits ``coverage.json`` — the single source of truth for WHAT is covered and by
WHOM (which layer/owner), plus whether each concern is currently ENFORCED (has a
check) or PENDING. `registry_violations()` is the completeness + non-duplication
lint: every concern has exactly one owner, owners match layers, and no id repeats.
"""
from __future__ import annotations

import json
from pathlib import Path

from .taxonomy import TAXONOMY, TAXONOMY_VERSION, Layer, Owner
from .deterministic import CHECKS


def coverage_manifest() -> dict:
    concerns = []
    for c in TAXONOMY:
        concerns.append({
            "id": c.id,
            "title": c.title,
            "layer": int(c.layer),
            "owner": c.owner.value,
            "gating": c.gating,
            "phase": c.phase,
            "stages": list(c.stages),
            "enforced": c.id in CHECKS,
        })
    enforced = [x["id"] for x in concerns if x["enforced"]]
    return {
        "taxonomy_version": TAXONOMY_VERSION,
        "n_concerns": len(concerns),
        "n_enforced": len(enforced),
        "n_pending": len(concerns) - len(enforced),
        "concerns": concerns,
    }


def registry_violations() -> list[str]:
    """Completeness + non-duplication + owner/layer consistency. Empty == sound."""
    v: list[str] = []
    seen: set[str] = set()
    for c in TAXONOMY:
        if c.id in seen:
            v.append(f"duplicate concern id: {c.id}")
        seen.add(c.id)
        # single-owner ⇔ layer: rubric only in layer 2, deterministic only in 0/1.
        if c.owner is Owner.RUBRIC and c.layer is not Layer.JUDGMENT:
            v.append(f"{c.id}: rubric concern not in layer 2")
        if c.owner is Owner.DETERMINISTIC and c.layer not in (Layer.LEGITIMACY, Layer.STRUCTURE):
            v.append(f"{c.id}: deterministic concern not in layer 0/1")
        # a rubric concern must never have a deterministic check (partition).
        if c.owner is Owner.RUBRIC and c.id in CHECKS:
            v.append(f"{c.id}: rubric concern has a deterministic check")
    # every registered check maps to a real deterministic concern.
    ids = {c.id: c for c in TAXONOMY}
    for cid in CHECKS:
        if cid not in ids:
            v.append(f"check {cid} has no concern")
        elif ids[cid].owner is not Owner.DETERMINISTIC:
            v.append(f"check {cid} targets a non-deterministic concern")
    return v


def emit_coverage(uuid_root: str | Path) -> Path:
    from . import layout
    out = layout.coverage_path(uuid_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(coverage_manifest(), indent=2), encoding="utf-8")
    return out
