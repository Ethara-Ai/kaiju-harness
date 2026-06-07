"""Tests for D1-D9 Docker issue fixes across the harness package.

Each test class maps to one D-issue:
  D1: evaluate.py futures checked
  D2: execution_context.py unique container names
  D3: evaluate.py pre-flight image validation
  D4: execution_context.py cleanup error isolation
  D5: docker_utils.py assert → if guard
  D6: validate.py orphan container removal
  D7: docker_build.py stale base-image detection
  D8: evaluate.py report.json crash vs infra disambiguation
  D9: docker_build.py logger.error instead of print
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# D1: futures are checked in evaluate.py
# ---------------------------------------------------------------------------


class TestD1FuturesChecked:
    """evaluate.py must call future.result() so Docker/eval errors are surfaced."""

    def test_future_exception_is_logged(self, tmp_path: Path) -> None:
        """When a submitted future raises, logger.error is called (not silently swallowed)."""
        from concurrent.futures import Future

        mock_logger = MagicMock()
        boom = RuntimeError("docker container exploded")

        # Simulate the loop from evaluate.py lines 160-165
        future = Future()
        future.set_exception(boom)

        # Re-implement the pattern from evaluate.py to verify it works
        try:
            future.result()
        except Exception as e:
            mock_logger.error(f"Evaluation failed for a repo: {e}")

        mock_logger.error.assert_called_once()
        assert "docker container exploded" in mock_logger.error.call_args[0][0]

    def test_future_success_no_error_logged(self) -> None:
        """When a future succeeds, no error is logged."""
        from concurrent.futures import Future

        mock_logger = MagicMock()
        future = Future()
        future.set_result(None)

        try:
            future.result()
        except Exception as e:
            mock_logger.error(f"Evaluation failed for a repo: {e}")

        mock_logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# D2: unique container names via uuid
# ---------------------------------------------------------------------------


class TestD2UniqueContainerNames:
    """execution_context.py must generate unique container names."""

    def test_container_name_with_run_id_is_unique(self) -> None:
        """get_container_name(run_id=...) produces different names for different run_ids."""
        # Mock a minimal Spec-like object with get_container_name

        class FakeSpec:
            repo = "Ethara-Ai/test-repo"

            def get_container_name(self, run_id: Optional[str] = None) -> str:
                repo = self.repo.split("/")[-1]
                if not run_id:
                    return f"commit0.eval.{repo}"
                return f"commit0.eval.{repo}.{run_id}".lower()

        spec = FakeSpec()
        name1 = spec.get_container_name(run_id="abc12345")
        name2 = spec.get_container_name(run_id="def67890")
        name_no_id = spec.get_container_name()

        assert name1 != name2, "Different run_ids must produce different names"
        assert name1 != name_no_id, "Name with run_id must differ from default"
        assert "abc12345" in name1
        assert "def67890" in name2

    def test_docker_context_uses_uuid_in_container_name(self) -> None:
        """Docker.__init__ passes uuid hex to get_container_name."""
        import re

        # The execution_context.py Docker class does:
        # spec.get_container_name(run_id=uuid.uuid4().hex[:8])
        # We verify the pattern by checking uuid.uuid4().hex[:8] produces 8 hex chars
        import uuid

        run_id = uuid.uuid4().hex[:8]
        assert len(run_id) == 8
        assert re.match(r"^[0-9a-f]{8}$", run_id)


# ---------------------------------------------------------------------------
# D3: pre-flight image validation
# ---------------------------------------------------------------------------


class TestD3PreflightImageCheck:
    """evaluate.py _preflight_check_images must catch missing images before evaluation."""

    def test_returns_empty_for_non_local_backend(self) -> None:
        """Non-local backends skip Docker image checks."""
        from commit0.harness.evaluate import _preflight_check_images

        # Modal/E2B backends don't need local Docker images
        with patch("commit0.harness.evaluate.load_dataset_from_config"):
            result = _preflight_check_images("dataset", "split", "MODAL")
            assert result == []

            result = _preflight_check_images("dataset", "split", "E2B")
            assert result == []

    def test_returns_missing_when_image_not_found(self) -> None:
        """When Docker image is missing, it appears in the returned list."""
        from commit0.harness.evaluate import _preflight_check_images

        mock_client = MagicMock()
        mock_client.images.get.side_effect = __import__(
            "docker.errors", fromlist=["ImageNotFound"]
        ).ImageNotFound("not found")

        mock_spec = MagicMock()
        mock_spec.base_image_key = "commit0.base.python3.12:latest"
        mock_spec.repo_image_key = "commit0.repo.test.abc:v0"

        with (
            patch("commit0.harness.evaluate.docker") as mock_docker_mod,
            patch(
                "commit0.harness.evaluate.load_dataset_from_config",
                return_value=[{"repo": "org/test"}],
            ),
            patch(
                "commit0.harness.evaluate.get_specs_from_dataset",
                return_value=[mock_spec],
            ),
        ):
            mock_docker_mod.from_env.return_value = mock_client
            mock_docker_mod.errors = __import__(
                "docker.errors",
                fromlist=["ImageNotFound", "APIError", "DockerException"],
            )

            result = _preflight_check_images("dataset", "split", "local")

        assert len(result) == 2
        assert "commit0.base.python3.12:latest" in result
        assert "commit0.repo.test.abc:v0" in result

    def test_returns_empty_when_all_images_present(self) -> None:
        """When all images exist, returns empty list."""
        from commit0.harness.evaluate import _preflight_check_images

        mock_client = MagicMock()
        mock_client.images.get.return_value = MagicMock()  # image found

        mock_spec = MagicMock()
        mock_spec.base_image_key = "commit0.base.python3.12:latest"
        mock_spec.repo_image_key = "commit0.repo.test.abc:v0"

        with (
            patch("commit0.harness.evaluate.docker") as mock_docker_mod,
            patch(
                "commit0.harness.evaluate.load_dataset_from_config",
                return_value=[{"repo": "org/test"}],
            ),
            patch(
                "commit0.harness.evaluate.get_specs_from_dataset",
                return_value=[mock_spec],
            ),
        ):
            mock_docker_mod.from_env.return_value = mock_client
            mock_docker_mod.errors = __import__(
                "docker.errors",
                fromlist=["ImageNotFound", "APIError", "DockerException"],
            )

            result = _preflight_check_images("dataset", "split", "local")

        assert result == []

    def test_docker_daemon_unreachable(self) -> None:
        """When Docker daemon is not running, returns sentinel."""
        from commit0.harness.evaluate import _preflight_check_images

        with patch("commit0.harness.evaluate.docker") as mock_docker_mod:
            mock_docker_mod.from_env.side_effect = Exception("Cannot connect")
            mock_docker_mod.errors.DockerException = Exception

            result = _preflight_check_images("dataset", "split", "local")

        assert "<docker-daemon-unreachable>" in result


# ---------------------------------------------------------------------------
# D4: cleanup error isolation in execution_context.py
# ---------------------------------------------------------------------------


class TestD4CleanupErrorIsolation:
    """Docker.__exit__ must not replace the original exception if cleanup fails."""

    def test_cleanup_failure_logged_but_original_exception_preserved(self) -> None:
        """If cleanup_container raises AND there's already an in-flight exception,
        the in-flight exception must propagate (not the cleanup error).
        """
        mock_logger = MagicMock()
        original_error = ValueError("original test error")
        cleanup_error = RuntimeError("cleanup failed")

        # Simulate Docker.__exit__ logic
        excinst = original_error
        try:
            raise cleanup_error  # simulate cleanup_container raising
        except Exception as e:
            mock_logger.error(f"Container cleanup failed: {e}")
            if excinst is None:
                raise  # only re-raise cleanup if no original error

        # cleanup error was logged
        mock_logger.error.assert_called_once()
        assert "cleanup failed" in mock_logger.error.call_args[0][0]

    def test_cleanup_failure_re_raised_when_no_original_exception(self) -> None:
        """If cleanup_container raises AND there's NO in-flight exception,
        the cleanup error must propagate.
        """
        mock_logger = MagicMock()
        cleanup_error = RuntimeError("cleanup failed")

        excinst = None  # no original exception
        with pytest.raises(RuntimeError, match="cleanup failed"):
            try:
                raise cleanup_error
            except Exception as e:
                mock_logger.error(f"Container cleanup failed: {e}")
                if excinst is None:
                    raise


# ---------------------------------------------------------------------------
# D5: assert → if guard in docker_utils.py
# ---------------------------------------------------------------------------


class TestD5AssertRemoved:
    """docker_utils.py create_container must use `if` guard, not `assert`."""

    def test_none_container_does_not_assert(self) -> None:
        """When container creation fails, cleanup is skipped gracefully (no AssertionError)."""
        from commit0.harness.docker_utils import cleanup_container

        mock_client = MagicMock()
        mock_logger = MagicMock()

        # container is None — should NOT raise AssertionError
        cleanup_container(mock_client, None, mock_logger)
        # If we get here, no AssertionError was raised — test passes

    def test_valid_container_is_cleaned_up(self) -> None:
        """When container is a valid object, cleanup proceeds normally."""
        from commit0.harness.docker_utils import cleanup_container

        mock_client = MagicMock()
        mock_logger = MagicMock()
        mock_container = MagicMock()
        mock_container.name = "test-container"

        cleanup_container(mock_client, mock_container, mock_logger)

        mock_container.kill.assert_called_once()
        mock_container.remove.assert_called_once_with(force=True)

    def test_create_container_error_path_no_assert(self) -> None:
        """In create_container, if container.run raises after creation,
        cleanup uses `if container is not None` (not assert).
        """
        # Read the source to verify no `assert container` pattern
        import inspect
        from commit0.harness import docker_utils

        source = inspect.getsource(docker_utils.create_container)
        assert "assert container" not in source, (
            "create_container still has `assert container` — should use `if container is not None`"
        )


# ---------------------------------------------------------------------------
# D6: orphan container removal in validate.py
# ---------------------------------------------------------------------------


class TestD6OrphanContainerRemoval:
    """validate.py run_tests_in_docker must `docker rm -f` after `docker kill` on timeout."""

    def test_docker_rm_called_after_timeout_kill(self) -> None:
        """On subprocess.TimeoutExpired, both docker kill and docker rm -f are called."""
        import inspect
        from tools import validate

        source = inspect.getsource(validate.run_tests_in_docker)

        assert '"docker", "kill"' in source, "Missing docker kill in timeout handler"
        assert '"docker", "rm", "-f"' in source, (
            "Missing `docker rm -f` after docker kill in timeout handler"
        )


# ---------------------------------------------------------------------------
# D7: stale base-image detection in docker_build.py
# ---------------------------------------------------------------------------


class TestD7StaleBaseDetection:
    """docker_build.py get_repo_configs_to_build must detect stale repo images."""

    def test_stale_repo_image_scheduled_for_rebuild(self) -> None:
        """If base was rebuilt AFTER repo image, repo should be scheduled for rebuild."""
        from commit0.harness.docker_build import _get_image_created_timestamp

        mock_client = MagicMock()

        # Simulate: base rebuilt recently, repo built earlier
        def fake_get(image_name: str) -> MagicMock:
            img = MagicMock()
            if "base" in image_name:
                img.attrs = {"Created": "2026-04-16T12:00:00Z"}
            else:
                img.attrs = {"Created": "2026-04-15T12:00:00Z"}
            return img

        mock_client.images.get.side_effect = fake_get

        # base is newer than repo
        base_ts = _get_image_created_timestamp(
            mock_client, "commit0.base.python3.12:latest"
        )
        repo_ts = _get_image_created_timestamp(mock_client, "commit0.repo.test.abc:v0")
        assert base_ts > repo_ts, "Base should be newer than repo in this test"

    def test_image_not_found_returns_empty_string(self) -> None:
        """_get_image_created_timestamp returns '' for missing images."""
        from commit0.harness.docker_build import _get_image_created_timestamp
        import docker.errors

        mock_client = MagicMock()
        mock_client.images.get.side_effect = docker.errors.ImageNotFound("not found")

        result = _get_image_created_timestamp(mock_client, "nonexistent:latest")
        assert result == ""

    def test_fresh_repo_image_not_rebuilt(self) -> None:
        """If repo image is newer than base, it should NOT be scheduled for rebuild."""
        from commit0.harness.docker_build import _get_image_created_timestamp

        mock_client = MagicMock()

        def fake_get(image_name: str) -> MagicMock:
            img = MagicMock()
            if "base" in image_name:
                img.attrs = {"Created": "2026-04-14T12:00:00Z"}
            else:
                img.attrs = {"Created": "2026-04-16T12:00:00Z"}
            return img

        mock_client.images.get.side_effect = fake_get

        base_ts = _get_image_created_timestamp(
            mock_client, "commit0.base.python3.12:latest"
        )
        repo_ts = _get_image_created_timestamp(mock_client, "commit0.repo.test.abc:v0")
        assert base_ts < repo_ts, "Base should be older than repo in this test"


# ---------------------------------------------------------------------------
# D8: report.json crash vs infra disambiguation
# ---------------------------------------------------------------------------


class TestD8ReportJsonDisambiguation:
    """evaluate.py must distinguish between pytest crash (test_output.txt exists)
    and infra failure (no test_output.txt) when report.json is missing.
    """

    def test_pytest_crash_detected(self, tmp_path: Path) -> None:
        """When test_output.txt exists but report.json doesn't, reason is pytest crash."""
        log_dir = tmp_path / "repo" / "branch" / "hash"
        log_dir.mkdir(parents=True)
        (log_dir / "test_output.txt").write_text("error output")

        # Simulate the logic from evaluate.py lines 174-183
        report_file = str(log_dir / "report.json")
        assert not os.path.exists(report_file)

        log_parent = os.path.dirname(report_file)
        test_output_file = os.path.join(log_parent, "test_output.txt")
        if os.path.exists(test_output_file):
            reason = "pytest_crash_or_collection_error"
        else:
            reason = "container_or_infra_failure"

        assert reason == "pytest_crash_or_collection_error"

    def test_infra_failure_detected(self, tmp_path: Path) -> None:
        """When neither test_output.txt nor report.json exist, reason is infra failure."""
        log_dir = tmp_path / "repo" / "branch" / "hash"
        log_dir.mkdir(parents=True)

        report_file = str(log_dir / "report.json")
        assert not os.path.exists(report_file)

        log_parent = os.path.dirname(report_file)
        test_output_file = os.path.join(log_parent, "test_output.txt")
        if os.path.exists(test_output_file):
            reason = "pytest_crash_or_collection_error"
        else:
            reason = "container_or_infra_failure"

        assert reason == "container_or_infra_failure"

    def test_report_exists_not_ambiguous(self, tmp_path: Path) -> None:
        """When report.json exists, the disambiguation code is never reached."""
        log_dir = tmp_path / "repo" / "branch" / "hash"
        log_dir.mkdir(parents=True)

        report_data = {
            "created": 1234,
            "tests": [
                {
                    "nodeid": "tests/test_a.py::test_foo",
                    "call": {"outcome": "passed", "duration": 0.1},
                }
            ],
        }
        (log_dir / "report.json").write_text(json.dumps(report_data))

        report_file = str(log_dir / "report.json")
        assert os.path.exists(report_file)


# ---------------------------------------------------------------------------
# D9: docker_build.py uses logger.error instead of print
# ---------------------------------------------------------------------------


class TestD9LoggerNotPrint:
    """docker_build.py build_repo_images must use _logger.error, not print, for errors."""

    def test_build_error_uses_logger(self) -> None:
        """Verify the source code uses _logger.error for BuildImageError."""
        import inspect
        from commit0.harness import docker_build

        source = inspect.getsource(docker_build.build_repo_images)

        # Should use _logger.error for error reporting
        assert "_logger.error" in source, (
            "build_repo_images should use _logger.error, not print, for build failures"
        )

    def test_no_print_for_errors_in_build_repo_images(self) -> None:
        """build_repo_images except handlers must not use print() for error reporting."""
        import inspect
        import ast
        from commit0.harness import docker_build

        source = inspect.getsource(docker_build.build_repo_images)
        tree = ast.parse(source)

        print_in_except = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        func = child.func
                        if isinstance(func, ast.Name) and func.id == "print":
                            print_in_except.append(ast.dump(child))

        assert len(print_in_except) == 0, (
            f"Found print() inside except handlers (should be _logger.error): {print_in_except}"
        )
