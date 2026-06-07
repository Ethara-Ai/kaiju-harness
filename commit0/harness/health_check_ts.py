"""TypeScript Docker health checks — co-located alongside health_check.py."""

from __future__ import annotations

import json as _json
import logging
import re
from typing import Optional

import docker
import docker.errors

logger = logging.getLogger(__name__)

_PKG_MANAGER_CACHE: dict[str, str] = {}


def _detect_image_pkg_manager(
    client: docker.DockerClient,
    image_name: str,
) -> str:
    """Detect package manager from lockfiles in /testbed. Returns one of:
    'pnpm' | 'yarn' | 'bun' | 'npm'. Result is cached per image.
    """
    if image_name in _PKG_MANAGER_CACHE:
        return _PKG_MANAGER_CACHE[image_name]
    probe = (
        'for f in pnpm-lock.yaml yarn.lock bun.lockb package-lock.json; do '
        '  [ -f "/testbed/$f" ] && echo "$f" && exit 0; '
        'done; echo none'
    )
    pm = "npm"
    try:
        output = client.containers.run(
            image_name,
            ["sh", "-c", probe],
            remove=True,
            stderr=True,
            stdout=True,
        )
        marker = output.decode().strip().splitlines()[-1] if output else ""
        if marker.startswith("pnpm-lock"):
            pm = "pnpm"
        elif marker.startswith("yarn.lock"):
            pm = "yarn"
        elif marker.startswith("bun.lockb"):
            pm = "bun"
    except Exception as e:
        logger.debug("pkg manager detect failed for %s: %s", image_name, e)
    _PKG_MANAGER_CACHE[image_name] = pm
    return pm


def _build_require_cmd(package_name: str, pkg_manager: str) -> list[str]:
    """Build a require() probe command that respects the package manager's
    module-resolution layout (pnpm symlinks, yarn pnp, etc.).
    """
    script = f'require({_json.dumps(package_name)})'
    if pkg_manager == "pnpm":
        return ["pnpm", "exec", "node", "-e", script]
    if pkg_manager == "yarn":
        return ["yarn", "node", "-e", script]
    if pkg_manager == "bun":
        return ["bun", "-e", script]
    return ["node", "-e", script]

def check_node_modules(
    client: docker.DockerClient,
    image_name: str,
) -> tuple[bool, str]:
    """Check if node_modules exists and has packages."""
    script = (
        'const fs = require("fs");'
        "try {"
        '  const mods = fs.readdirSync("/testbed/node_modules")'
        '    .filter(d => !d.startsWith("."));'
        "  console.log(JSON.stringify({count: mods.length}));"
        "} catch(e) {"
        "  console.log(JSON.stringify({count: -1, error: e.message}));"
        "}"
    )
    try:
        output = client.containers.run(
            image_name, ["node", "-e", script], remove=True, stderr=True, stdout=True
        )
        result = _json.loads(output.decode().strip())
        count = result.get("count", -1)
        if count > 0:
            return (True, f"{count} packages in node_modules")
        elif count == 0:
            return (False, "node_modules exists but is empty")
        else:
            return (False, f"node_modules missing: {result.get('error', 'unknown')}")
    except Exception as e:
        logger.warning("check_node_modules failed for %s: %s", image_name, e)
        return (False, f"node_modules check error: {e}")


def check_node_version(
    client: docker.DockerClient,
    image_name: str,
    expected: str,
) -> tuple[bool, str]:
    """Check Node.js major version inside container."""
    try:
        output = client.containers.run(
            image_name,
            ["node", "-e", "console.log(process.versions.node.split('.')[0])"],
            remove=True,
            stderr=True,
            stdout=True,
        )
        actual_major = output.decode().strip()
        if actual_major == expected:
            return (True, f"Node {actual_major}")
        return (False, f"Expected Node {expected}, got {actual_major}")
    except Exception as e:
        logger.warning("check_node_version failed: %s", e)
        return (False, f"Node version check error: {e}")


def check_require(
    client: docker.DockerClient,
    image_name: str,
    package_name: str,
) -> tuple[bool, str]:
    """Check if a package can be require()'d inside the container."""
    if package_name.startswith("@types/"):
        return (True, f"Skipped (type-only): {package_name}")
    _NPM_PACKAGE_RE = re.compile(
        r"(@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*"
    )
    if not _NPM_PACKAGE_RE.fullmatch(package_name):
        return (False, f"Invalid package name for require() check: {package_name!r}")
    safe_name = package_name
    pm = _detect_image_pkg_manager(client, image_name)
    cmd = _build_require_cmd(safe_name, pm)
    try:
        client.containers.run(
            image_name,
            cmd,
            working_dir="/testbed",
            remove=True,
            stderr=True,
            stdout=True,
        )
        return (True, f"require('{package_name}') OK [{pm}]")
    except docker.errors.ContainerError:
        return (False, f"require('{package_name}') failed [{pm}]")
    except Exception as e:
        logger.warning("check_require('%s') failed: %s", package_name, e)
        return (False, f"require('{package_name}') error: {e}")


def run_ts_health_checks(
    client: docker.DockerClient,
    image_name: str,
    node_version: Optional[str] = None,
    packages: Optional[list[str]] = None,
) -> list[tuple[bool, str, str]]:
    """Run all TS health checks and return results."""
    results: list[tuple[bool, str, str]] = []
    passed, detail = check_node_modules(client, image_name)
    results.append((passed, "node_modules", detail))
    if node_version:
        passed, detail = check_node_version(client, image_name, node_version)
        results.append((passed, "node_version", detail))
    if packages:
        for pkg in packages:
            if pkg.startswith("@types/"):
                continue
            passed, detail = check_require(client, image_name, pkg)
            results.append((passed, f"require:{pkg}", detail))
    return results
