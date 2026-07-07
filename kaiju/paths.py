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
