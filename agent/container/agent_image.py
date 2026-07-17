"""Build the agent-capable image: a thin layer on top of a repo env image that
adds Python 3.12, the aider fork, and the kaiju packages so the *whole* agent
(aider + edit/cost/thinking capture hooks) can run inside the container.

Why a layer (not a fresh image): the repo image (`spec.repo_image_key`) already
has the exact toolchain + source the eval uses. Editing in that same image is
what closes the "compiles on host, differs in eval" gap. We only add the Python
runtime the agent process needs; the reward-hack-critical eval logic is
untouched and runs in-place via the `local_inplace` backend.

The build context is filtered to just the source needed to `pip install -e .`
(the kaiju packages + pyproject + uv.lock + aider settings) — never `repos/`,
`dump/`, `.git`, etc. — so the layer stays small and cache-stable.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tarfile
from collections import deque
from pathlib import Path
from typing import Optional


# Paths (relative to project root) that make up the build context. Only what
# `uv pip install -e ".[agent]"` and the in-container entrypoint need.
CONTEXT_INCLUDE = [
    "agent",
    "commit0",
    "kaiju",
    "scripts",              # pipeline sources scripts/*.sh (_outputs_layout, etc.)
    # The full trajectory pipeline (per language) runs INSIDE the container.
    "run_pipeline.sh", "run_pipeline_rust.sh", "run_pipeline_go.sh",
    "run_pipeline_js.sh", "run_pipeline_ts.sh", "run_pipeline_java.sh",
    "run_pipeline_c.sh", "run_pipeline_cpp.sh",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    ".aider.model.settings.yml",
    ".aider.model.metadata.json",
]

# Directory names to prune anywhere in the tree (build artifacts / bulk data
# that must never enter the image even if nested under an included package).
CONTEXT_PRUNE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".git",
    "data",
}


def _agent_dockerfile(repo_image: str) -> str:
    """Dockerfile for the agent layer on top of `repo_image`.

    - Installs `uv` and a managed Python 3.12 (bookworm ships 3.11; the project
      requires >=3.12) into an isolated venv, so the repo's own toolchain env is
      untouched.
    - Installs the project editable with the `agent` extra, which pulls the
      pinned aider + litellm forks via `[tool.uv.sources]`.
    - Keeps `.aider.model.settings.yml` at /root so aider resolves edit_format
      "diff" (EditBlockCoder), which the edit-capture hooks patch.
    - Leaves WORKDIR at /testbed (the repo checkout) and the repo's own
      CARGO/RUST env intact.
    """
    return "\n".join(
        [
            f"FROM {repo_image}",
            "",
            "ENV DEBIAN_FRONTEND=noninteractive",
            "# `bc`  : run_pipeline_rust.sh preflight (float math) needs it.",
            "# `lsof`: the watchdog uses it to detect a live model connection and",
            "#         veto the inactivity kill during long LLM calls (_pgroup_has_",
            "#         live_conn). Without it the watchdog SIGKILLs slow modules.",
            "# The rust base image ships jq but neither bc nor lsof.",
            "RUN apt-get update && apt-get install -y --no-install-recommends bc lsof \\",
            "    && rm -rf /var/lib/apt/lists/*",
            "# eslint (global) backs the JS default-ruleset lint (commit0/harness/",
            "# lint_js.py + eslint_default.config.mjs) so SDE stage 2 always has a real",
            "# linter for repos that ship no ESLint config — parity with rust clippy /",
            "# go vet / python ruff. Installed HERE (the agent layer, rebuilt by",
            "# --rebuild-agent-image) rather than the base node image, which",
            "# --rebuild-agent-image does NOT rebuild. Guarded by `command -v npm` so it",
            "# no-ops on non-node base images (rust/python/java/go/c/cpp).",
            "RUN if command -v npm >/dev/null 2>&1; then npm install -g eslint@9 || (echo 'AGENT_IMAGE_ESLINT_INSTALL_FAILED' >&2; exit 1); else echo 'skipping eslint install: no npm in base image'; fi",
            "# uv provides a fast, hermetic Python 3.12 + resolver without",
            "# disturbing the image's system python or toolchain.",
            "RUN curl -LsSf https://astral.sh/uv/install.sh | sh",
            'ENV PATH="/root/.local/bin:${PATH}"',
            "",
            "COPY . /opt/kaiju",
            "WORKDIR /opt/kaiju",
            "RUN uv venv --python 3.12 /opt/kaiju/.venv \\",
            "    && . /opt/kaiju/.venv/bin/activate \\",
            '    && uv pip install -e ".[agent]"',
            "",
            "# Put the venv first on PATH so `python`/`aider` are the agent's,",
            "# and expose the packages for `python -m agent.run_in_container`.",
            'ENV PATH="/opt/kaiju/.venv/bin:${PATH}"',
            "ENV PYTHONPATH=/opt/kaiju",
            "# aider reads model settings from the home dir; keep edit_format=diff.",
            "RUN cp -f /opt/kaiju/.aider.model.settings.yml /root/.aider.model.settings.yml 2>/dev/null || true",
            "RUN cp -f /opt/kaiju/.aider.model.metadata.json /root/.aider.model.metadata.json 2>/dev/null || true",
            "",
            "WORKDIR /testbed",
            "",
        ]
    )


def agent_image_key(repo_image_key: str, project_root: Optional[Path] = None) -> str:
    """Deterministic tag for the agent layer built on `repo_image_key`.

    Keyed by (repo image + dockerfile + the packaging inputs pyproject/uv.lock)
    so the layer rebuilds when either the base image or the dependency set
    changes, and is cache-hit otherwise. Mirrors spec.repo_image_key's scheme:
    `<name>.<hash>:<tag>` with `-agent` appended to the name.
    """
    root = Path(project_root) if project_root else _default_root()
    h = hashlib.sha256()
    h.update(repo_image_key.encode())
    h.update(_agent_dockerfile(repo_image_key).encode())
    # Include the context file-list so adding/removing baked-in paths busts cache.
    h.update(repr(sorted(CONTEXT_INCLUDE)).encode())
    for rel in ("pyproject.toml", "uv.lock"):
        p = root / rel
        if p.exists():
            h.update(p.read_bytes())
    digest = h.hexdigest()[:22]

    # Split repo_image_key into name and tag: "commit0.repo.x.hash:v0".
    if ":" in repo_image_key:
        name, tag = repo_image_key.rsplit(":", 1)
    else:
        name, tag = repo_image_key, "latest"
    return f"{name}-agent.{digest}:{tag}".lower()


def _default_root() -> Path:
    # agent/container/agent_image.py -> project root is two parents up from agent/.
    return Path(__file__).resolve().parents[2]


def _iter_context_files(root: Path):
    """Yield (abs_path, arcname) for every file in the filtered build context."""
    for rel in CONTEXT_INCLUDE:
        src = root / rel
        if not src.exists():
            continue
        if src.is_file():
            yield src, rel
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            # Prune unwanted dirs in-place so os.walk doesn't descend into them.
            dirnames[:] = [d for d in dirnames if d not in CONTEXT_PRUNE_DIRS]
            for fn in filenames:
                if fn.endswith((".pyc", ".pyo")):
                    continue
                abs_path = Path(dirpath) / fn
                arcname = str(abs_path.relative_to(root))
                yield abs_path, arcname


def build_context_tar(root: Optional[Path] = None) -> io.BytesIO:
    """Build an in-memory tar of the filtered context + the Dockerfile.

    The Dockerfile is generated per repo image at build time and added as
    `Dockerfile`; callers pass the same repo_image to build().
    """
    root = Path(root) if root else _default_root()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for abs_path, arcname in _iter_context_files(root):
            tar.add(str(abs_path), arcname=arcname)
    buf.seek(0)
    return buf


def build_agent_image(
    client,
    repo_image_key: str,
    logger: logging.Logger,
    project_root: Optional[Path] = None,
    rebuild: bool = False,
) -> str:
    """Build (or reuse) the agent image on top of `repo_image_key`.

    Returns the agent image tag. If the tag already exists locally and
    `rebuild` is False, the existing image is reused (cache hit).
    """
    root = Path(project_root) if project_root else _default_root()
    tag = agent_image_key(repo_image_key, root)

    if not rebuild:
        try:
            client.images.get(tag)
            logger.info("Agent image %s already present; skipping build", tag)
            return tag
        except Exception:  # noqa: BLE001 - image not found -> build it
            pass

    dockerfile = _agent_dockerfile(repo_image_key)

    def _build_context() -> io.BytesIO:
        # Regenerated per attempt: client.api.build consumes the fileobj, and a retry
        # needs a fresh stream.
        ctx = build_context_tar(root)
        df_bytes = dockerfile.encode()
        with tarfile.open(fileobj=ctx, mode="a") as tar:
            info = tarfile.TarInfo("Dockerfile")
            info.size = len(df_bytes)
            tar.addfile(info, io.BytesIO(df_bytes))
        ctx.seek(0)
        return ctx

    def _attempt() -> tuple[str | None, str]:
        """Run one build. Returns (error_message_or_None, tail_of_build_output)."""
        stream = client.api.build(
            fileobj=_build_context(),
            custom_context=True,
            tag=tag,
            rm=True,
            forcerm=True,
            decode=True,
        )
        tail: "deque[str]" = deque(maxlen=250)
        for chunk in stream:
            if "stream" in chunk:
                line = chunk["stream"].rstrip()
                if line:
                    logger.debug(line)
                    tail.append(line)
            elif "error" in chunk:
                return str(chunk["error"]), "\n".join(l for l in tail if l.strip())
        return None, ""

    logger.info("Building agent image %s (FROM %s)", tag, repo_image_key)
    # A fresh agent image installs a LARGE dependency tree (aider-chat et al.), so
    # `uv pip install` can fail with a bare exit-1 on a transient network/disk/memory
    # hiccup. Retry once (Docker layer cache lets the retry resume), and on final
    # failure surface the ACTUAL build output — the docker 'error' chunk is only the
    # generic "command returned non-zero code 1"; the real pip/compiler error lives
    # in the preceding stream lines, which were previously dropped to logger.debug.
    err, tail = _attempt()
    if err is not None:
        logger.warning(
            "Agent image build failed (attempt 1/2): %s — retrying (layer cache "
            "resumes; transient network/disk/memory is the usual cause).", err,
        )
        err, tail = _attempt()
    if err is not None:
        raise RuntimeError(
            f"Agent image build failed after 2 attempts: {err}\n"
            f"--- last build output (the real error) ---\n{tail[-4000:]}\n"
            "--------------------------------------------\n"
            "If this is a transient network/disk/memory hiccup, re-run with "
            "--rebuild-agent-image. If it persists, check Docker Desktop free "
            "disk + memory (a fresh agent image needs several GB)."
        )
    logger.info("Built agent image %s", tag)
    return tag


__all__ = [
    "agent_image_key",
    "build_agent_image",
    "build_context_tar",
    "CONTEXT_INCLUDE",
    "CONTEXT_PRUNE_DIRS",
]
