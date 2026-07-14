"""Regression tests for tools.create_dataset_cpp.

Fixture that motivated these tests: outputs/6e96c7a8-.../datasets/ contained
ONLY fmt_test_ids.bz2 (no entries.json, no fmt_spec.pdf.bz2). Root cause:
create_dataset_cpp.py staged the test_ids inventory but neither wrote
entries.json nor documented the repo_base contract, so a follow-up refactor
could re-break the spec lookup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "create_dataset_cpp.py"
SOURCE = TOOL.read_text(encoding="utf-8")


class TestSourceInvariants:
    def test_imports_datasets_dir(self) -> None:
        assert "from kaiju.paths import datasets_dir" in SOURCE

    def test_calls_copy_inference_inputs_for_cpp(self) -> None:
        assert 'test_ids_subdir="cpp_test_ids"' in SOURCE, (
            "cpp test-id inventory must resolve under commit0/data/cpp_test_ids"
        )
        assert 'repo_base="repos"' in SOURCE, (
            "spec.pdf.bz2 lookup in copy_inference_inputs expects the same "
            "REPO_BASE that run_pipeline_cpp.sh + ensure_spec_docs_cpp write to"
        )

    def test_writes_entries_json_in_consolidated_layout(self) -> None:
        assert 'datasets_dir(_run_uuid) / "entries.json"' in SOURCE, (
            "entries.json must be staged in outputs/<uuid>/datasets/ so downstream"
            " tooling (verify_inventory, agent) can find the run's entries"
        )


def _make_stub_entry(repo: str = "fmtlib/fmt") -> dict:
    return {
        "instance_id": repo.replace("/", "__"),
        "repo": repo,
        "original_repo": repo,
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "reference_commit": "abcdef0123456789abcdef0123456789abcdef01",
        "setup": {"build_system": "cmake", "install": "", "packages": "", "pip_packages": "",
                  "pre_install": "", "specification": ""},
        "test": {"test_cmd": "ctest --test-dir build --verbose", "test_dir": "tests"},
        "src_dir": "src",
    }


@pytest.fixture
def isolated_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    entries_file = tmp_path / "entries.json"
    entries_file.write_text(json.dumps([_make_stub_entry()], indent=2))
    outputs_root = tmp_path / "outputs"
    output_dataset = tmp_path / "cpp_dataset.json"
    return entries_file, outputs_root, output_dataset


class TestConsolidatedStaging:
    def test_create_writes_entries_json_under_uuid(
        self, isolated_run: tuple[Path, Path, Path]
    ) -> None:
        entries_file, outputs_root, output_dataset = isolated_run
        env = os.environ.copy()
        env["KAIJU_OUTPUTS_ROOT"] = str(outputs_root)
        env["KAIJU_LOG_LAYOUT"] = "consolidated"
        result = subprocess.run(
            [sys.executable, "-m", "tools.create_dataset_cpp", "create",
             str(entries_file), "--output", str(output_dataset)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"create_dataset_cpp exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert output_dataset.exists()
        staged_entries = json.loads(output_dataset.read_text())
        assert isinstance(staged_entries, list) and staged_entries
        run_uuid = staged_entries[0]["id"]
        consolidated_entries = outputs_root / run_uuid / "datasets" / "entries.json"
        assert consolidated_entries.exists(), (
            f"entries.json should be staged at {consolidated_entries}. "
            "Regression: earlier CPP runs left datasets/ with only *_test_ids.bz2."
        )
        parsed = json.loads(consolidated_entries.read_text())
        assert isinstance(parsed, list) and parsed
        assert parsed[0]["repo"] == "fmtlib/fmt"
        assert parsed[0]["id"] == run_uuid

    def test_flat_layout_does_not_stage_entries_json(
        self, isolated_run: tuple[Path, Path, Path]
    ) -> None:
        entries_file, outputs_root, output_dataset = isolated_run
        env = os.environ.copy()
        env["KAIJU_OUTPUTS_ROOT"] = str(outputs_root)
        env["KAIJU_LOG_LAYOUT"] = "flat"
        result = subprocess.run(
            [sys.executable, "-m", "tools.create_dataset_cpp", "create",
             str(entries_file), "--output", str(output_dataset)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=60,
        )
        assert result.returncode == 0
        assert not (outputs_root).exists() or not any(outputs_root.rglob("entries.json"))
