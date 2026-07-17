from __future__ import annotations

import logging

from commit0.harness.constants_rust import (
    CARGO_NEXTEST_VERSION,
    DOCKERFILES_RUST_DIR,
    RUST_VERSION,
)

_logger = logging.getLogger(__name__)


def get_dockerfile_base_rust() -> str:
    from commit0.harness.dockerfiles._template import render_dockerfile
    return render_dockerfile(
        DOCKERFILES_RUST_DIR / "Dockerfile.rust",
        {
            "__RUST_VERSION__": RUST_VERSION,
            "__CARGO_NEXTEST_VERSION__": CARGO_NEXTEST_VERSION,
        },
    )


def get_dockerfile_repo_rust(
    base_image: str,
    pre_install: list[str] | None = None,
    install_cmd: str | None = None,
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
        "COPY ./setup.sh /root/",
        "RUN chmod +x /root/setup.sh && /bin/bash /root/setup.sh",
        "",
        "WORKDIR /testbed/",
        "",
    ]

    if pre_install:
        for cmd in pre_install:
            lines.append(f"RUN {cmd}")
        lines.append("")

    if install_cmd:
        lines.append(f"RUN {install_cmd}")
        lines.append("")

    lines.append(
        "RUN cargo --version > /testbed/.dep-manifest.txt && rustc --version >> /testbed/.dep-manifest.txt"
    )
    lines.append("")

    lines.append("WORKDIR /testbed/")
    lines.append("")

    return "\n".join(lines)


__all__: list[str] = [
    "get_dockerfile_base_rust",
    "get_dockerfile_repo_rust",
]
