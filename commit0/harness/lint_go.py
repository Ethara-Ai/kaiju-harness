"""Lint Go repos using goimports, staticcheck, and go vet.

Runs linters inside the Docker container where Go toolchain is available.
Does NOT modify the original lint.py.
"""

import logging
import sys
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


def main(
    dataset_name: str,
    dataset_split: str,
    repo_or_repo_dir: str,
    base_dir: str,
    timeout: int = 300,
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
        logger.error("Cannot connect to Docker: %s", e)
        sys.exit(1)

    lint_commands = [
        "echo '=== goimports ===' && goimports -d . 2>&1 || true",
        "echo '=== staticcheck ===' && staticcheck ./... 2>&1 || true",
        "echo '=== go vet ===' && go vet ./... 2>&1 || true",
    ]

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
