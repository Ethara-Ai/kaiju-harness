from __future__ import annotations

import logging

from commit0.harness.constants_cpp import DOCKERFILES_CPP_DIR

_logger = logging.getLogger(__name__)


def get_dockerfile_base_cpp() -> str:
    template_path = DOCKERFILES_CPP_DIR / "Dockerfile.cpp"
    if not template_path.exists():
        raise FileNotFoundError(
            f"C++ base Dockerfile template not found: {template_path}"
        )
    from commit0.harness.constants_cpp import CPP_UBUNTU_VERSION
    return template_path.read_text().replace("__CPP_UBUNTU_VERSION__", CPP_UBUNTU_VERSION)


def get_dockerfile_repo_cpp(
    base_image: str,
    pre_install: list[str] | None = None,
    install_cmd: str | None = None,
    packages: str | list[str] | None = None,
) -> str:
    lines = [
        f"FROM {base_image}",
        "",
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        'ARG HTTP_PROXY=""',
        'ARG HTTPS_PROXY=""',
        'ARG no_proxy="localhost,127.0.0.1,::1"',
        'ARG NO_PROXY="localhost,127.0.0.1,::1"',
        "",
    ]

    apt_packages: list[str] = []
    if isinstance(packages, str):
        apt_packages = [p for p in packages.replace(",", " ").split() if p]
    elif isinstance(packages, list):
        apt_packages = [p for p in packages if p]
    if apt_packages:
        pkg_str = " ".join(sorted(set(apt_packages)))
        lines.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends "
            f"{pkg_str} && rm -rf /var/lib/apt/lists/*"
        )
        lines.append("")

    lines.extend([
        "COPY ./setup.sh /root/",
        "RUN chmod +x /root/setup.sh && /bin/bash /root/setup.sh",
        "",
        "WORKDIR /testbed/",
        "",
    ])

    if pre_install:
        for cmd in pre_install:
            lines.append(f"RUN {cmd}")
        lines.append("")

    if install_cmd:
        lines.append(f"RUN {install_cmd} 2>/dev/null || true")
        lines.append("")

    lines.append(
        "RUN g++ --version | head -1 > /testbed/.dep-manifest.txt"
        " && cmake --version | head -1 >> /testbed/.dep-manifest.txt"
        " && clang++ --version | head -1 >> /testbed/.dep-manifest.txt"
    )
    lines.append("")

    lines.append("WORKDIR /testbed/")
    lines.append("")

    return "\n".join(lines)


__all__: list[str] = [
    "get_dockerfile_base_cpp",
    "get_dockerfile_repo_cpp",
]
