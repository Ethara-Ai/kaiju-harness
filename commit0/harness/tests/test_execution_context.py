from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest
from strenum import StrEnum

MODULE = "commit0.harness.execution_context"


def _make_spec(
    repo: str = "org/repo",
    repo_image_key: str = "image:tag",
    repo_directory: str = "/repo",
    container_name: str = "container-name",
    setup_script: str = "#!/bin/bash\necho setup",
) -> MagicMock:
    spec = MagicMock()
    spec.repo = repo
    spec.repo_image_key = repo_image_key
    spec.repo_directory = repo_directory
    spec.get_container_name.return_value = container_name
    spec.setup_script = setup_script
    return spec


def _make_logger() -> MagicMock:
    logger = MagicMock(spec=logging.Logger)
    logger.log_file = Path("/tmp/test.log")
    return logger


# ---------------------------------------------------------------------------
# TestExecutionBackend
# ---------------------------------------------------------------------------


class TestExecutionBackend:
    def test_local_value(self) -> None:
        from commit0.harness.execution_context import ExecutionBackend

        assert ExecutionBackend.LOCAL == "LOCAL"

    def test_modal_value(self) -> None:
        from commit0.harness.execution_context import ExecutionBackend

        assert ExecutionBackend.MODAL == "MODAL"

    def test_e2b_value(self) -> None:
        from commit0.harness.execution_context import ExecutionBackend

        assert ExecutionBackend.E2B == "E2B"

    def test_is_strenum_subclass(self) -> None:
        from commit0.harness.execution_context import ExecutionBackend

        assert issubclass(ExecutionBackend, StrEnum)


# ---------------------------------------------------------------------------
# TestExecutionContextABC
# ---------------------------------------------------------------------------


class TestExecutionContextABC:
    def test_cannot_instantiate_directly(self) -> None:
        from commit0.harness.execution_context import ExecutionContext

        with pytest.raises(TypeError):
            ExecutionContext(_make_spec(), _make_logger(), 300, 1, Path("/tmp"))

    def test_enter_returns_self(self) -> None:

        from commit0.harness.execution_context import ExecutionContext

        class _Concrete(ExecutionContext):
            def exec_run_with_timeout(self, command: str) -> tuple[str, bool, float]:
                return ("", False, 0.0)

            def __exit__(self, exctype, excinst, exctb):
                pass

        ctx = _Concrete(_make_spec(), _make_logger(), 300, 1, Path("/tmp"))
        assert ctx.__enter__() is ctx


# ---------------------------------------------------------------------------
# TestDockerContext
# ---------------------------------------------------------------------------


class TestDockerContext:
    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_init_creates_client_and_container(
        self, mock_docker, mock_proxy, mock_create, mock_copy
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_container = MagicMock()
        mock_create.return_value = mock_container

        spec = _make_spec()
        logger = _make_logger()
        ctx = Docker(spec, logger, 300, 2, Path("/logs"))

        mock_docker.from_env.assert_called_once()
        mock_create.assert_called_once_with(
            client=mock_client,
            image_name="image:tag",
            container_name="container-name",
            nano_cpus=2,
            logger=logger,
            environment=None,
        )
        assert ctx.client is mock_client
        assert ctx.container is mock_container

    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_init_starts_container(
        self, mock_docker, mock_proxy, mock_create, mock_copy
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_container = MagicMock()
        mock_create.return_value = mock_container

        Docker(_make_spec(), _make_logger(), 300, 1, Path("/logs"))
        mock_container.start.assert_called_once()

    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_init_copies_files_when_provided(
        self, mock_docker, mock_proxy, mock_create, mock_copy
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_container = MagicMock()
        mock_create.return_value = mock_container

        files = {
            "file1": {"src": Path("/src/a.py"), "dest": Path("/dest/a.py")},
            "file2": {"src": Path("/src/b.py"), "dest": Path("/dest/b.py")},
        }
        Docker(_make_spec(), _make_logger(), 300, 1, Path("/logs"), files_to_copy=files)

        assert mock_copy.call_count == 2
        mock_copy.assert_any_call(mock_container, Path("/src/a.py"), Path("/dest/a.py"))
        mock_copy.assert_any_call(mock_container, Path("/src/b.py"), Path("/dest/b.py"))

    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_init_no_files_to_copy(
        self, mock_docker, mock_proxy, mock_create, mock_copy
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_create.return_value = MagicMock()

        Docker(_make_spec(), _make_logger(), 300, 1, Path("/logs"), files_to_copy=None)
        mock_copy.assert_not_called()

    @patch(f"{MODULE}.copy_from_container")
    @patch(f"{MODULE}.exec_run_with_timeout")
    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_exec_run_delegates_to_docker_utils(
        self,
        mock_docker,
        mock_proxy,
        mock_create,
        mock_copy_to,
        mock_exec,
        mock_copy_from,
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_container = MagicMock()
        mock_create.return_value = mock_container
        mock_exec.return_value = ("output", False, 1.5)

        ctx = Docker(_make_spec(), _make_logger(), 300, 1, Path("/logs"))
        result = ctx.exec_run_with_timeout("pytest tests/")

        mock_exec.assert_called_once_with(mock_container, "pytest tests/", 300)
        assert result == ("output", False, 1.5)

    @patch(f"{MODULE}.copy_from_container")
    @patch(f"{MODULE}.exec_run_with_timeout")
    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_exec_run_collects_files_when_exist(
        self,
        mock_docker,
        mock_proxy,
        mock_create,
        mock_copy_to,
        mock_exec,
        mock_copy_from,
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_container = MagicMock()
        mock_create.return_value = mock_container
        mock_exec.return_value = ("output", False, 1.0)
        mock_container.exec_run.return_value = (0, b"")

        ctx = Docker(
            _make_spec(),
            _make_logger(),
            300,
            1,
            Path("/logs"),
            files_to_collect=["report.xml"],
        )
        ctx.exec_run_with_timeout("pytest")

        mock_container.exec_run.assert_called_once_with(
            "test -e /repo/report.xml", demux=True
        )
        mock_copy_from.assert_called_once_with(
            mock_container, Path("/repo/report.xml"), Path("/logs/report.xml")
        )

    @patch(f"{MODULE}.copy_from_container")
    @patch(f"{MODULE}.exec_run_with_timeout")
    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_exec_run_skips_missing_files(
        self,
        mock_docker,
        mock_proxy,
        mock_create,
        mock_copy_to,
        mock_exec,
        mock_copy_from,
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_container = MagicMock()
        mock_create.return_value = mock_container
        mock_exec.return_value = ("output", False, 1.0)
        mock_container.exec_run.return_value = (1, b"")

        ctx = Docker(
            _make_spec(),
            _make_logger(),
            300,
            1,
            Path("/logs"),
            files_to_collect=["missing.xml"],
        )
        ctx.exec_run_with_timeout("pytest")

        mock_container.exec_run.assert_called_once()
        mock_copy_from.assert_not_called()

    @patch(f"{MODULE}.copy_from_container")
    @patch(f"{MODULE}.exec_run_with_timeout")
    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_exec_run_no_files_to_collect(
        self,
        mock_docker,
        mock_proxy,
        mock_create,
        mock_copy_to,
        mock_exec,
        mock_copy_from,
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_container = MagicMock()
        mock_create.return_value = mock_container
        mock_exec.return_value = ("output", False, 1.0)

        ctx = Docker(
            _make_spec(),
            _make_logger(),
            300,
            1,
            Path("/logs"),
            files_to_collect=None,
        )
        ctx.exec_run_with_timeout("pytest")

        mock_container.exec_run.assert_not_called()
        mock_copy_from.assert_not_called()

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.cleanup_container")
    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value=None)
    @patch(f"{MODULE}.docker")
    def test_exit_cleans_up_container_and_logger(
        self,
        mock_docker,
        mock_proxy,
        mock_create,
        mock_copy_to,
        mock_cleanup,
        mock_close,
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_container = MagicMock()
        mock_create.return_value = mock_container
        logger = _make_logger()

        ctx = Docker(_make_spec(), logger, 300, 1, Path("/logs"))
        ctx.__exit__(None, None, None)

        mock_cleanup.assert_called_once_with(mock_client, mock_container, logger)
        mock_close.assert_called_once_with(logger)

    @patch(f"{MODULE}.copy_to_container")
    @patch(f"{MODULE}.create_container")
    @patch(f"{MODULE}.get_proxy_env", return_value={"HTTP_PROXY": "http://proxy:8080"})
    @patch(f"{MODULE}.docker")
    def test_proxy_env_passed_to_container(
        self, mock_docker, mock_proxy, mock_create, mock_copy
    ) -> None:
        from commit0.harness.execution_context import Docker

        mock_docker.from_env.return_value = MagicMock()
        mock_create.return_value = MagicMock()

        Docker(_make_spec(), _make_logger(), 300, 1, Path("/logs"))

        _, kwargs = mock_create.call_args
        assert kwargs["environment"] == {"HTTP_PROXY": "http://proxy:8080"}


# ---------------------------------------------------------------------------
# TestModalContext
# ---------------------------------------------------------------------------


class TestModalContext:
    @patch(f"{MODULE}.modal")
    def test_init_creates_app_and_image(self, mock_modal) -> None:
        from commit0.harness.execution_context import Modal

        mock_app = MagicMock()
        mock_modal.App.lookup.return_value = mock_app
        mock_image = MagicMock()
        mock_modal.Image.from_registry.return_value = mock_image

        ctx = Modal(_make_spec(), _make_logger(), 300, 1, Path("/logs"))

        mock_modal.App.lookup.assert_called_once_with("commit0", create_if_missing=True)
        mock_modal.Image.from_registry.assert_called_once_with(
            "wentingzhao/repo:v0", force_build=False
        )
        assert ctx.app is mock_app
        assert ctx.image is mock_image

    @patch(f"{MODULE}.modal")
    def test_init_adds_files_to_image(self, mock_modal) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_image = MagicMock()
        mock_image.add_local_file.return_value = mock_image
        mock_modal.Image.from_registry.return_value = mock_image

        files = {
            "f1": {"src": Path("/src/a.py"), "dest": Path("/dest/a.py")},
        }
        ctx = Modal(
            _make_spec(),
            _make_logger(),
            300,
            1,
            Path("/logs"),
            files_to_copy=files,
        )

        mock_image.add_local_file.assert_called_once_with(
            str(Path("/src/a.py")), str(Path("/dest/a.py"))
        )
        assert ctx.image is mock_image

    @patch(f"{MODULE}.modal")
    def test_init_image_name_lowercased(self, mock_modal) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_modal.Image.from_registry.return_value = MagicMock()

        spec = _make_spec(repo="org/MyUpperRepo")
        Modal(spec, _make_logger(), 300, 1, Path("/logs"))

        image_name_arg = mock_modal.Image.from_registry.call_args[0][0]
        assert image_name_arg == "wentingzhao/myupperrepo:v0"
        assert image_name_arg == image_name_arg.lower()

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.modal")
    def test_exec_creates_sandbox_with_volume(self, mock_modal, mock_time) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_image = MagicMock()
        mock_modal.Image.from_registry.return_value = mock_image

        mock_vol = MagicMock()
        mock_vol.__enter__ = MagicMock(return_value=mock_vol)
        mock_vol.__exit__ = MagicMock(return_value=False)
        mock_modal.Volume.ephemeral.return_value = mock_vol

        mock_sandbox = MagicMock()
        mock_sandbox.returncode = 0
        mock_stderr = MagicMock()
        mock_stderr.read.return_value = "stderr output"
        mock_sandbox.stderr = mock_stderr
        mock_modal.Sandbox.create.return_value = mock_sandbox

        mock_time.time.side_effect = [100.0, 105.0]

        ctx = Modal(_make_spec(), _make_logger(), 300, 4, Path("/logs"))
        result = ctx.exec_run_with_timeout("pytest tests/")

        mock_modal.Sandbox.create.assert_called_once()
        create_kwargs = mock_modal.Sandbox.create.call_args
        assert create_kwargs.kwargs["image"] is mock_image
        assert create_kwargs.kwargs["cpu"] == 4
        assert create_kwargs.kwargs["timeout"] == 300
        assert create_kwargs.kwargs["volumes"] == {"/vol": mock_vol}
        mock_sandbox.wait.assert_called_once()
        mock_sandbox.terminate.assert_called_once()
        assert result == ("stderr output", False, 5.0)

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.modal")
    def test_exec_timeout_detected(self, mock_modal, mock_time) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_modal.Image.from_registry.return_value = MagicMock()

        mock_vol = MagicMock()
        mock_vol.__enter__ = MagicMock(return_value=mock_vol)
        mock_vol.__exit__ = MagicMock(return_value=False)
        mock_modal.Volume.ephemeral.return_value = mock_vol

        mock_sandbox = MagicMock()
        mock_sandbox.returncode = 124
        mock_sandbox.stderr = MagicMock()
        mock_sandbox.stderr.read.return_value = ""
        mock_modal.Sandbox.create.return_value = mock_sandbox

        mock_time.time.side_effect = [0.0, 300.0]

        ctx = Modal(_make_spec(), _make_logger(), 300, 1, Path("/logs"))
        _, timed_out, _ = ctx.exec_run_with_timeout("sleep 999")

        assert timed_out is True

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.modal")
    def test_exec_normal_return(self, mock_modal, mock_time) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_modal.Image.from_registry.return_value = MagicMock()

        mock_vol = MagicMock()
        mock_vol.__enter__ = MagicMock(return_value=mock_vol)
        mock_vol.__exit__ = MagicMock(return_value=False)
        mock_modal.Volume.ephemeral.return_value = mock_vol

        mock_sandbox = MagicMock()
        mock_sandbox.returncode = 0
        mock_sandbox.stderr = MagicMock()
        mock_sandbox.stderr.read.return_value = "ok"
        mock_modal.Sandbox.create.return_value = mock_sandbox

        mock_time.time.side_effect = [0.0, 2.0]

        ctx = Modal(_make_spec(), _make_logger(), 300, 1, Path("/logs"))
        stderr, timed_out, elapsed = ctx.exec_run_with_timeout("echo hi")

        assert timed_out is False
        assert stderr == "ok"
        assert elapsed == 2.0

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.modal")
    def test_exec_collects_files_from_volume(self, mock_modal, mock_time) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_modal.Image.from_registry.return_value = MagicMock()

        mock_vol = MagicMock()
        mock_vol.__enter__ = MagicMock(return_value=mock_vol)
        mock_vol.__exit__ = MagicMock(return_value=False)
        mock_modal.Volume.ephemeral.return_value = mock_vol

        file_entry = MagicMock()
        file_entry.path = "report.xml"
        mock_vol.listdir.return_value = [file_entry]
        mock_vol.read_file.return_value = [b"<xml>data</xml>"]

        mock_sandbox = MagicMock()
        mock_sandbox.returncode = 0
        mock_sandbox.stderr = MagicMock()
        mock_sandbox.stderr.read.return_value = ""
        mock_modal.Sandbox.create.return_value = mock_sandbox

        mock_time.time.side_effect = [0.0, 1.0]

        log_dir = Path("/tmp/test_modal_logs")
        ctx = Modal(
            _make_spec(),
            _make_logger(),
            300,
            1,
            log_dir,
            files_to_collect=["report.xml"],
        )

        mock_file = MagicMock()
        mock_file.__enter__ = MagicMock(return_value=mock_file)
        mock_file.__exit__ = MagicMock(return_value=False)

        with patch.object(Path, "open", return_value=mock_file):
            ctx.exec_run_with_timeout("pytest")

        mock_vol.listdir.assert_called_once_with("")
        mock_vol.read_file.assert_called_once_with("report.xml")
        mock_file.write.assert_called_once_with(b"<xml>data</xml>")

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.modal")
    def test_exit_closes_logger(self, mock_modal, mock_close) -> None:
        from commit0.harness.execution_context import Modal

        mock_modal.App.lookup.return_value = MagicMock()
        mock_modal.Image.from_registry.return_value = MagicMock()

        logger = _make_logger()
        ctx = Modal(_make_spec(), logger, 300, 1, Path("/logs"))
        ctx.__exit__(None, None, None)

        mock_close.assert_called_once_with(logger)


# ---------------------------------------------------------------------------
# TestE2BContext
# ---------------------------------------------------------------------------


class TestE2BContext:
    @patch(f"{MODULE}.Sandbox")
    def test_init_creates_sandbox(self, mock_sandbox_cls) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb

        ctx = E2B(_make_spec(), _make_logger(), 300, 1, Path("/logs"))

        mock_sandbox_cls.assert_called_once_with(timeout=3600)
        assert ctx.sb is mock_sb

    @patch(f"{MODULE}.Sandbox")
    def test_init_runs_pip_upgrade_and_setup(self, mock_sandbox_cls) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb

        spec = _make_spec(setup_script="#!/bin/bash\necho setup")
        E2B(spec, _make_logger(), 300, 1, Path("/logs"))

        mock_sb.commands.run.assert_any_call("pip install --upgrade pip")
        mock_sb.files.write.assert_any_call("setup.sh", "#!/bin/bash\necho setup")
        mock_sb.commands.run.assert_any_call("bash setup.sh")

    @patch("builtins.open", new_callable=mock_open, read_data="file content")
    @patch(f"{MODULE}.Sandbox")
    def test_init_copies_files(self, mock_sandbox_cls, mock_file_open) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb

        files = {
            "f1": {"src": Path("/src/test.py"), "dest": Path("/dest/test.py")},
        }
        E2B(_make_spec(), _make_logger(), 300, 1, Path("/logs"), files_to_copy=files)

        mock_file_open.assert_called_once_with(Path("/src/test.py"), "r")
        mock_sb.files.write.assert_any_call("test.py", "file content")

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.Sandbox")
    def test_exec_runs_command(self, mock_sandbox_cls, mock_time) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb
        mock_result = MagicMock()
        mock_result.stderr = "error output"
        mock_sb.commands.run.return_value = mock_result
        mock_sb.is_running.return_value = True

        mock_time.time.side_effect = [0.0, 5.0]

        ctx = E2B(_make_spec(), _make_logger(), 300, 1, Path("/logs"))
        mock_sb.commands.run.reset_mock()

        stderr, timed_out, elapsed = ctx.exec_run_with_timeout("pytest tests/")

        mock_sb.commands.run.assert_called_once_with("pytest tests/", timeout=300)
        assert stderr == "error output"
        assert timed_out is False
        assert elapsed == 5.0

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.Sandbox")
    def test_exec_collects_files(self, mock_sandbox_cls, mock_time) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb
        mock_result = MagicMock()
        mock_result.stderr = ""
        mock_sb.commands.run.return_value = mock_result
        mock_sb.is_running.return_value = True
        mock_sb.files.read.return_value = "<xml>report</xml>"

        mock_time.time.side_effect = [0.0, 1.0]

        log_dir = Path("/tmp/test_e2b_logs")
        ctx = E2B(
            _make_spec(),
            _make_logger(),
            300,
            1,
            log_dir,
            files_to_collect=["report.xml"],
        )

        mock_file = MagicMock()
        mock_file.__enter__ = MagicMock(return_value=mock_file)
        mock_file.__exit__ = MagicMock(return_value=False)

        with patch.object(Path, "open", return_value=mock_file):
            ctx.exec_run_with_timeout("pytest")

        mock_sb.files.read.assert_called_once_with("testbed/report.xml")
        mock_file.write.assert_called_once_with("<xml>report</xml>")

    @patch(f"{MODULE}.time")
    @patch(f"{MODULE}.Sandbox")
    def test_exec_timeout_detected(self, mock_sandbox_cls, mock_time) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb
        mock_result = MagicMock()
        mock_result.stderr = ""
        mock_sb.commands.run.return_value = mock_result
        mock_sb.is_running.return_value = False

        mock_time.time.side_effect = [0.0, 300.0]

        ctx = E2B(_make_spec(), _make_logger(), 300, 1, Path("/logs"))
        _, timed_out, _ = ctx.exec_run_with_timeout("sleep 999")

        assert timed_out is True

    @patch(f"{MODULE}.close_logger")
    @patch(f"{MODULE}.Sandbox")
    def test_exit_kills_sandbox_and_closes_logger(
        self, mock_sandbox_cls, mock_close
    ) -> None:
        from commit0.harness.execution_context import E2B

        mock_sb = MagicMock()
        mock_sandbox_cls.return_value = mock_sb
        logger = _make_logger()

        ctx = E2B(_make_spec(), logger, 300, 1, Path("/logs"))
        ctx.__exit__(None, None, None)

        mock_sb.kill.assert_called_once()
        mock_close.assert_called_once_with(logger)
