from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

import docker.errors
import pytest

MODULE = "commit0.harness.docker_build"

MULTIARCH_BUILDER_NAME = "commit0-multiarch"


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> CompletedProcess:
    return CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _make_tar(dest: Path, members: dict[str, bytes] | None = None) -> None:
    with tarfile.open(dest, "w") as tf:
        for name, data in (members or {}).items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


class TestNativePlatform:
    @patch(f"{MODULE}._platform.machine", return_value="arm64")
    def test_arm64_returns_linux_arm64(self, _mock):
        from commit0.harness.docker_build import _native_platform

        assert _native_platform() == "linux/arm64"

    @patch(f"{MODULE}._platform.machine", return_value="aarch64")
    def test_aarch64_returns_linux_arm64(self, _mock):
        from commit0.harness.docker_build import _native_platform

        assert _native_platform() == "linux/arm64"

    @patch(f"{MODULE}._platform.machine", return_value="x86_64")
    def test_x86_64_returns_linux_amd64(self, _mock):
        from commit0.harness.docker_build import _native_platform

        assert _native_platform() == "linux/amd64"

    @patch(f"{MODULE}._platform.machine", return_value="i686")
    def test_unknown_arch_returns_amd64(self, _mock):
        from commit0.harness.docker_build import _native_platform

        assert _native_platform() == "linux/amd64"


class TestSafeBuilderArgs:
    @patch(f"{MODULE}.subprocess.run")
    def test_non_docker_container_returns_empty(self, mock_run):
        mock_run.return_value = _completed(stdout="Driver: docker\n", stderr="")
        from commit0.harness.docker_build import _safe_builder_args

        assert _safe_builder_args() == []

    @patch(f"{MODULE}.subprocess.run")
    def test_docker_container_finds_desktop_linux_fallback(self, mock_run):
        def side_effect(cmd, **kw):
            if cmd == ["docker", "buildx", "inspect"]:
                return _completed(stdout="Driver: docker-container")
            if cmd == ["docker", "buildx", "inspect", "desktop-linux"]:
                return _completed(returncode=0, stdout="Driver: docker")
            return _completed(returncode=1)

        mock_run.side_effect = side_effect
        from commit0.harness.docker_build import _safe_builder_args

        assert _safe_builder_args() == ["--builder", "desktop-linux"]

    @patch(f"{MODULE}.subprocess.run")
    def test_docker_container_finds_default_fallback(self, mock_run):
        def side_effect(cmd, **kw):
            if cmd == ["docker", "buildx", "inspect"]:
                return _completed(stdout="Driver: docker-container")
            if cmd == ["docker", "buildx", "inspect", "desktop-linux"]:
                return _completed(returncode=0, stdout="docker-container")
            if cmd == ["docker", "buildx", "inspect", "default"]:
                return _completed(returncode=0, stdout="Driver: docker")
            return _completed(returncode=1)

        mock_run.side_effect = side_effect
        from commit0.harness.docker_build import _safe_builder_args

        assert _safe_builder_args() == ["--builder", "default"]

    @patch(f"{MODULE}.subprocess.run")
    def test_docker_container_no_fallback_returns_empty(self, mock_run):
        def side_effect(cmd, **kw):
            if cmd == ["docker", "buildx", "inspect"]:
                return _completed(stdout="docker-container")
            if cmd == ["docker", "buildx", "inspect", "desktop-linux"]:
                return _completed(returncode=0, stdout="docker-container")
            if cmd == ["docker", "buildx", "inspect", "default"]:
                return _completed(returncode=0, stdout="docker-container")
            return _completed(returncode=1)

        mock_run.side_effect = side_effect
        from commit0.harness.docker_build import _safe_builder_args

        assert _safe_builder_args() == []

    @patch(f"{MODULE}.subprocess.run", side_effect=OSError("docker not found"))
    def test_exception_returns_empty(self, _mock):
        from commit0.harness.docker_build import _safe_builder_args

        assert _safe_builder_args() == []


class TestMultiarchBuilderArgs:
    @patch(f"{MODULE}.subprocess.run")
    def test_dedicated_builder_exists(self, mock_run):
        mock_run.return_value = _completed(
            returncode=0, stdout="Driver: docker-container"
        )
        from commit0.harness.docker_build import _multiarch_builder_args

        assert _multiarch_builder_args() == ["--builder", MULTIARCH_BUILDER_NAME]

    @patch(f"{MODULE}.subprocess.run")
    def test_default_builder_has_docker_container(self, mock_run):
        def side_effect(cmd, **kw):
            if MULTIARCH_BUILDER_NAME in cmd:
                return _completed(returncode=1, stderr="not found")
            return _completed(stdout="docker-container")

        mock_run.side_effect = side_effect
        from commit0.harness.docker_build import _multiarch_builder_args

        assert _multiarch_builder_args() == []

    @patch(f"{MODULE}.subprocess.run")
    def test_creates_new_builder(self, mock_run):
        call_count = 0

        def side_effect(cmd, **kw):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return _completed(returncode=1, stderr="not found")
            return _completed(returncode=0)

        mock_run.side_effect = side_effect
        from commit0.harness.docker_build import _multiarch_builder_args

        assert _multiarch_builder_args() == ["--builder", MULTIARCH_BUILDER_NAME]

    @patch(f"{MODULE}.subprocess.run", side_effect=OSError("no docker"))
    def test_all_fail_returns_empty(self, _mock):
        from commit0.harness.docker_build import _multiarch_builder_args

        assert _multiarch_builder_args() == []


class TestEnsureOciLayout:
    def test_existing_layout_returns_dir(self, tmp_path):
        oci_tar = tmp_path / "img.tar"
        oci_tar.touch()
        layout_dir = tmp_path / "oci-layout"
        layout_dir.mkdir()
        (layout_dir / "index.json").write_text("{}")
        from commit0.harness.docker_build import _ensure_oci_layout

        assert _ensure_oci_layout(oci_tar) == layout_dir

    def test_missing_tarball_returns_none(self, tmp_path):
        oci_tar = tmp_path / "nonexistent.tar"
        from commit0.harness.docker_build import _ensure_oci_layout

        assert _ensure_oci_layout(oci_tar) is None

    def test_zero_byte_tarball_returns_none(self, tmp_path):
        oci_tar = tmp_path / "empty.tar"
        oci_tar.touch()
        from commit0.harness.docker_build import _ensure_oci_layout

        assert _ensure_oci_layout(oci_tar) is None

    def test_valid_tarball_extracts(self, tmp_path):
        oci_tar = tmp_path / "valid.tar"
        _make_tar(oci_tar, {"index.json": b'{"schemaVersion": 2}'})
        from commit0.harness.docker_build import _ensure_oci_layout

        result = _ensure_oci_layout(oci_tar)
        assert result is not None
        assert (result / "index.json").exists()

    @patch(f"{MODULE}.tarfile.open", side_effect=tarfile.TarError("corrupt"))
    def test_extraction_failure_returns_none(self, _mock, tmp_path):
        oci_tar = tmp_path / "bad.tar"
        oci_tar.write_bytes(b"not a tar")
        from commit0.harness.docker_build import _ensure_oci_layout

        assert _ensure_oci_layout(oci_tar) is None


class TestCheckQemuSupport:
    @patch(f"{MODULE}.subprocess.run")
    def test_success_returns_true(self, mock_run):
        mock_run.return_value = _completed(returncode=0)
        from commit0.harness.docker_build import _check_qemu_support

        assert _check_qemu_support("linux/arm64") is True

    @patch(f"{MODULE}.subprocess.run")
    def test_failure_returns_false(self, mock_run):
        mock_run.return_value = _completed(returncode=1)
        from commit0.harness.docker_build import _check_qemu_support

        assert _check_qemu_support("linux/arm64") is False

    @patch(f"{MODULE}.subprocess.run", side_effect=OSError("no docker"))
    def test_exception_returns_false(self, _mock):
        from commit0.harness.docker_build import _check_qemu_support

        assert _check_qemu_support("linux/arm64") is False


class TestMitmDisabled:
    @pytest.mark.parametrize(
        "val,expected",
        [
            ("1", True),
            ("true", True),
            ("yes", True),
            ("  1 ", True),
            ("  true  ", True),
            ("  yes  ", True),
            ("0", False),
            ("false", False),
            ("no", False),
            ("", False),
        ],
    )
    def test_values(self, monkeypatch, val, expected):
        monkeypatch.setenv("COMMIT0_MITM_DISABLED", val)
        from commit0.harness.docker_build import _mitm_disabled

        assert _mitm_disabled() is expected

    def test_not_set_returns_false(self, monkeypatch):
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        from commit0.harness.docker_build import _mitm_disabled

        assert _mitm_disabled() is False


class TestGetProxyEnv:
    def test_returns_set_proxy_vars(self, monkeypatch):
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        monkeypatch.setenv("http_proxy", "http://proxy:8080")
        monkeypatch.setenv("HTTPS_PROXY", "https://proxy:8443")
        for key in ("https_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
            monkeypatch.delenv(key, raising=False)
        from commit0.harness.docker_build import get_proxy_env

        result = get_proxy_env()
        assert result == {
            "http_proxy": "http://proxy:8080",
            "HTTPS_PROXY": "https://proxy:8443",
        }

    def test_mitm_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("COMMIT0_MITM_DISABLED", "1")
        monkeypatch.setenv("http_proxy", "http://proxy:8080")
        from commit0.harness.docker_build import get_proxy_env

        assert get_proxy_env() == {}

    def test_no_proxy_vars_returns_empty(self, monkeypatch):
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        for key in (
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "no_proxy",
            "NO_PROXY",
        ):
            monkeypatch.delenv(key, raising=False)
        from commit0.harness.docker_build import get_proxy_env

        assert get_proxy_env() == {}


class TestIsPemCert:
    def test_valid_cert_returns_true(self, tmp_path):
        cert = tmp_path / "ca.pem"
        cert.write_text(
            "-----BEGIN CERTIFICATE-----\nMIIBx...\n-----END CERTIFICATE-----\n"
        )
        from commit0.harness.docker_build import _is_pem_cert

        assert _is_pem_cert(cert) is True

    def test_invalid_cert_returns_false(self, tmp_path):
        cert = tmp_path / "not-a-cert.txt"
        cert.write_text("hello world\n")
        from commit0.harness.docker_build import _is_pem_cert

        assert _is_pem_cert(cert) is False

    def test_nonexistent_file_returns_false(self, tmp_path):
        from commit0.harness.docker_build import _is_pem_cert

        assert _is_pem_cert(tmp_path / "missing.pem") is False


class TestResolveMitmCaCert:
    def test_env_var_with_valid_cert(self, monkeypatch, tmp_path):
        cert = tmp_path / "ca.pem"
        cert.write_text(
            "-----BEGIN CERTIFICATE-----\ndata\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        monkeypatch.setenv("MITM_CA_CERT", str(cert))
        from commit0.harness.docker_build import _resolve_mitm_ca_cert

        assert _resolve_mitm_ca_cert() == cert

    def test_env_var_with_invalid_cert(self, monkeypatch, tmp_path):
        cert = tmp_path / "bad.pem"
        cert.write_text("not a certificate\n")
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        monkeypatch.setenv("MITM_CA_CERT", str(cert))
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")
        from commit0.harness.docker_build import _resolve_mitm_ca_cert

        assert _resolve_mitm_ca_cert() is None

    def test_default_mitmproxy_path(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        monkeypatch.delenv("MITM_CA_CERT", raising=False)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        mitmdir = fake_home / ".mitmproxy"
        mitmdir.mkdir()
        cert = mitmdir / "mitmproxy-ca-cert.pem"
        cert.write_text(
            "-----BEGIN CERTIFICATE-----\ndata\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        from commit0.harness.docker_build import _resolve_mitm_ca_cert

        assert _resolve_mitm_ca_cert() == cert

    def test_mitm_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("COMMIT0_MITM_DISABLED", "1")
        from commit0.harness.docker_build import _resolve_mitm_ca_cert

        assert _resolve_mitm_ca_cert() is None

    def test_no_cert_found_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.delenv("COMMIT0_MITM_DISABLED", raising=False)
        monkeypatch.delenv("MITM_CA_CERT", raising=False)
        fake_home = tmp_path / "nohome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        from commit0.harness.docker_build import _resolve_mitm_ca_cert

        assert _resolve_mitm_ca_cert() is None


class TestBuildImageError:
    def test_str_includes_image_name(self):
        from commit0.harness.docker_build import BuildImageError

        err = BuildImageError(
            "myimage:latest", "something broke", logging.getLogger("test")
        )
        assert "myimage:latest" in str(err)

    def test_str_includes_message(self):
        from commit0.harness.docker_build import BuildImageError

        err = BuildImageError("img", "disk full", logging.getLogger("test"))
        assert "disk full" in str(err)

    def test_log_path_default_empty(self):
        from commit0.harness.docker_build import BuildImageError

        err = BuildImageError("img", "msg", logging.getLogger("test"))
        assert err.log_path == ""


class TestBuildImage:
    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}._safe_builder_args", return_value=[])
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}._native_platform", return_value="linux/amd64")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}.subprocess.run")
    def test_writes_setup_scripts_and_dockerfile(
        self,
        mock_run,
        _proxy,
        _native,
        _multi,
        _safe,
        mock_setup_logger,
        _close,
        tmp_path,
    ):
        mock_setup_logger.return_value = MagicMock()
        mock_run.return_value = _completed(returncode=0, stderr="")

        from commit0.harness.docker_build import build_image

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        oci_dir = tmp_path / "oci"
        oci_dir.mkdir(parents=True)

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            build_image(
                image_name="test:v1",
                setup_scripts={"setup.sh": "#!/bin/bash\necho hello"},
                dockerfile="FROM ubuntu:22.04\nCOPY setup.sh /setup.sh",
                platform="linux/amd64",
                client=MagicMock(),
                build_dir=build_dir,
            )

        assert (build_dir / "setup.sh").exists()
        assert (build_dir / "setup.sh").read_text() == "#!/bin/bash\necho hello"
        assert (build_dir / "Dockerfile").exists()
        assert "FROM ubuntu:22.04" in (build_dir / "Dockerfile").read_text()

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}._safe_builder_args", return_value=[])
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}._native_platform", return_value="linux/amd64")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}.subprocess.run")
    def test_oci_build_failure_single_arch_non_fatal(
        self,
        mock_run,
        _proxy,
        _native,
        _multi,
        _safe,
        mock_setup_logger,
        _close,
        tmp_path,
    ):
        mock_setup_logger.return_value = MagicMock()
        call_count = 0

        def side_effect(cmd, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _completed(returncode=1, stderr="OCI failed\nerror line")
            return _completed(returncode=0, stderr="")

        mock_run.side_effect = side_effect

        from commit0.harness.docker_build import build_image

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        oci_dir = tmp_path / "oci"
        oci_dir.mkdir(parents=True)

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            build_image(
                image_name="test:v1",
                setup_scripts={},
                dockerfile="FROM ubuntu:22.04",
                platform="linux/amd64",
                client=MagicMock(),
                build_dir=build_dir,
            )

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}._safe_builder_args", return_value=[])
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}._native_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._check_qemu_support", return_value=True)
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}.subprocess.run")
    def test_oci_build_failure_multi_arch_raises(
        self,
        mock_run,
        _proxy,
        _qemu,
        _native,
        _multi,
        _safe,
        mock_setup_logger,
        _close,
        tmp_path,
    ):
        mock_setup_logger.return_value = MagicMock()
        mock_run.return_value = _completed(
            returncode=1, stderr="multi-arch fail\nerror"
        )

        from commit0.harness.docker_build import build_image, BuildImageError

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        oci_dir = tmp_path / "oci"
        oci_dir.mkdir(parents=True)

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            with pytest.raises(BuildImageError):
                build_image(
                    image_name="test:v1",
                    setup_scripts={},
                    dockerfile="FROM ubuntu:22.04",
                    platform="linux/amd64,linux/arm64",
                    client=MagicMock(),
                    build_dir=build_dir,
                )

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}._safe_builder_args", return_value=[])
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}._native_platform", return_value="linux/amd64")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}.subprocess.run")
    def test_load_failure_raises_build_image_error(
        self,
        mock_run,
        _proxy,
        _native,
        _multi,
        _safe,
        mock_setup_logger,
        _close,
        tmp_path,
    ):
        mock_setup_logger.return_value = MagicMock()
        call_count = 0

        def side_effect(cmd, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _completed(returncode=0, stderr="")
            return _completed(returncode=1, stderr="load failed\ndaemon error")

        mock_run.side_effect = side_effect

        from commit0.harness.docker_build import build_image, BuildImageError

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        oci_dir = tmp_path / "oci"
        oci_dir.mkdir(parents=True)

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            with pytest.raises(BuildImageError):
                build_image(
                    image_name="test:v1",
                    setup_scripts={},
                    dockerfile="FROM ubuntu:22.04",
                    platform="linux/amd64",
                    client=MagicMock(),
                    build_dir=build_dir,
                )

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.setup_logger")
    @patch(f"{MODULE}._safe_builder_args", return_value=[])
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}._native_platform", return_value="linux/amd64")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}.subprocess.run")
    def test_mitm_cert_adds_secret_flags(
        self,
        mock_run,
        _proxy,
        _native,
        _multi,
        _safe,
        mock_setup_logger,
        _close,
        tmp_path,
    ):
        mock_setup_logger.return_value = MagicMock()
        mock_run.return_value = _completed(returncode=0, stderr="")

        from commit0.harness.docker_build import build_image

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        oci_dir = tmp_path / "oci"
        oci_dir.mkdir(parents=True)
        cert = tmp_path / "ca.pem"
        cert.write_text("-----BEGIN CERTIFICATE-----\n")

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            build_image(
                image_name="test:v1",
                setup_scripts={},
                dockerfile="FROM ubuntu:22.04",
                platform="linux/amd64",
                client=MagicMock(),
                build_dir=build_dir,
                mitm_ca_cert=cert,
            )

        found_secret = False
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            if "buildx" in cmd and "build" in cmd:
                assert "--secret" in cmd
                secret_idx = cmd.index("--secret")
                assert f"id=mitm_ca,src={cert}" in cmd[secret_idx + 1]
                found_secret = True
                break
        assert found_secret, "No buildx build command was called"


class TestBuildBaseImages:
    def _make_test_spec(self, base_key, dockerfile, platform):
        spec = MagicMock()
        spec.base_image_key = base_key
        spec.base_dockerfile = dockerfile
        spec.platform = platform
        return spec

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}.get_specs_from_dataset")
    def test_skips_existing_images(
        self, mock_specs, _multi, mock_build, tmp_path, caplog
    ):
        caplog.set_level(logging.INFO, logger="commit0.harness.docker_build")
        spec = self._make_test_spec(
            "commit0.base.python3.12:v1", "FROM python:3.12", "linux/amd64"
        )
        mock_specs.return_value = [spec]

        mock_client = MagicMock()
        mock_client.images.get.return_value = MagicMock()

        oci_dir = tmp_path / "oci"
        oci_key = "commit0.base.python3.12__v1"
        tar_dir = oci_dir / oci_key
        tar_dir.mkdir(parents=True)
        (tar_dir / f"{oci_key}.tar").write_bytes(b"fake tar")

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            from commit0.harness.docker_build import build_base_images

            build_base_images(mock_client, ["ds"], "commit0")

        mock_build.assert_not_called()
        assert any("already exists" in r.message for r in caplog.records)

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}.get_specs_from_dataset")
    def test_builds_missing_base_images(self, mock_specs, _multi, mock_build, tmp_path):
        spec = self._make_test_spec(
            "commit0.base.python3.12:v1", "FROM python:3.12", "linux/amd64"
        )
        mock_specs.return_value = [spec]

        mock_client = MagicMock()
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        oci_dir = tmp_path / "oci"
        oci_dir.mkdir(parents=True)

        with (
            patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir),
            patch(f"{MODULE}.BASE_IMAGE_BUILD_DIR", tmp_path / "base"),
        ):
            from commit0.harness.docker_build import build_base_images

            build_base_images(mock_client, ["ds"], "commit0")

        mock_build.assert_called_once()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}.get_specs_from_dataset")
    def test_warns_about_mitm_cert_on_existing(
        self, mock_specs, _multi, mock_build, tmp_path, caplog
    ):
        caplog.set_level(logging.WARNING, logger="commit0.harness.docker_build")
        spec = self._make_test_spec(
            "commit0.base.python3.12:v1", "FROM python:3.12", "linux/amd64"
        )
        mock_specs.return_value = [spec]

        mock_client = MagicMock()
        mock_client.images.get.return_value = MagicMock()

        oci_dir = tmp_path / "oci"
        oci_key = "commit0.base.python3.12__v1"
        tar_dir = oci_dir / oci_key
        tar_dir.mkdir(parents=True)
        (tar_dir / f"{oci_key}.tar").write_bytes(b"fake tar")

        cert = tmp_path / "ca.pem"
        cert.touch()

        with patch(f"{MODULE}.OCI_IMAGE_DIR", oci_dir):
            from commit0.harness.docker_build import build_base_images

            build_base_images(mock_client, ["ds"], "commit0", mitm_ca_cert=cert)

        mock_build.assert_not_called()
        assert any(r.levelname == "WARNING" for r in caplog.records)


class TestGetRepoConfigsToBuild:
    def _make_spec(self, base_key, repo_key, setup, dockerfile, platform):
        spec = MagicMock()
        spec.base_image_key = base_key
        spec.repo_image_key = repo_key
        spec.setup_script = setup
        spec.repo_dockerfile = dockerfile
        spec.platform = platform
        return spec

    @patch(f"{MODULE}.get_specs_from_dataset")
    def test_returns_only_missing_images(self, mock_specs):
        spec = self._make_spec(
            "base:v1", "repo:v1", "pip install", "FROM base:v1", "linux/amd64"
        )
        mock_specs.return_value = [spec]

        mock_client = MagicMock()

        def get_side_effect(name):
            if name == "base:v1":
                return MagicMock()
            raise docker.errors.ImageNotFound("not found")

        mock_client.images.get.side_effect = get_side_effect

        from commit0.harness.docker_build import get_repo_configs_to_build

        result = get_repo_configs_to_build(mock_client, ["ds"], "commit0")

        assert "repo:v1" in result
        assert result["repo:v1"]["setup_script"] == "pip install"
        assert result["repo:v1"]["dockerfile"] == "FROM base:v1"

    @patch(f"{MODULE}.get_specs_from_dataset")
    def test_raises_if_base_image_missing(self, mock_specs):
        spec = self._make_spec(
            "base:v1", "repo:v1", "pip install", "FROM base:v1", "linux/amd64"
        )
        mock_specs.return_value = [spec]

        mock_client = MagicMock()
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        from commit0.harness.docker_build import get_repo_configs_to_build

        with pytest.raises(Exception, match="Base image base:v1 not found"):
            get_repo_configs_to_build(mock_client, ["ds"], "commit0")


class TestBuildRepoImages:
    @patch(f"{MODULE}.get_repo_configs_to_build", return_value={})
    @patch(f"{MODULE}.build_base_images")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    def test_empty_configs_returns_early(self, _cert, _proxy, _base, _configs, caplog):
        caplog.set_level(logging.INFO, logger="commit0.harness.docker_build")
        from commit0.harness.docker_build import build_repo_images

        mock_client = MagicMock()
        successful, failed = build_repo_images(mock_client, ["ds"], "commit0")
        assert successful == []
        assert failed == []
        assert any(
            "No repo images need to be built" in r.message for r in caplog.records
        )

    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_repo_configs_to_build")
    @patch(f"{MODULE}.build_base_images")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    def test_successful_builds_returned(
        self, _cert, _proxy, _base, mock_configs, mock_build, _multi
    ):
        mock_configs.return_value = {
            "repo1:v1": {
                "setup_script": "s1",
                "dockerfile": "FROM base",
                "platform": "linux/amd64",
            },
        }
        mock_build.return_value = None

        from commit0.harness.docker_build import build_repo_images

        mock_client = MagicMock()
        successful, failed = build_repo_images(
            mock_client, ["ds"], "commit0", max_workers=1
        )
        assert "repo1:v1" in successful
        assert failed == []

    @patch(f"{MODULE}._multiarch_builder_args", return_value=[])
    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_repo_configs_to_build")
    @patch(f"{MODULE}.build_base_images")
    @patch(f"{MODULE}.get_proxy_env", return_value={})
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    def test_failed_builds_returned(
        self, _cert, _proxy, _base, mock_configs, mock_build, _multi
    ):
        from commit0.harness.docker_build import BuildImageError

        mock_configs.return_value = {
            "repo1:v1": {
                "setup_script": "s1",
                "dockerfile": "FROM base",
                "platform": "linux/amd64",
            },
        }
        mock_build.side_effect = BuildImageError(
            "repo1:v1", "build failed", logging.getLogger("test")
        )

        from commit0.harness.docker_build import build_repo_images

        mock_client = MagicMock()
        successful, failed = build_repo_images(
            mock_client, ["ds"], "commit0", max_workers=1
        )
        assert successful == []
        assert "repo1:v1" in failed


class TestUnsubstitutedPlaceholderGuard:
    """`build_image` must fail LOUD if a Dockerfile template placeholder was
    never substituted, instead of shipping `FROM gcc:__C_GCC_VERSION__-...` to
    Docker (which fails with a cryptic 'not found'). Universal for all languages.
    """

    def test_regex_matches_version_placeholders(self):
        from commit0.harness.docker_build import _UNSUBSTITUTED_PLACEHOLDER_RE as RE
        for tok in (
            "__C_GCC_VERSION__", "__GO_VERSION__", "__RUST_VERSION__",
            "__CPP_UBUNTU_VERSION__", "__CARGO_NEXTEST_VERSION__",
        ):
            assert RE.findall(f"FROM base:{tok}-x") == [tok]

    def test_regex_ignores_lowercase_and_shell(self):
        """Must NOT match real Dockerfile content: lowercase dunders, env refs."""
        from commit0.harness.docker_build import _UNSUBSTITUTED_PLACEHOLDER_RE as RE
        assert RE.findall("RUN echo __pycache__ && echo ${HTTP_PROXY}") == []
        assert RE.findall("COPY __init__.py /app/") == []

    def test_guard_raises_naming_every_leftover(self):
        from commit0.harness.docker_build import (
            _assert_dockerfile_fully_substituted,
            BuildImageError,
        )
        df = "FROM gcc:__C_GCC_VERSION__-bookworm\nARG X=__GO_VERSION__\n"
        with pytest.raises(BuildImageError) as ei:
            _assert_dockerfile_fully_substituted("img", df)
        msg = str(ei.value)
        assert "__C_GCC_VERSION__" in msg and "__GO_VERSION__" in msg

    def test_guard_passes_on_clean_dockerfile(self):
        from commit0.harness.docker_build import _assert_dockerfile_fully_substituted
        _assert_dockerfile_fully_substituted("img", "FROM gcc:13-bookworm\n")
        _assert_dockerfile_fully_substituted("img", "")  # tolerates empty

    def test_build_image_rejects_placeholder_before_any_docker_call(self):
        """The guard runs before _build_image_once, so no Docker work happens."""
        from commit0.harness.docker_build import build_image, BuildImageError
        with patch(f"{MODULE}._build_image_once") as once:
            with pytest.raises(BuildImageError):
                build_image(
                    "commit0.base.c:latest", {},
                    "FROM gcc:__C_GCC_VERSION__-bookworm\n",
                    "linux/amd64", MagicMock(), Path("/tmp"),
                )
            once.assert_not_called()


class TestTransientBuildErrorClassification:
    """Regression for wave5 SmartSim: a buildx/daemon socket drop mid-build must
    be classified transient so build_image retries, while genuine build defects
    (parse errors, missing distributions, compile failures) must NOT be retried."""

    def test_docker_socket_drop_is_transient(self) -> None:
        from commit0.harness.docker_build import _is_transient_build_error

        msg = (
            "Multi-arch OCI build failed (fatal for multi-arch): rc=1 ERROR: "
            "failed to build: listing workers for Build: failed to list workers: "
            'Unavailable: connection error: desc = "error reading server preface: '
            'read unix @->/run/docker.sock: use of closed network connection"'
        )
        assert _is_transient_build_error(msg) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "error: Failed to parse: `dishka ; python_version >= 3.10`",
            "ERROR: No matching distribution found for pypandoc_binary==1.12",
            "process \"/bin/sh -c pip install --no-cache-dir uv\" did not "
            "complete successfully: exit code: 1",
            "autoreconf: command not found",
            "fatal error: IpIpoptApplication.hpp: No such file or directory",
        ],
    )
    def test_real_build_defects_are_not_transient(self, msg: str) -> None:
        from commit0.harness.docker_build import _is_transient_build_error

        assert _is_transient_build_error(msg) is False
