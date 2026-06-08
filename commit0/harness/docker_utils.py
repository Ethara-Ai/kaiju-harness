from __future__ import annotations

import docker
import logging
import os
import platform as platform_mod
import secrets

import tarfile
import threading
import time
import traceback
from pathlib import Path
from io import BytesIO
from typing import Optional, List

import docker.errors
from docker.models.containers import Container

logger = logging.getLogger(__name__)

HEREDOC_DELIMITER = "EOF_1399519320"


def get_docker_platform() -> str:
    """Return the Docker platform string for the current machine architecture."""
    machine = platform_mod.machine()
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, "amd64"
    )
    return f"linux/{arch}"


def copy_to_container(container: Container, src: Path, dst: Path) -> None:
    """Copy a file from local to a docker container

    Args:
    ----
        container (Container): Docker container to copy to
        src (Path): Source file path
        dst (Path): Destination file path in the container

    """
    # Check if destination path is valid
    if os.path.dirname(dst) == "":
        raise ValueError(
            f"Destination path parent directory cannot be empty!, dst: {dst}"
        )

    # temporary tar file
    tar_path = src.with_suffix(".tar")
    with tarfile.open(tar_path, "w") as tar:
        tar.add(src, arcname=src.name)

    # get bytes for put_archive cmd
    with open(tar_path, "rb") as tar_file:
        data = tar_file.read()

    # Make directory if necessary
    logger.debug("Container exec: mkdir -p %s", dst.parent)
    container.exec_run(f"mkdir -p {dst.parent}")

    # Send tar file to container and extract
    container.put_archive(os.path.dirname(dst), data)
    logger.debug("Container exec: tar extract %s", dst)
    container.exec_run(f"tar -xf {dst}.tar -C {dst.parent}")

    # clean up in locally and in container
    tar_path.unlink()
    logger.debug("Container exec: rm %s.tar", dst)
    container.exec_run(f"rm {dst}.tar")


def copy_from_container(container: Container, src: Path, dst: Path) -> None:
    """Copy a file from a docker container to local

    Args:
    ----
        container (Container): Docker container to copy from
        src (Path): Source file path in the container
        dst (Path): Destination file path locally

    """
    if not isinstance(src, Path):
        src = Path(src)

    if not isinstance(dst, Path):
        dst = Path(dst)

    # Ensure destination directory exists
    if not dst.parent.exists():
        os.makedirs(dst.parent)

    # Copy the file out of the container
    stream, stat = container.get_archive(str(src))

    # Create a temporary tar file
    tar_stream = BytesIO()
    for chunk in stream:
        tar_stream.write(chunk)
    tar_stream.seek(0)

    with tarfile.open(fileobj=tar_stream, mode="r") as tar:
        # Extract file from tar stream
        def is_within_directory(directory: str, target: str) -> bool:
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)

            try:
                common = os.path.commonpath([abs_directory, abs_target])
                return common == abs_directory
            except ValueError:
                return False

        def safe_extract(
            tar: tarfile.TarFile,
            path: str = ".",
            members: Optional[List[tarfile.TarInfo]] = None,
            *,
            numeric_owner: bool = False,
        ) -> None:
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")

            tar.extractall(path, members, numeric_owner=numeric_owner)

        safe_extract(tar, path=str(dst.parent))

    # Move the extracted file to desired dst path if tar extraction gives src.name
    extracted_file_path = dst.parent / src.name
    if extracted_file_path != dst:
        extracted_file_path.rename(dst)


def write_to_container(container: Container, data: str, dst: Path) -> None:
    """Write a string to a file in a docker container"""
    heredoc_delim = f"EOF_{secrets.token_hex(8)}"
    command = f"cat <<'{heredoc_delim}' > {dst}\n{data}\n{heredoc_delim}"
    container.exec_run(command)


def cleanup_container(
    client: docker.DockerClient,
    container: Container,
    logger: logging.Logger,
) -> None:
    """Stop and remove a Docker container.
    Performs this forcefully if the container cannot be stopped with the python API.

    Args:
    ----
        client (docker.DockerClient): Docker client.
        container (docker.Container): Container to remove.
        logger (logging.Logger): Logger instance or log level as string for logging container creation messages.

    """
    if not container:
        return


    # Attempt to stop the container
    try:
        if container:
            logger.info(f"Attempting to stop container {container.name}...")
            container.kill()
    except Exception as e:
        logger.error(
            f"Failed to stop container {container.name}: {e}. Trying to forcefully kill..."
        )
        try:
            logger.info(
                f"Forcefully killing container {container.name} via Docker API..."
            )
            container.kill(signal="SIGKILL")
        except Exception as e2:
            raise Exception(
                f"Failed to forcefully kill container {container.name}: {e2}\n"
                f"{traceback.format_exc()}"
            ) from e2

    # Attempt to remove the container
    try:
        logger.info(f"Attempting to remove container {container.name}...")
        container.remove(force=True)
        logger.info(f"Container {container.name} removed.")
    except Exception as e:
        raise Exception(
            f"Failed to remove container {container.name}: {e}\n"
            f"{traceback.format_exc()}"
        ) from e


def image_exists_locally(
    client: docker.DockerClient, image_name: str, tag: str, logger: logging.Logger
) -> bool:
    """Check if a Docker image exists locally.

    Args:
    ----
    client (docker.DockerClient): Docker client instance.
    image_name (str): The name of the Docker image.
    tag (str, optional): Tag of the Docker image.
    logger (logging.Logger): Logger instance.

    Returns:
    -------
    bool: True if the image exists locally, False otherwise.

    """
    images = client.images.list(name=image_name)
    for image in images:
        if f"{image_name}:{tag}" in image.tags:
            logger.info(f"Using {image_name}:{tag} found locally.")
            return True
    logger.info(f"{image_name}:{tag} cannot be found locally")
    return False


def pull_image_from_docker_hub(
    client: docker.DockerClient, image_name: str, tag: str, logger: logging.Logger
) -> None:
    """Pull a Docker image from Docker Hub.

    Args:
    ----
    client (docker.DockerClient): Docker client instance.
    image_name (str): The name of the Docker image.
    tag (str, optional): Tag of the Docker image.
    logger (logging.Logger): Logger instance.

    Returns:
    -------
    docker.models.images.Image: The pulled Docker image.

    Raises:
    ------
    docker.errors.ImageNotFound: If the image is not found on Docker Hub.
    docker.errors.APIError: If there's an issue with the Docker API during the pull.

    """
    try:
        client.images.pull(image_name, tag=tag)
        logger.info(f"Loaded {image_name}:{tag} from Docker Hub.")
    except docker.errors.ImageNotFound as e:
        raise Exception(f"Image {image_name}:{tag} not found on Docker Hub.") from e
    except docker.errors.APIError as e:
        raise Exception(f"Error pulling image: {e}") from e


def sandbox_hardening_kwargs() -> dict:
    """Opt-in Docker hardening for eval containers (S-001).

    OFF by default — returns ``{}`` so container creation is unchanged. Enable
    with ``COMMIT0_SANDBOX_HARDEN=1`` to drop all Linux capabilities, forbid
    privilege escalation, and cap pids/memory. Network is left untouched unless
    ``COMMIT0_SANDBOX_NETWORK`` is set (e.g. ``none``), because many eval/install
    steps legitimately need network access.
    """
    if os.environ.get("COMMIT0_SANDBOX_HARDEN", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return {}
    kwargs: dict = {
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": int(os.environ.get("COMMIT0_SANDBOX_PIDS_LIMIT", "2048")),
        "mem_limit": os.environ.get("COMMIT0_SANDBOX_MEM_LIMIT", "4g"),
    }
    network = os.environ.get("COMMIT0_SANDBOX_NETWORK", "").strip()
    if network:
        kwargs["network_mode"] = network
    return kwargs


def create_container(
    client: docker.DockerClient,
    image_name: str,
    container_name: str,
    logger: logging.Logger,
    user: Optional[str] = None,
    command: Optional[str] = "tail -f /dev/null",
    nano_cpus: Optional[int] = None,
    environment: Optional[dict[str, str]] = None,
    sandbox_hardening: Optional[dict] = None,
) -> Container:
    """Start a Docker container using the specified image.

    Args:
    ----
    client (docker.DockerClient): Docker client.
    image_name (str): The name of the Docker image.
    container_name (str): Name for the Docker container.
    logger (logging.Logger): Logger instance or log level as string for logging container creation messages.
    user (str, option): Log in as which user. Defaults to None.
    command (str, optional): Command to run in the container. Defaults to None.
    nano_cpus (int, optional): The number of CPUs for the container. Defaults to None.
    environment (dict, optional): Environment variables to inject into the container runtime.

    Returns:
    -------
    docker.models.containers.Container: The started Docker container.

    Raises:
    ------
    docker.errors.APIError: If there's an error interacting with the Docker API.
    Exception: For other general errors.

    """
    image, tag = image_name.rsplit(":", 1)
    if not image_exists_locally(client, image, tag, logger):
        pull_image_from_docker_hub(client, image, tag, logger)

    container = None
    try:
        logger.info(f"Creating container for {image_name}...")
        run_kwargs = dict(
            image=image_name,
            name=container_name,
            user=user,
            command=command,
            nano_cpus=nano_cpus,
            environment=environment,
            detach=True,
        )
        # Opt-in hardening flags only (default: none -> identical to prior behavior).
        if sandbox_hardening:
            run_kwargs.update(sandbox_hardening)
            logger.info("Sandbox hardening enabled: %s", sorted(sandbox_hardening))
        container = client.containers.run(**run_kwargs)
        logger.info(f"Container for {image_name} created: {container.id}")
        return container
    except Exception as e:
        # If an error occurs, clean up the container and raise an exception
        logger.error(f"Error creating container for {image_name}: {e}")
        logger.info(traceback.format_exc())
        if container is not None:
            cleanup_container(client, container, logger)
        raise


def exec_run_with_timeout(
    container: Container, cmd: str, timeout: Optional[int] = 60
) -> tuple[str, bool, float]:
    """Run a command in a container with a timeout.

    Args:
    ----
        container (Container): Container to run the command in.
        cmd (str): Command to run.
        timeout (int): Timeout in seconds.

    """
    # Local variables to store the result of executing the command
    exec_result = ""
    _chunks: list[str] = []
    exec_id = None
    timed_out = False

    # Wrapper function to run the command
    def run_command() -> None:
        nonlocal exec_id
        try:
            exec_id = container.client.api.exec_create(container=container.id, cmd=cmd)[  # pyright: ignore
                "Id"
            ]
            exec_stream = container.client.api.exec_start(exec_id=exec_id, stream=True)  # pyright: ignore
            for chunk in exec_stream:
                _chunks.append(chunk.decode("utf-8", errors="replace"))
        except docker.errors.APIError as e:
            raise Exception(
                f"Container {container.id} cannot execute {cmd}.\n{str(e)}"
            ) from e

    # Start the command in a separate thread
    thread = threading.Thread(target=run_command)
    start_time = time.time()
    thread.start()
    thread.join(timeout)

    # If the thread is still alive, the command timed out
    if thread.is_alive():
        if exec_id is not None:
            exec_pid = container.client.api.exec_inspect(exec_id=exec_id)["Pid"]  # pyright: ignore
            container.exec_run(f"kill -TERM {exec_pid}", detach=True)
        timed_out = True
    end_time = time.time()
    exec_result = "".join(_chunks)
    return exec_result, timed_out, end_time - start_time


__all__ = []
