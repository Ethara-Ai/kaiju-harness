"""C-specific health checks for Docker images."""

from __future__ import annotations

import logging
from typing import Optional

import docker

logger = logging.getLogger(__name__)


def check_c_compilers(
    client: docker.DockerClient,
    image_name: str,
) -> tuple[bool, str]:
    cmd = "gcc --version && clang --version"
    try:
        output = client.containers.run(
            image_name,
            ["bash", "-c", cmd],
            remove=True,
            stderr=True,
            stdout=True,
        )
        text = output.decode().strip()
        if "gcc" in text.lower() and "clang" in text.lower():
            first_line = text.splitlines()[0] if text else ""
            return True, f"{first_line}"
        return False, f"Unexpected compiler output: {text[:200]}"
    except Exception as e:
        logger.warning("Non-critical failure during C compiler check: %s", e)
        return False, f"C compiler check error: {e}"


def check_c_build_tools(
    client: docker.DockerClient,
    image_name: str,
) -> tuple[bool, str]:
    cmd = "which cmake && which ninja && cmake --version | head -n1 && ninja --version"
    try:
        output = client.containers.run(
            image_name,
            ["bash", "-c", cmd],
            remove=True,
            stderr=True,
            stdout=True,
        )
        text = output.decode().strip()
        if "cmake" in text.lower():
            return True, f"build tools: {text.splitlines()[-2:]}"
        return False, f"C build tools check unexpected output: {text[:200]}"
    except Exception as e:
        logger.warning("Non-critical failure during C build tools check: %s", e)
        return False, f"C build tools check error: {e}"


def check_c_lint_tools(
    client: docker.DockerClient,
    image_name: str,
) -> tuple[bool, str]:
    cmd = "which clang-tidy && which cppcheck && echo OK"
    try:
        output = client.containers.run(
            image_name,
            ["bash", "-c", cmd],
            remove=True,
            stderr=True,
            stdout=True,
        )
        if b"OK" in output:
            return True, "clang-tidy and cppcheck available"
        return False, f"C lint tools check unexpected output: {output.decode().strip()}"
    except Exception as e:
        logger.warning("Non-critical failure during C lint tools check: %s", e)
        return False, f"C lint tools check error: {e}"


def run_c_health_checks(
    client: docker.DockerClient,
    image_name: str,
    c_version: Optional[str] = None,
) -> list[tuple[bool, str, str]]:
    del c_version  # accepted for signature symmetry; not yet enforced

    results: list[tuple[bool, str, str]] = []

    passed, detail = check_c_compilers(client, image_name)
    results.append((passed, "c_compilers", detail))

    passed, detail = check_c_build_tools(client, image_name)
    results.append((passed, "c_build_tools", detail))

    passed, detail = check_c_lint_tools(client, image_name)
    results.append((passed, "c_lint_tools", detail))

    return results


__all__ = [
    "check_c_compilers",
    "check_c_build_tools",
    "check_c_lint_tools",
    "run_c_health_checks",
]
