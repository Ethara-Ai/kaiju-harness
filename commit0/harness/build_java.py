"""Build Java Docker images — base and per-repo."""
import docker
from commit0.harness.docker_utils import docker_client  # context-aware client (B10)
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional
from commit0.harness.docker_build import build_image, _resolve_mitm_ca_cert
from commit0.harness.docker_utils import get_docker_platform
from commit0.harness.spec_java import make_java_spec
from commit0.harness.constants import base_image_build_dir, repo_image_build_dir
from commit0.harness.constants_java import (
    JAVA_BASE_IMAGE_PREFIX, SUPPORTED_JAVA_VERSIONS, JAVA_SPLIT
)

logger = logging.getLogger(__name__)


def _run_java_health_check(
    client: docker.DockerClient,
    image_key: str,
    java_version: str,
) -> list[tuple[bool, str, str]]:
    """Run a health check on a Java Docker image by verifying java -version inside the container."""
    results: list[tuple[bool, str, str]] = []
    try:
        output = client.containers.run(
            image_key,
            "java -version",
            remove=True,
            stderr=True,
        )
        decoded = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        passed = java_version in decoded
        results.append((passed, "java_version", decoded.split("\n")[0].strip()))
    except Exception as e:
        results.append((False, "java_version", str(e)))
    return results


def _scripts_list_to_dict(scripts: List[str]) -> dict:
    return {"setup.sh": "\n".join(["#!/bin/bash", "set -euxo pipefail"] + scripts + [""])}


def build_java_base_images(
    java_versions: Optional[List[str]] = None,
    nocache: bool = False,
) -> None:
    client = docker_client()
    platform = get_docker_platform()
    mitm_ca_cert = _resolve_mitm_ca_cert()
    versions = java_versions or list(SUPPORTED_JAVA_VERSIONS)
    health_failures: list[str] = []
    for version in versions:
        tag = f"{JAVA_BASE_IMAGE_PREFIX}{version}:latest"
        spec = make_java_spec({"java_version": version})
        dockerfile_content = spec.base_dockerfile
        build_dir = base_image_build_dir() / tag.replace(":", "__")
        build_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Building Java {version} base image: {tag}")
        build_image(
            image_name=tag,
            setup_scripts={},
            dockerfile=dockerfile_content,
            platform=platform,
            client=client,
            build_dir=build_dir,
            nocache=nocache,
            mitm_ca_cert=mitm_ca_cert,
        )
        for passed, check_name, detail in _run_java_health_check(client, tag, version):
            if not passed:
                logger.warning(
                    "Health check FAILED [%s] for %s: %s (non-blocking)",
                    check_name, tag, detail,
                )
                health_failures.append(tag)
            else:
                logger.info("Health check passed [%s] for %s: %s", check_name, tag, detail)

    if health_failures:
        logger.warning(
            "%d base image(s) built but had health check warnings: %s",
            len(health_failures), health_failures,
        )


def _build_single_repo(
    repo_name: str,
    instance: dict,
    platform: str,
    nocache: bool,
    mitm_ca_cert: Optional[Path],
) -> str:
    client = docker_client()
    spec = make_java_spec(instance)
    # Tag the image with the SPEC's repo_image_key — the exact name the runner
    # (run_pipeline_containerized) looks up as the agent-image FROM base. Deriving
    # it from spec (not from the raw dataset key) guarantees build and runner agree
    # even if a repo's instance_id basename differs from its repo basename, since
    # spec.repo_image_key keys off instance_id-or-repo. Mirrors Go, which builds
    # via spec.repo_image_key.
    tag = spec.repo_image_key
    dockerfile_content = spec.repo_dockerfile
    setup_scripts = _scripts_list_to_dict(spec.make_repo_script_list())
    build_dir = repo_image_build_dir() / tag.replace(":", "__")
    build_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Building Java repo image: {tag}")
    build_image(
        image_name=tag,
        setup_scripts=setup_scripts,
        dockerfile=dockerfile_content,
        platform=platform,
        client=client,
        build_dir=build_dir,
        nocache=nocache,
        mitm_ca_cert=mitm_ca_cert,
    )
    return tag


def build_java_repo_images(
    repo_names: Optional[List[str]] = None,
    dataset: Optional[dict] = None,
    nocache: bool = False,
    num_workers: int = 4,
) -> None:
    platform = get_docker_platform()
    mitm_ca_cert = _resolve_mitm_ca_cert()
    # Resolve the repo list. Precedence:
    #   1. explicit repo_names (from `cli_java build --repo <name>`);
    #   2. every repo in the loaded dataset — this is the "all" behaviour and
    #      mirrors Go's resolve_split("all", dataset) (derive_all_split), so a
    #      plain `cli_java build` builds a per-repo image for each dataset entry.
    #      JAVA_SPLIT has NO static "all" key (it is derived from the dataset),
    #      so `JAVA_SPLIT.get("all", [])` would return [] and build NOTHING,
    #      leaving the runner's agent-image FROM (commit0-java-<repo>:latest)
    #      with no base image -> "pull access denied ... repository does not exist".
    #   3. only if no dataset was supplied, fall back to the curated lite split.
    if repo_names:
        repos = repo_names
    elif dataset:
        repos = list(dataset.keys())
    else:
        repos = JAVA_SPLIT.get("lite", [])

    tasks = []
    for repo_name in repos:
        if dataset is not None and repo_name in dataset:
            instance = dataset[repo_name]
        else:
            instance = {
                "repo": repo_name,
                "instance_id": repo_name,
                "base_commit": "HEAD",
                "reference_commit": "HEAD",
                "setup": {},
                "test": {},
                "src_dir": ".",
            }
            logger.warning(
                f"No dataset entry for '{repo_name}' — using default instance. "
                "Repo-specific java_version/build_system will not be applied."
            )
        tasks.append((repo_name, instance))

    if len(tasks) <= 1:
        for repo_name, instance in tasks:
            _build_single_repo(repo_name, instance, platform, nocache, mitm_ca_cert)
        return

    failed = []
    with ThreadPoolExecutor(max_workers=min(num_workers, len(tasks))) as executor:
        futures = {
            executor.submit(
                _build_single_repo, repo_name, instance, platform, nocache, mitm_ca_cert
            ): repo_name
            for repo_name, instance in tasks
        }
        for future in as_completed(futures):
            repo_name = futures[future]
            try:
                tag = future.result()
                logger.info(f"Successfully built: {tag}")
            except Exception as exc:
                logger.error(f"Failed to build image for {repo_name}: {exc}")
                failed.append(repo_name)

    if failed:
        raise RuntimeError(f"Failed to build images for: {failed}")
