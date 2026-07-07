"""TypeScript Dockerfile generation — co-located alongside dockerfiles/__init__.py."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from commit0.harness.constants_ts import SUPPORTED_NODE_VERSIONS

DOCKERFILES_DIR = Path(__file__).parent / "dockerfiles"
_logger = logging.getLogger(__name__)

# npm package → required Debian apt packages
TS_NATIVE_DEP_MAP: dict[str, list[str]] = {
    "sharp": ["libvips-dev"],
    "canvas": [
        "libcairo2-dev",
        "libjpeg-dev",
        "libpango1.0-dev",
        "libgif-dev",
        "librsvg2-dev",
    ],
    "bcrypt": [],  # build-essential + python3 already in base
    "sqlite3": ["libsqlite3-dev"],
    "better-sqlite3": ["libsqlite3-dev"],
    "pg-native": ["libpq-dev"],
    "libxmljs": ["libxml2-dev"],
    "libxmljs2": ["libxml2-dev"],
    "re2": ["libre2-dev"],
    "cpu-features": [],  # only needs build-essential
}

_BASE_APT_PACKAGES: frozenset[str] = frozenset(
    {
        "git",
        "build-essential",
        "python3",
        "ca-certificates",
        "curl",
        "jq",
        "libatomic1",
        "locales",
        "locales-all",
    }
)


def detect_ts_system_dependencies(npm_packages: list[str]) -> list[str]:
    """Map npm package names to required apt packages, minus base packages."""
    apt_deps: set[str] = set()
    for pkg_spec in npm_packages:
        name = pkg_spec.strip()
        if name.startswith("@types/"):
            continue
        # Strip version pins: handle scoped packages (@scope/pkg@1.0.0)
        if name.startswith("@"):
            at_idx = name.find("@", 1)
            if at_idx > 0:
                name = name[:at_idx]
        else:
            for sep in ("@", ">", "<", "=", "^", "~"):
                name = name.split(sep)[0]
        name = name.strip().lower()
        apt_deps.update(TS_NATIVE_DEP_MAP.get(name, []))
    return sorted(apt_deps - _BASE_APT_PACKAGES)


def get_dockerfile_base_ts(node_version: str) -> str:
    """Read the Dockerfile template for a given Node.js version."""
    if node_version not in SUPPORTED_NODE_VERSIONS:
        _logger.error(
            "Unsupported Node version: %s (supported: %s)",
            node_version,
            sorted(SUPPORTED_NODE_VERSIONS),
        )
        raise ValueError(
            f"Unsupported Node version: {node_version}. Supported: {sorted(SUPPORTED_NODE_VERSIONS)}"
        )
    template_path = DOCKERFILES_DIR / f"Dockerfile.node{node_version}"
    if not template_path.exists():
        raise FileNotFoundError(
            f"Node base Dockerfile template not found: {template_path}"
        )
    return template_path.read_text()


def get_dockerfile_repo_ts(
    base_image: str,
    install_cmd: Optional[str] = None,
    packages: Optional[List[str]] = None,
    pre_install: Optional[List[str]] = None,
) -> str:
    """Generate a repo-specific Dockerfile for a TypeScript project."""
    lines: list[str] = []
    lines.append(f"FROM {base_image}")
    lines.append("")
    for arg in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        default = '"localhost,127.0.0.1,::1"' if "no_proxy" in arg.lower() else '""'
        lines.append(f"ARG {arg}={default}")
    lines.append("")
    lines.append("COPY ./setup.sh /root/")
    lines.append("RUN chmod +x /root/setup.sh && /bin/bash /root/setup.sh")
    lines.append("")
    lines.append("WORKDIR /testbed/")
    lines.append("")

    apt_packages: list[str] = []
    _SAFE_PRE_INSTALL_PREFIXES = (
        "apt-get ",
        "apt ",
        "npm ",
        "yarn ",
        "pnpm ",
        "pip ",
        "pip3 ",
        "curl ",
        "wget ",
        "chmod ",
        "mkdir ",
        "ln ",
        "echo ",
        "export ",
    )
    if pre_install:
        for cmd in pre_install:
            if cmd.startswith("apt-get install") or cmd.startswith("apt install"):
                pkgs = cmd.split("install", 1)[1].replace("-y", "").strip().split()
                apt_packages.extend(p for p in pkgs if not p.startswith("-"))
            else:
                if not any(
                    cmd.startswith(prefix) for prefix in _SAFE_PRE_INSTALL_PREFIXES
                ):
                    _logger.warning(
                        "pre_install command does not match known-safe prefixes: %r",
                        cmd,
                    )
                lines.append(f"RUN {cmd}")
    if packages:
        apt_packages.extend(detect_ts_system_dependencies(packages))
    if apt_packages:
        pkg_str = " \\\n    ".join(sorted(set(apt_packages)))
        lines.append(
            f"RUN apt-get update && apt-get install -y --no-install-recommends \\\n    {pkg_str} \\\n    && rm -rf /var/lib/apt/lists/*"
        )
        lines.append("")

    lines.append(
        'RUN if [ -d node_modules ] && [ "$(ls -A node_modules 2>/dev/null | wc -l)" -gt 0 ]; then '
        'echo "node_modules OK: $(ls node_modules | wc -l) packages"; '
        'else '
        'echo "ERROR: node_modules missing or empty after install — refusing to publish broken image" >&2; '
        'exit 1; '
        'fi'
    )
    lines.append("")
    lines.append("RUN ls node_modules > /testbed/.dep-manifest.txt 2>/dev/null || true")
    lines.append("")
    lines.append("WORKDIR /testbed/")
    lines.append("")
    return "\n".join(lines)
