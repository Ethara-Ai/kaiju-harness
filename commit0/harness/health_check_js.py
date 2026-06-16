from __future__ import annotations

import json as _json
import logging
import re
import threading

import docker
import docker.errors

from commit0.harness.constants_js import DEFAULT_NODE_VERSION

logger = logging.getLogger(__name__)

_PKG_MANAGER_CACHE: dict[str, str] = {}
_PKG_MANAGER_CACHE_LOCK = threading.Lock()

_LOCKFILE_TO_PM: dict[str, str] = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "package-lock.json": "npm",
}

_NPM_PACKAGE_RE = re.compile(
    r"(@[a-z0-9\-~][a-z0-9\-._~]*/)?[a-z0-9\-~][a-z0-9\-._~]*"
)


class MultipleLockfilesError(RuntimeError):
    def __init__(self, image_name: str, lockfiles: list[str]) -> None:
        self.image_name = image_name
        self.lockfiles = lockfiles
        super().__init__(
            f"Multiple lockfiles in {image_name}: {lockfiles} — "
            "exactly-one invariant violated"
        )


def _detect_image_pkg_manager(
    client: docker.DockerClient,
    image_name: str,
) -> str:
    cached = _PKG_MANAGER_CACHE.get(image_name)
    if cached is not None:
        return cached
    probe = (
        'found=""; '
        'for f in pnpm-lock.yaml yarn.lock bun.lockb package-lock.json; do '
        '  [ -f "/testbed/$f" ] && found="$found $f"; '
        'done; '
        'echo "${found:- none}"'
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
        markers = output.decode().strip().split()
        if len(markers) > 1:
            raise MultipleLockfilesError(image_name, markers)
        first = markers[0] if markers and markers[0] != "none" else ""
        pm = _LOCKFILE_TO_PM.get(first, "npm")
    except MultipleLockfilesError:
        raise
    except Exception as e:
        logger.debug("pkg manager detect failed for %s: %s", image_name, e)
    with _PKG_MANAGER_CACHE_LOCK:
        existing = _PKG_MANAGER_CACHE.get(image_name)
        if existing is not None:
            return existing
        _PKG_MANAGER_CACHE[image_name] = pm
        return pm


def detect_test_framework_from_package_json(
    client: docker.DockerClient,
    image_name: str,
) -> str:
    probe = "cat /testbed/package.json 2>/dev/null || echo '{}'"
    try:
        output = client.containers.run(
            image_name,
            ["sh", "-c", probe],
            remove=True,
            stderr=True,
            stdout=True,
        )
        pkg = _json.loads(output.decode() or "{}")
    except Exception as e:
        logger.debug("read package.json failed for %s: %s", image_name, e)
        return "unknown"
    deps = {
        **(pkg.get("dependencies") or {}),
        **(pkg.get("devDependencies") or {}),
    }
    if "jest" in deps:
        return "jest"
    if "vitest" in deps:
        return "vitest"
    if "mocha" in deps:
        return "mocha"
    scripts = pkg.get("scripts") or {}
    test_script = str(scripts.get("test", ""))
    if test_script.startswith("node --test") or " node --test" in test_script:
        return "node_test"
    return "unknown"


def _build_require_cmd(package_name: str, pkg_manager: str) -> list[str]:
    script = f"require({_json.dumps(package_name)})"
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
            image_name,
            ["node", "-e", script],
            remove=True,
            stderr=True,
            stdout=True,
        )
        result = _json.loads(output.decode().strip())
        count = result.get("count", -1)
        if count > 0:
            return (True, f"{count} packages in node_modules")
        if count == 0:
            return (False, "node_modules exists but is empty")
        return (False, f"node_modules missing: {result.get('error', 'unknown')}")
    except Exception as e:
        logger.warning("check_node_modules failed for %s: %s", image_name, e)
        return (False, f"node_modules check error: {e}")


def check_node_version(
    client: docker.DockerClient,
    image_name: str,
    expected: str | None = None,
) -> tuple[bool, str]:
    if expected is None:
        expected = str(DEFAULT_NODE_VERSION)
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
    if package_name.startswith("@types/"):
        return (True, f"Skipped (type-only): {package_name}")
    if not _NPM_PACKAGE_RE.fullmatch(package_name):
        return (False, f"Invalid package name for require() check: {package_name!r}")
    try:
        pm = _detect_image_pkg_manager(client, image_name)
    except MultipleLockfilesError as e:
        return (False, str(e))
    cmd = _build_require_cmd(package_name, pm)
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


def run_js_health_checks(
    client: docker.DockerClient,
    image_name: str,
    node_version: str | None = None,
    packages: list[str] | None = None,
) -> list[tuple[bool, str, str]]:
    results: list[tuple[bool, str, str]] = []
    passed, detail = check_node_modules(client, image_name)
    results.append((passed, "node_modules", detail))
    expected = node_version if node_version is not None else str(DEFAULT_NODE_VERSION)
    passed, detail = check_node_version(client, image_name, expected)
    results.append((passed, "node_version", detail))
    try:
        pm = _detect_image_pkg_manager(client, image_name)
        results.append((True, "package_manager", f"Detected: {pm}"))
    except MultipleLockfilesError as e:
        results.append((False, "package_manager", str(e)))
    framework = detect_test_framework_from_package_json(client, image_name)
    results.append(
        (framework != "unknown", "test_framework", f"Detected: {framework}")
    )
    if packages:
        for pkg in packages:
            if pkg.startswith("@types/"):
                continue
            passed, detail = check_require(client, image_name, pkg)
            results.append((passed, f"require:{pkg}", detail))
    return results
