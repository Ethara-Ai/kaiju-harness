from __future__ import annotations

import io
import logging
import tarfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import docker.errors
import pytest

from commit0.harness.docker_utils import (
    HEREDOC_DELIMITER,
    cleanup_container,
    copy_from_container,
    copy_to_container,
    create_container,
    exec_run_with_timeout,
    image_exists_locally,
    pull_image_from_docker_hub,
    write_to_container,
)

MODULE = "commit0.harness.docker_utils"


def _make_tar_bytes(name: str, content: bytes) -> list[bytes]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    return [buf.read()]


def _make_traversal_tar_bytes() -> list[bytes]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="../../evil.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    buf.seek(0)
    return [buf.read()]


def _mock_container() -> MagicMock:
    container = MagicMock()
    container.id = "abc123"
    container.name = "test_container"
    return container


def _logger() -> logging.Logger:
    return logging.getLogger("test_docker_utils")


class TestHeredocDelimiter:
    def test_delimiter_is_string(self):
        assert isinstance(HEREDOC_DELIMITER, str)

    def test_delimiter_value(self):
        assert HEREDOC_DELIMITER == "EOF_1399519320"


class TestCopyToContainer:
    def test_creates_tar_and_copies(self, tmp_path):
        src = tmp_path / "hello.txt"
        src.write_text("contents")
        dst = Path("/workspace/hello.txt")

        container = _mock_container()

        copy_to_container(container, src, dst)

        container.exec_run.assert_any_call(f"mkdir -p {dst.parent}")
        container.put_archive.assert_called_once()
        assert container.put_archive.call_args[0][0] == "/workspace"
        container.exec_run.assert_any_call(f"tar -xf {dst}.tar -C {dst.parent}")
        container.exec_run.assert_any_call(f"rm {dst}.tar")

    def test_empty_dst_parent_raises_value_error(self, tmp_path):
        src = tmp_path / "file.txt"
        src.write_text("data")
        dst = Path("file.txt")

        container = _mock_container()
        with pytest.raises(
            ValueError, match="Destination path parent directory cannot be empty"
        ):
            copy_to_container(container, src, dst)

    def test_cleanup_after_copy(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_text("payload")
        dst = Path("/opt/data.txt")

        container = _mock_container()
        copy_to_container(container, src, dst)

        tar_path = src.with_suffix(".tar")
        assert not tar_path.exists()

    def test_mkdir_called_on_container(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text("x")
        dst = Path("/deep/nested/dir/a.txt")

        container = _mock_container()
        copy_to_container(container, src, dst)

        container.exec_run.assert_any_call("mkdir -p /deep/nested/dir")


class TestCopyFromContainer:
    def test_extracts_and_renames_file(self, tmp_path):
        tar_chunks = _make_tar_bytes("source.txt", b"hello world")
        container = _mock_container()
        container.get_archive.return_value = (tar_chunks, {"size": 11})

        src = Path("/container/source.txt")
        dst = tmp_path / "renamed.txt"

        copy_from_container(container, src, dst)

        assert dst.exists()
        assert dst.read_text() == "hello world"

    def test_creates_dst_parent_if_missing(self, tmp_path):
        tar_chunks = _make_tar_bytes("file.txt", b"data")
        container = _mock_container()
        container.get_archive.return_value = (tar_chunks, {"size": 4})

        missing_parent = tmp_path / "new_dir" / "sub"
        dst = missing_parent / "file.txt"
        src = Path("/container/file.txt")

        copy_from_container(container, src, dst)

        assert dst.parent.exists()
        assert dst.exists()

    def test_path_traversal_raises_exception(self, tmp_path):
        tar_chunks = _make_traversal_tar_bytes()
        container = _mock_container()
        container.get_archive.return_value = (tar_chunks, {"size": 4})

        src = Path("/container/evil.txt")
        dst = tmp_path / "evil.txt"

        with pytest.raises(Exception, match="Attempted Path Traversal in Tar File"):
            copy_from_container(container, src, dst)

    def test_string_src_converted_to_path(self, tmp_path):
        tar_chunks = _make_tar_bytes("myfile.txt", b"abc")
        container = _mock_container()
        container.get_archive.return_value = (tar_chunks, {"size": 3})

        src = "/container/myfile.txt"
        dst = tmp_path / "result.txt"

        copy_from_container(container, src, dst)

        container.get_archive.assert_called_once_with("/container/myfile.txt")
        assert dst.exists()

    def test_no_rename_when_names_match(self, tmp_path):
        tar_chunks = _make_tar_bytes("same.txt", b"content")
        container = _mock_container()
        container.get_archive.return_value = (tar_chunks, {"size": 7})

        src = Path("/container/same.txt")
        dst = tmp_path / "same.txt"

        copy_from_container(container, src, dst)

        assert dst.exists()
        assert dst.read_text() == "content"


class TestWriteToContainer:
    def test_uses_heredoc_command(self):
        container = _mock_container()
        write_to_container(container, "some data", Path("/tmp/out.txt"))

        cmd = container.exec_run.call_args[0][0]
        assert "cat <<" in cmd
        assert "EOF_" in cmd

    def test_command_contains_data_and_dst(self):
        container = _mock_container()
        write_to_container(container, "payload123", Path("/app/config.yml"))

        cmd = container.exec_run.call_args[0][0]
        assert "payload123" in cmd
        assert "/app/config.yml" in cmd


class TestCleanupContainer:
    def test_none_container_returns_immediately(self):
        client = MagicMock()
        logger = _logger()
        cleanup_container(client, None, logger)

    def test_successful_kill_and_remove(self):
        container = _mock_container()
        client = MagicMock()
        logger = _logger()

        cleanup_container(client, container, logger)

        container.kill.assert_called_once()
        container.remove.assert_called_once_with(force=True)

    def test_kill_fails_uses_sigkill_fallback(self):
        container = _mock_container()
        # First kill() fails, second kill(signal="SIGKILL") succeeds
        container.kill.side_effect = [Exception("docker kill failed"), None]

        client = MagicMock()
        logger = _logger()

        cleanup_container(client, container, logger)

        assert container.kill.call_count == 2
        container.kill.assert_any_call()
        container.kill.assert_any_call(signal="SIGKILL")
        container.remove.assert_called_once_with(force=True)

    def test_kill_and_sigkill_both_fail_raises(self):
        container = _mock_container()
        # Both kill attempts fail
        container.kill.side_effect = [
            Exception("docker kill failed"),
            Exception("sigkill also failed"),
        ]

        client = MagicMock()
        logger = _logger()

        with pytest.raises(Exception, match="Failed to forcefully kill"):
            cleanup_container(client, container, logger)

    def test_remove_fails_raises_exception(self):
        container = _mock_container()
        container.remove.side_effect = Exception("remove error")

        client = MagicMock()
        logger = _logger()

        with pytest.raises(Exception, match="Failed to remove container"):
            cleanup_container(client, container, logger)


class TestImageExistsLocally:
    def test_image_found_returns_true(self):
        client = MagicMock()
        image_mock = MagicMock()
        image_mock.tags = ["myimage:latest"]
        client.images.list.return_value = [image_mock]
        logger = _logger()

        result = image_exists_locally(client, "myimage", "latest", logger)

        assert result is True
        client.images.list.assert_called_once_with(name="myimage")

    def test_image_not_found_returns_false(self):
        client = MagicMock()
        image_mock = MagicMock()
        image_mock.tags = ["myimage:v1"]
        client.images.list.return_value = [image_mock]
        logger = _logger()

        result = image_exists_locally(client, "myimage", "v2", logger)

        assert result is False

    def test_empty_image_list_returns_false(self):
        client = MagicMock()
        client.images.list.return_value = []
        logger = _logger()

        result = image_exists_locally(client, "nothing", "latest", logger)

        assert result is False


class TestPullImageFromDockerHub:
    def test_successful_pull(self):
        client = MagicMock()
        logger = _logger()

        pull_image_from_docker_hub(client, "ubuntu", "22.04", logger)

        client.images.pull.assert_called_once_with("ubuntu", tag="22.04")

    def test_image_not_found_raises(self):
        client = MagicMock()
        client.images.pull.side_effect = docker.errors.ImageNotFound("nope")
        logger = _logger()

        with pytest.raises(Exception, match="not found on Docker Hub"):
            pull_image_from_docker_hub(client, "nonexistent", "latest", logger)

    def test_api_error_raises(self):
        client = MagicMock()
        client.images.pull.side_effect = docker.errors.APIError("api fail")
        logger = _logger()

        with pytest.raises(Exception, match="Error pulling image"):
            pull_image_from_docker_hub(client, "ubuntu", "latest", logger)


class TestCreateContainer:
    @patch(f"{MODULE}.image_exists_locally", return_value=True)
    def test_creates_with_correct_args(self, mock_exists):
        client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "container_id_123"
        client.containers.run.return_value = mock_container
        logger = _logger()

        result = create_container(
            client,
            "myimage:v1",
            "my_container",
            logger,
            user="root",
            nano_cpus=500000000,
        )

        assert result is mock_container
        client.containers.run.assert_called_once_with(
            image="myimage:v1",
            name="my_container",
            user="root",
            command="tail -f /dev/null",
            nano_cpus=500000000,
            environment=None,
            detach=True,
        )

    @patch(f"{MODULE}.pull_image_from_docker_hub")
    @patch(f"{MODULE}.image_exists_locally", return_value=False)
    def test_pulls_when_image_not_local(self, mock_exists, mock_pull):
        client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "cid"
        client.containers.run.return_value = mock_container
        logger = _logger()

        create_container(client, "repo:tag", "name", logger)

        mock_pull.assert_called_once_with(client, "repo", "tag", logger)

    @patch(f"{MODULE}.cleanup_container")
    @patch(f"{MODULE}.image_exists_locally", return_value=True)
    def test_error_cleans_up_and_reraises(self, mock_exists, mock_cleanup):
        client = MagicMock()
        client.containers.run.side_effect = Exception("creation failed")
        logger = _logger()

        with pytest.raises(Exception, match="creation failed"):
            create_container(client, "img:tag", "cname", logger)

    @patch(f"{MODULE}.image_exists_locally", return_value=True)
    def test_splits_image_name_and_tag(self, mock_exists):
        client = MagicMock()
        mock_c = MagicMock()
        mock_c.id = "x"
        client.containers.run.return_value = mock_c
        logger = _logger()

        create_container(client, "registry/myimg:v2.1", "c", logger)

        mock_exists.assert_called_once_with(client, "registry/myimg", "v2.1", logger)


class TestExecRunWithTimeout:
    def test_command_completes_within_timeout(self):
        container = _mock_container()
        container.client = MagicMock()
        container.client.api.exec_create.return_value = {"Id": "exec1"}
        container.client.api.exec_start.return_value = [b"line1\n", b"line2\n"]

        result, timed_out, elapsed = exec_run_with_timeout(
            container, "echo hi", timeout=10
        )

        assert timed_out is False
        assert "line1" in result
        assert "line2" in result
        assert elapsed >= 0

    def test_command_times_out(self):
        container = _mock_container()
        container.client = MagicMock()
        container.client.api.exec_create.return_value = {"Id": "exec_timeout"}
        container.client.api.exec_inspect.return_value = {"Pid": 999}

        def slow_stream(*args, **kwargs):
            time.sleep(5)
            return []

        container.client.api.exec_start.side_effect = slow_stream

        result, timed_out, elapsed = exec_run_with_timeout(
            container, "sleep 100", timeout=0.1
        )

        assert timed_out is True
        container.client.api.exec_inspect.assert_called_once_with(
            exec_id="exec_timeout"
        )
        container.exec_run.assert_called_once()

    def test_returns_output_and_runtime(self):
        container = _mock_container()
        container.client = MagicMock()
        container.client.api.exec_create.return_value = {"Id": "e1"}
        container.client.api.exec_start.return_value = [b"output data"]

        result, timed_out, elapsed = exec_run_with_timeout(container, "cmd", timeout=10)

        assert result == "output data"
        assert timed_out is False
        assert isinstance(elapsed, float)
        assert elapsed >= 0

    def test_timeout_kills_process(self):
        container = _mock_container()
        container.client = MagicMock()
        container.client.api.exec_create.return_value = {"Id": "kill_me"}
        container.client.api.exec_inspect.return_value = {"Pid": 1234}

        def blocking_stream(*args, **kwargs):
            time.sleep(5)
            return []

        container.client.api.exec_start.side_effect = blocking_stream

        result, timed_out, elapsed = exec_run_with_timeout(
            container, "hang", timeout=0.1
        )

        assert timed_out is True
        container.exec_run.assert_called_once_with("kill -TERM 1234", detach=True)

    def test_api_error_in_exec(self):
        container = _mock_container()
        container.client = MagicMock()
        container.client.api.exec_create.side_effect = docker.errors.APIError(
            "exec fail"
        )

        result, timed_out, elapsed = exec_run_with_timeout(container, "bad", timeout=5)

        assert result == ""
        assert timed_out is False
