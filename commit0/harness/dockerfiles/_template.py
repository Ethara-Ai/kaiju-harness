"""Shared Dockerfile template renderer for every language spec.

Every language whose base image is pinned to a runtime version stores the
version as a `__PLACEHOLDER__` in ``Dockerfile.<lang>`` and substitutes it at
spec-load time. Historically each language re-implemented this substitution
inline; a spec that forgot to call ``.replace()`` shipped the raw template to
Docker and failed with a cryptic ``docker.io/library/gcc:__C_GCC_VERSION__-...:
not found`` after ~90 s of buildx overhead.

This module is the single source of truth. Callers pass the template path and
the placeholder→value mapping; ``render_dockerfile`` raises
:class:`DockerfileTemplateError` at spec-load time (not build time) if any
``__X__``-style token remains unresolved. That converts the class of bug from
"Docker Hub not-found after minutes of setup" into "actionable stacktrace
naming the exact leaked token before any network I/O".

See ``commit0/harness/docker_build.py::_assert_dockerfile_fully_substituted``
for the last-line-of-defense guard that runs immediately before build. This
module fires *earlier* in the pipeline so the failure surfaces at spec
construction rather than build time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")


class DockerfileTemplateError(RuntimeError):
    """Raised when a rendered Dockerfile still contains ``__X__`` placeholders.

    Signals a bug in the caller's substitutions dict (missing key, typo, or a
    new placeholder added to the template without updating the caller).
    """


def render_dockerfile(path: Path, substitutions: Mapping[str, str]) -> str:
    """Load ``path`` and substitute every placeholder from ``substitutions``.

    Parameters
    ----------
    path
        Absolute path to the Dockerfile template.
    substitutions
        Mapping of ``__PLACEHOLDER__`` → replacement value. Keys must include
        the leading and trailing double underscores; e.g.
        ``{"__C_GCC_VERSION__": "13"}``.

    Returns
    -------
    str
        Fully-rendered Dockerfile content.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    DockerfileTemplateError
        If, after substitution, any ``__X__``-style token remains — meaning
        the caller's mapping is incomplete for this template.

    """
    if not path.exists():
        raise FileNotFoundError(f"Dockerfile template not found: {path}")
    text = path.read_text(encoding="utf-8")
    for placeholder, value in substitutions.items():
        text = text.replace(placeholder, value)
    leftovers = sorted(set(_PLACEHOLDER_RE.findall(text)))
    if leftovers:
        raise DockerfileTemplateError(
            f"Dockerfile {path.name} still contains unresolved placeholder(s) "
            f"after substitution: {', '.join(leftovers)}. Every __X__ token in "
            f"the template must appear as a key in the substitutions dict "
            f"passed to render_dockerfile()."
        )
    return text


__all__ = ["DockerfileTemplateError", "render_dockerfile"]
