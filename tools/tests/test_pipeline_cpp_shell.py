"""Regression tests for run_pipeline_cpp.sh.

Locks in shell-driver invariants that were missing when outputs/6e96c7a8-.../
datasets/ contained only fmt_test_ids.bz2:
  - ensure_spec_docs_cpp() must exist (parity with C/JS/TS/Go/Rust drivers).
  - It must be invoked in the main pipeline entrypoint, not just defined.
  - Syntactic validity via `bash -n`.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRIVER = REPO_ROOT / "run_pipeline_cpp.sh"
SOURCE = DRIVER.read_text(encoding="utf-8")


class TestShellInvariants:
    def test_bash_syntax_ok(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not on PATH")
        result = subprocess.run(
            [bash, "-n", str(DRIVER)], capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, (
            f"bash -n failed: stderr={result.stderr!r}"
        )

    def test_ensure_spec_docs_function_defined(self) -> None:
        assert "ensure_spec_docs_cpp()" in SOURCE, (
            "run_pipeline_cpp.sh must define ensure_spec_docs_cpp so <split>_spec"
            ".pdf.bz2 can be staged into outputs/<uuid>/datasets/ by"
            " copy_inference_inputs. Regression: previous CPP runs left"
            " datasets/ with only *_test_ids.bz2."
        )

    def test_ensure_spec_docs_invoked_in_pipeline(self) -> None:
        assert "ensure_spec_docs_cpp || true" in SOURCE, (
            "ensure_spec_docs_cpp must be CALLED in the pipeline entrypoint,"
            " not just defined. Best-effort (|| true) so USE_SPEC_INFO=false"
            " doesn't fail the run."
        )

    def test_repo_base_matches_create_dataset_cpp(self) -> None:
        assert 'REPO_BASE="${BASE_DIR}/repos"' in SOURCE, (
            "REPO_BASE must be ${BASE_DIR}/repos (this is where the spec.pdf"
            ".bz2 gets written by ensure_spec_docs_cpp and read by"
            " copy_inference_inputs(repo_base='repos') in create_dataset_cpp.py."
            " If REPO_BASE changes, create_dataset_cpp's repo_base MUST change"
            " to match or spec staging silently breaks.)"
        )
