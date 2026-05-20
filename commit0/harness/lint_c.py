"""Lint C repos using clang-tidy and cppcheck.

Runs linters inside the Docker container where the C toolchain is available.
Does NOT modify the original lint.py. Advisory: lint output is informational;
exit codes are surfaced but never block downstream stages.
"""

import logging
import sys
from typing import Iterator

import docker
import docker.errors

from commit0.harness.constants_c import CRepoInstance, C_SPLIT
from commit0.harness.spec_c import make_c_spec
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)

del C_SPLIT  # imported for cli_c parity; consumed by callers via constants_c


def _run_in_container(
    client: docker.DockerClient,
    image_key: str,
    commands: list[str],
    workdir: str = "/testbed",
    timeout: int = 300,
) -> tuple[int, str]:
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
    """Lint a C repo using clang-tidy and cppcheck. Advisory."""
    dataset: Iterator[CRepoInstance] = load_dataset_from_config(
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
        logger.error("No matching C repo found for %r", repo_or_repo_dir)
        sys.exit(1)

    spec = make_c_spec(example, absolute=True)

    try:
        client = docker.from_env()
        client.images.get(spec.repo_image_key)
    except docker.errors.ImageNotFound:
        logger.error(
            "Docker image %s not found. Run C build first.",
            spec.repo_image_key,
        )
        sys.exit(1)
    except docker.errors.DockerException as e:
        logger.error("Cannot connect to Docker: %s", e)
        sys.exit(1)

    # Use compile_commands.json if present so clang-tidy resolves -I and -D flags.
    lint_commands = [
        (
            "if [ ! -f compile_commands.json ] && [ ! -f build/compile_commands.json ]; then "
            "cmake -S . -B build -G Ninja "
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON 2>/dev/null || true; "
            "fi"
        ),
        (
            "echo '=== clang-tidy ===' && "
            "find . -name '*.c' -not -path './build/*' -not -path './tests/*' "
            "-not -path './test/*' -not -path './third_party/*' -not -path './vendor/*' "
            "| head -100 | xargs -r clang-tidy --quiet "
            "-p build 2>&1 || true"
        ),
        (
            "echo '=== cppcheck ===' && "
            "cppcheck --enable=warning,performance,portability "
            "--error-exitcode=0 --quiet . 2>&1 || true"
        ),
    ]

    logger.info("Running C linters on %s (image: %s)", repo_name, spec.repo_image_key)

    exit_code, output = _run_in_container(
        client,
        spec.repo_image_key,
        lint_commands,
        workdir="/testbed",
        timeout=timeout,
    )

    print(output)

    if exit_code != 0:
        logger.warning("C lint completed with issues (exit code %d) — advisory", exit_code)
    else:
        logger.info("C lint completed successfully")

    sys.exit(exit_code)


__all__: list[str] = []
