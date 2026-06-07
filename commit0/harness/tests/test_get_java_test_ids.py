from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


from commit0.harness.get_java_test_ids import (
    _extract_test_ids_from_source,
    _find_test_source_dirs,
    _scan_compiled_test_classes,
    get_java_test_ids,
    get_test_ids_from_sources,
)

MODULE = "commit0.harness.get_java_test_ids"


def _write_java_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestFindTestSourceDirs:
    def test_standard_layout(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        (test_dir / "FooTest.java").write_text("class FooTest {}")
        result = _find_test_source_dirs(str(tmp_path))
        assert len(result) >= 1
        assert test_dir in result

    def test_monorepo_layout(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "module-a" / "test"
        test_dir.mkdir(parents=True)
        (test_dir / "BarTest.java").write_text("class BarTest {}")
        result = _find_test_source_dirs(str(tmp_path))
        assert len(result) >= 1

    def test_fallback_convention(self, tmp_path: Path) -> None:
        fallback = tmp_path / "src" / "test" / "java"
        fallback.mkdir(parents=True)
        result = _find_test_source_dirs(str(tmp_path))
        assert len(result) >= 1

    def test_no_test_dirs(self, tmp_path: Path) -> None:
        result = _find_test_source_dirs(str(tmp_path))
        assert result == []


class TestExtractTestIdsFromSource:
    def test_junit5_annotated(self, tmp_path: Path) -> None:
        java_file = tmp_path / "FooTest.java"
        java_file.write_text(
            "package com.example;\n"
            "public class FooTest {\n"
            "    @Test\n"
            "    public void testAdd() {}\n"
            "}\n"
        )
        ids = _extract_test_ids_from_source(java_file)
        assert "com.example.FooTest#testAdd" in ids

    def test_junit4_annotated(self, tmp_path: Path) -> None:
        java_file = tmp_path / "BarTest.java"
        java_file.write_text(
            "package org.test;\n"
            "public class BarTest {\n"
            "    @Test\n"
            "    public void testSub() {}\n"
            "}\n"
        )
        ids = _extract_test_ids_from_source(java_file)
        assert "org.test.BarTest#testSub" in ids

    def test_junit3_extends(self, tmp_path: Path) -> None:
        java_file = tmp_path / "OldTest.java"
        java_file.write_text(
            "package legacy;\n"
            "public class OldTest extends TestCase {\n"
            "    public void testOld() {}\n"
            "}\n"
        )
        ids = _extract_test_ids_from_source(java_file)
        assert "legacy.OldTest#testOld" in ids

    def test_package_prefix(self, tmp_path: Path) -> None:
        java_file = tmp_path / "PkgTest.java"
        java_file.write_text(
            "package a.b.c;\n"
            "public class PkgTest {\n"
            "    @Test\n"
            "    void check() {}\n"
            "}\n"
        )
        ids = _extract_test_ids_from_source(java_file)
        assert len(ids) > 0
        assert ids[0].startswith("a.b.c.")


class TestGetTestIdsFromSources:
    def test_combines_multiple_files(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        (test_dir / "ATest.java").write_text(
            "package x;\npublic class ATest {\n    @Test\n    public void testA() {}\n}\n"
        )
        (test_dir / "BTest.java").write_text(
            "package x;\npublic class BTest {\n    @Test\n    public void testB() {}\n}\n"
        )
        ids = get_test_ids_from_sources(str(tmp_path))
        assert "x.ATest#testA" in ids
        assert "x.BTest#testB" in ids


class TestScanCompiledTestClasses:
    def test_finds_test_classes(self, tmp_path: Path) -> None:
        class_dir = tmp_path / "com" / "example"
        class_dir.mkdir(parents=True)
        (class_dir / "FooTest.class").touch()
        (class_dir / "BarTests.class").touch()
        result = _scan_compiled_test_classes(tmp_path)
        assert "com.example.FooTest" in result
        assert "com.example.BarTests" in result

    def test_skips_inner_classes(self, tmp_path: Path) -> None:
        class_dir = tmp_path / "com"
        class_dir.mkdir(parents=True)
        (class_dir / "FooTest.class").touch()
        (class_dir / "FooTest$Inner.class").touch()
        result = _scan_compiled_test_classes(tmp_path)
        assert "com.FooTest" in result
        assert all("$" not in r for r in result)


class TestGetJavaTestIds:
    @patch(f"{MODULE}.get_test_ids_maven", return_value=["com.X#m1"])
    def test_auto_strategy_source_first(self, mock_maven: MagicMock) -> None:
        instance = {"repo_path": "/fake", "build_system": "maven"}
        result = get_java_test_ids(instance, strategy="auto")
        assert result == ["com.X#m1"]
        mock_maven.assert_called_once_with("/fake")

    @patch(f"{MODULE}.get_test_ids_from_sources", return_value=["com.Y#m2"])
    def test_source_strategy(self, mock_src: MagicMock) -> None:
        instance = {"repo_path": "/fake", "build_system": "maven"}
        result = get_java_test_ids(instance, strategy="source")
        assert result == ["com.Y#m2"]
        mock_src.assert_called_once_with("/fake")

    @patch(f"{MODULE}.get_test_ids_maven", return_value=["com.Z#m3"])
    def test_maven_strategy(self, mock_maven: MagicMock) -> None:
        instance = {"repo_path": "/fake", "build_system": "maven"}
        result = get_java_test_ids(instance, strategy="maven")
        assert result == ["com.Z#m3"]

    @patch(f"{MODULE}.get_test_ids_gradle", return_value=["com.G#m4"])
    def test_gradle_strategy(self, mock_gradle: MagicMock) -> None:
        instance = {"repo_path": "/fake", "build_system": "gradle"}
        result = get_java_test_ids(instance, strategy="gradle")
        assert result == ["com.G#m4"]

    @patch(f"{MODULE}.get_test_ids_maven", return_value=["com.D#m5"])
    def test_unknown_falls_to_source(self, mock_maven: MagicMock) -> None:
        instance = {"repo_path": "/fake", "build_system": "maven"}
        result = get_java_test_ids(instance, strategy="unknown_strat")
        assert result == ["com.D#m5"]
        mock_maven.assert_called_once()


class TestEdgeCases:
    @patch(f"{MODULE}.get_test_ids_maven", return_value=["a#b"])
    def test_unknown_strategy_defaults(self, mock_maven: MagicMock) -> None:
        instance = {"repo_path": ".", "build_system": "maven"}
        result = get_java_test_ids(instance, strategy="nonexistent")
        assert len(result) > 0

    def test_missing_key_handled(self) -> None:
        instance: dict = {}
        with patch(f"{MODULE}.get_test_ids_maven", return_value=[]):
            result = get_java_test_ids(instance)
            assert isinstance(result, list) is True

    def test_unreadable_file_returns_empty(self, tmp_path: Path) -> None:
        java_file = tmp_path / "Broken.java"
        java_file.write_text("")
        java_file.chmod(0o000)
        try:
            result = _extract_test_ids_from_source(java_file)
            assert result == []
        finally:
            java_file.chmod(0o644)

    def test_empty_java_file_returns_empty(self, tmp_path: Path) -> None:
        java_file = tmp_path / "Empty.java"
        java_file.write_text("")
        result = _extract_test_ids_from_source(java_file)
        assert result == []
