"""Unit tests for agent.agent_utils_go (audit finding T-001: Go module had no tests).

Characterization tests for source/test collection (with skip-dir/skip-file
pruning), stub extraction, and lint-command building.
"""

from __future__ import annotations

from pathlib import Path

from agent.agent_utils_go import (
    collect_go_files,
    collect_go_test_files,
    extract_go_function_stubs,
    get_go_lint_cmd,
)


class TestCollectGoFiles:
    def test_excludes_tests_doc_and_vendor(self, tmp_path: Path) -> None:
        (tmp_path / "foo.go").write_text("package foo")
        (tmp_path / "foo_test.go").write_text("package foo")
        (tmp_path / "doc.go").write_text("// doc")  # GO_SKIP_FILENAMES
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "dep.go").write_text("package dep")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "bar.go").write_text("package bar")

        result = collect_go_files(str(tmp_path))
        names = {Path(p).name for p in result}
        assert names == {"foo.go", "bar.go"}

    def test_returns_sorted(self, tmp_path: Path) -> None:
        for n in ("c.go", "a.go", "b.go"):
            (tmp_path / n).write_text("package x")
        result = collect_go_files(str(tmp_path))
        assert result == sorted(result)


class TestCollectGoTestFiles:
    def test_collects_only_test_files(self, tmp_path: Path) -> None:
        (tmp_path / "foo.go").write_text("package foo")
        (tmp_path / "foo_test.go").write_text("package foo")
        (tmp_path / "bar_test.go").write_text("package bar")
        result = collect_go_test_files(str(tmp_path))
        names = {Path(p).name for p in result}
        assert names == {"foo_test.go", "bar_test.go"}


class TestExtractGoFunctionStubs:
    def test_detects_stub_marker_function(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.go"
        f.write_text(
            'func Add(a int, b int) int {\n'
            '    panic("STUB: not implemented")\n'
            '}\n'
        )
        stubs = extract_go_function_stubs(str(f))
        assert len(stubs) == 1
        assert stubs[0]["name"] == "Add"

    def test_method_receiver_function(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.go"
        f.write_text(
            'func (s *Service) Run() error {\n'
            '    panic("STUB: not implemented")\n'
            '}\n'
        )
        stubs = extract_go_function_stubs(str(f))
        assert len(stubs) == 1
        assert stubs[0]["name"] == "Run"

    def test_no_stub_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.go"
        f.write_text("func Add(a int, b int) int {\n    return a + b\n}\n")
        assert extract_go_function_stubs(str(f)) == []


class TestGetGoLintCmd:
    def test_includes_repo_and_config(self) -> None:
        cmd = get_go_lint_cmd("myrepo", "/path/cfg.yaml")
        assert "lint" in cmd
        assert "myrepo" in cmd
        assert "/path/cfg.yaml" in cmd

    def test_defaults_to_local_inplace_backend(self) -> None:
        """Agent-container pipelines can't spawn a Docker container for lint;
        the default backend must be local_inplace so linters run in-place."""
        cmd = get_go_lint_cmd("myrepo", "/path/cfg.yaml")
        assert "--backend local_inplace" in cmd

    def test_explicit_backend_override(self) -> None:
        cmd = get_go_lint_cmd("myrepo", "/path/cfg.yaml", backend="local")
        assert "--backend local" in cmd
        assert "local_inplace" not in cmd
