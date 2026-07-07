"""Write a stage-wise cumulative patch.diff for a repo.

Each language pipeline runs multiple stages (draft -> lint -> test) that commit
to the SAME branch progressively, so the branch HEAD after a stage is that
stage's full state. This writes the cumulative diff (base_commit..HEAD) as
`patch.diff` in the stage's output dir, so every stage carries an applyable
patch next to its per-module output.json — the same diff the eval reconstructs
from. Shared by every language's agent runner so the artifact is identical.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union


def write_stage_patch(
    local_repo,
    base_commit: str,
    experiment_log_dir: Union[str, Path],
    logger: logging.Logger,
    filter_fn=None,
) -> None:
    """Write `experiment_log_dir/patch.diff` = diff(base_commit..HEAD).

    ``filter_fn`` (optional) post-processes the raw diff string (e.g. the Rust
    ``filter_rust_patch`` that strips ``target/`` artifacts). Best-effort: never
    raises — the patch is a convenience artifact, not load-bearing.
    """
    try:
        stage_patch = local_repo.git.diff(base_commit, "HEAD")
        if filter_fn is not None and stage_patch:
            try:
                stage_patch = filter_fn(stage_patch)
            except Exception:  # noqa: BLE001 - fall back to the unfiltered diff
                pass
        out = Path(experiment_log_dir) / "patch.diff"
        out.write_text(
            (stage_patch + "\n") if stage_patch.strip() else "",
            encoding="utf-8", errors="surrogateescape",
        )
        logger.info("Wrote stage patch.diff (%d bytes) to %s", len(stage_patch), out)
    except Exception as e:  # noqa: BLE001 - convenience artifact
        logger.warning("Failed to write stage patch.diff: %s", e)


__all__ = ["write_stage_patch"]
