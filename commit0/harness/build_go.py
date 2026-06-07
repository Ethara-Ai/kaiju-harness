"""Build Docker images for Go repos in the commit0 Go pipeline.

Mirrors commit0/harness/build.py but uses Go-specific specs and health checks.
Does NOT modify the original build.py — this is a parallel pipeline file.
"""

import logging
import sys
from typing import Iterator

import docker

from commit0.harness.constants_go import GoRepoInstance, GO_SPLIT
from commit0.harness.split_utils import resolve_split
from commit0.harness.docker_build import build_repo_images
from commit0.harness.health_check_go import run_go_health_checks
from commit0.harness.spec_go import make_go_spec
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)


def _get_go_specs(dataset: list, split: str) -> list:
    """Build Commit0GoSpec instances from the dataset, filtered by split."""
    allowed_repos = set(resolve_split(split, dataset, curated=GO_SPLIT))
    specs = []
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in allowed_repos:
            continue
        spec = make_go_spec(example, absolute=True)
        specs.append(spec)
    return specs


def main(
    dataset_name: str,
    dataset_split: str,
    split: str,
    num_workers: int,
    verbose: int,
) -> None:
    """Build Go repo Docker images.

    Parameters
    ----------
    dataset_name : str
        Name or path of the Go dataset (e.g. a local JSON file).
    dataset_split : str
        HuggingFace split name, or "test" for local JSON files.
    split : str
        Repo split key from GO_SPLIT, or "all".
    num_workers : int
        Maximum parallel Docker builds.
    verbose : int
        Verbosity level.

    """
    dataset: Iterator[GoRepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    dataset_list = list(dataset) if not isinstance(dataset, list) else dataset

    specs = _get_go_specs(dataset_list, split)
    if not specs:
        logger.error(
            "No Go repos matched split=%r. Check GO_SPLIT and dataset.",
            split,
        )
        return

    logger.info("Building %d Go repo image(s)", len(specs))

    client = docker.from_env()
    successful, failed = build_repo_images(
        client, specs, "commit0", num_workers, verbose
    )

    health_failures: list[str] = []
    for spec in specs:
        image_key = spec.repo_image_key
        if image_key in failed:
            continue
        results = run_go_health_checks(client, image_key)
        for passed, check_name, detail in results:
            if not passed:
                logger.warning(
                    "Health check FAILED [%s] for %s: %s (non-blocking)",
                    check_name,
                    image_key,
                    detail,
                )
                health_failures.append(image_key)
            else:
                logger.info(
                    "Health check passed [%s] for %s: %s",
                    check_name,
                    image_key,
                    detail,
                )

    if failed:
        logger.error(
            "Failed to build %d image(s): %s",
            len(failed),
            list(failed),
        )
        sys.exit(1)
    if health_failures:
        logger.warning(
            "%d image(s) built but had health check warnings: %s",
            len(health_failures),
            health_failures,
        )


__all__: list[str] = []
