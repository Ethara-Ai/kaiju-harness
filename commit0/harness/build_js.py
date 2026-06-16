from __future__ import annotations

import logging
import sys

from commit0.harness.constants_js import JS_SPLIT
from commit0.harness.docker_build import build_repo_images
from commit0.harness.health_check_js import run_js_health_checks
from commit0.harness.spec_js import Commit0JsSpec, make_js_spec
from commit0.harness.split_utils import resolve_split
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)


def main(
    dataset_name: str,
    dataset_split: str,
    split: str,
    num_workers: int,
    verbose: int,
) -> None:
    dataset = load_dataset_from_config(dataset_name, split=dataset_split)

    specs: list[Commit0JsSpec] = []
    allowed_repos = set(resolve_split(split, dataset, curated=JS_SPLIT))

    for example in dataset:
        repo_full = (
            example.get("repo", "") if isinstance(example, dict) else example.repo
        )
        if repo_full.split("/")[-1] not in allowed_repos:
            continue
        specs.append(make_js_spec(example, absolute=True))

    if not specs:
        logger.warning("No JS repos matched split '%s'. Nothing to build.", split)
        return

    import docker

    logger.info("Building %d JS repo image(s) for split '%s'", len(specs), split)
    client = docker.from_env()

    health_failures: list[str] = []
    try:
        successful, failed = build_repo_images(
            client, specs, "commit0", num_workers, verbose
        )

        for spec in specs:
            image_key = spec.repo_image_key
            if image_key in failed:
                continue
            setup = spec._get_setup_dict()
            nv_raw = setup.get("node_version")
            node_version_arg = str(nv_raw) if nv_raw is not None else None
            results = run_js_health_checks(
                client,
                image_key,
                node_version=node_version_arg,
                packages=setup.get("packages"),
            )
            for passed, check_name, detail in results:
                if not passed:
                    logger.warning(
                        "Health check FAILED [%s] for %s: %s (non-blocking — image may still be functional)",
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
    finally:
        client.close()

    if failed:
        logger.error("Failed to build %d image(s): %s", len(failed), list(failed))
        sys.exit(1)
    if health_failures:
        logger.warning(
            "%d image(s) built but had health check warnings: %s",
            len(health_failures),
            health_failures,
        )
