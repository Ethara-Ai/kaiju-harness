from __future__ import annotations

import logging
import textwrap
from typing import Optional

import docker

logger = logging.getLogger(__name__)

_PIP_IMPORT_MAP = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-dateutil": "dateutil",
    "scikit-learn": "sklearn",
    "beautifulsoup4": "bs4",
    "python-dotenv": "dotenv",
    "attrs": "attr",
    "pyjwt": "jwt",
    "python-jose": "jose",
    "python-multipart": "multipart",
    "msgpack-python": "msgpack",
    "biscuit-python": "biscuit_auth",
    "google-cloud-storage": "google.cloud.storage",
    "google-auth": "google.auth",
    "protobuf": "google.protobuf",
    "grpcio": "grpc",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "ruamel.yaml": "ruamel.yaml",
    "importlib-metadata": "importlib_metadata",
    "typing-extensions": "typing_extensions",
}


def _normalize_pip_name(pip_name: str) -> str:
    # Strip the PEP 508 environment marker (`; python_version ...`) and direct-URL
    # (`name @ url`) FIRST, then extras (`[test]`) and version specifiers, to get
    # the bare distribution name. Without the `;` strip, `tzdata; python_version>=
    # "3.9"` became the garbage import name `tzdata; python_version`.
    normalized = pip_name.split(";")[0].split("@")[0].lower().split("[")[0]
    for sep in (">", "<", "=", "!", "~", " "):
        normalized = normalized.split(sep)[0]
    return normalized.strip()


def _marker_applies(pip_spec: str, python_version: "Optional[str]") -> bool:
    """Does this dep's PEP 508 environment marker apply to the target image env?

    A conditional dep like ``backports.zoneinfo; python_version < "3.9"`` is NOT
    installed on Python 3.9, so checking its import there always (falsely) fails.
    Evaluate the marker against the target python (and linux, since the image is
    linux) and skip deps that don't apply. Fail-OPEN (return True) when there is no
    marker, no target version, or ``packaging`` is unavailable — i.e. never hide a
    dep from the check due to our own inability to evaluate it."""
    if ";" not in pip_spec:
        return True
    marker_str = pip_spec.split(";", 1)[1].strip()
    if not marker_str:
        return True
    try:
        from packaging.markers import Marker
    except Exception:  # noqa: BLE001 - packaging missing -> old behavior
        return True
    env = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"}
    if python_version:
        env["python_version"] = python_version
        env["python_full_version"] = python_version
    try:
        return bool(Marker(marker_str).evaluate(env))
    except Exception:  # noqa: BLE001 - unparseable marker -> check it anyway
        return True


def pip_to_import(pip_name: str) -> str:
    """Static fallback: map pip name to import name using known map + heuristic.

    Used at Dockerfile generation time (before packages are installed).
    For post-install verification, prefer discover_import_names() instead.
    """
    normalized = _normalize_pip_name(pip_name)
    return _PIP_IMPORT_MAP.get(normalized, normalized.replace("-", "_"))


_DISCOVER_SCRIPT = textwrap.dedent("""\
    import json, sys
    from importlib.metadata import packages_distributions, PackageNotFoundError

    pip_names = json.loads(sys.argv[1])
    pkg_to_modules = {}
    try:
        dist_map = packages_distributions()
        reverse = {}
        for mod, dists in dist_map.items():
            for d in dists:
                reverse.setdefault(d.lower().replace("-", "_"), []).append(mod)
    except Exception:
        reverse = {}

    for pip_name in pip_names:
        norm = pip_name.lower().replace("-", "_")
        modules = reverse.get(norm, [])
        if modules:
            pkg_to_modules[pip_name] = modules
        else:
            pkg_to_modules[pip_name] = None
    print(json.dumps(pkg_to_modules))
""")


def discover_import_names(
    client: docker.DockerClient,
    image_name: str,
    pip_names: list[str],
) -> dict[str, list[str] | None]:
    """Query Docker container for actual importable module names via importlib.metadata.

    Returns {pip_name: [module1, module2, ...]} for discovered packages,
    or {pip_name: None} when metadata lookup fails (fall back to static map).
    """
    import json as _json

    cmd = ["python", "-c", _DISCOVER_SCRIPT, _json.dumps(pip_names)]
    try:
        output = client.containers.run(
            image_name, cmd, remove=True, stderr=True, stdout=True
        )
        return _json.loads(output.decode().strip())
    except Exception as e:
        logger.debug(
            "Metadata discovery failed for %s: %s — falling back to static map",
            image_name,
            e,
        )
        return {p: None for p in pip_names}


def check_imports(
    client: docker.DockerClient,
    image_name: str,
    packages: list[str],
    python_version: "Optional[str]" = None,
) -> tuple[bool, str]:
    skip_prefixes = ("pytest", "coverage", "pip", "setuptools", "wheel")
    to_check = [
        _normalize_pip_name(p)
        for p in packages
        if not any(p.lower().startswith(s) for s in skip_prefixes)
        # Skip conditional deps whose environment marker excludes this image (e.g.
        # `backports.zoneinfo; python_version < "3.9"` on Python 3.9) — they aren't
        # installed there, so checking their import is a guaranteed false failure.
        and _marker_applies(p, python_version)
    ]
    if not to_check:
        return True, "No packages to check"

    discovered = discover_import_names(client, image_name, to_check)

    failed = []
    checked = 0
    for pip_name in to_check:
        modules = discovered.get(pip_name)
        if modules is None:
            modules = [pip_to_import(pip_name)]

        top_module = modules[0].split(".")[0]
        cmd = f'python -c "import {top_module}"'
        try:
            client.containers.run(
                image_name, cmd, remove=True, stderr=True, stdout=True
            )
            checked += 1
        except docker.errors.ContainerError:
            failed.append(f"{pip_name} (tried: {top_module})")
        except Exception as e:
            logger.warning("Non-critical failure checking %s: %s", pip_name, e)
            failed.append(f"{pip_name} (error: {e})")

    if failed:
        return False, f"Import check failed for: {', '.join(failed)}"
    return True, f"All {checked} packages importable"


def check_python_version(
    client: docker.DockerClient,
    image_name: str,
    expected: str,
) -> tuple[bool, str]:
    version_cmd = "python -c \"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')\""
    try:
        output = client.containers.run(
            image_name, version_cmd, remove=True, stderr=True, stdout=True
        )
        actual = output.decode().strip()
        if actual == expected:
            return True, f"Python {actual}"
        return False, f"Expected Python {expected}, got {actual}"
    except Exception as e:
        logger.warning("Non-critical failure during Python version check: %s", e)
        return False, f"Python version check error: {e}"


def run_health_checks(
    client: docker.DockerClient,
    image_name: str,
    pip_packages: Optional[list[str]] = None,
    python_version: Optional[str] = None,
) -> list[tuple[bool, str, str]]:
    results: list[tuple[bool, str, str]] = []
    if pip_packages:
        passed, detail = check_imports(client, image_name, pip_packages, python_version)
        results.append((passed, "imports", detail))
    if python_version:
        passed, detail = check_python_version(client, image_name, python_version)
        results.append((passed, "python_version", detail))
    return results


__all__ = [
    "check_imports",
    "check_python_version",
    "discover_import_names",
    "pip_to_import",
    "run_health_checks",
]
