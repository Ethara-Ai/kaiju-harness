from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch


from commit0.harness.health_check_java import (
    check_docker_java_images,
    check_java_toolchain,
    check_java_version,
    health_check_java,
)

MODULE = "commit0.harness.health_check_java"


class TestCheckJavaToolchain:
    @patch(f"{MODULE}.subprocess.run")
    def test_all_tools_found(self, mock_run: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.stdout = "openjdk version 17.0.1\nsecond line"
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        result = check_java_toolchain()
        assert result["java"] is not None
        assert result["javac"] is not None
        assert result["mvn"] is not None
        assert result["gradle"] is not None

    @patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError)
    def test_tool_not_found_returns_none(self, mock_run: MagicMock) -> None:
        result = check_java_toolchain()
        for tool_output in result.values():
            assert tool_output is None

    @patch(f"{MODULE}.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="java", timeout=10))
    def test_tool_timeout_returns_none(self, mock_run: MagicMock) -> None:
        result = check_java_toolchain()
        for tool_output in result.values():
            assert tool_output is None


class TestCheckJavaVersion:
    @patch(f"{MODULE}.check_java_toolchain")
    def test_version_found_in_output(self, mock_toolchain: MagicMock) -> None:
        mock_toolchain.return_value = {"java": 'openjdk version "17.0.1"'}
        assert check_java_version("17") is True

    @patch(f"{MODULE}.check_java_toolchain")
    def test_version_not_found(self, mock_toolchain: MagicMock) -> None:
        mock_toolchain.return_value = {"java": 'openjdk version "11.0.2"'}
        assert check_java_version("17") is False

    @patch(f"{MODULE}.check_java_toolchain")
    def test_java_not_installed(self, mock_toolchain: MagicMock) -> None:
        mock_toolchain.return_value = {"java": None}
        assert check_java_version("17") is False


class TestCheckDockerJavaImages:
    @patch(f"{MODULE}.subprocess.run")
    def test_images_found(self, mock_run: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "commit0-java17:latest\ncommit0-java21:latest\nubuntu:22.04"
        mock_run.return_value = mock_result
        result = check_docker_java_images()
        assert len(result) == 2
        assert "commit0-java17:latest" in result

    @patch(f"{MODULE}.subprocess.run", side_effect=FileNotFoundError)
    def test_docker_unavailable(self, mock_run: MagicMock) -> None:
        result = check_docker_java_images()
        assert result == []

    @patch(f"{MODULE}.subprocess.run")
    def test_no_matching_images(self, mock_run: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ubuntu:22.04\npython:3.12"
        mock_run.return_value = mock_result
        result = check_docker_java_images()
        assert result == []

    @patch(f"{MODULE}.subprocess.run")
    def test_nonzero_exit_code(self, mock_run: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_run.return_value = mock_result
        result = check_docker_java_images()
        assert result == []


class TestHealthCheckJava:
    @patch(f"{MODULE}.check_docker_java_images", return_value=["commit0-java17:latest"])
    @patch(f"{MODULE}.check_java_toolchain")
    def test_all_healthy(self, mock_toolchain: MagicMock, mock_docker: MagicMock) -> None:
        mock_toolchain.return_value = {
            "java": "openjdk 17",
            "javac": "javac 17",
            "mvn": "Apache Maven 3.9",
            "gradle": "Gradle 8.5",
        }
        result = health_check_java()
        assert result["java_installed"] is True
        assert result["javac_installed"] is True
        assert result["maven_installed"] is True
        assert result["gradle_installed"] is True
        assert result["docker_available"] is True

    @patch(f"{MODULE}.check_docker_java_images", return_value=[])
    @patch(f"{MODULE}.check_java_toolchain")
    def test_docker_unhealthy(self, mock_toolchain: MagicMock, mock_docker: MagicMock) -> None:
        mock_toolchain.return_value = {
            "java": "openjdk 17",
            "javac": "javac 17",
            "mvn": "Apache Maven 3.9",
            "gradle": "Gradle 8.5",
        }
        result = health_check_java()
        assert result["docker_available"] is False

    @patch(f"{MODULE}.check_docker_java_images", return_value=[])
    @patch(f"{MODULE}.check_java_toolchain")
    def test_aggregates_all_checks(self, mock_toolchain: MagicMock, mock_docker: MagicMock) -> None:
        mock_toolchain.return_value = {
            "java": None,
            "javac": None,
            "mvn": None,
            "gradle": None,
        }
        result = health_check_java()
        assert result["java_installed"] is False
        assert result["javac_installed"] is False
        assert result["maven_installed"] is False
        assert result["gradle_installed"] is False
        assert result["docker_available"] is False
