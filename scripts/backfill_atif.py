#!/usr/bin/env python3
"""backfill_atif.py — post-hoc Harbor/ATIF conversion for a run dir.

The pipelines convert each run's trajectory to ATIF **in-run** (run_pipeline*.sh
invokes commit0_to_atif_v2.py right after save_results), so a run executed on
ANOTHER machine — or one whose in-run conversion failed — arrives without
``Harbor_Data/``. This wrapper replays exactly that in-pipeline invocation for
every completed run under a uuid root, writing the same self-contained layout:

    <uuid_root>/Harbor_Data/Trajectory/<task>/<model>/agent/<stage__module>/trajectory.json

Usage:
    python scripts/backfill_atif.py <uuid_root> [--force] [--mirror]

    <uuid_root>   outputs/<uuid> or a copied dir like ringbuf_<uuid>
    --force       reconvert even if trajectory.json files already exist
    --mirror      also copy into the shared top-level Harbor_Data/Trajectory
                  (what the in-pipeline flow does on this machine's runs)

Skips (with a reason) any run without pipeline_results.json — conversion needs
the per-stage results for rewards, and a run that never finished has nothing
final to convert (resume it first).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONVERTER = REPO / "scripts" / "commit0_to_atif_v2.py"


def _task_name(results: dict) -> str:
    # Mirror run_pipeline.sh: DATASET_DIR_NAME=$(echo "$DATASET_SHORT" | tr -dc 'a-zA-Z0-9._-')
    short = str(results.get("dataset_short") or "dataset")
    return re.sub(r"[^a-zA-Z0-9._-]", "", short) or "dataset"


def _already_converted(out_root: Path, task: str, model: str) -> int:
    d = out_root / task / model
    return len(list(d.rglob("trajectory.json"))) if d.is_dir() else 0


def backfill(uuid_root: Path, *, force: bool = False, mirror: bool = False) -> int:
    out_root = uuid_root / "Harbor_Data" / "Trajectory"
    run_dirs = sorted(uuid_root.glob("runs/*/agent/run_*"))
    if not run_dirs:
        print(f"[backfill] no runs/*/agent/run_* under {uuid_root}")
        return 1
    failures = 0
    converted = 0
    for run_dir in run_dirs:
        model = run_dir.parts[-3]
        results_path = run_dir / "pipeline_results.json"
        if not results_path.exists():
            print(f"[backfill] SKIP {run_dir.relative_to(uuid_root)} — no "
                  "pipeline_results.json (run incomplete; resume it first)")
            continue
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[backfill] SKIP {run_dir.relative_to(uuid_root)} — unreadable "
                  f"pipeline_results.json: {exc}")
            failures += 1
            continue
        task = _task_name(results)
        existing = _already_converted(out_root, task, model)
        if existing and not force:
            print(f"[backfill] SKIP {run_dir.relative_to(uuid_root)} — "
                  f"{existing} trajectory.json already present (use --force)")
            continue
        cmd = [sys.executable, str(CONVERTER), str(run_dir), str(out_root),
               "--kaiju-mode", "--pipeline", str(results_path),
               "--task-name", task]
        print(f"[backfill] converting {run_dir.relative_to(uuid_root)} "
              f"(task={task}, model={model}) ...")
        r = subprocess.run(cmd, cwd=str(REPO))
        if r.returncode != 0:
            # The converter fails loud on hard errors / silent-corruption
            # tripwires (edit_format_ok) — surface, don't mask.
            print(f"[backfill] FAILED rc={r.returncode} for {run_dir}")
            failures += 1
            continue
        n = _already_converted(out_root, task, model)
        print(f"[backfill] OK — {n} trajectory.json under "
              f"{out_root.relative_to(uuid_root)}/{task}/{model}")
        converted += 1
    if mirror and out_root.is_dir():
        shared = REPO / "Harbor_Data" / "Trajectory"
        shared.mkdir(parents=True, exist_ok=True)
        shutil.copytree(out_root, shared, dirs_exist_ok=True)
        print(f"[backfill] mirrored -> {shared}")
    print(f"[backfill] done: {converted} run(s) converted, {failures} failure(s)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("uuid_root")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--mirror", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.uuid_root).resolve()
    if not root.is_dir():
        print(f"[backfill] not a directory: {root}")
        return 2
    return backfill(root, force=args.force, mirror=args.mirror)


if __name__ == "__main__":
    raise SystemExit(main())
