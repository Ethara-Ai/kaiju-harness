"""Run the FULL local rust trajectory pipeline INSIDE the repo image container.

This is Option A: the exact `run_pipeline_rust.sh` (all 3 stages draft->lint->test,
per-stage eval, RESULTS_JSON, consolidated outputs/<dataset-id>/ structure) runs
inside the isolated container. Nothing on the host runs the agent, cargo, or eval.

Host does only Docker orchestration:
  1. build the agent image (repo image + python + aider + kaiju code),
  2. start one container with the bridge/git/UUID env,
  3. run `run_pipeline_rust.sh --backend local_inplace` in it (KAIJU_IN_CONTAINER=1
     makes the pipeline skip the docker build + docker preflight and eval via the
     local_inplace git-worktree backend — no docker-in-docker),
  4. copy the produced outputs/<dataset-id>/ tree back to the host,
  5. remove the container.

Usage:
  python -m agent.container.run_pipeline_containerized \
      --dataset evmap_dataset.json --repo-split evmap \
      [--model anthropic/claude-opus-4-8] [--pipeline-args "--max-iteration 1"]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path


logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pipeline_container")

DEFAULT_BRIDGE_URL = "http://host.docker.internal:8765"          # Anthropic/Claude Code
DEFAULT_CODEX_BRIDGE_URL = "http://host.docker.internal:8788"    # OpenAI Codex


def _default_bridge_url(model: str) -> str:
    """Pick the subscription-bridge URL a model needs, so callers don't have to
    pass --bridge-url. OpenAI/Codex -> :8788, Anthropic/Claude -> :8765, and
    Vertex/Bedrock/Gemini -> '' (no bridge; they use forwarded host creds).
    Env overrides: KAIJU_CODEX_BRIDGE_URL / KAIJU_CC_BRIDGE_URL."""
    if model.startswith("openai/") or model.startswith("gpt"):
        return os.environ.get("KAIJU_CODEX_BRIDGE_URL", DEFAULT_CODEX_BRIDGE_URL)
    if "claude" in model and not model.startswith(("bedrock/", "vertex_ai")):
        return os.environ.get("KAIJU_CC_BRIDGE_URL", DEFAULT_BRIDGE_URL)
    return ""

# Host credential env vars forwarded into the container when present, so the
# containerized pipeline reaches the SAME providers the local run does (Vertex,
# Bedrock, Gemini, direct Anthropic/OpenAI keys) — not only the two subscription
# bridges. Mirrors the provider matrix in run_pipeline_*.sh's preflight.
_CREDENTIAL_PASSTHROUGH = (
    # OpenAI
    "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
    # Anthropic (direct)
    "ANTHROPIC_API_KEY", "ANTHROPIC_API_BASE",
    # Google Gemini + Vertex AI (VERTEXAI_LOCATION is required for regional 404s)
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "VERTEX_AI_API_KEY", "VERTEXAI_API_KEY",
    "VERTEXAI_PROJECT", "VERTEXAI_LOCATION", "GOOGLE_CLOUD_PROJECT", "CLOUD_ML_REGION",
    # AWS Bedrock
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE",
)

# Where a staged Vertex/GCP service-account key lands inside the container.
_IN_CONTAINER_GAC = "/opt/kaiju/.gcp-creds.json"


def _build_provider_env(model: str, bridge_url: str, logger) -> tuple:
    """Return ``(env_updates, gac_source_path)`` for the container.

    Brings the containerized run to provider parity with the local pipeline:
      1. forwards every provider credential present on the host,
      2. for Vertex/GCP, flags the service-account key FILE for copy-in and
         repoints ``GOOGLE_APPLICATION_CREDENTIALS`` at its in-container path,
      3. wires the subscription BRIDGE for anthropic/openai when ``bridge_url``
         is set (overriding a direct key with the bridge stub/secret); pass
         ``--bridge-url ""`` to use a real Anthropic/OpenAI key directly instead.
    """
    env: dict = {}
    for k in _CREDENTIAL_PASSTHROUGH:
        v = os.environ.get(k)
        if v:
            env[k] = v
    gac_src = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if gac_src and Path(gac_src).is_file():
        env["GOOGLE_APPLICATION_CREDENTIALS"] = _IN_CONTAINER_GAC
    elif gac_src:
        logger.warning("GOOGLE_APPLICATION_CREDENTIALS=%s is not a readable file; "
                       "not forwarded", gac_src)
        gac_src = ""
    is_openai = model.startswith("openai/") or model.startswith("gpt")
    is_anthropic = ("claude" in model and not model.startswith("bedrock/")
                    and not model.startswith("vertex_ai"))
    if bridge_url and is_openai:
        # OpenAI Codex bridge: litellm reads OPENAI_API_BASE; SDK reads OPENAI_BASE_URL.
        env["OPENAI_API_BASE"] = bridge_url
        env["OPENAI_BASE_URL"] = bridge_url
        env["OPENAI_API_KEY"] = os.environ.get("KAIJU_CODEX_BRIDGE_SECRET", "kaiju-codex-stub")
    elif bridge_url and is_anthropic:
        # Anthropic/Claude Code bridge.
        env["ANTHROPIC_API_BASE"] = bridge_url
        env["ANTHROPIC_API_KEY"] = os.environ.get("KAIJU_CC_BRIDGE_SECRET", "kaiju-cc-stub")
    return env, (gac_src or None)


def _extra_hosts():
    """Map host.docker.internal to the host gateway on Linux so the container can
    reach the host bridge. Mac/Windows Docker Desktop resolve it natively.
    """
    import platform
    if platform.system() == "Linux":
        return {"host.docker.internal": "host-gateway"}
    return None


# language -> (spec module, spec factory, pipeline script, in-container repo base)
LANG_REGISTRY = {
    "rust":   ("spec_rust", "make_rust_spec", "run_pipeline_rust.sh", "repos"),
    "python": ("spec",      "make_spec",      "run_pipeline.sh",      "repos"),
    "go":     ("spec_go",   "make_go_spec",   "run_pipeline_go.sh",   "repos"),
    "js":     ("spec_js",   "make_js_spec",   "run_pipeline_js.sh",   "repos_js"),
    "ts":     ("spec_ts",   "make_ts_spec",   "run_pipeline_ts.sh",   "repos_ts"),
    "java":   ("spec_java", "make_java_spec", "run_pipeline_java.sh", "repos/java"),
    "c":      ("spec_c",    "make_c_spec",    "run_pipeline_c.sh",    "repos"),
    "cpp":    ("spec_cpp",  "make_cpp_spec",  "run_pipeline_cpp.sh",  "repos"),
}


def _make_spec(language: str, example: dict):
    """Build the language's Spec (for repo_image_key) via its factory."""
    import importlib
    mod, fac, _, _ = LANG_REGISTRY[language]
    factory = getattr(importlib.import_module(f"commit0.harness.{mod}"), fac)
    try:
        return factory(example, absolute=True)
    except TypeError:
        # commit0/python make_spec(instance, dataset_type, absolute).
        return factory(example, "commit0", True)


def _stream_exec(client, container_id: str, cmd: str, workdir: str = "/opt/kaiju") -> int:
    """Run a command in the container, streaming stdout+stderr live; return exit code."""
    exec_id = client.api.exec_create(
        container_id, cmd, workdir=workdir, tty=False,
    )["Id"]
    for chunk in client.api.exec_start(exec_id, stream=True, demux=False):
        if chunk:
            sys.stdout.write(chunk.decode("utf-8", "replace"))
            sys.stdout.flush()
    return client.api.exec_inspect(exec_id).get("ExitCode", 1)


def _copy_host_image_build_logs(spec, build_logs: Path, logger) -> list:
    """Copy the host-built base + repo image build logs into ``build_logs`` so
    ``outputs/<id>/build_logs`` is self-contained.

    The base and repo images are built on the HOST by ``commit0 <lang> build``
    BEFORE this containerized run (the container never rebuilds them — the repo
    image IS its sandbox base), and those builds log to the legacy
    ``logs/build_images/{base,repo}/<image-key>/`` (Dockerfile + build_image.log
    + setup.sh). We copy them next to the agent-image build log we capture live.
    Returns a list of (kind, key, dest_name) actually copied.
    """
    copied = []
    root = Path("logs/build_images")
    for kind, key in (
        ("base", getattr(spec, "base_image_key", None)),
        ("repo", getattr(spec, "repo_image_key", None)),
    ):
        if not key:
            continue
        # build dir name mirrors docker_build_*: image key with ':' -> '__'.
        src = root / kind / key.replace(":", "__")
        if not src.is_dir():
            logger.info("%s image build log not found at %s (built elsewhere?)",
                        kind, src)
            continue
        dst = build_logs / f"{kind}_image"
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied.append((kind, key, dst.name))
        except Exception as e:  # noqa: BLE001 - best-effort artifact copy
            logger.warning("could not copy %s image build log %s: %s", kind, src, e)
    return copied


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--language", default="rust", choices=sorted(LANG_REGISTRY),
                    help="Which language pipeline to run inside the container")
    ap.add_argument("--dataset", required=True, help="Path to the dataset JSON")
    ap.add_argument("--repo-split", required=True, help="Repo split / repo name (e.g. evmap)")
    ap.add_argument("--model", default="anthropic/claude-opus-4-8")
    ap.add_argument("--bridge-url", default=None,
                    help="Subscription-bridge URL reachable from the container. "
                         "DEFAULT: auto per model — Anthropic :8765, OpenAI-Codex "
                         ":8788, none for Vertex/Bedrock/Gemini. Pass '' to force a "
                         "direct Anthropic/OpenAI key; only override to point at a "
                         "non-standard host/port.")
    ap.add_argument("--pipeline-args", default="",
                    help="Extra args passed through to run_pipeline_rust.sh")
    ap.add_argument("--eval-timeout", type=int, default=10800,
                    help="Hard cap (s) for the whole in-container pipeline")
    ap.add_argument("--rebuild-agent-image", action="store_true")
    ap.add_argument("--keep-container", action="store_true",
                    help="Do not remove the container on exit (for debugging)")
    args = ap.parse_args(argv)

    import docker
    from commit0.harness.docker_utils import (
        create_container, copy_to_container, copy_from_container, cleanup_container,
    )
    from agent.container.agent_image import build_agent_image

    _spec_mod, _spec_fac, pipeline_script, repo_base = LANG_REGISTRY[args.language]

    dataset = json.loads(Path(args.dataset).read_text())
    example = dataset[0] if isinstance(dataset, list) else dataset
    repo_name = example["repo"].split("/")[-1]
    dataset_id = example.get("id")
    if not dataset_id:
        logger.error("Dataset entry has no 'id' — the pipeline keys outputs/<id> on it.")
        return 2
    spec = _make_spec(args.language, example)

    client = docker.from_env()
    # Capture the agent-image build into outputs/<id>/build_logs/ so it isn't
    # empty: the in-container pipeline SKIPS the docker build (KAIJU_IN_CONTAINER
    # guard), and host image builds otherwise log to the legacy
    # logs/build_images/ (no experiment UUID). Written host-side before the
    # container run so the later outputs copy-out merges around it.
    build_logs = Path("outputs") / dataset_id / "build_logs"
    build_logs.mkdir(parents=True, exist_ok=True)
    # Make build_logs/ self-contained: copy the host base+repo image build logs
    # (Dockerfile + build_image.log + setup.sh) in, alongside the agent-image
    # build we capture live below.
    _copied = _copy_host_image_build_logs(spec, build_logs, logger)
    _img_lines = [
        f"repo_image: {spec.repo_image_key}",
        f"base_image: {getattr(spec, 'base_image_key', 'n/a')}",
        "",
    ]
    if _copied:
        _img_lines.append("Host image build logs copied into this dir:")
        _img_lines += [f"  {kind}: {key} -> {name}/" for kind, key, name in _copied]
    else:
        _img_lines.append(
            "(host image build logs not found under logs/build_images/ — the "
            "repo/base images were built elsewhere.)")
    (build_logs / "images.txt").write_text("\n".join(_img_lines) + "\n",
                                           encoding="utf-8")
    _bh = logging.FileHandler(build_logs / "agent_image_build.log")
    _bh.setLevel(logging.DEBUG)
    _blogger = logging.getLogger(f"agent_image_build.{dataset_id[:8]}")
    _blogger.setLevel(logging.DEBUG)
    _blogger.propagate = False
    _blogger.addHandler(_bh)
    try:
        agent_tag = build_agent_image(
            client, spec.repo_image_key, _blogger, rebuild=args.rebuild_agent_image)
    finally:
        _bh.close()
        _blogger.removeHandler(_bh)
    logger.info("Agent image: %s (build log -> %s)", agent_tag,
                build_logs / "agent_image_build.log")

    env = {
        # git identity so aider can commit (no global gitconfig in the image).
        "GIT_AUTHOR_NAME": "Kaiju Agent", "GIT_AUTHOR_EMAIL": "agent@kaiju.local",
        "GIT_COMMITTER_NAME": "Kaiju Agent", "GIT_COMMITTER_EMAIL": "agent@kaiju.local",
        # Container-mode: pipeline skips docker build + docker preflight.
        "KAIJU_IN_CONTAINER": "1",
        "KAIJU_EXPERIMENT_UUID": dataset_id,
        "KAIJU_LOG_LAYOUT": "consolidated",
    }
    # Provider parity with the local pipeline: forward all host provider creds,
    # wire the bridge for anthropic/openai, and stage a Vertex/GCP key file.
    # Resolve the bridge URL: explicit flag wins; otherwise auto per model.
    bridge_url = (args.bridge_url if args.bridge_url is not None
                  else _default_bridge_url(args.model))
    _prov_env, _gac_src = _build_provider_env(args.model, bridge_url, logger)
    env.update(_prov_env)
    _prov = "openai-bridge" if "OPENAI_API_BASE" in _prov_env and bridge_url else (
        "anthropic-bridge" if "ANTHROPIC_API_BASE" in _prov_env and bridge_url
        else "direct-creds")
    logger.info("Provider wiring: model=%s mode=%s bridge=%s forwarded=%s%s",
                args.model, _prov, bridge_url or "(none)", sorted(_prov_env),
                " +gcp-creds-file" if _gac_src else "")

    container = None
    rc = 1
    try:
        container = create_container(
            client=client, image_name=agent_tag,
            container_name=f"kaiju.{repo_name}.{dataset_id[:6]}".lower(),
            logger=logger, environment=env, extra_hosts=_extra_hosts(),
        )
        container.start()

        # Copy the dataset in and expose /testbed as repos/<name> (the pipeline
        # expects the checkout under REPO_BASE=./repos).
        # NOTE: copy_to_container lands the file at {dst.parent}/{src.name}, so the
        # staged source MUST be named exactly as the destination basename.
        with tempfile.TemporaryDirectory() as td:
            staged_ds = Path(td) / "dataset.json"
            staged_ds.write_text(Path(args.dataset).read_text(), encoding="utf-8")
            copy_to_container(container, staged_ds, Path("/opt/kaiju/dataset.json"))
            # Vertex/GCP: copy the service-account key in under the exact basename
            # GOOGLE_APPLICATION_CREDENTIALS points at inside the container.
            if _gac_src:
                staged_gac = Path(td) / Path(_IN_CONTAINER_GAC).name
                staged_gac.write_bytes(Path(_gac_src).read_bytes())
                copy_to_container(container, staged_gac, Path(_IN_CONTAINER_GAC))
                logger.info("Copied GCP service-account key -> %s", _IN_CONTAINER_GAC)
        # aider commits via `git config --get user.name` (reads git CONFIG, not the
        # GIT_AUTHOR_* env) — set a global identity or every auto-commit fails and
        # git_patch comes out empty. Then expose /testbed as repos/<name>.
        link = f"{repo_base}/{repo_name}"
        setup = (
            'git config --global user.name "Kaiju Agent" && '
            'git config --global user.email "agent@kaiju.local" && '
            f"cd /opt/kaiju && mkdir -p {shlex.quote(repo_base)} && "
            f"ln -sfn /testbed {shlex.quote(link)}"
        )
        _stream_exec(client, container.id, f"bash -c {shlex.quote(setup)}")

        pipeline_cmd = (
            f"cd /opt/kaiju && bash {pipeline_script} "
            f"--model {shlex.quote(args.model)} "
            "--dataset dataset.json "
            f"--repo-split {shlex.quote(args.repo_split)} "
            "--backend local_inplace "
            + args.pipeline_args
        )
        logger.info("Running pipeline in container %s:\n  %s", container.name, pipeline_cmd)
        start = time.time()
        rc = _stream_exec(client, container.id, f"bash -c {shlex.quote(pipeline_cmd)}")
        logger.info("Pipeline exited rc=%s after %.0fs", rc, time.time() - start)

        # Copy outputs/<dataset-id>/ back to the host (merge into any existing dir).
        host_out = Path("outputs") / dataset_id
        try:
            with tempfile.TemporaryDirectory() as td:
                staged = Path(td) / dataset_id
                copy_from_container(
                    container, Path(f"/opt/kaiju/outputs/{dataset_id}"), staged)
                shutil.copytree(staged, host_out, dirs_exist_ok=True)
            logger.info("Copied outputs to %s", host_out.resolve())
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not copy outputs/%s: %s", dataset_id, e)
    finally:
        if container is not None and not args.keep_container:
            try:
                cleanup_container(client, container, logger)
            except Exception as e:  # noqa: BLE001
                logger.warning("cleanup failed: %s", e)
        elif container is not None:
            logger.info("Left container %s running (--keep-container)", container.name)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
