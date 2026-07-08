"""Write a stage-wise ``model_changes.diff`` for a repo.

Each language pipeline runs multiple stages (draft -> lint -> test) that commit
to the SAME branch progressively, so the branch HEAD after a stage is that
stage's full state. This writes the cumulative diff (base_commit..HEAD) into the
stage's output dir as ``model_changes.diff``.

This is deliberately the FULL record of everything the model changed — it KEEPS
the model's edits to ``tests/``, ``benches/`` and manifests (``Cargo.toml`` …)
so they can be tracked/audited downstream. It is NOT the scored patch: the eval
writes its own source-only, protected-path-excluded ``patch.diff`` under
``<stage>_eval_artifacts/`` (that is the diff the eval actually applied and
scored). Keeping the two artifacts distinct — ``model_changes.diff`` (complete)
vs the eval's ``patch.diff`` (scored) — is intentional.

To stay a clean, text-only, applyable artifact, ``model_changes.diff`` still
excludes pure noise: the binary/bz2 spec doc, aider's own cache files, and build
directories. (Language build artifacts like Rust's ``target/`` are additionally
stripped via ``filter_fn``.)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

# Noise that must never enter model_changes.diff even though the model "touched"
# it: the binary spec doc, aider's internal cache, and build dirs. These are git
# ``:(exclude)`` pathspecs (magic prefix), applied to the diff directly.
_STAGE_PATCH_EXCLUDES: tuple[str, ...] = (
    ":(exclude)spec.pdf",
    ":(exclude)spec.pdf.bz2",
    ":(exclude)*.pdf",
    ":(exclude)*.pdf.bz2",
    ":(exclude).aider*",
    ":(exclude)**/.aider*",
    ":(exclude)target/**",
    ":(exclude)**/target/**",
    ":(exclude)node_modules/**",
    ":(exclude)**/node_modules/**",
    ":(exclude)__pycache__/**",
    ":(exclude)**/__pycache__/**",
)

DEFAULT_FILENAME = "model_changes.diff"


def write_stage_patch(
    local_repo,
    base_commit: str,
    experiment_log_dir: Union[str, Path],
    logger: logging.Logger,
    filter_fn=None,
    filename: str = DEFAULT_FILENAME,
) -> None:
    """Write ``experiment_log_dir/<filename>`` = the full model-changes diff
    (``base_commit..HEAD``), excluding binary/cache/build noise.

    ``filter_fn`` (optional) post-processes the raw diff (e.g. the Rust
    ``filter_rust_patch`` that precisely strips ``target/`` artifacts). This is
    the COMPLETE record of the model's changes (keeps tests/benches/manifests) —
    NOT the eval's scored patch. Best-effort: never raises.
    """
    try:
        stage_patch = local_repo.git.diff(
            "--no-renames", base_commit, "HEAD", "--", ".", *_STAGE_PATCH_EXCLUDES
        )
        if filter_fn is not None and stage_patch:
            try:
                stage_patch = filter_fn(stage_patch)
            except Exception:  # noqa: BLE001 - fall back to the unfiltered diff
                pass
        out = Path(experiment_log_dir) / filename
        out.write_text(
            (stage_patch + "\n") if stage_patch.strip() else "",
            encoding="utf-8", errors="surrogateescape",
        )
        logger.info("Wrote %s (%d bytes) to %s", filename, len(stage_patch), out)
    except Exception as e:  # noqa: BLE001 - audit artifact, not load-bearing
        logger.warning("Failed to write %s: %s", filename, e)


__all__ = ["write_stage_patch", "DEFAULT_FILENAME"]
