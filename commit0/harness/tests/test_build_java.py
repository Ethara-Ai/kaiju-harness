from __future__ import annotations

from unittest.mock import MagicMock, patch

import docker
import pytest

MODULE = "commit0.harness.build_java"


class TestScriptsListToDict:
    def test_wraps_in_setup_sh(self) -> None:
        from commit0.harness.build_java import _scripts_list_to_dict

        result = _scripts_list_to_dict(["echo hello", "echo world"])
        assert "setup.sh" in result
        assert "echo hello" in result["setup.sh"]
        assert "echo world" in result["setup.sh"]

    def test_adds_shebang(self) -> None:
        from commit0.harness.build_java import _scripts_list_to_dict

        result = _scripts_list_to_dict(["cmd1"])
        content = result["setup.sh"]
        assert content.startswith("#!/bin/bash")
        assert "set -euxo pipefail" in content


class TestBuildJavaBaseImages:
    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_docker_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_builds_three_versions(
        self,
        mock_docker: MagicMock,
        mock_mitm: MagicMock,
        mock_platform: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_docker.from_env.return_value = MagicMock()
        from commit0.harness.build_java import build_java_base_images

        build_java_base_images(java_versions=["11", "17", "21"])
        assert mock_build.call_count == 3

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_docker_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_calls_build_image(
        self,
        mock_docker: MagicMock,
        mock_mitm: MagicMock,
        mock_platform: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_docker.from_env.return_value = MagicMock()
        from commit0.harness.build_java import build_java_base_images

        build_java_base_images(java_versions=["17"])
        mock_build.assert_called_once()
        kwargs = mock_build.call_args[1]
        assert "commit0-java" in kwargs["image_name"]

    @patch(f"{MODULE}.build_image", side_effect=docker.errors.BuildError("fail", []))
    @patch(f"{MODULE}.get_docker_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_docker_error_propagates(
        self,
        mock_docker: MagicMock,
        mock_mitm: MagicMock,
        mock_platform: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_docker.from_env.return_value = MagicMock()
        from commit0.harness.build_java import build_java_base_images

        with pytest.raises(docker.errors.BuildError):
            build_java_base_images(java_versions=["17"])


class TestBuildJavaRepoImages:
    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_docker_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    @patch(f"{MODULE}.docker")
    @patch(f"{MODULE}.make_java_spec")
    def test_builds_per_repo(
        self,
        mock_spec: MagicMock,
        mock_docker: MagicMock,
        mock_mitm: MagicMock,
        mock_platform: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_docker.from_env.return_value = MagicMock()
        spec = MagicMock()
        spec.repo_dockerfile = "FROM java:17"
        spec.make_repo_script_list.return_value = ["mvn install"]
        mock_spec.return_value = spec
        from commit0.harness.build_java import build_java_repo_images

        build_java_repo_images(repo_names=["org/repoA", "org/repoB"])
        assert mock_build.call_count == 2

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_docker_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    @patch(f"{MODULE}.docker")
    @patch(f"{MODULE}.make_java_spec")
    def test_creates_default_instance(
        self,
        mock_spec: MagicMock,
        mock_docker: MagicMock,
        mock_mitm: MagicMock,
        mock_platform: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_docker.from_env.return_value = MagicMock()
        spec = MagicMock()
        spec.repo_dockerfile = "FROM java:17"
        spec.make_repo_script_list.return_value = []
        mock_spec.return_value = spec
        from commit0.harness.build_java import build_java_repo_images

        build_java_repo_images(repo_names=["org/myrepo"], dataset=None)
        call_args = mock_spec.call_args[0][0]
        assert call_args["repo"] == "org/myrepo"
        assert call_args["base_commit"] == "HEAD"

    @patch(f"{MODULE}.build_image")
    @patch(f"{MODULE}.get_docker_platform", return_value="linux/amd64")
    @patch(f"{MODULE}._resolve_mitm_ca_cert", return_value=None)
    @patch(f"{MODULE}.docker")
    @patch(f"{MODULE}.make_java_spec")
    def test_empty_repo_list_no_builds(
        self,
        mock_spec: MagicMock,
        mock_docker: MagicMock,
        mock_mitm: MagicMock,
        mock_platform: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        mock_docker.from_env.return_value = MagicMock()
        from commit0.harness.build_java import build_java_repo_images

        # Empty list is falsy so falls back to JAVA_SPLIT; patch it empty
        with patch(f"{MODULE}.JAVA_SPLIT", {"all": []}):
            build_java_repo_images(repo_names=None)
        mock_build.assert_not_called()
