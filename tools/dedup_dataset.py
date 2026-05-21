"""De-duplicate hyphen/underscore variants of the same repo in a dataset tree.

Section 5 of ``MISSING_TEST_IDS_BZ2_ISSUE.md`` identified ~12 repos that
appear twice under ``datasets/python/`` — once hyphenated and once with
underscores (e.g. ``dsdanielpark_bard-api`` AND ``dsdanielpark_bard_api``).
These came from a noisy input repo list where the same upstream was listed
under two spellings.

This tool walks the dataset directory, groups folders that
:func:`tools._repo_naming.normalized_for_dedup` collapses to the same key,
and for each duplicate group picks a *winner* (the variant with more files,
tie-broken by preferring the hyphen form per Section 5 prior art). It
*emits a plan as JSON*, requiring an explicit ``--apply`` flag to actually
remove the loser variants.

Safety:

* Never deletes by default — ``--apply`` required.
* Even with ``--apply``, the plan is written to disk first so deletions
  are recoverable.
* Files in losers are listed in the plan so post-hoc inspection works.

Usage::

    # Inspect duplicates, no deletions
    python -m tools.dedup_dataset /path/to/datasets/python

    # Write the plan to JSON
    python -m tools.dedup_dataset /path/to/datasets/python --plan dedup_plan.json

    # Apply the plan (deletes the loser folders!)
    python -m tools.dedup_dataset /path/to/datasets/python --plan dedup_plan.json --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tools._repo_naming import normalized_for_dedup

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

__all__ = [
    "DedupDecision",
    "DedupGroup",
    "build_dedup_plan",
    "find_duplicate_groups",
    "pick_winner",
]


@dataclass(frozen=True)
class DedupGroup:
    """A group of folders that normalize to the same dedup key."""

    key: str
    folders: tuple[Path, ...]


@dataclass(frozen=True)
class DedupDecision:
    """The pick-a-winner + delete-the-losers decision for one group."""

    key: str
    winner: Path
    losers: tuple[Path, ...]
    winner_file_count: int
    loser_file_counts: tuple[int, ...]
    winner_files: tuple[str, ...]
    loser_files: tuple[tuple[str, ...], ...] = field(default_factory=tuple)


def find_duplicate_groups(root: Path) -> list[DedupGroup]:
    """Group ``<org>_<repo>`` folders under ``root`` by their dedup key.

    Returns only groups with more than one member (i.e. actual duplicates).
    """
    if not root.is_dir():
        return []
    groups_map: dict[str, list[Path]] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # We dedup only on the folder's short name (everything after the first '_')
        # because the org prefix is stable; collision-by-org is unlikely
        # and would be the actual upstream renaming kaiju shouldn't auto-fix.
        key = normalized_for_dedup(child.name)
        groups_map.setdefault(key, []).append(child)
    return [
        DedupGroup(key=key, folders=tuple(folders))
        for key, folders in sorted(groups_map.items())
        if len(folders) > 1
    ]


def _count_files(folder: Path) -> tuple[int, list[str]]:
    files = [str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()]
    return len(files), files


def pick_winner(folders: tuple[Path, ...]) -> tuple[Path, tuple[Path, ...]]:
    """Pick the winner from a duplicate group.

    Rule (from Oracle review):
    1. Most files wins.
    2. Tie-break: prefer the **hyphen** variant (matches upstream PyPI/GitHub
       spelling more often than the underscore variant).
    3. Stable secondary tie-break: lexicographic name (so the choice is
       deterministic across runs).
    """
    def _key(p: Path) -> tuple[int, int, str]:
        count, _ = _count_files(p)
        hyphen_score = 1 if "-" in p.name else 0  # higher = preferred
        return (count, hyphen_score, p.name)

    ranked = sorted(folders, key=_key, reverse=True)
    return ranked[0], tuple(ranked[1:])


def build_dedup_plan(root: Path) -> list[DedupDecision]:
    """Build the full delete-the-losers plan for ``root``."""
    decisions: list[DedupDecision] = []
    for group in find_duplicate_groups(root):
        winner, losers = pick_winner(group.folders)
        winner_count, winner_files = _count_files(winner)
        loser_counts: list[int] = []
        loser_files: list[tuple[str, ...]] = []
        for loser in losers:
            c, files = _count_files(loser)
            loser_counts.append(c)
            loser_files.append(tuple(files))
        decisions.append(
            DedupDecision(
                key=group.key,
                winner=winner,
                losers=losers,
                winner_file_count=winner_count,
                loser_file_counts=tuple(loser_counts),
                winner_files=tuple(winner_files),
                loser_files=tuple(loser_files),
            )
        )
    return decisions


def _decision_to_dict(d: DedupDecision) -> dict:
    return {
        "key": d.key,
        "winner": {
            "path": str(d.winner),
            "name": d.winner.name,
            "file_count": d.winner_file_count,
            "files": list(d.winner_files),
        },
        "losers": [
            {
                "path": str(p),
                "name": p.name,
                "file_count": d.loser_file_counts[i],
                "files": list(d.loser_files[i]) if i < len(d.loser_files) else [],
            }
            for i, p in enumerate(d.losers)
        ],
    }


def apply_plan(plan: list[DedupDecision], *, dry_run: bool = False) -> int:
    """Delete loser folders. Returns count of folders deleted.

    ``dry_run=True`` logs what would be deleted but does nothing.
    """
    deleted = 0
    for decision in plan:
        for loser in decision.losers:
            if dry_run:
                logger.info("[dry-run] Would delete %s", loser)
            else:
                shutil.rmtree(loser)
                logger.info("Deleted %s", loser)
                deleted += 1
    return deleted


def print_plan(plan: list[DedupDecision]) -> None:
    """Print a human-readable summary."""
    if not plan:
        print("No duplicate groups found.")
        return
    print(f"\n{'=' * 70}")
    print(f"Dedup plan: {len(plan)} duplicate groups, "
          f"{sum(len(d.losers) for d in plan)} folders would be deleted")
    print(f"{'=' * 70}\n")
    for d in plan:
        print(f"  Group: {d.key}")
        print(f"    WINNER ({d.winner_file_count:3d} files): {d.winner.name}")
        for i, loser in enumerate(d.losers):
            print(f"    LOSER  ({d.loser_file_counts[i]:3d} files): {loser.name}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Find and (optionally) delete hyphen/underscore duplicate "
            "<org>_<repo> folders in a dataset directory."
        )
    )
    parser.add_argument(
        "root",
        type=str,
        help="Directory containing the <org>_<repo> folders to scan.",
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=None,
        help="Write the dedup plan as JSON to PATH (always written before any --apply).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the loser folders. Without this, dry-run only.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt (use in scripts).",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        logger.error("Directory not found: %s", root)
        return 2

    plan = build_dedup_plan(root)
    print_plan(plan)

    if args.plan:
        plan_path = Path(args.plan)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(
            json.dumps(
                {
                    "root": str(root),
                    "groups": [_decision_to_dict(d) for d in plan],
                    "_schema": "kaiju-dedup-plan/1",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("Wrote plan to %s", plan_path)

    if not args.apply:
        if plan:
            logger.info("Dry-run only. Re-run with --apply to delete loser folders.")
        return 0

    if not plan:
        return 0

    if not args.yes:
        n_losers = sum(len(d.losers) for d in plan)
        try:
            ans = input(f"\nDelete {n_losers} loser folder(s)? [y/N]: ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in {"y", "yes"}:
            logger.info("Aborted.")
            return 0

    deleted = apply_plan(plan)
    logger.info("Deleted %d folder(s).", deleted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
