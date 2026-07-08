"""Scope a module's ``git_patch`` to the file(s) that module actually owns.

Each pipeline module owns a specific source file (or a small set of edit-target
files), but aider commits on its own cadence and often bundles edits to several
files into one commit. So the naive per-module patch — ``diff(pre_sha,
post_sha)`` (commit window) or ``extract_git_patch(base)`` (whole branch) —
attributes *other* modules' files to this module (e.g. the ``src__hazard``
module showing ``src/domain.rs``).

``module_file_patch`` restricts the diff to the module's own file(s), so each
module's ``output.json`` records only its own contribution — regardless of when
aider chose to commit. Shared by every language's agent runner.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional, Union


def module_file_patch(
    local_repo,
    base_commit: str,
    target_sha: str,
    rel_paths: Union[str, Iterable[str]],
    filter_fn=None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Diff of ONLY this module's own file(s), ``base_commit..target_sha``.

    ``rel_paths`` is the module's assigned repo-relative source file (a str) or
    the set of edit-target files it was allowed to touch (an iterable). Using
    ``base_commit`` (not ``pre_sha``) makes the result self-contained — the
    file's full diff from the stub — and independent of aider's commit timing.

    ``filter_fn`` (optional) post-processes the raw diff (e.g. Rust's
    ``filter_rust_patch`` stripping ``target/``). Best-effort: never raises.
    """
    paths = [rel_paths] if isinstance(rel_paths, str) else [p for p in rel_paths if p]
    if not paths:
        return ""
    try:
        raw = local_repo.git.diff(
            "--no-renames", base_commit, target_sha, "--", *paths
        )
    except Exception as e:  # noqa: BLE001 - patch is a reporting artifact
        if logger is not None:
            logger.warning("module_file_patch failed for %s: %s", paths, e)
        return ""
    if not raw.strip():
        return ""
    if filter_fn is not None:
        try:
            raw = filter_fn(raw)
        except Exception:  # noqa: BLE001 - fall back to the unfiltered diff
            pass
    return raw


__all__ = ["module_file_patch"]
