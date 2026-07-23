"""The single source of truth for the verification output layout, so every reader
and writer stays consistent. Root holds ONLY verifiers/ (definitions) and results/
(findings); pytest and rubric mirror each other under both.

    verification/
      verifiers/  TRUTH.md  coverage.json  target_manifest.json
                  pytest/ { test_truth_generated.py, predicates.json }
                  rubric/ { rubric.json }
      results/    pytest/ { anchors.json, meta_verification.json, <run>/ { results.json, logs/ } }
                  rubric/ { anchors.json,                          <run>/ { results.json } }
                  <run>/  { report.json, reexec.json }
    (<run> = <model>/agent/run_N)
"""
from __future__ import annotations

from pathlib import Path


def uuid_root_of(run_dir: str | Path) -> Path:
    """The task/uuid root from a run dir (…/<uuid>/runs/<model>/agent/run_N)."""
    p = Path(run_dir).resolve()
    for anc in (p, *p.parents):
        if anc.name == "runs":
            return anc.parent
        if (anc / "datasets" / "entries.json").exists() or (anc / "runs").is_dir():
            return anc
    return p


def vroot(uuid_root: str | Path) -> Path:
    return Path(uuid_root) / "verification"


# ---- verifiers/ (definitions) --------------------------------------------- #
def verifiers_dir(uuid_root: str | Path) -> Path:
    return vroot(uuid_root) / "verifiers"


def truth_path(uuid_root) -> Path:
    return verifiers_dir(uuid_root) / "TRUTH.md"


def coverage_path(uuid_root) -> Path:
    return verifiers_dir(uuid_root) / "coverage.json"


def manifest_path(uuid_root) -> Path:
    return verifiers_dir(uuid_root) / "target_manifest.json"


def pytest_code_path(uuid_root) -> Path:
    return verifiers_dir(uuid_root) / "pytest" / "test_truth_generated.py"


def predicates_path(uuid_root) -> Path:
    return verifiers_dir(uuid_root) / "pytest" / "predicates.json"


def rubric_path(uuid_root) -> Path:
    return verifiers_dir(uuid_root) / "rubric" / "rubric.json"


# ---- results/ (findings) -------------------------------------------------- #
def results_dir(uuid_root) -> Path:
    return vroot(uuid_root) / "results"


def run_tail(run_dir: str | Path) -> Path:
    """<model>/agent/run_N from a run dir (…/runs/<model>/agent/run_N)."""
    parts = Path(run_dir).resolve().parts
    if "runs" in parts:
        return Path(*parts[parts.index("runs") + 1:])
    return Path(Path(run_dir).name)


# per-task (shared across runs of the same task)
def pytest_anchors_path(uuid_root) -> Path:
    return results_dir(uuid_root) / "pytest" / "anchors.json"


def pytest_meta_path(uuid_root) -> Path:
    return results_dir(uuid_root) / "pytest" / "meta_verification.json"


def pytest_task_logs_dir(uuid_root) -> Path:
    return results_dir(uuid_root) / "pytest" / "logs"


def rubric_anchors_path(uuid_root) -> Path:
    return results_dir(uuid_root) / "rubric" / "anchors.json"


# per-run
def pytest_results_path(uuid_root, run_dir) -> Path:
    return results_dir(uuid_root) / "pytest" / run_tail(run_dir) / "results.json"


def pytest_logs_dir(uuid_root, run_dir) -> Path:
    return results_dir(uuid_root) / "pytest" / run_tail(run_dir) / "logs"


def rubric_results_path(uuid_root, run_dir) -> Path:
    return results_dir(uuid_root) / "rubric" / run_tail(run_dir) / "results.json"


def report_path(uuid_root, run_dir) -> Path:
    return results_dir(uuid_root) / run_tail(run_dir) / "report.json"


def reexec_path(uuid_root, run_dir) -> Path:
    return results_dir(uuid_root) / run_tail(run_dir) / "reexec.json"
