"""Unit tests for ``tools.node_version``."""

from __future__ import annotations

from pathlib import Path

import pytest
from packaging.version import Version

from tools._versioning import (
    NoSignalsError,
    Signal,
    Tier,
    VersionConflictError,
    normalize_semver_range,
)
from tools.node_version import collect_signals, detect, detect_from_signals

SUPPORTED = {"18", "20", "22"}


class TestSemverNormalization:
    @pytest.mark.parametrize(
        "raw,contains,excludes",
        [
            ("^18", "18.5.0", "19.0.0"),
            (">=18", "18.0.0", None),
            ("~18.10", "18.10.5", "18.11.0"),
            ("18.x", "18.99.0", "19.0.0"),
            ("18", "18.5.0", "19.0.0"),
            (">=18 <21", "20.0.0", "21.0.0"),
        ],
    )
    def test_ranges(self, raw, contains, excludes):
        spec = normalize_semver_range(raw)
        assert spec is not None
        assert Version(contains) in spec
        if excludes is not None:
            assert Version(excludes) not in spec

    def test_wildcard_returns_none(self):
        assert normalize_semver_range("*") is None
        assert normalize_semver_range("") is None
        assert normalize_semver_range("any") is None
        assert normalize_semver_range("latest") is None


class TestCollectFromPackageJson:
    def test_engines_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"engines": {"node": ">=18 <21"}}'
        )
        sigs = collect_signals(tmp_path)
        eng = next(s for s in sigs if "engines" in s.source)
        assert eng.tier == Tier.A_DECLARED
        assert Version("20.0.0") in eng.constraint
        assert Version("21.0.0") not in eng.constraint

    def test_volta_node(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text('{"volta": {"node": "20.10.0"}}')
        sigs = collect_signals(tmp_path)
        v = next(s for s in sigs if "volta" in s.source)
        assert Version("20.10.0") in v.constraint

    def test_malformed_package_json_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("not json at all")
        sigs = collect_signals(tmp_path)
        assert sigs == []


class TestCollectFromNvmrc:
    def test_short_form(self, tmp_path: Path) -> None:
        (tmp_path / ".nvmrc").write_text("20\n")
        sigs = collect_signals(tmp_path)
        s = next(sig for sig in sigs if sig.source == ".nvmrc")
        assert Version("20.0.0") in s.constraint

    def test_with_v_prefix(self, tmp_path: Path) -> None:
        (tmp_path / ".nvmrc").write_text("v20.10.0\n")
        sigs = collect_signals(tmp_path)
        s = next(sig for sig in sigs if sig.source == ".nvmrc")
        assert Version("20.10.0") in s.constraint

    def test_lts_alias_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".nvmrc").write_text("lts/iron\n")
        sigs = collect_signals(tmp_path)
        assert not any(s.source == ".nvmrc" for s in sigs)


class TestCollectFromGhaMatrix:
    def test_inline_list(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            "jobs:\n  t:\n    strategy:\n      matrix:\n        node-version: [18, 20, 22]\n"
        )
        sigs = collect_signals(tmp_path)
        m = next(s for s in sigs if "matrix" in s.source)
        assert set(m.versions) == {"18", "20", "22"}


class TestDetectResolution:
    def test_no_signals(self, tmp_path: Path) -> None:
        assert detect(tmp_path, SUPPORTED).version is None

    def test_fallback(self, tmp_path: Path) -> None:
        assert detect(tmp_path, SUPPORTED, fallback="20").version == "20"

    def test_strict_raises(self, tmp_path: Path) -> None:
        with pytest.raises(NoSignalsError):
            detect(tmp_path, SUPPORTED, strict=True)

    def test_engines_picks_lowest_compatible(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"engines": {"node": ">=18 <21"}}'
        )
        result = detect(tmp_path, SUPPORTED)
        assert result.version == "18"

    def test_matrix_narrows(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(
            '{"engines": {"node": ">=18"}}'
        )
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            'matrix:\n  node-version: ["20", "22"]\n'
        )
        result = detect(tmp_path, SUPPORTED)
        assert result.version == "20"
