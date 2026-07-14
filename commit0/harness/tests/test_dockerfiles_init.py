from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from commit0.harness.dockerfiles import get_dockerfile_base, get_dockerfile_repo


class TestGetDockerfileBase:
    @pytest.mark.parametrize("version", ["3.9", "3.10", "3.11", "3.12", "3.13"])
    def test_valid_version_returns_content(self, version: str) -> None:
        result = get_dockerfile_base(version)
        assert isinstance(result, str)
        assert len(result) > 0
        assert result.strip().startswith("FROM")

    def test_invalid_version_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            get_dockerfile_base("3.8")

    def test_version_with_spaces_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            get_dockerfile_base(" 3.12 ")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            get_dockerfile_base("")

    def test_missing_template_raises_filenotfounderror(self) -> None:
        with patch.object(Path, "exists", return_value=False):
            with pytest.raises(
                FileNotFoundError, match="Base Dockerfile template not found"
            ):
                get_dockerfile_base("3.12")


class TestGetDockerfileRepo:
    def test_minimal_base_image_only(self) -> None:
        result = get_dockerfile_repo("commit0.base:latest")
        assert "FROM commit0.base:latest" in result

    def test_has_proxy_args(self) -> None:
        result = get_dockerfile_repo("img:tag")
        assert 'ARG http_proxy=""' in result
        assert 'ARG https_proxy=""' in result
        assert 'ARG HTTP_PROXY=""' in result
        assert 'ARG HTTPS_PROXY=""' in result
        assert "ARG no_proxy=" in result
        assert "ARG NO_PROXY=" in result

    def test_has_setup_sh_copy(self) -> None:
        result = get_dockerfile_repo("img:tag")
        assert "COPY ./setup.sh /root/" in result
        assert "chmod +x /root/setup.sh" in result

    def test_has_pytest_layer(self) -> None:
        result = get_dockerfile_repo("img:tag")
        assert "pytest pytest-cov coverage pytest-json-report" in result

    def test_workdir_testbed(self) -> None:
        result = get_dockerfile_repo("img:tag")
        assert "WORKDIR /testbed/" in result

    def test_with_pre_install_apt(self) -> None:
        result = get_dockerfile_repo(
            "img:tag",
            pre_install=["apt-get install -y libxml2 libxslt1-dev"],
        )
        assert "apt-get update" in result
        assert "libxml2" in result
        assert "libxslt1-dev" in result
        assert "--no-install-recommends" in result

    def test_with_pre_install_non_apt(self) -> None:
        result = get_dockerfile_repo(
            "img:tag",
            pre_install=["curl -O http://example.com/file.tar.gz"],
        )
        assert "RUN curl -O http://example.com/file.tar.gz" in result

    def test_with_pre_install_mixed(self) -> None:
        result = get_dockerfile_repo(
            "img:tag",
            pre_install=[
                "apt-get install -y git",
                "curl -sL https://example.com | bash",
            ],
        )
        assert "apt-get update" in result
        assert "git" in result
        assert "RUN curl -sL https://example.com | bash" in result

    def test_with_packages(self) -> None:
        result = get_dockerfile_repo("img:tag", packages="requirements.txt")
        assert "pip install --no-cache-dir -r requirements.txt" in result

    def test_with_pip_packages(self) -> None:
        result = get_dockerfile_repo("img:tag", pip_packages=["numpy", "pandas"])
        assert '"numpy"' in result
        assert '"pandas"' in result
        assert "pip install --no-cache-dir" in result

    def test_with_install_cmd_uses_uv_primary_pip_fallback(self) -> None:
        # Python installs now prefer uv (fast) with a pip fallback. A `uv pip
        # install` install_cmd is normalized and run as `uv … || pip …`.
        result = get_dockerfile_repo("img:tag", install_cmd="uv pip install -e .")
        assert "uv pip install --system -e ." in result
        assert "pip install --no-cache-dir -e ." in result

    def test_with_install_cmd_pip_gets_no_cache(self) -> None:
        result = get_dockerfile_repo("img:tag", install_cmd="pip install -e .")
        assert "pip install --no-cache-dir -e ." in result

    def test_with_all_params(self) -> None:
        result = get_dockerfile_repo(
            base_image="myregistry/base:v1",
            pre_install=["apt-get install -y gcc", "wget http://x.com/f"],
            packages="requirements.txt",
            pip_packages=["requests", "flask"],
            install_cmd="uv pip install -e .[dev]",
        )
        assert "FROM myregistry/base:v1" in result
        assert 'ARG http_proxy=""' in result
        assert "COPY ./setup.sh /root/" in result
        assert "apt-get update" in result
        assert "gcc" in result
        assert "RUN wget http://x.com/f" in result
        assert "pip install --no-cache-dir -r requirements.txt" in result
        assert '"requests"' in result
        assert '"flask"' in result
        assert "pip install" in result
        assert "-e .[dev]" in result
        # uv is now the primary python installer (with a pip fallback).
        assert "uv pip install --system" in result
        assert "pytest pytest-cov coverage pytest-json-report" in result
        assert "WORKDIR /testbed/" in result

    def test_apt_deduplicates_packages(self) -> None:
        result = get_dockerfile_repo(
            "img:tag",
            pre_install=[
                "apt-get install -y libxml2 gcc",
                "apt-get install -y gcc libssl-dev",
            ],
        )
        assert result.count("apt-get update") == 1
        assert "gcc" in result
        assert "libxml2" in result
        assert "libssl-dev" in result
        gcc_count = sum(1 for line in result.splitlines() if "gcc" in line)
        assert gcc_count == 1

    def test_no_optional_params_produces_clean_output(self) -> None:
        result = get_dockerfile_repo("img:tag")
        lines = result.splitlines()
        pip_lines = [l for l in lines if "pip install" in l]
        assert len(pip_lines) == 1
        assert "pytest" in pip_lines[0]
