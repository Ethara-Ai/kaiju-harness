"""Unit tests for ``tools.go_version``."""

from __future__ import annotations

from pathlib import Path

from tools.go_version import collect_signals, detect


class TestCollect:
    def test_go_directive(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
        sigs = collect_signals(tmp_path)
        assert sigs.get("go.mod[go]") == "1.21"

    def test_toolchain_directive(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text(
            "module example.com/foo\n\ngo 1.21\ntoolchain go1.22.5\n"
        )
        sigs = collect_signals(tmp_path)
        assert sigs.get("go.mod[toolchain]") == "1.22.5"
        assert sigs.get("go.mod[go]") == "1.21"

    def test_gha_matrix(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            'matrix:\n  go-version: ["1.21", "1.22"]\n'
        )
        sigs = collect_signals(tmp_path)
        assert sigs.get(".github/workflows/*.yml[matrix.go-version]") == "1.21, 1.22"

    def test_dockerfile(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM golang:1.22\n")
        sigs = collect_signals(tmp_path)
        assert sigs.get("Dockerfile[FROM golang:X]") == "1.22"


class TestDetect:
    def test_no_signals_returns_fallback(self, tmp_path: Path) -> None:
        r = detect(tmp_path, fallback="1.22")
        assert r.version == "1.22"
        assert r.source == "default"

    def test_toolchain_beats_directive(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text(
            "module x\n\ngo 1.21\ntoolchain go1.22.5\n"
        )
        r = detect(tmp_path)
        assert r.version == "1.22.5"
        assert "toolchain" in r.source

    def test_directive_used_when_no_toolchain(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x\n\ngo 1.21\n")
        r = detect(tmp_path)
        assert r.version == "1.21"

    def test_gha_picks_min(self, tmp_path: Path) -> None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            'matrix:\n  go-version: ["1.20", "1.22"]\n'
        )
        r = detect(tmp_path)
        assert r.version == "1.20"  # min from matrix

    def test_dockerfile_fallback(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM golang:1.22\n")
        r = detect(tmp_path)
        assert r.version == "1.22"
        assert "Dockerfile" in r.source
