"""Resume a partially-completed trajectory from where a subscription limit (or a
kill) stopped it — WITHOUT redoing the modules that already succeeded.

The pipeline already persists everything needed to the HOST mount as it runs:
  * per-module ``output.json`` carrying ``test_result.git_patch`` (that module's
    cumulative ``base..HEAD`` diff, scoped to the files it owns), and
  * a ``.done`` marker beside it,
under ``<run_dir>/stage{1,2,3}_*/<repo>/<branch>/current/<module>/``.

Container removal loses the live git branch, but NOT these artifacts. So on a
fresh-container resume we can reconstruct the exact code state by replaying,
for each module, the git_patch from the HIGHEST stage it completed in — modules
own disjoint files, so the cumulative patches compose onto a clean base. The
existing ``_is_module_done`` (``.done``) check then skips the finished modules,
so only the module it died on + the remainder re-run.

Two entry points:
  * ``which_stage(results_json)`` — first stage lacking a recorded eval result,
    for the pipeline script to pass as ``--skip-to-stage`` (CLI: ``which-stage``).
  * ``restore_prior_progress(...)`` — rebuild the branch from the host patches,
    called by every ``run_agent_<lang>.py`` after ``create_branch`` when
    ``KAIJU_RESUME=1``.

Language-agnostic: it only manipulates git patches, so one implementation serves
go/rust/c/cpp/js/ts/java.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


_STAGES = ("stage1", "stage2", "stage3")
# Map a results-JSON stage key -> the stage dir glob prefix used on disk.
_STAGE_DIR_ORDER = ("stage1", "stage2", "stage3")


def which_stage(results_path: str | Path) -> str:
    """Return the first stage number ('1'/'2'/'3') that has NO recorded eval
    result yet — i.e. where a resume should re-enter — or '' if the run already
    completed all three (nothing to resume) or the results file is unusable.

    A stage's result is written only after its eval, so a stage that was killed
    mid-agent has no entry and is correctly chosen as the resume point (its
    finished modules are still skipped by their .done markers).
    """
    p = Path(results_path)
    if not p.is_file():
        return "1"  # no prior results at all -> resume re-runs stage 1 (done modules still skip)
    try:
        data = json.loads(p.read_text(errors="replace"))
    except Exception:  # noqa: BLE001
        return ""
    for i, key in enumerate(_STAGES, start=1):
        if not isinstance(data.get(key), dict):
            return str(i)
    return ""  # all three present -> complete


def _patch_target_files(patch_text: str) -> list[str]:
    """Extract the repo-relative target paths a unified diff touches (the `b/`
    side of each `diff --git a/... b/...` header)."""
    files: list[str] = []
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+)$", patch_text, flags=re.MULTILINE):
        files.append(m.group(2).strip())
    return files


def _collect_highest_stage_patches(
    run_dir: Path, repo_name: str, branch: str, logger: logging.Logger
) -> dict[str, str]:
    """For each module, return the git_patch from the HIGHEST stage it completed
    in. Layout-agnostic across languages:
      * per-module (go/rust/c/js/ts/java): ``current/<module>/output.json`` with a
        sibling ``.done``;
      * per-repo (cpp-style): ``current/output.json`` (or per-module output.json)
        covered by a single repo-level ``current/.done``.
    A patch is included when its module has a sibling ``.done`` OR the enclosing
    ``current/.done`` (repo-level) marks the whole repo complete. The HIGHEST stage
    wins even when its patch is EMPTY: a module that had code in stage 1 but was
    reverted to base in a later stage (e.g. a compile-gate revert) must NOT be
    restored from the stale stage-1 patch — an empty top-stage patch means "restore
    nothing", so the module is correctly left at base.
    """
    best: dict[str, tuple[int, str]] = {}  # key -> (highest stage index, patch)
    for stage_idx, stage in enumerate(_STAGE_DIR_ORDER):  # low -> high
        for current in sorted(run_dir.glob(f"{stage}_*/{repo_name}/{branch}/current")):
            repo_done = (current / ".done").exists()  # cpp/repo-level marker
            for out in sorted(current.rglob("output.json")):
                mod_dir = out.parent
                key = "__repo__" if mod_dir == current else mod_dir.name
                if not (repo_done or (mod_dir / ".done").exists()):
                    continue
                if key in best and best[key][0] >= stage_idx:
                    continue  # a same-or-higher stage already decided this module
                try:
                    d = json.loads(out.read_text(errors="replace"))
                except Exception:  # noqa: BLE001
                    continue
                gp = ((d.get("test_result") or {}).get("git_patch")) or d.get("git_patch") or ""
                best[key] = (stage_idx, gp if (gp and gp.strip()) else "")
    # Only modules whose winning (highest) stage has a non-empty patch are restored;
    # an empty winning patch = leave that module at base.
    by_module: dict[str, str] = {k: gp for k, (idx, gp) in best.items() if gp}
    return by_module


def restore_prior_progress(
    local_repo,
    base_commit: str,
    branch: str,
    run_dir: str | Path,
    repo_name: str,
    logger: Optional[logging.Logger] = None,
) -> dict:
    """Rebuild ``branch`` to the state a prior (killed) run left it in, by
    replaying each completed module's highest-stage patch onto ``base_commit``.

    Best-effort and idempotent: called after ``create_branch`` on the fresh
    checkout. Returns a summary dict; never raises (a resume that can't restore
    a given module simply lets that module re-run).
    """
    logger = logger or logging.getLogger("resume")
    run_dir = Path(run_dir)
    summary = {"modules_restored": 0, "modules_failed": 0, "files": 0, "modules": []}

    patches = _collect_highest_stage_patches(run_dir, repo_name, branch, logger)
    if not patches:
        logger.info("RESUME: no prior module patches found under %s — starting clean", run_dir)
        return summary

    repo_root = Path(local_repo.working_tree_dir)
    # Start from a pristine base so the cumulative (base..HEAD) module patches apply.
    try:
        local_repo.git.reset("--hard", base_commit)
    except Exception as e:  # noqa: BLE001
        logger.warning("RESUME: could not reset to base %s: %s", base_commit, e)

    for module, patch_text in patches.items():
        files = _patch_target_files(patch_text)
        # `git apply` rejects a patch whose final line lacks a newline as
        # "corrupt patch at line N"; the pipeline persists patches WITHOUT a
        # trailing newline, so normalize before applying.
        pt = patch_text if patch_text.endswith("\n") else patch_text + "\n"
        try:
            # Apply this module's cumulative diff. --whitespace=nowarn keeps it
            # quiet; the patch is base-relative so it lands on the clean base.
            subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                input=pt.encode(), cwd=str(repo_root),
                check=True, capture_output=True,
            )
            summary["modules_restored"] += 1
            summary["files"] += len(files)
            summary["modules"].append(module)
        except subprocess.CalledProcessError as e:
            summary["modules_failed"] += 1
            logger.warning("RESUME: patch for module %s did not apply (%s) — it will re-run",
                           module, (e.stderr or b"").decode(errors="replace").strip()[:200])
            # Revert any partial hunks for this module's files so the tree stays clean.
            for f in files:
                try:
                    local_repo.git.checkout(base_commit, "--", f)
                except Exception:  # noqa: BLE001
                    pass

    if summary["modules_restored"]:
        try:
            local_repo.git.add(A=True)
            local_repo.index.commit(
                f"resume: restore {summary['modules_restored']} completed module(s)")
        except Exception as e:  # noqa: BLE001
            logger.warning("RESUME: commit of restored state failed: %s", e)
    logger.info("RESUME: restored %d module(s) (%d file(s)); %d could not apply and will re-run",
                summary["modules_restored"], summary["files"], summary["modules_failed"])
    return summary


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Resume-state helper for the trajectory pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ws = sub.add_parser("which-stage", help="print the first incomplete stage (1/2/3) or empty")
    ws.add_argument("--results", required=True, help="path to pipeline_results.json")
    args = ap.parse_args(argv)
    if args.cmd == "which-stage":
        sys.stdout.write(which_stage(args.results))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
