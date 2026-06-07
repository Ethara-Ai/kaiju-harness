"""Unit tests for ``tools.java_version``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools._versioning import NoSignalsError, VersionConflictError
from tools.java_version import (
    collect_signals,
    detect,
    normalize_java_version,
)

SUPPORTED = {"11", "17", "21"}


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("17", "17"),
            ("1.8", "8"),
            ("1.8.0_322", "8"),
            ("17.0.2", "17"),
            ("VERSION_17", "17"),
            ("21.0.2-tem", "21"),
            ("garbage", None),
            ("", None),
        ],
    )
    def test_normalize(self, raw: str, expected: str | None) -> None:
        assert normalize_java_version(raw) == expected


class TestCollect:
    def test_pom_release(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><properties>"
            "<maven.compiler.release>17</maven.compiler.release>"
            "</properties></project>"
        )
        sigs = collect_signals(tmp_path)
        assert any("maven.compiler.release" in s.source for s in sigs)

    def test_gradle_source_compat(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text(
            "sourceCompatibility = JavaVersion.VERSION_17\n"
        )
        sigs = collect_signals(tmp_path)
        assert any("sourceCompatibility" in s.source for s in sigs)

    def test_gradle_toolchain(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text(
            "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }"
        )
        sigs = collect_signals(tmp_path)
        assert any("toolchain" in s.source for s in sigs)

    def test_tool_versions(self, tmp_path: Path) -> None:
        (tmp_path / ".tool-versions").write_text("java 21.0.2-tem\nnodejs 20\n")
        sigs = collect_signals(tmp_path)
        assert any("tool-versions" in s.source for s in sigs)

    def test_gha_matrix(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            'matrix:\n  java-version: ["11", "17", "21"]\n'
        )
        sigs = collect_signals(tmp_path)
        gha = next(s for s in sigs if "matrix" in s.source)
        assert set(gha.versions) == {"11", "17", "21"}


class TestDetect:
    def test_pom_pins_to_17(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><properties>"
            "<maven.compiler.release>17</maven.compiler.release>"
            "</properties></project>"
        )
        r = detect(tmp_path, SUPPORTED)
        assert r.version == "17"

    def test_unsupported_version_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text(
            "<project><properties>"
            "<maven.compiler.release>8</maven.compiler.release>"
            "</properties></project>"
        )
        with pytest.raises(VersionConflictError):
            detect(tmp_path, SUPPORTED)

    def test_gradle_overrides_default(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text(
            "sourceCompatibility = '21'\n"
        )
        r = detect(tmp_path, SUPPORTED)
        assert r.version == "21"

    def test_no_signals_strict_raises(self, tmp_path: Path) -> None:
        with pytest.raises(NoSignalsError):
            detect(tmp_path, SUPPORTED, strict=True)

    def test_no_signals_with_fallback(self, tmp_path: Path) -> None:
        r = detect(tmp_path, SUPPORTED, fallback="17")
        assert r.version == "17"
