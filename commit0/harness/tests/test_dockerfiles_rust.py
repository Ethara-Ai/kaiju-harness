"""Exhaustive unit tests for commit0.harness.dockerfiles.__init__rust."""

import pytest
from unittest.mock import patch

MODULE = "commit0.harness.dockerfiles.__init__rust"


# ===== get_dockerfile_base_rust =====
from commit0.harness.dockerfiles.__init__rust import get_dockerfile_base_rust


class TestGetDockerfileBaseRust:
    def test_reads_template_file(self, tmp_path):
        template = tmp_path / "Dockerfile.rust"
        template.write_text("FROM rust:stable\nRUN cargo --version\n")
        with patch(f"{MODULE}.DOCKERFILES_RUST_DIR", tmp_path):
            result = get_dockerfile_base_rust()
        assert "FROM rust:stable" in result

    def test_missing_template_raises(self, tmp_path):
        with patch(f"{MODULE}.DOCKERFILES_RUST_DIR", tmp_path):
            with pytest.raises(
                FileNotFoundError, match="Rust base Dockerfile template not found"
            ):
                get_dockerfile_base_rust()

    def test_returns_string(self, tmp_path):
        template = tmp_path / "Dockerfile.rust"
        template.write_text("FROM rust\n")
        with patch(f"{MODULE}.DOCKERFILES_RUST_DIR", tmp_path):
            result = get_dockerfile_base_rust()
        assert isinstance(result, str)

    def test_empty_template_file(self, tmp_path):
        template = tmp_path / "Dockerfile.rust"
        template.write_text("")
        with patch(f"{MODULE}.DOCKERFILES_RUST_DIR", tmp_path):
            result = get_dockerfile_base_rust()
        assert result == ""

    def test_preserves_content_exactly(self, tmp_path):
        content = "FROM rust:1.75\nRUN apt-get update\nWORKDIR /app\n"
        template = tmp_path / "Dockerfile.rust"
        template.write_text(content)
        with patch(f"{MODULE}.DOCKERFILES_RUST_DIR", tmp_path):
            result = get_dockerfile_base_rust()
        assert result == content

    def test_multiline_template(self, tmp_path):
        lines = [
            "FROM rust:stable",
            "RUN cargo install nextest",
            "WORKDIR /testbed",
            "",
        ]
        template = tmp_path / "Dockerfile.rust"
        template.write_text("\n".join(lines))
        with patch(f"{MODULE}.DOCKERFILES_RUST_DIR", tmp_path):
            result = get_dockerfile_base_rust()
        assert result.count("\n") == 3


# ===== get_dockerfile_repo_rust =====
from commit0.harness.dockerfiles.__init__rust import get_dockerfile_repo_rust


class TestGetDockerfileRepoRust:
    def test_minimal_no_extras(self):
        result = get_dockerfile_repo_rust("commit0/rust-base:latest")
        assert result.startswith("FROM commit0/rust-base:latest")

    def test_contains_from_line(self):
        result = get_dockerfile_repo_rust("myimage:v1")
        assert "FROM myimage:v1" in result

    def test_contains_proxy_args(self):
        result = get_dockerfile_repo_rust("img")
        assert "ARG http_proxy" in result
        assert "ARG https_proxy" in result
        assert "ARG HTTP_PROXY" in result
        assert "ARG HTTPS_PROXY" in result
        assert "ARG no_proxy" in result
        assert "ARG NO_PROXY" in result

    def test_contains_setup_copy(self):
        result = get_dockerfile_repo_rust("img")
        assert "COPY ./setup.sh /root/" in result

    def test_contains_setup_run(self):
        result = get_dockerfile_repo_rust("img")
        assert "chmod +x /root/setup.sh" in result
        assert "/bin/bash /root/setup.sh" in result

    def test_contains_workdir(self):
        result = get_dockerfile_repo_rust("img")
        assert "WORKDIR /testbed/" in result

    def test_contains_dep_manifest(self):
        result = get_dockerfile_repo_rust("img")
        assert ".dep-manifest.txt" in result
        assert "cargo --version" in result
        assert "rustc --version" in result

    def test_with_pre_install(self):
        result = get_dockerfile_repo_rust(
            "img", pre_install=["apt-get update", "apt-get install -y cmake"]
        )
        assert "RUN apt-get update" in result
        assert "RUN apt-get install -y cmake" in result

    def test_with_install_cmd(self):
        result = get_dockerfile_repo_rust("img", install_cmd="cargo build --release")
        assert "RUN cargo build --release" in result

    def test_with_both_pre_install_and_install_cmd(self):
        result = get_dockerfile_repo_rust(
            "img",
            pre_install=["apt-get update"],
            install_cmd="cargo build",
        )
        assert "RUN apt-get update" in result
        assert "RUN cargo build" in result
        # pre_install should come before install_cmd
        pre_idx = result.index("apt-get update")
        install_idx = result.index("cargo build")
        assert pre_idx < install_idx

    def test_no_pre_install_no_extra_run(self):
        result = get_dockerfile_repo_rust("img")
        lines = result.split("\n")
        run_lines = [l for l in lines if l.startswith("RUN ")]
        # Should have: setup.sh, dep-manifest
        assert len(run_lines) == 2

    def test_empty_pre_install_list(self):
        result = get_dockerfile_repo_rust("img", pre_install=[])
        lines = result.split("\n")
        run_lines = [l for l in lines if l.startswith("RUN ")]
        assert len(run_lines) == 2

    def test_none_pre_install(self):
        result = get_dockerfile_repo_rust("img", pre_install=None)
        lines = result.split("\n")
        run_lines = [l for l in lines if l.startswith("RUN ")]
        assert len(run_lines) == 2

    def test_none_install_cmd(self):
        result = get_dockerfile_repo_rust("img", install_cmd=None)
        assert result.count("RUN ") == 2

    def test_returns_string(self):
        assert isinstance(get_dockerfile_repo_rust("img"), str)

    def test_multiple_pre_install_commands(self):
        cmds = [f"cmd{i}" for i in range(5)]
        result = get_dockerfile_repo_rust("img", pre_install=cmds)
        for cmd in cmds:
            assert f"RUN {cmd}" in result

    def test_base_image_with_tag(self):
        result = get_dockerfile_repo_rust("registry.example.com/img:v2.1.0")
        assert "FROM registry.example.com/img:v2.1.0" in result

    def test_base_image_with_digest(self):
        result = get_dockerfile_repo_rust("img@sha256:abc123")
        assert "FROM img@sha256:abc123" in result

    def test_install_cmd_with_special_chars(self):
        result = get_dockerfile_repo_rust(
            "img", install_cmd="cargo build && echo 'done'"
        )
        assert "cargo build && echo 'done'" in result

    def test_pre_install_order_preserved(self):
        cmds = ["first", "second", "third"]
        result = get_dockerfile_repo_rust("img", pre_install=cmds)
        first_idx = result.index("first")
        second_idx = result.index("second")
        third_idx = result.index("third")
        assert first_idx < second_idx < third_idx

    def test_no_proxy_defaults(self):
        result = get_dockerfile_repo_rust("img")
        assert 'no_proxy="localhost,127.0.0.1,::1"' in result
        assert 'NO_PROXY="localhost,127.0.0.1,::1"' in result

    @pytest.mark.parametrize(
        "base_image",
        [
            "rust:latest",
            "ubuntu:22.04",
            "commit0/rust-base:v1",
            "ghcr.io/org/image:tag",
            "localhost:5000/myimg",
        ],
    )
    def test_various_base_images(self, base_image):
        result = get_dockerfile_repo_rust(base_image)
        assert result.startswith(f"FROM {base_image}")

    def test_ends_with_newline_join(self):
        result = get_dockerfile_repo_rust("img")
        # Should be newline-joined (no trailing extra stuff)
        assert isinstance(result, str)


# ===== __all__ exports =====
class TestModuleExports:
    def test_exports_get_dockerfile_base_rust(self):
        import importlib

        mod = importlib.import_module("commit0.harness.dockerfiles.__init__rust")
        assert "get_dockerfile_base_rust" in mod.__all__

    def test_exports_get_dockerfile_repo_rust(self):
        import importlib

        mod = importlib.import_module("commit0.harness.dockerfiles.__init__rust")
        assert "get_dockerfile_repo_rust" in mod.__all__

    def test_all_exports_count(self):
        import importlib

        mod = importlib.import_module("commit0.harness.dockerfiles.__init__rust")
        assert len(mod.__all__) == 2
