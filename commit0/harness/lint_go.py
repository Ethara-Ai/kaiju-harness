"""Lint Go repos using goimports, staticcheck, and go vet.

Runs linters inside a Docker container OR directly in the local environment
Does NOT modify the original lint.py.
"""

import logging
import sys
import os
import subprocess
from typing import Iterator

import docker
import docker.errors

from commit0.harness.constants_go import GoRepoInstance
from commit0.harness.spec_go import make_go_spec
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)


def _run_in_container(
    client: docker.DockerClient,
    image_key: str,
    commands: list[str],
    workdir: str = "/testbed",
    timeout: int = 300,
) -> tuple[int, str]:
    """Run commands inside a Docker container and return (exit_code, output)."""
    full_cmd = " && ".join(commands)
    try:
        container = client.containers.run(
            image_key,
            command=["bash", "-c", full_cmd],
            working_dir=workdir,
            detach=True,
            remove=False,
        )
        result = container.wait(timeout=timeout)
        logs = container.logs(stdout=True, stderr=True).decode(
            "utf-8", errors="replace"
        )
        exit_code = result.get("StatusCode", 1)
        try:
            container.remove(force=True)
        except Exception:
            pass
        return exit_code, logs
    except docker.errors.ContainerError as e:
        return 1, str(e)
    except Exception as e:
        logger.error("Error running lint container: %s", e)
        return 1, str(e)


def _run_locally(
    commands: list[str],
    cwd: str,
    timeout: int = 300,
) -> tuple[int, str]:
    """Run lint commands directly in the current shell (no Docker).

    Agent-container pipelines don't mount /var/run/docker.sock; spawning a
    lint container from inside the agent container fails at docker.from_env().
    The Go toolchain is already installed in the agent env, so run the linters
    directly — parity with lint_js.py and lint.py which never touch Docker.
    """
    outputs: list[str] = []
    max_rc = 0
    for cmd in commands:
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            outputs.append(f"[TIMEOUT after {timeout}s running: {cmd}]")
            max_rc = 124
            continue
        except OSError as e:
            outputs.append(f"[OSError running {cmd}: {e}]")
            max_rc = max(max_rc, 1)
            continue
        if result.stdout:
            outputs.append(result.stdout.rstrip("\n"))
        if result.stderr:
            outputs.append(result.stderr.rstrip("\n"))
        if result.returncode > max_rc:
            max_rc = result.returncode
    return max_rc, "\n".join(outputs)


def main(
    dataset_name: str,
    dataset_split: str,
    repo_or_repo_dir: str,
    base_dir: str,
    timeout: int = 300,
    backend: str = "local",
) -> None:
    """Lint a Go repo using goimports, staticcheck, and go vet.

    Parameters
    ----------
    dataset_name : str
        Name or path of Go dataset.
    dataset_split : str
        HuggingFace split or "test".
    repo_or_repo_dir : str
        Repo name or path to repo directory.
    base_dir : str
        Local directory containing cloned repos.
    timeout : int
        Timeout in seconds for linting.

    """
    dataset: Iterator[GoRepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore

    example = None
    repo_name = None
    for ex in dataset:
        name = ex["repo"].split("/")[-1]
        if repo_or_repo_dir.rstrip("/").endswith(name) or name == repo_or_repo_dir:
            example = ex
            repo_name = name
            break

    if example is None or repo_name is None:
        logger.error("No matching Go repo found for %r", repo_or_repo_dir)
        sys.exit(1)

    spec = make_go_spec(example, absolute=True)

    lint_commands = [
        "echo '=== goimports ===' && goimports -d . 2>&1 || true",
        "echo '=== staticcheck ===' && staticcheck ./... 2>&1 || true",
        "echo '=== go vet ===' && go vet ./... 2>&1 || true",
    ]

    if backend == "local_inplace":
        cwd = os.path.join(base_dir, repo_name)
        logger.info(
            "Running Go linters on %s locally (backend=local_inplace, cwd=%s)",
            repo_name,
            cwd,
        )
        exit_code, output = _run_locally(lint_commands, cwd=cwd, timeout=timeout)
    else:
        try:
            client = docker.from_env()
            client.images.get(spec.repo_image_key)
        except docker.errors.ImageNotFound:
            logger.error(
                "Docker image %s not found. Run Go build first.",
                spec.repo_image_key,
            )
            sys.exit(1)
        except docker.errors.DockerException as e:
            logger.error(
                "Cannot connect to Docker: %s (pass --backend local_inplace to "
                "run linters directly without Docker)", e,
            )
            sys.exit(1)

        logger.info("Running Go linters on %s (image: %s)", repo_name, spec.repo_image_key)
        exit_code, output = _run_in_container(
            client,
            spec.repo_image_key,
            lint_commands,
            workdir="/testbed",
            timeout=timeout,
        )

    print(output)

    if exit_code != 0:
        logger.warning("Go lint completed with issues (exit code %d)", exit_code)
    else:
        logger.info("Go lint completed successfully")

    sys.exit(exit_code)


__all__: list[str] = []
