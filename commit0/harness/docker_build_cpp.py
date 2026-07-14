"""C++ Docker image build orchestrator.

Mirrors docker_build.py but uses CppSpec and separate C++ Dockerfiles.
Reuses build_image() from docker_build.py since it is language-agnostic.
"""

import logging
from pathlib import Path
from typing import Any, Optional

import docker
import docker.errors
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

from commit0.harness.constants import (
    BASE_IMAGE_BUILD_DIR,
    REPO_IMAGE_BUILD_DIR,
    OCI_IMAGE_DIR,
)
from commit0.harness.docker_build import (
    BuildImageError,
    build_image,
    get_proxy_env,
    _multiarch_builder_args,
    _resolve_mitm_ca_cert,
    _get_image_created_timestamp,
)
from commit0.harness.dockerfiles.__init__cpp import get_dockerfile_base_cpp
from commit0.harness.spec_cpp import get_cpp_specs_from_dataset

_logger = logging.getLogger(__name__)


def build_base_images_cpp(
    client: docker.DockerClient,
    mitm_ca_cert: Optional[Path] = None,
) -> None:
    """Build the single C++ base image if it does not already exist."""
    image_name = "commit0.base.cpp:latest"
    dockerfile = get_dockerfile_base_cpp()

    oci_key = image_name.replace(":", "__")
    oci_tar_path = OCI_IMAGE_DIR / oci_key / f"{oci_key}.tar"

    daemon_exists = False
    try:
        client.images.get(image_name)
        daemon_exists = True
    except docker.errors.ImageNotFound:
        pass

    if daemon_exists and oci_tar_path.exists():
        if mitm_ca_cert:
            _logger.warning(
                "Base image %s already exists but MITM CA cert "
                "was found at %s. If the cert was added after the base "
                "image was built, delete the old image: docker rmi %s",
                image_name,
                mitm_ca_cert,
                image_name,
            )
        else:
            _logger.info("Base image %s already exists, skipping build.", image_name)
        return
    elif daemon_exists:
        _logger.info(
            "Base image %s in daemon but OCI tarball missing, rebuilding.",
            image_name,
        )

    import os

    # Default to NATIVE architecture only (single-arch). Multi-arch (buildx + QEMU)
    # is opt-in via COMMIT0_BUILD_PLATFORMS env var. Multi-arch requires host-side
    # buildx + qemu-user-static; on a single-arch machine (macOS ARM, Linux amd64,
    # etc.) the multi-arch build fails without those. Native single-arch is what
    # 99% of users want. Set COMMIT0_BUILD_PLATFORMS=linux/amd64,linux/arm64 for
    # registry pushes.
    import platform as _platform
    _machine = _platform.machine().lower()
    _default_platform = "linux/" + ("arm64" if _machine in ("arm64", "aarch64") else "amd64")
    platform = os.environ.get("COMMIT0_BUILD_PLATFORMS", _default_platform)

    _multiarch_builder_args()

    _logger.info("Building C++ base image (%s)", image_name)
    build_image(
        image_name=image_name,
        setup_scripts={},
        dockerfile=dockerfile,
        platform=platform,
        client=client,
        build_dir=BASE_IMAGE_BUILD_DIR / image_name.replace(":", "__"),
        mitm_ca_cert=mitm_ca_cert,
    )
    _logger.info("C++ base image built successfully.")


def get_cpp_repo_configs_to_build(
    client: docker.DockerClient,
    dataset: list,
) -> dict[str, Any]:
    """Return repo image configs that need building.

    Returns a dict of ``{image_key: {setup_script, dockerfile, platform}}``.
    Skips images that already exist and are newer than the base image.
    """
    test_specs = get_cpp_specs_from_dataset(dataset, absolute=True)
    image_scripts: dict[str, Any] = {}

    base_image_key = "commit0.base.cpp:latest"
    try:
        client.images.get(base_image_key)
    except docker.errors.ImageNotFound as e:
        raise Exception(
            f"Base image {base_image_key} not found. Please build the base image first."
        ) from e

    base_ts = _get_image_created_timestamp(client, base_image_key)

    for spec in test_specs:
        image_exists = False
        try:
            client.images.get(spec.repo_image_key)
            image_exists = True
        except docker.errors.ImageNotFound:
            pass

        if image_exists:
            repo_ts = _get_image_created_timestamp(client, spec.repo_image_key)
            if base_ts and repo_ts:
                from datetime import datetime

                try:
                    base_dt = datetime.fromisoformat(base_ts.replace("Z", "+00:00"))
                    repo_dt = datetime.fromisoformat(repo_ts.replace("Z", "+00:00"))
                    if base_dt > repo_dt:
                        _logger.warning(
                            "Repo image %s is stale (built %s, base rebuilt %s) — scheduling rebuild",
                            spec.repo_image_key,
                            repo_ts[:19],
                            base_ts[:19],
                        )
                        image_exists = False
                except (ValueError, TypeError):
                    _logger.debug(
                        "Could not parse timestamps for stale check on %s",
                        spec.repo_image_key,
                    )

        if not image_exists:
            image_scripts[spec.repo_image_key] = {
                "setup_script": spec.setup_script,
                "dockerfile": spec.repo_dockerfile,
                "platform": spec.platform,
            }

    return image_scripts


def build_cpp_repo_images(
    client: docker.DockerClient,
    dataset: list,
    max_workers: int = 4,
    verbose: int = 1,
) -> tuple[list[str], list[str]]:
    """Build C++ repo images for all entries in the dataset.

    Builds the base image first (if needed), then builds each repo image
    in parallel using the same ``build_image()`` as the Python pipeline.

    Returns (successful, failed) lists of image keys.
    """
    mitm_ca_cert = _resolve_mitm_ca_cert()
    if mitm_ca_cert:
        _logger.info("MITM CA cert: %s", mitm_ca_cert)
    proxy_env = get_proxy_env()
    if proxy_env:
        _logger.info("Proxy env vars detected: %s", list(proxy_env.keys()))
    if mitm_ca_cert and not proxy_env:
        _logger.warning(
            "MITM CA cert found but no proxy env vars (http_proxy/https_proxy) "
            "are set. The cert will be installed but traffic won't route through a proxy."
        )

    build_base_images_cpp(client, mitm_ca_cert=mitm_ca_cert)
    configs_to_build = get_cpp_repo_configs_to_build(client, dataset)

    if len(configs_to_build) == 0:
        _logger.info("No C++ repo images need to be built.")
        return [], []

    _logger.info("Total C++ repo images to build: %d", len(configs_to_build))

    _multiarch_builder_args()

    successful: list[str] = []
    failed: list[str] = []

    with tqdm(
        total=len(configs_to_build), smoothing=0, desc="Building C++ repo images"
    ) as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    build_image,
                    image_name,
                    {"setup.sh": config["setup_script"]},
                    config["dockerfile"],
                    config["platform"],
                    client,
                    REPO_IMAGE_BUILD_DIR / image_name.replace(":", "__"),
                    False,
                    mitm_ca_cert,
                ): image_name
                for image_name, config in configs_to_build.items()
            }

            for future in as_completed(futures):
                pbar.update(1)
                try:
                    future.result()
                    successful.append(futures[future])
                except BuildImageError as e:
                    _logger.error("BuildImageError %s", e.image_name, exc_info=True)
                    failed.append(futures[future])
                    continue
                except Exception:
                    _logger.error(
                        "Error building image %s", futures[future], exc_info=True
                    )
                    failed.append(futures[future])
                    continue

    if len(failed) == 0:
        _logger.info("All C++ repo images built successfully.")
    else:
        _logger.warning("%d C++ repo images failed to build.", len(failed))

    return successful, failed


__all__ = [
    "build_base_images_cpp",
    "get_cpp_repo_configs_to_build",
    "build_cpp_repo_images",
]
