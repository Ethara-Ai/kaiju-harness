"""Unit tests for ``tools.report_incomplete``."""

from __future__ import annotations

import json
from pathlib import Path


from tools.report_incomplete import (
    EntryReport,
    Layout,
    build_report,
    detect_layout,
    summarize,
    write_csv,
    write_json,
    write_markdown,
)


# ---------------------------------------------------------------------------
# Layout detection
# ---------------------------------------------------------------------------


class TestDetectLayout:
    def test_hf_dataset_layout(self, tmp_path: Path) -> None:
        sub = tmp_path / "acme_widget"
        sub.mkdir()
        (sub / "widget.bz2").write_bytes(b"\x00")
        assert detect_layout(tmp_path) == Layout.HF_DATASET

    def test_hf_layout_via_docker_dir(self, tmp_path: Path) -> None:
        sub = tmp_path / "acme_widget"
        (sub / "Docker").mkdir(parents=True)
        assert detect_layout(tmp_path) == Layout.HF_DATASET

    def test_flat_output_dir_layout(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.bz2").write_bytes(b"\x00")
        assert detect_layout(tmp_path) == Layout.FLAT_OUTPUT_DIR

    def test_flat_via_status_json_only(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.status.json").write_text("{}")
        assert detect_layout(tmp_path) == Layout.FLAT_OUTPUT_DIR

    def test_empty_dir_unknown(self, tmp_path: Path) -> None:
        assert detect_layout(tmp_path) == Layout.UNKNOWN

    def test_missing_dir_unknown(self, tmp_path: Path) -> None:
        assert detect_layout(tmp_path / "missing") == Layout.UNKNOWN


# ---------------------------------------------------------------------------
# HF layout scanning
# ---------------------------------------------------------------------------


def _make_hf_entry(
    root: Path,
    name: str,
    *,
    bz2: bool = True,
    spec_pdf: bool = True,
    entries_json: bool = True,
    docker_files: int = 3,
    status_json: dict | None = None,
) -> Path:
    folder = root / name
    folder.mkdir()
    short = name.split("_")[-1]
    if bz2:
        (folder / f"{short}.bz2").write_bytes(b"\x00")
    if spec_pdf:
        (folder / "spec.pdf.bz2").write_bytes(b"\x00")
    if entries_json:
        (folder / f"{name}_entries.json").write_text("{}")
    if docker_files:
        docker = folder / "Docker"
        docker.mkdir()
        for f in ("Dockerfile", "Dockerfile.base", "setup.sh")[:docker_files]:
            (docker / f).write_text("# stub")
    if status_json is not None:
        (folder / f"{name}.status.json").write_text(json.dumps(status_json))
    return folder


class TestBuildReportHfLayout:
    def test_complete_entry(self, tmp_path: Path) -> None:
        _make_hf_entry(tmp_path, "acme_widget")
        reports = build_report(tmp_path, layout=Layout.HF_DATASET)
        assert len(reports) == 1
        r = reports[0]
        assert r.name == "acme_widget"
        assert r.bz2_present is True
        assert r.entries_json_present is True
        assert r.spec_pdf_present is True
        assert r.docker_files_present == 3
        assert r.status == "ok"
        assert r.is_complete is True

    def test_missing_bz2_only(self, tmp_path: Path) -> None:
        _make_hf_entry(tmp_path, "acme_widget", bz2=False)
        reports = build_report(tmp_path, layout=Layout.HF_DATASET)
        r = reports[0]
        assert r.bz2_present is False
        assert r.status == "missing_bz2"
        assert r.is_complete is False

    def test_missing_both_bz2_and_spec(self, tmp_path: Path) -> None:
        _make_hf_entry(tmp_path, "acme_widget", bz2=False, spec_pdf=False)
        reports = build_report(tmp_path, layout=Layout.HF_DATASET)
        r = reports[0]
        assert r.bz2_present is False
        assert r.spec_pdf_present is False

    def test_status_json_picked_up(self, tmp_path: Path) -> None:
        _make_hf_entry(
            tmp_path, "acme_widget",
            status_json={
                "status": "missing_system_deps",
                "failing_module": "qgis",
                "test_count": 0,
                "python_version": "3.12",
                "system_deps_hint": ["qgis", "PyQt5"],
                "stderr_snippet": "ModuleNotFoundError: qgis",
            },
        )
        reports = build_report(tmp_path, layout=Layout.HF_DATASET)
        r = reports[0]
        assert r.status == "missing_system_deps"
        assert r.failing_module == "qgis"
        assert r.system_deps_hint == ["qgis", "PyQt5"]


# ---------------------------------------------------------------------------
# Flat layout scanning
# ---------------------------------------------------------------------------


class TestBuildReportFlatLayout:
    def test_bz2_only(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.bz2").write_bytes(b"\x00")
        reports = build_report(tmp_path, layout=Layout.FLAT_OUTPUT_DIR)
        assert len(reports) == 1
        r = reports[0]
        assert r.bz2_present is True
        assert r.status == "ok"

    def test_status_only_no_bz2(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.status.json").write_text(
            json.dumps({"status": "import_error", "failing_module": "tensorflow"})
        )
        reports = build_report(tmp_path, layout=Layout.FLAT_OUTPUT_DIR)
        r = reports[0]
        assert r.bz2_present is False
        assert r.status == "import_error"
        assert r.failing_module == "tensorflow"

    def test_both_files_present(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.bz2").write_bytes(b"\x00")
        (tmp_path / "mylib.status.json").write_text(
            json.dumps({"status": "ok", "test_count": 42, "python_version": "3.11"})
        )
        reports = build_report(tmp_path, layout=Layout.FLAT_OUTPUT_DIR)
        r = reports[0]
        assert r.bz2_present is True
        assert r.test_count == 42
        assert r.python_version == "3.11"

    def test_corrupt_status_json_handled(self, tmp_path: Path) -> None:
        (tmp_path / "mylib.bz2").write_bytes(b"\x00")
        (tmp_path / "mylib.status.json").write_text("not json")
        reports = build_report(tmp_path, layout=Layout.FLAT_OUTPUT_DIR)
        r = reports[0]
        assert r.bz2_present is True
        # status inferred as "ok" from bz2 presence
        assert r.status == "ok"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestSummarize:
    def test_counts(self) -> None:
        reports = [
            EntryReport(name="a", folder=".", bz2_present=True, entries_json_present=True,
                        spec_pdf_present=True, docker_files_present=3, status="ok"),
            EntryReport(name="b", folder=".", bz2_present=False, entries_json_present=True,
                        spec_pdf_present=True, docker_files_present=3, status="missing_bz2"),
            EntryReport(name="c", folder=".", bz2_present=False, entries_json_present=False,
                        spec_pdf_present=False, docker_files_present=0, status="missing_bz2"),
            EntryReport(name="d", folder=".", bz2_present=True, entries_json_present=True,
                        spec_pdf_present=True, docker_files_present=3, status="missing_system_deps",
                        failing_module="qgis"),
        ]
        s = summarize(reports)
        assert s["total"] == 4
        assert s["complete"] == 1
        assert s["incomplete"] == 3
        assert s["missing_bz2"] == 2
        assert s["missing_both_bz2_and_spec"] == 1
        assert s["status_breakdown"]["ok"] == 1
        assert s["status_breakdown"]["missing_system_deps"] == 1
        assert s["by_failing_module"]["qgis"] == 1
        # missing_system_deps is not recoverable; others are
        assert s["incomplete_unrecoverable"] == 1
        assert s["incomplete_recoverable"] == 2


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


def _sample_reports() -> list[EntryReport]:
    return [
        EntryReport(
            name="a", folder="/x/a", bz2_present=True, entries_json_present=True,
            spec_pdf_present=True, docker_files_present=3, status="ok",
            python_version="3.11", test_count=12,
        ),
        EntryReport(
            name="b", folder="/x/b", bz2_present=False, entries_json_present=True,
            spec_pdf_present=True, docker_files_present=3, status="missing_system_deps",
            failing_module="qgis", system_deps_hint=["qgis", "PyQt5"],
            python_version="3.12", stderr_snippet="ModuleNotFoundError: qgis",
        ),
    ]


class TestOutputFormats:
    def test_csv(self, tmp_path: Path) -> None:
        out = tmp_path / "r.csv"
        write_csv(_sample_reports(), out)
        text = out.read_text()
        assert "name,folder,status" in text
        assert "qgis" in text
        assert "qgis;PyQt5" in text

    def test_json(self, tmp_path: Path) -> None:
        out = tmp_path / "r.json"
        write_json(_sample_reports(), summarize(_sample_reports()), out)
        data = json.loads(out.read_text())
        assert data["_schema"] == "kaiju-incomplete-report/1"
        assert data["summary"]["total"] == 2
        assert len(data["entries"]) == 2

    def test_markdown(self, tmp_path: Path) -> None:
        out = tmp_path / "r.md"
        write_markdown(_sample_reports(), summarize(_sample_reports()), out)
        text = out.read_text()
        assert "# Kaiju test-ID completeness report" in text
        assert "| `missing_system_deps` |" in text
        assert "sys-deps: qgis, PyQt5" in text
