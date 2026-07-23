"""#3a — the persisted target-module manifest.

The harness re-derives the target file set each run and never persists it, so
"did the agent address ALL N target modules?" can't be checked. The manifest is
derivable HOST-SIDE from the golden diff (the files the golden solution changed =
the files that were stubbed) — no run-time harness change needed. It is written
alongside the frozen bundle and consumed by an upgraded DRAFT_MODULES_ADDRESSED.
"""
from __future__ import annotations

import json
from pathlib import Path


def target_files_from_diff(golden_diff: str) -> list[str]:
    out = []
    for line in golden_diff.splitlines():
        if line.startswith("+++ b/") and line[6:].strip() != "/dev/null":
            out.append(line[6:].strip())
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                out.append(parts[-1][2:] if parts[-1].startswith("b/") else parts[-1])
    return sorted(set(out))


def write_manifest(uuid_root: str | Path, stub_files: list[str]) -> Path:
    from . import layout
    out = layout.manifest_path(uuid_root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"stage1": sorted(set(stub_files))}, indent=2), encoding="utf-8")
    return out


def load_manifest(run_dir_or_uuid: str | Path) -> dict | None:
    from . import layout
    p = Path(run_dir_or_uuid)
    for anc in (p, *p.parents):
        cand = layout.manifest_path(anc)
        if cand.exists():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None
