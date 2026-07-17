"""Regression tests for the shared Dockerfile template renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.dockerfiles._template import (
    DockerfileTemplateError,
    render_dockerfile,
)


class TestRenderDockerfile:
    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            render_dockerfile(tmp_path / "nope.Dockerfile", {})

    def test_no_placeholders_no_substitution(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM alpine:3.19\nRUN echo hi\n")
        assert render_dockerfile(df, {}) == "FROM alpine:3.19\nRUN echo hi\n"

    def test_single_substitution(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM gcc:__C_GCC_VERSION__-bookworm\n")
        rendered = render_dockerfile(df, {"__C_GCC_VERSION__": "13"})
        assert rendered == "FROM gcc:13-bookworm\n"

    def test_multiple_substitutions_all_applied(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM rust:__RUST_VERSION__\nRUN cargo-nextest --version __CARGO_NEXTEST_VERSION__\n")
        rendered = render_dockerfile(
            df,
            {"__RUST_VERSION__": "1.96", "__CARGO_NEXTEST_VERSION__": "0.9.96"},
        )
        assert "1.96" in rendered
        assert "0.9.96" in rendered
        assert "__" not in rendered.replace("${", "")

    def test_missing_substitution_raises_named(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM gcc:__C_GCC_VERSION__\nARG X=__GO_VERSION__\n")
        with pytest.raises(DockerfileTemplateError) as exc_info:
            render_dockerfile(df, {"__C_GCC_VERSION__": "13"})
        msg = str(exc_info.value)
        assert "__GO_VERSION__" in msg
        assert "__C_GCC_VERSION__" not in msg

    def test_lists_all_missing_alphabetically(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("__Z__ __A__ __M__")
        with pytest.raises(DockerfileTemplateError) as exc_info:
            render_dockerfile(df, {})
        msg = str(exc_info.value)
        assert msg.index("__A__") < msg.index("__M__") < msg.index("__Z__")

    def test_ignores_lowercase_and_shell_vars(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text(
            "COPY __init__.py /app/\n"
            "RUN echo __pycache__ && echo ${HTTP_PROXY}\n"
            "ARG DEBIAN_FRONTEND=noninteractive\n"
        )
        rendered = render_dockerfile(df, {})
        assert "__init__.py" in rendered
        assert "__pycache__" in rendered
        assert "${HTTP_PROXY}" in rendered

    def test_utf8_content_preserved(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("# comment with unicode: café\nFROM base:__X__\n", encoding="utf-8")
        rendered = render_dockerfile(df, {"__X__": "1"})
        assert "café" in rendered

    def test_substitution_with_empty_string_value(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM base__SUFFIX__\n")
        rendered = render_dockerfile(df, {"__SUFFIX__": ""})
        assert rendered == "FROM base\n"

    def test_placeholder_appearing_multiple_times(self, tmp_path: Path) -> None:
        df = tmp_path / "Dockerfile"
        df.write_text("FROM __V__\nRUN echo __V__\nARG X=__V__\n")
        rendered = render_dockerfile(df, {"__V__": "42"})
        assert rendered.count("42") == 3
        assert "__V__" not in rendered


class TestAllLanguageSpecsRenderCleanly:
    """End-to-end: every language spec's base_dockerfile must render without
    raising and produce a Dockerfile with no leftover placeholders."""

    def test_c_base_dockerfile_no_placeholder_leak(self) -> None:
        from commit0.harness.constants_c import C_GCC_VERSION, CRepoInstance
        from commit0.harness.spec_c import Commit0CSpec

        inst = CRepoInstance(
            repo="a/b",
            instance_id="b",
            reference_commit="x",
            base_commit="y",
            setup={},
            test={"test_cmd": ""},
            num_lines_added=0,
            num_lines_removed=0,
            num_hunks=0,
            num_files_edited=0,
        )
        spec = Commit0CSpec(absolute=True, repo="a/b", repo_directory="/testbed/", instance=inst)
        df = spec.base_dockerfile
        assert f"FROM gcc:{C_GCC_VERSION}-bookworm" in df
        import re
        assert not re.findall(r"__[A-Z][A-Z0-9_]*__", df)

    def test_go_base_dockerfile_no_placeholder_leak(self) -> None:
        from commit0.harness.constants_go import GO_VERSION, GoRepoInstance
        from commit0.harness.spec_go import Commit0GoSpec

        inst = GoRepoInstance(
            repo="a/b",
            instance_id="b",
            reference_commit="x",
            base_commit="y",
            setup={},
            test={"test_cmd": ""},
            num_lines_added=0,
            num_lines_removed=0,
            num_hunks=0,
            num_files_edited=0,
        )
        spec = Commit0GoSpec(absolute=True, repo="a/b", repo_directory="/testbed/", instance=inst)
        df = spec.base_dockerfile
        assert f"FROM golang:{GO_VERSION}-bookworm" in df
        import re
        assert not re.findall(r"__[A-Z][A-Z0-9_]*__", df)

    def test_cpp_base_dockerfile_no_placeholder_leak(self) -> None:
        import importlib
        from commit0.harness.constants_cpp import CPP_UBUNTU_VERSION
        cpp = importlib.import_module("commit0.harness.dockerfiles.__init__cpp")
        df = cpp.get_dockerfile_base_cpp()
        assert f"FROM ubuntu:{CPP_UBUNTU_VERSION}" in df
        import re
        assert not re.findall(r"__[A-Z][A-Z0-9_]*__", df)

    def test_rust_base_dockerfile_no_placeholder_leak(self) -> None:
        import importlib
        from commit0.harness.constants_rust import RUST_VERSION
        rust = importlib.import_module("commit0.harness.dockerfiles.__init__rust")
        df = rust.get_dockerfile_base_rust()
        assert f"FROM rust:{RUST_VERSION}-bookworm" in df
        import re
        assert not re.findall(r"__[A-Z][A-Z0-9_]*__", df)
