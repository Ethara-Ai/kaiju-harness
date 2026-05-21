"""Unit tests for ``tools.rust_version``."""

from __future__ import annotations

from pathlib import Path

from tools.rust_version import (
    RUST_CHANNELS,
    collect_signals,
    detect,
    detect_edition,
)


class TestCollect:
    def test_rust_toolchain_toml(self, tmp_path: Path) -> None:
        (tmp_path / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.70.0"\n'
        )
        sigs = collect_signals(tmp_path)
        assert "rust-toolchain.toml[toolchain.channel]" in sigs
        assert sigs["rust-toolchain.toml[toolchain.channel]"] == "1.70.0"

    def test_rust_toolchain_legacy_file(self, tmp_path: Path) -> None:
        (tmp_path / "rust-toolchain").write_text("stable\n")
        sigs = collect_signals(tmp_path)
        assert sigs.get("rust-toolchain") == "stable"

    def test_cargo_rust_version(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\nversion = "0.1.0"\nrust-version = "1.70"\n'
        )
        sigs = collect_signals(tmp_path)
        assert sigs.get("Cargo.toml[package.rust-version]") == "1.70"

    def test_cargo_edition(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\nversion = "0.1"\nedition = "2021"\n'
        )
        ed, src = detect_edition(tmp_path)
        assert ed == "2021"
        assert "Cargo.toml" in src

    def test_dockerfile_from(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM rust:1.75-slim\n")
        sigs = collect_signals(tmp_path)
        assert sigs.get("Dockerfile[FROM rust:X]") == "1.75-slim"


class TestDetect:
    def test_no_signals(self, tmp_path: Path) -> None:
        r = detect(tmp_path)
        assert r.version == "stable"
        assert r.edition == "2021"
        assert r.source == "default"

    def test_toolchain_toml_wins(self, tmp_path: Path) -> None:
        (tmp_path / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.70.0"\n'
        )
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\nversion = "0.1"\nrust-version = "1.60"\n'
        )
        r = detect(tmp_path)
        assert r.version == "1.70.0"
        assert "rust-toolchain.toml" in r.source

    def test_legacy_file_below_toml(self, tmp_path: Path) -> None:
        (tmp_path / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "stable"\n'
        )
        (tmp_path / "rust-toolchain").write_text("nightly\n")
        r = detect(tmp_path)
        assert r.version == "stable"  # toml wins

    def test_channel_normalized(self, tmp_path: Path) -> None:
        (tmp_path / "rust-toolchain").write_text("STABLE\n")
        r = detect(tmp_path)
        assert r.version == "stable"
        assert r.version in RUST_CHANNELS

    def test_dockerfile_tag_normalized(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM rust:1.75-slim\n")
        r = detect(tmp_path)
        # Normalized via _normalize_toolchain → strips -slim suffix
        assert r.version == "1.75"

    def test_conflicts_reported_for_numeric_disagreement(self, tmp_path: Path) -> None:
        (tmp_path / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.70.0"\n'
        )
        (tmp_path / "Dockerfile").write_text("FROM rust:1.75\n")
        r = detect(tmp_path)
        assert r.version == "1.70.0"
        assert any("Dockerfile" in c for c in r.conflicts)
