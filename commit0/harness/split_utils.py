"""Universal split resolution for every per-language pipeline.

Replaces hardcoded ``*_SPLIT["all"]`` lists across ``constants_*.py``. The
``"all"`` subset is derived dynamically from whichever dataset the user
loaded; curated subsets like ``"lite"`` / ``"ethara"`` / ``"c_lite"`` stay
hand-curated in their respective ``constants_*.py`` modules.

Every consumer (``setup_*.py``, ``build_*.py``, ``evaluate_*.py``,
``save.py``, ``run_agent_*.py``) calls :func:`resolve_split` instead of
indexing into a hardcoded ``SPLIT[name]`` dict. Adding a new repo to a
dataset is therefore enough — no constants edit needed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _entry_field(entry: Any, key: str) -> str:
    """Read a field from a dataset entry (dict, mapping, or attribute object).

    Prefers ``entry[key]`` (matches how consumers read dataset rows) and falls
    back to ``getattr(entry, key)`` for objects without ``__getitem__``.
    Returns ``""`` for missing fields, ``None`` values, or auto-generated
    ``MagicMock`` attributes (which compare equal only to themselves).
    """
    try:
        value = entry[key]
    except (KeyError, TypeError, AttributeError):
        value = getattr(entry, key, None)
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return ""
    return value


def _materialize(dataset: Iterable[Any]) -> list[Any]:
    """Return a re-iterable view of ``dataset``.

    ``load_dataset_from_config`` returns either a list (local JSON) or a
    HuggingFace ``Dataset`` (re-iterable). Both pass through unchanged;
    raw iterators are materialized so callers can iterate again.
    """
    if isinstance(dataset, list):
        return dataset
    # HuggingFace Datasets and similar sequences expose ``__len__``;
    # treat them as already-materialized to avoid copying large objects.
    if hasattr(dataset, "__len__"):
        return dataset  # type: ignore[return-value]
    return list(dataset)


def derive_all_split(dataset: Iterable[Any]) -> list[str]:
    """Return every unique repo basename in ``dataset``, in first-seen order.

    Reads the canonical ``entry["repo"]`` field (e.g. ``"Zahgon/fmt"``) and
    returns ``["fmt", ...]``. Entries missing the field are skipped.
    """
    seen: set[str] = set()
    result: list[str] = []
    for entry in dataset:
        repo_path = _entry_field(entry, "repo")
        if not repo_path:
            continue
        basename = repo_path.split("/")[-1]
        if basename and basename not in seen:
            seen.add(basename)
            result.append(basename)
    return result


def build_dataset_alias_map(dataset: Iterable[Any]) -> dict[str, list[str]]:
    """Build ``alias → [basename]`` from a dataset.

    Each entry contributes up to four valid alias keys, each mapping to a
    one-element list with the repo basename:

    * ``instance_id`` (e.g. ``"go-version_go"``)
    * ``repo`` (canonical fork path, e.g. ``"Zahgon/go-version"``)
    * ``original_repo`` if present (e.g. ``"hashicorp/go-version"``)
    * the bare basename (e.g. ``"go-version"``)

    Lets ``run_pipeline_<lang>.sh`` scripts pass any of those names
    without having to hand-register them.
    """
    merged: dict[str, list[str]] = {}
    for entry in dataset:
        repo_path = _entry_field(entry, "repo")
        if not repo_path:
            continue
        basename = repo_path.split("/")[-1]
        aliases = {
            _entry_field(entry, "instance_id"),
            repo_path,
            _entry_field(entry, "original_repo"),
            basename,
        }
        aliases.discard("")
        for alias in aliases:
            merged.setdefault(alias, [basename])
    return merged


def resolve_split(
    split_name: str,
    dataset: Iterable[Any],
    curated: dict[str, list[str]] | None = None,
) -> list[str]:
    """Resolve a user-supplied split name to a list of repo basenames.

    Resolution order:

    1. ``"all"`` (or any ``"all_<lang>"`` variant) → :func:`derive_all_split`.
       This is intentionally unforgiving: callers asking for *all* repos in
       a dataset get every repo in the dataset, never a curated subset.
    2. Key in ``curated`` → that curated list. Values may be full paths
       (``"org/repo"``) or basenames; both are normalized to basenames so
       consumers can compare against ``example["repo"].split("/")[-1]``.
    3. Alias in the dataset (``instance_id`` / ``repo`` / ``original_repo`` /
       basename of some entry) → that entry's basename.
    4. Fuzzy basename match (normalizing ``-`` ↔ ``_``) → matching basename.
    5. No match → empty list. Callers should treat empty as "filter
       excludes everything" and surface a clear error to the user.

    Parameters
    ----------
    split_name : str
        User-supplied split key.
    dataset : Iterable
        Result of :func:`commit0.harness.utils.load_dataset_from_config`.
    curated : dict[str, list[str]] | None
        Optional hand-curated subset map
        (e.g. ``{"lite": [...], "ethara": [...]}``).

    Returns
    -------
    list[str]
        Repo basenames to include in this run.

    """
    entries = _materialize(dataset)

    if split_name == "all" or split_name.startswith("all_"):
        return derive_all_split(entries)

    if curated and split_name in curated:
        return [r.split("/")[-1] for r in curated[split_name]]

    alias_map = build_dataset_alias_map(entries)
    if split_name in alias_map:
        return alias_map[split_name]

    normalized = split_name.replace("-", "_")
    for basename in derive_all_split(entries):
        if basename.replace("-", "_") == normalized:
            return [basename]

    return []


__all__ = [
    "derive_all_split",
    "build_dataset_alias_map",
    "resolve_split",
]
