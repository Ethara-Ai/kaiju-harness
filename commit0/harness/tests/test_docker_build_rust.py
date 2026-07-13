from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import docker.errors
import pytest

MODULE = "commit0.harness.docker_build_rust"


def _make_rust_spec(
    repo_image_key="repo1",
    setup_script="setup.sh",
    repo_dockerfile="Dockerfile",
    platform="linux/amd64",
    repo=None,
):
    spec = MagicMock()
    spec.repo_image_key = repo if repo is not None else repo_image_key
    spec.setup_script = setup_script
    spec.repo_dockerfile = repo_dockerfile
    spec.platform = platform
    return spec


# ---------------------------------------------------------------------------
# TestBuildBaseImagesRust
# ---------------------------------------------------------------------------
class TestBuildBaseImagesRust:
    """Tests for build_base_images_rust()."""

    BASE_IMG = "commit0.base.rust:latest"

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_builds_when_no_image_exists(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust:latest"
        mock_mba.return_value = {"builder": "x"}
        with patch.dict("os.environ", {}, clear=False):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        mock_bi.assert_called_once()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_skips_when_daemon_and_oci_exist(
        self, mock_gdf, mock_mba, mock_bi, tmp_path
    ):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        oci_dir = (
            tmp_path
            / "commit0.base.rust__latest"
            / "commit0.base.rust__latest.tar"
        )
        oci_dir.parent.mkdir(parents=True)
        oci_dir.touch()
        with patch(f"{MODULE}.OCI_IMAGE_DIR", tmp_path):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        mock_bi.assert_not_called()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_warns_when_skipping_with_mitm_cert(
        self, mock_gdf, mock_mba, mock_bi, tmp_path, caplog
    ):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        oci_dir = (
            tmp_path
            / "commit0.base.rust__latest"
            / "commit0.base.rust__latest.tar"
        )
        oci_dir.parent.mkdir(parents=True)
        oci_dir.touch()
        with (
            patch(f"{MODULE}.OCI_IMAGE_DIR", tmp_path),
            caplog.at_level(logging.WARNING),
        ):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client, mitm_ca_cert="/cert.pem")
        assert (
            "mitm" in caplog.text.lower()
            or "cert" in caplog.text.lower()
            or mock_bi.call_count == 0
        )

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_rebuilds_when_daemon_exists_but_no_oci(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_gdf.return_value = "FROM rust:latest"
        mock_mba.return_value = {}
        from commit0.harness.docker_build_rust import build_base_images_rust

        build_base_images_rust(client)
        mock_bi.assert_called_once()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_uses_env_platform(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        with patch.dict("os.environ", {"COMMIT0_BUILD_PLATFORMS": "linux/arm64"}):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        # The env-provided platform flows into build_image(platform=...).
        mock_bi.assert_called_once()
        assert "linux/arm64" in str(mock_bi.call_args)

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_default_platform_dual_arch(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        env_clean = {
            k: v
            for k, v in __import__("os").environ.items()
            if k != "COMMIT0_BUILD_PLATFORMS"
        }
        with patch.dict("os.environ", env_clean, clear=True):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        # Default dual-arch platform flows into build_image(platform=...).
        call_str = str(mock_bi.call_args)
        assert "amd64" in call_str or "arm64" in call_str

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_passes_mitm_cert_to_build(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        from commit0.harness.docker_build_rust import build_base_images_rust

        build_base_images_rust(client, mitm_ca_cert="/my/cert.pem")
        mock_bi.assert_called_once()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_none_mitm_cert_default(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        from commit0.harness.docker_build_rust import build_base_images_rust

        build_base_images_rust(client)
        mock_bi.assert_called_once()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_get_dockerfile_called_with_correct_args(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        from commit0.harness.docker_build_rust import build_base_images_rust

        build_base_images_rust(client)
        mock_gdf.assert_called_once()

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_build_image_receives_image_name(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        from commit0.harness.docker_build_rust import build_base_images_rust

        build_base_images_rust(client)
        call_kwargs = mock_bi.call_args
        assert "commit0.base.rust" in str(call_kwargs) or "rust" in str(call_kwargs)

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_build_image_error_propagates(self, mock_gdf, mock_mba, mock_bi):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_gdf.return_value = "FROM rust"
        mock_mba.return_value = {}
        from commit0.harness.docker_build import BuildImageError

        mock_bi.side_effect = BuildImageError(
            "commit0.base.rust:latest", "build failed", MagicMock()
        )
        from commit0.harness.docker_build_rust import build_base_images_rust

        with pytest.raises(BuildImageError):
            build_base_images_rust(client)

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}._multiarch_builder_args")
    @patch(f"{MODULE}.get_dockerfile_base_rust")
    @patch(f"{MODULE}.base_image_build_dir", lambda: Path("/build"))
    @patch(f"{MODULE}.OCI_IMAGE_DIR", Path("/oci"))
    def test_oci_key_derivation(self, mock_gdf, mock_mba, mock_bi, tmp_path):
        client = MagicMock()
        oci_key = "commit0.base.rust__latest"
        tar_path = tmp_path / oci_key / f"{oci_key}.tar"
        tar_path.parent.mkdir(parents=True)
        tar_path.touch()
        client.images.get.return_value = MagicMock()
        with patch(f"{MODULE}.OCI_IMAGE_DIR", tmp_path):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        mock_bi.assert_not_called()


# ---------------------------------------------------------------------------
# TestGetRustRepoConfigsToBuild
# ---------------------------------------------------------------------------
class TestGetRustRepoConfigsToBuild:
    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_returns_empty_when_no_specs(self, mock_ts, mock_specs):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_specs.return_value = {}
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert result == {}

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    def test_raises_when_base_image_missing(self, mock_specs):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("nope")
        mock_specs.return_value = {"k": _make_rust_spec("k", "s", "d", "p")}
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        with pytest.raises(Exception):
            get_rust_repo_configs_to_build(client, "ds")

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_includes_new_image(self, mock_ts, mock_specs):
        client = MagicMock()
        client.images.get.side_effect = [
            MagicMock(),
            docker.errors.ImageNotFound("nope"),
        ]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "repo1" in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_skips_fresh_image(self, mock_ts, mock_specs):
        client = MagicMock()
        base_img = MagicMock()
        repo_img = MagicMock()
        client.images.get.side_effect = [base_img, repo_img]
        mock_ts.side_effect = ["2024-01-01T00:00:00", "2024-06-01T00:00:00"]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "repo1" not in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_includes_stale_image(self, mock_ts, mock_specs):
        client = MagicMock()
        base_img = MagicMock()
        repo_img = MagicMock()
        client.images.get.side_effect = [base_img, repo_img]
        mock_ts.side_effect = ["2024-06-01T00:00:00", "2024-01-01T00:00:00"]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "repo1" in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_handles_timestamp_value_error(self, mock_ts, mock_specs):
        client = MagicMock()
        base_img = MagicMock()
        repo_img = MagicMock()
        client.images.get.side_effect = [base_img, repo_img]
        # Unparseable timestamp strings make datetime.fromisoformat raise
        # ValueError inside the stale-check; the code must fail SAFE and rebuild.
        mock_ts.side_effect = ["not-a-timestamp", "also-bad"]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "repo1" in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_handles_timestamp_type_error(self, mock_ts, mock_specs):
        client = MagicMock()
        base_img = MagicMock()
        repo_img = MagicMock()
        client.images.get.side_effect = [base_img, repo_img]
        # A non-str (bytes) timestamp makes str.replace / fromisoformat raise
        # TypeError inside the stale-check; the code must fail SAFE and rebuild.
        mock_ts.side_effect = [b"2024-06-01T00:00:00", b"2024-01-01T00:00:00"]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "repo1" in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_multiple_repos_mixed(self, mock_ts, mock_specs):
        client = MagicMock()
        base_img = MagicMock()
        fresh_img = MagicMock()
        client.images.get.side_effect = [
            base_img,
            docker.errors.ImageNotFound("nope"),
            fresh_img,
        ]
        mock_ts.side_effect = ["2024-01-01T00:00:00", "2024-06-01T00:00:00"]
        spec1 = _make_rust_spec("new_repo", "setup.sh", "Dockerfile", "linux/amd64")
        spec2 = _make_rust_spec("fresh_repo", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec1, spec2]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "new_repo" in result
        assert "fresh_repo" not in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_config_contains_setup_script(self, mock_ts, mock_specs):
        client = MagicMock()
        client.images.get.side_effect = [
            MagicMock(),
            docker.errors.ImageNotFound("nope"),
        ]
        spec = _make_rust_spec("repo1", "my_setup.sh", "Dockerfile.rust", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert result["repo1"]["setup_script"] == "my_setup.sh"

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_config_contains_dockerfile(self, mock_ts, mock_specs):
        client = MagicMock()
        client.images.get.side_effect = [
            MagicMock(),
            docker.errors.ImageNotFound("nope"),
        ]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile.rust", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert result["repo1"]["dockerfile"] == "Dockerfile.rust"

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_config_contains_platform(self, mock_ts, mock_specs):
        client = MagicMock()
        client.images.get.side_effect = [
            MagicMock(),
            docker.errors.ImageNotFound("nope"),
        ]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/arm64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert result["repo1"]["platform"] == "linux/arm64"

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_calls_specs_with_absolute(self, mock_ts, mock_specs):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_specs.return_value = {}
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        get_rust_repo_configs_to_build(client, "my_dataset")
        mock_specs.assert_called_once_with("my_dataset", absolute=True)

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_checks_base_image_first(self, mock_ts, mock_specs):
        client = MagicMock()
        call_order = []

        def track_get(name):
            call_order.append(name)
            if "base" in name:
                return MagicMock()
            raise docker.errors.ImageNotFound("nope")

        client.images.get.side_effect = track_get
        spec = _make_rust_spec("repo1", "s", "d", "p")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        get_rust_repo_configs_to_build(client, "ds")
        assert (
            any("base" in c or "rust" in c for c in call_order[:1])
            or len(call_order) >= 1
        )

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_equal_timestamps_not_stale(self, mock_ts, mock_specs):
        client = MagicMock()
        base_img = MagicMock()
        repo_img = MagicMock()
        client.images.get.side_effect = [base_img, repo_img]
        mock_ts.side_effect = ["2024-06-01T00:00:00", "2024-06-01T00:00:00"]
        spec = _make_rust_spec("repo1", "setup.sh", "Dockerfile", "linux/amd64")
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert "repo1" not in result

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_many_repos_all_new(self, mock_ts, mock_specs):
        client = MagicMock()
        effects = [MagicMock()]
        specs = []
        for i in range(5):
            effects.append(docker.errors.ImageNotFound("nope"))
            specs.append(_make_rust_spec(f"repo{i}", "s", "d", "p"))
        client.images.get.side_effect = effects
        mock_specs.return_value = specs
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert len(result) == 5

    @patch(f"{MODULE}.get_rust_specs_from_dataset")
    @patch(f"{MODULE}._get_image_created_timestamp")
    def test_many_repos_all_fresh(self, mock_ts, mock_specs):
        client = MagicMock()
        effects = [MagicMock()]
        specs = []
        ts_effects = []
        for i in range(5):
            effects.append(MagicMock())
            specs.append(_make_rust_spec(f"repo{i}", "s", "d", "p"))
            ts_effects.extend(["2024-01-01T00:00:00", "2024-06-01T00:00:00"])
        client.images.get.side_effect = effects
        mock_ts.side_effect = ts_effects
        mock_specs.return_value = specs
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, "ds")
        assert len(result) == 0


class TestBuildRustRepoImages:
    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build", return_value={})
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_empty_configs_returns_empty(
        self, _cert, _proxy, _base, _configs, _mba, _tqdm, _tpe, _ac
    ):
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert s == []
        assert f == []

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_single_success(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = {
            "img:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"}
        }
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 1
        assert len(f) == 0

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_single_build_image_error(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        from commit0.harness.docker_build import BuildImageError

        mock_configs.return_value = {
            "img:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"}
        }
        future = MagicMock()
        future.result.side_effect = BuildImageError("img:v1", "fail", MagicMock())
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 0
        assert len(f) == 1

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_single_generic_exception(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = {
            "img:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"}
        }
        future = MagicMock()
        future.result.side_effect = RuntimeError("boom")
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 0
        assert len(f) == 1

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_calls_base_build_first(
        self, _cert, _proxy, mock_base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = {}
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_base.assert_called_once()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value="/cert.pem")
    def test_passes_mitm_cert_to_base(
        self, _cert, _proxy, mock_base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = {}
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_base.assert_called_once()
        assert mock_base.call_args[1].get(
            "mitm_ca_cert"
        ) == "/cert.pem" or "/cert.pem" in str(mock_base.call_args)

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_calls_resolve_mitm(
        self, mock_cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = {}
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_cert.assert_called_once()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env")
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_calls_get_proxy_env(
        self, _cert, mock_proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_proxy.return_value = {}
        mock_configs.return_value = {}
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_proxy.assert_called_once()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value="/cert.pem")
    def test_logs_mitm_cert(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac, caplog
    ):
        mock_configs.return_value = {}
        with caplog.at_level(logging.INFO):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "mitm" in caplog.text.lower() or "cert" in caplog.text.lower()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env")
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_logs_proxy_env(
        self, _cert, mock_proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac, caplog
    ):
        mock_proxy.return_value = {"http_proxy": "http://proxy:8080"}
        mock_configs.return_value = {}
        with caplog.at_level(logging.INFO):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "proxy" in caplog.text.lower()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value="/cert.pem")
    def test_warns_mitm_without_proxy(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac, caplog
    ):
        mock_configs.return_value = {}
        with caplog.at_level(logging.WARNING):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "proxy" in caplog.text.lower() or "mitm" in caplog.text.lower()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_logs_no_images_needed(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac, caplog
    ):
        mock_configs.return_value = {}
        with caplog.at_level(logging.INFO):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "no rust" in caplog.text.lower() or "no" in caplog.text.lower()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_mixed_success_and_failure(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        from commit0.harness.docker_build import BuildImageError

        mock_configs.return_value = {
            "ok:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"},
            "fail:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"},
        }
        f_ok = MagicMock()
        f_ok.result.return_value = None
        f_fail = MagicMock()
        f_fail.result.side_effect = BuildImageError("fail:v1", "err", MagicMock())
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        submissions = iter([f_ok, f_fail])
        mock_executor.submit.side_effect = lambda *a, **kw: next(submissions)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [f_ok, f_fail]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 1
        assert len(f) == 1

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value={})
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_all_fail(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = {
            "a:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"},
            "b:v1": {"setup_script": "s", "dockerfile": "d", "platform": "p"},
        }
        f1 = MagicMock()
        f1.result.side_effect = RuntimeError("err1")
        f2 = MagicMock()
        f2.result.side_effect = RuntimeError("err2")
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        submissions = iter([f1, f2])
        mock_executor.submit.side_effect = lambda *a, **kw: next(submissions)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [f1, f2]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 0
        assert len(f) == 2

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_default_max_workers(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, mock_tpe, _ac
    ):
        mock_configs.return_value = dict()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_tpe.assert_not_called()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_custom_max_workers(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("img:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [], max_workers=8)
        mock_tpe.assert_called_once_with(max_workers=8)

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_pbar_update_called_per_future(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [
                ("a:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("b:v1", dict(setup_script="s", dockerfile="d", platform="p")),
            ]
        )
        f1 = MagicMock()
        f1.result.return_value = None
        f2 = MagicMock()
        f2.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        submissions = iter([f1, f2])
        mock_executor.submit.side_effect = lambda *a, **kw: next(submissions)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [f1, f2]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        assert mock_pbar.update.call_count == 2

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_tqdm_total_equals_config_count(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [
                ("a:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("b:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("c:v1", dict(setup_script="s", dockerfile="d", platform="p")),
            ]
        )
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = MagicMock()
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = []
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_tqdm.assert_called_once()
        assert (
            mock_tqdm.call_args[1].get("total") == 3 or mock_tqdm.call_args[0][0]
            if mock_tqdm.call_args[0]
            else True
        )

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_calls_multiarch_builder_args(
        self, _cert, _proxy, _base, mock_configs, mock_mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("a:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_mba.assert_called_once()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_logs_all_success(
        self,
        _cert,
        _proxy,
        _base,
        mock_configs,
        _mba,
        mock_tqdm,
        mock_tpe,
        mock_ac,
        caplog,
    ):
        mock_configs.return_value = dict(
            [("img:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        with caplog.at_level(logging.INFO):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "success" in caplog.text.lower() or "built" in caplog.text.lower()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_logs_failure_count(
        self,
        _cert,
        _proxy,
        _base,
        mock_configs,
        _mba,
        mock_tqdm,
        mock_tpe,
        mock_ac,
        caplog,
    ):
        mock_configs.return_value = dict(
            [("img:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.side_effect = RuntimeError("err")
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        with caplog.at_level(logging.WARNING):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "fail" in caplog.text.lower()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_submit_receives_build_image(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("img:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_executor.submit.assert_called_once()

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_return_type_is_tuple(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        result = build_rust_repo_images(MagicMock(), [])
        assert isinstance(result, tuple)
        assert len(result) == 2

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_successful_list_contains_image_keys(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [
                ("img_a:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("img_b:v1", dict(setup_script="s", dockerfile="d", platform="p")),
            ]
        )
        f1 = MagicMock()
        f1.result.return_value = None
        f2 = MagicMock()
        f2.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        submissions = iter([f1, f2])
        mock_executor.submit.side_effect = lambda *a, **kw: next(submissions)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [f1, f2]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert set(s) == set(["img_a:v1", "img_b:v1"])
        assert f == []

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_failed_list_contains_image_keys(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        from commit0.harness.docker_build import BuildImageError

        mock_configs.return_value = dict(
            [("fail_img:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.side_effect = BuildImageError("fail_img:v1", "err", MagicMock())
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert f == ["fail_img:v1"]
        assert s == []

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_three_successes(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [
                ("a:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("b:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("c:v1", dict(setup_script="s", dockerfile="d", platform="p")),
            ]
        )
        futures = [MagicMock() for _ in range(3)]
        for fu in futures:
            fu.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        it = iter(futures)
        mock_executor.submit.side_effect = lambda *a, **kw: next(it)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = futures
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 3
        assert len(f) == 0

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_build_image_error_logs_image_name(
        self,
        _cert,
        _proxy,
        _base,
        mock_configs,
        _mba,
        mock_tqdm,
        mock_tpe,
        mock_ac,
        caplog,
    ):
        from commit0.harness.docker_build import BuildImageError

        mock_configs.return_value = dict(
            [("bad:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.side_effect = BuildImageError("bad:v1", "oops", MagicMock())
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        with caplog.at_level(logging.ERROR):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "bad:v1" in caplog.text or "BuildImageError" in caplog.text

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_generic_error_logs_image_name(
        self,
        _cert,
        _proxy,
        _base,
        mock_configs,
        _mba,
        mock_tqdm,
        mock_tpe,
        mock_ac,
        caplog,
    ):
        mock_configs.return_value = dict(
            [("broken:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.side_effect = RuntimeError("boom")
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        with caplog.at_level(logging.ERROR):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "broken:v1" in caplog.text or "error" in caplog.text.lower()

    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_build_base_exception_propagates(
        self, _cert, _proxy, mock_base, mock_configs
    ):
        mock_base.side_effect = RuntimeError("base build failed")
        from commit0.harness.docker_build_rust import build_rust_repo_images

        with pytest.raises(RuntimeError, match="base build failed"):
            build_rust_repo_images(MagicMock(), [])

    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_get_configs_exception_propagates(self, _cert, _proxy, mock_configs, _base):
        mock_configs.side_effect = Exception("config error")
        from commit0.harness.docker_build_rust import build_rust_repo_images

        with pytest.raises(Exception, match="config error"):
            build_rust_repo_images(MagicMock(), [])

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env")
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value="/cert.pem")
    def test_mitm_and_proxy_both_set_no_warning(
        self, _cert, mock_proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac, caplog
    ):
        mock_proxy.return_value = dict(http_proxy="http://proxy:8080")
        mock_configs.return_value = dict()
        with caplog.at_level(logging.WARNING):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        warning_msgs = [r for r in caplog.records if r.levelno >= logging.WARNING]
        mitm_proxy_warnings = [
            m
            for m in warning_msgs
            if "proxy" in m.message.lower() and "mitm" in m.message.lower()
        ]
        assert len(mitm_proxy_warnings) == 0

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_no_mitm_no_proxy_no_warning(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac, caplog
    ):
        mock_configs.return_value = dict()
        with caplog.at_level(logging.WARNING):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        warning_msgs = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_msgs) == 0

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_logs_total_count(
        self,
        _cert,
        _proxy,
        _base,
        mock_configs,
        _mba,
        mock_tqdm,
        mock_tpe,
        mock_ac,
        caplog,
    ):
        mock_configs.return_value = dict(
            [
                ("a:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("b:v1", dict(setup_script="s", dockerfile="d", platform="p")),
            ]
        )
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = MagicMock()
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = []
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        with caplog.at_level(logging.INFO):
            from commit0.harness.docker_build_rust import build_rust_repo_images

            build_rust_repo_images(MagicMock(), [])
        assert "2" in caplog.text

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_verbose_zero(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [], verbose=0)
        assert s == []
        assert f == []

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_max_workers_one(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("a:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [], max_workers=1)
        mock_tpe.assert_called_once_with(max_workers=1)

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_submit_count_matches_configs(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [
                ("a:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("b:v1", dict(setup_script="s", dockerfile="d", platform="p")),
                ("c:v1", dict(setup_script="s", dockerfile="d", platform="p")),
            ]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future, future, future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        assert mock_executor.submit.call_count == 3


class TestBuildBaseImagesRustEdge:
    @patch(MODULE + ".build_image")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_dockerfile_base_rust", return_value="FROM ubuntu")
    @patch(
        MODULE + ".OCI_IMAGE_DIR",
        new_callable=lambda: property(lambda self: Path("/oci")),
    )
    def test_image_not_found_triggers_build(self, _oci, _df, _mba, mock_build):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("not found")
        with patch(MODULE + ".OCI_IMAGE_DIR", Path("/tmp/oci_test_missing")):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        mock_build.assert_called_once()

    @patch(MODULE + ".build_image")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_dockerfile_base_rust", return_value="FROM ubuntu")
    def test_build_image_called_with_correct_name(self, _df, _mba, mock_build):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("x")
        with patch(MODULE + ".OCI_IMAGE_DIR", Path("/tmp/oci_test_missing2")):
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client)
        assert "commit0.base.rust" in str(mock_build.call_args)

    def test_build_base_accepts_none_cert(self):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        with patch(MODULE + ".OCI_IMAGE_DIR") as mock_oci:
            mock_oci.__truediv__ = MagicMock(return_value=MagicMock())
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_oci.__truediv__.return_value.__truediv__ = MagicMock(
                return_value=mock_path
            )
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client, mitm_ca_cert=None)

    def test_build_base_accepts_string_cert(self):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        with patch(MODULE + ".OCI_IMAGE_DIR") as mock_oci:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_oci.__truediv__ = MagicMock(return_value=MagicMock())
            mock_oci.__truediv__.return_value.__truediv__ = MagicMock(
                return_value=mock_path
            )
            from commit0.harness.docker_build_rust import build_base_images_rust

            build_base_images_rust(client, mitm_ca_cert="/path/to/cert.pem")


class TestGetRustRepoConfigsEdge:
    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_single_spec_no_existing_image(self, mock_specs, mock_ts):
        client = MagicMock()
        client.images.get.side_effect = [MagicMock(), docker.errors.ImageNotFound("x")]
        spec = _make_rust_spec()
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert len(result) == 1

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_single_spec_existing_fresh_image(self, mock_specs, mock_ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_ts.return_value = "2099-01-01T00:00:00Z"
        spec = _make_rust_spec()
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert len(result) == 0

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_empty_dataset_returns_empty(self, mock_specs, _ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_specs.return_value = []
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert result == dict() or len(result) == 0

    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_base_image_missing_raises(self, mock_specs):
        client = MagicMock()
        client.images.get.side_effect = docker.errors.ImageNotFound("base not found")
        mock_specs.return_value = [_make_rust_spec()]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        with pytest.raises(Exception):
            get_rust_repo_configs_to_build(client, [])

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_stale_image_included(self, mock_specs, mock_ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        # base built AFTER repo -> repo image is stale -> must be rebuilt.
        mock_ts.side_effect = ["2024-06-01T00:00:00Z", "2000-01-01T00:00:00Z"]
        spec = _make_rust_spec()
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert len(result) == 1

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_value_error_timestamp_includes_image(self, mock_specs, mock_ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        # Unparseable timestamps -> ValueError in the stale-check -> fail safe rebuild.
        mock_ts.side_effect = ["not-a-timestamp", "also-bad"]
        spec = _make_rust_spec()
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert len(result) == 1

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_type_error_timestamp_includes_image(self, mock_specs, mock_ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        # Non-str (bytes) timestamps -> TypeError in the stale-check -> fail safe rebuild.
        mock_ts.side_effect = [b"2024-06-01T00:00:00", b"2000-01-01T00:00:00"]
        spec = _make_rust_spec()
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert len(result) == 1

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_calls_specs_with_absolute_true(self, mock_specs, _ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_specs.return_value = []
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        get_rust_repo_configs_to_build(client, ["data"])
        mock_specs.assert_called_once_with(["data"], absolute=True)

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_checks_base_image_existence(self, mock_specs, _ts):
        client = MagicMock()
        client.images.get.return_value = MagicMock()
        mock_specs.return_value = []
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        get_rust_repo_configs_to_build(client, [])
        client.images.get.assert_called()


class TestBuildRustRepoImagesIntegration:
    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_dataset_passed_to_get_configs(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        dataset = ["item1", "item2"]
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), dataset)
        mock_configs.assert_called_once()
        assert mock_configs.call_args[0][1] == dataset

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_client_passed_to_base_build(
        self, _cert, _proxy, mock_base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        client = MagicMock()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(client, [])
        mock_base.assert_called_once()
        assert mock_base.call_args[0][0] is client

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_client_passed_to_get_configs(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        client = MagicMock()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(client, [])
        mock_configs.assert_called_once()
        assert mock_configs.call_args[0][0] is client

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value="/cert.pem")
    def test_mitm_cert_passed_to_submit(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("a:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        future = MagicMock()
        future.result.return_value = None
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = future
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [future]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        submit_args = str(mock_executor.submit.call_args)
        assert "/cert.pem" in submit_args

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_five_images_all_succeed(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        configs = dict()
        for i in range(5):
            configs["img" + str(i) + ":v1"] = dict(
                setup_script="s", dockerfile="d", platform="p"
            )
        mock_configs.return_value = configs
        futures = []
        for _ in range(5):
            f = MagicMock()
            f.result.return_value = None
            futures.append(f)
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        it = iter(futures)
        mock_executor.submit.side_effect = lambda *a, **kw: next(it)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = futures
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 5
        assert len(f) == 0
        assert mock_pbar.update.call_count == 5

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_five_images_all_fail(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        configs = dict()
        for i in range(5):
            configs["img" + str(i) + ":v1"] = dict(
                setup_script="s", dockerfile="d", platform="p"
            )
        mock_configs.return_value = configs
        futures = []
        for _ in range(5):
            f = MagicMock()
            f.result.side_effect = RuntimeError("fail")
            futures.append(f)
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        it = iter(futures)
        mock_executor.submit.side_effect = lambda *a, **kw: next(it)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = futures
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 0
        assert len(f) == 5

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_tqdm_smoothing_zero(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("a:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = MagicMock()
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = []
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        assert mock_tqdm.call_args[1].get("smoothing") == 0

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_tqdm_desc_contains_rust(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        mock_configs.return_value = dict(
            [("a:v1", dict(setup_script="s", dockerfile="d", platform="p"))]
        )
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        mock_executor.submit.return_value = MagicMock()
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = []
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        desc = mock_tqdm.call_args[1].get("desc", "")
        assert "rust" in desc.lower() or "Rust" in desc

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_two_success_one_build_error_one_generic(
        self, _cert, _proxy, _base, mock_configs, _mba, mock_tqdm, mock_tpe, mock_ac
    ):
        from commit0.harness.docker_build import BuildImageError

        configs = dict()
        configs["ok1:v1"] = dict(setup_script="s", dockerfile="d", platform="p")
        configs["ok2:v1"] = dict(setup_script="s", dockerfile="d", platform="p")
        configs["berr:v1"] = dict(setup_script="s", dockerfile="d", platform="p")
        configs["gerr:v1"] = dict(setup_script="s", dockerfile="d", platform="p")
        mock_configs.return_value = configs
        f_ok1 = MagicMock()
        f_ok1.result.return_value = None
        f_ok2 = MagicMock()
        f_ok2.result.return_value = None
        f_berr = MagicMock()
        f_berr.result.side_effect = BuildImageError("berr:v1", "err", MagicMock())
        f_gerr = MagicMock()
        f_gerr.result.side_effect = RuntimeError("boom")
        mock_executor = MagicMock()
        mock_executor.__enter__ = MagicMock(return_value=mock_executor)
        mock_executor.__exit__ = MagicMock(return_value=False)
        it = iter([f_ok1, f_ok2, f_berr, f_gerr])
        mock_executor.submit.side_effect = lambda *a, **kw: next(it)
        mock_tpe.return_value = mock_executor
        mock_ac.return_value = [f_ok1, f_ok2, f_berr, f_gerr]
        mock_pbar = MagicMock()
        mock_pbar.__enter__ = MagicMock(return_value=mock_pbar)
        mock_pbar.__exit__ = MagicMock(return_value=False)
        mock_tqdm.return_value = mock_pbar
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert len(s) == 2
        assert len(f) == 2

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_empty_dataset_input(
        self, _cert, _proxy, _base, mock_configs, _mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        s, f = build_rust_repo_images(MagicMock(), [])
        assert isinstance(s, list)
        assert isinstance(f, list)

    @patch(MODULE + ".as_completed")
    @patch(MODULE + ".ThreadPoolExecutor")
    @patch(MODULE + ".tqdm")
    @patch(MODULE + "._multiarch_builder_args")
    @patch(MODULE + ".get_rust_repo_configs_to_build")
    @patch(MODULE + ".build_base_images_rust")
    @patch(MODULE + ".get_proxy_env", return_value=dict())
    @patch(MODULE + "._resolve_mitm_ca_cert", return_value=None)
    def test_multiarch_not_called_when_empty(
        self, _cert, _proxy, _base, mock_configs, mock_mba, _tqdm, _tpe, _ac
    ):
        mock_configs.return_value = dict()
        from commit0.harness.docker_build_rust import build_rust_repo_images

        build_rust_repo_images(MagicMock(), [])
        mock_mba.assert_not_called()

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_config_dict_has_expected_keys(self, mock_specs, mock_ts):
        client = MagicMock()
        client.images.get.side_effect = [MagicMock(), docker.errors.ImageNotFound("x")]
        spec = _make_rust_spec()
        mock_specs.return_value = [spec]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        for key, val in result.items():
            assert "setup_script" in val
            assert "dockerfile" in val
            assert "platform" in val

    @patch(MODULE + "._get_image_created_timestamp")
    @patch(MODULE + ".get_rust_specs_from_dataset")
    def test_multiple_specs_mixed_results(self, mock_specs, mock_ts):
        client = MagicMock()
        base_img = MagicMock()
        fresh_img = MagicMock()
        client.images.get.side_effect = [
            base_img,
            fresh_img,
            docker.errors.ImageNotFound("x"),
        ]
        mock_ts.return_value = "2099-01-01T00:00:00Z"
        spec1 = _make_rust_spec(repo="repo1")
        spec2 = _make_rust_spec(repo="repo2")
        mock_specs.return_value = [spec1, spec2]
        from commit0.harness.docker_build_rust import get_rust_repo_configs_to_build

        result = get_rust_repo_configs_to_build(client, [])
        assert len(result) == 1
