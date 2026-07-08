"""Centralized path resolution for experiment outputs.

Public API: every consumer that needs an output directory for a specific
experiment (identified by its UUID) resolves the path here. This is the
single place that knows whether we are in ``flat`` (legacy) or
``consolidated`` (``outputs/<uuid>/``) layout mode. Do not hardcode
``outputs/`` or ``logs/`` paths outside this module.
"""
from __future__ import annotations
import os
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def outputs_root() -> Path:
    return Path(os.environ.get("KAIJU_OUTPUTS_ROOT", REPO_ROOT / "outputs"))


def layout() -> str:
    return os.environ.get("KAIJU_LOG_LAYOUT", "consolidated").lower()


def is_consolidated() -> bool:
    return layout() == "consolidated"


def experiment_dir(uuid: str) -> Path:
    d = outputs_root() / uuid
    d.mkdir(parents=True, exist_ok=True)
    return d


def datasets_dir(uuid: str) -> Path:
    d = experiment_dir(uuid) / "datasets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def configs_dir(uuid: str) -> Path:
    d = experiment_dir(uuid) / "configs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_logs_dir(uuid: str) -> Path:
    d = experiment_dir(uuid) / "build_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def runs_dir(uuid: str) -> Path:
    d = experiment_dir(uuid) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def harbor_dir(uuid: str) -> Path:
    d = experiment_dir(uuid) / "harbor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def spec_path(uuid: str) -> Path:
    return experiment_dir(uuid) / "spec.pdf.bz2"


def prep_log_path(uuid: str) -> Path:
    return experiment_dir(uuid) / "prep.log"


def normalize_test_ids_key(name: str) -> str:
    """Canonical bz2 name key: ``name.lower().replace(".", "-")``.

    This MUST stay byte-for-byte identical to the key ``save_test_ids`` writes
    (``tools/generate_test_ids.py``): the file is stored under the normalized
    name, so every reader/copier has to normalize too or it silently misses the
    inventory (dropping the scoring denominator to the observed count). The
    transform is idempotent, so callers that already pre-normalize (ts/c/pytest
    readers) are unaffected. Underscores are intentionally preserved to match
    ``save_test_ids`` exactly.
    """
    return name.lower().replace(".", "-")


def find_test_ids_file(commit0_path: str, subdir: str, filename: str) -> Path | None:
    # Normalize the STEM to the same key save_test_ids used (lower + dot->hyphen).
    # Callers pass a raw repo basename (rust/cpp) OR an already-normalized name
    # (ts/c/pytest); normalization is idempotent so both resolve to the on-disk
    # <normalized>.bz2. Without this a repo basename with uppercase or a dot
    # (e.g. "RustCrypto", "foo.bar") saved as "rustcrypto"/"foo-bar" never
    # resolves and the denominator silently collapses to the observed count.
    suffix = ".bz2" if filename.endswith(".bz2") else ""
    raw_stem = filename[: -len(suffix)] if suffix else filename
    stem = normalize_test_ids_key(raw_stem)
    filename = f"{stem}{suffix}"
    override = os.environ.get("KAIJU_TEST_IDS_DIR", "")
    if override:
        override_dir = Path(override)
        for candidate in (override_dir / filename, override_dir / f"{stem}_test_ids.bz2"):
            if candidate.exists():
                return candidate
    legacy = Path(commit0_path) / "data" / subdir / filename
    if legacy.exists():
        return legacy
    return None


def copy_inference_inputs(uuid: str, split_name: str, *, test_ids_subdir: str = "test_ids", repo_base: str = "repos") -> dict:
    import shutil
    dst = datasets_dir(uuid)
    copied = {}
    # The inventory is saved under the NORMALIZED key (save_test_ids does
    # lower + dot->hyphen). Read AND write under that same key so the container
    # eval's find_test_ids_file lookup (which also normalizes) resolves it.
    # Using the raw basename here silently missed the source for any repo with
    # uppercase / dots, leaving no *_test_ids.bz2 staged -> observed-count
    # fallback. spec_rel paths key off the on-disk repo/spec dir, which uses the
    # raw basename, so those keep split_name.
    tid_key = normalize_test_ids_key(split_name)
    tid = REPO_ROOT / "commit0" / "data" / test_ids_subdir / f"{tid_key}.bz2"
    if tid.exists():
        target = dst / f"{tid_key}_test_ids.bz2"
        shutil.copy2(tid, target)
        copied["test_ids"] = str(target)
    for spec_rel in (Path(repo_base) / split_name / "spec.pdf.bz2", Path("specs") / f"{split_name}_readme_spec.pdf.bz2", Path("specs") / f"{split_name}.pdf.bz2"):
        spec_abs = REPO_ROOT / spec_rel
        if spec_abs.exists():
            target = dst / f"{split_name}_spec.pdf.bz2"
            shutil.copy2(spec_abs, target)
            copied["spec"] = str(target)
            break
    return copied
