"""Unit tests for ``tools.system_deps_scanner``."""

from __future__ import annotations

from pathlib import Path

from tools.system_deps_scanner import (
    SYSTEM_DEP_MODULES,
    scan_imports,
    scan_repo_for_system_deps,
)


class TestScanImports:
    def test_simple_import(self) -> None:
        assert scan_imports("import qgis") == {"qgis"}

    def test_from_import(self) -> None:
        assert scan_imports("from osgeo import gdal") == {"osgeo"}

    def test_nested_import_takes_toplevel(self) -> None:
        assert scan_imports("import qgis.core.QgsProject") == {"qgis"}

    def test_multiple_imports(self) -> None:
        src = "import cv2\nfrom PyQt5.QtWidgets import QApplication\nimport os"
        assert scan_imports(src) == {"cv2", "PyQt5", "os"}

    def test_relative_imports_excluded(self) -> None:
        # `from . import x` has level=1, no top-level module
        assert scan_imports("from . import sibling") == set()

    def test_syntax_error_falls_back_to_regex(self) -> None:
        bad_src = "import qgis\nthis is not valid python )))"
        assert "qgis" in scan_imports(bad_src)


class TestScanRepoForSystemDeps:
    def test_empty_repo(self, tmp_path: Path) -> None:
        assert scan_repo_for_system_deps(tmp_path) == []

    def test_qgis_in_tests_dir(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_foo.py").write_text("from qgis.core import QgsProject\n")
        assert scan_repo_for_system_deps(tmp_path) == ["qgis"]

    def test_multiple_system_deps(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "a.py").write_text("import cv2\n")
        (tests / "b.py").write_text("from PyQt5.QtWidgets import QApplication\n")
        assert set(scan_repo_for_system_deps(tmp_path)) == {"cv2", "PyQt5"}

    def test_no_system_deps_returns_empty(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "a.py").write_text("import requests\nfrom flask import Flask\n")
        assert scan_repo_for_system_deps(tmp_path) == []

    def test_toplevel_conftest_scanned(self, tmp_path: Path) -> None:
        (tmp_path / "conftest.py").write_text("import rospy\n")
        assert scan_repo_for_system_deps(tmp_path) == ["rospy"]

    def test_alt_test_dir_name(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "test"
        test_dir.mkdir()
        (test_dir / "test_x.py").write_text("import gdal\n")
        assert scan_repo_for_system_deps(tmp_path) == ["gdal"]

    def test_max_files_short_circuit(self, tmp_path: Path) -> None:
        tests = tmp_path / "tests"
        tests.mkdir()
        for i in range(20):
            (tests / f"t{i}.py").write_text("import os\n")
        # Even with cap of 2, the first 2 files have no system deps → empty
        assert scan_repo_for_system_deps(tmp_path, max_files=2) == []

    def test_module_list_no_self_check_drift(self) -> None:
        # Guard against the scanner's vocabulary drifting away from the
        # failure classifier's vocabulary.
        from tools.python_runtime import _SYSTEM_DEP_MODULES

        assert SYSTEM_DEP_MODULES >= _SYSTEM_DEP_MODULES, (
            "system_deps_scanner.SYSTEM_DEP_MODULES must be a superset of "
            "python_runtime._SYSTEM_DEP_MODULES"
        )
