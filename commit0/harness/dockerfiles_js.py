from __future__ import annotations

import shlex

from commit0.harness.constants_js import ALLOWED_APT_PACKAGES, SUPPORTED_NODE_VERSIONS
from commit0.harness.dockerfiles_ts import (
    get_dockerfile_base_ts,
    get_dockerfile_repo_ts,
)

_ALLOWED_PRE_INSTALL_PREFIXES: tuple[str, ...] = (
    "apt-get ",
    "apt ",
    "sudo apt-get ",
    "sudo apt ",
    "npm ",
    "pnpm ",
    "yarn ",
    "bun ",
    "node ",
    "npx ",
    "chmod ",
    "mkdir ",
    "ln ",
    "echo ",
    "export ",
)


def get_dockerfile_base(node_version: int) -> str:
    if node_version not in SUPPORTED_NODE_VERSIONS:
        raise ValueError(
            f"Unsupported Node version: {node_version}. Supported: {sorted(SUPPORTED_NODE_VERSIONS)}"
        )
    return get_dockerfile_base_ts(str(node_version))


def _extract_apt_packages_from_pre_install(
    pre_install: list[str] | None,
) -> list[str]:
    if not pre_install:
        return []
    pkgs: list[str] = []
    for cmd in pre_install:
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            continue
        try:
            # Loose scan: any `apt-get`/`apt` token (even after `sudo`, env-vars, etc.).
            # Security gate is ALLOWED_APT_PACKAGES applied in get_dockerfile_repo below.
            i = next(
                idx
                for idx, t in enumerate(tokens)
                if t in {"apt-get", "apt"}
                and any(t2 == "install" for t2 in tokens[idx + 1 :])
            )
        except StopIteration:
            continue
        install_idx = next(
            j for j, t in enumerate(tokens[i + 1 :], start=i + 1) if t == "install"
        )
        for tok in tokens[install_idx + 1 :]:
            if tok.startswith("-"):
                continue
            if tok in {"&&", "||", ";", "|"}:
                break
            pkgs.append(tok)
    return pkgs


def get_dockerfile_repo(
    base_image_key: str,
    install_cmd: str | None = None,
    packages: list[str] | None = None,
    pre_install: list[str] | None = None,
) -> str:
    user_apt = _extract_apt_packages_from_pre_install(pre_install)
    disallowed = sorted({p for p in user_apt if p not in ALLOWED_APT_PACKAGES})
    if disallowed:
        raise ValueError(
            f"Disallowed apt packages in pre_install: {disallowed}. "
            f"Allowed: {sorted(ALLOWED_APT_PACKAGES)}. "
            f"npm-resolved apt deps (via TS_NATIVE_DEP_MAP) bypass this gate by design."
        )
    if pre_install:
        bad_prefix = [
            cmd
            for cmd in pre_install
            if not any(cmd.startswith(p) for p in _ALLOWED_PRE_INSTALL_PREFIXES)
        ]
        if bad_prefix:
            raise ValueError(
                f"pre_install commands not matching allowed prefixes: {bad_prefix}. "
                f"Allowed prefixes: {list(_ALLOWED_PRE_INSTALL_PREFIXES)}."
            )
    return get_dockerfile_repo_ts(
        base_image=base_image_key,
        install_cmd=install_cmd,
        packages=packages,
        pre_install=pre_install,
    )


__all__ = ["get_dockerfile_base", "get_dockerfile_repo"]
