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
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pipeline_container")

DEFAULT_BRIDGE_URL = "http://host.docker.internal:8765"          # Anthropic/Claude Code
DEFAULT_CODEX_BRIDGE_URL = "http://host.docker.internal:8788"    # OpenAI Codex


_RESOLVE_MODEL_SH = (Path(__file__).resolve().parents[2]
                     / "commit0" / "harness" / "resolve_model.sh")


def _resolve_model_name(model_arg: str) -> str:
    """Resolve a model ALIAS (e.g. 'gpt55', 'opus48cc', 'opus48v') to its full
    name using the SAME resolve_model.sh the pipeline uses — so the orchestrator
    detects the real provider (and bridge) even when the alias carries no
    provider hint. Falls back to the arg unchanged (full names pass through)."""
    if not _RESOLVE_MODEL_SH.is_file():
        return model_arg
    try:
        out = subprocess.run(
            ["bash", "-c",
             f"source {shlex.quote(str(_RESOLVE_MODEL_SH))}; "
             'resolve_model "$1" >/dev/null 2>&1; printf "%s" "${MODEL_NAME:-}"',
             "_", model_arg],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or model_arg
    except Exception:  # noqa: BLE001 - best-effort; fall back to the raw arg
        return model_arg


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


_SWEEP_GRACE_SEC = 600  # a RUNNING container younger than this may still be in its
                        # pre-pipeline setup window (dataset copy, git config, mkdir
                        # execs) where only the PID-1 keepalive runs — do not mistake
                        # it for a dead orphan and reap a live concurrent sibling.


def _container_age_seconds(container) -> float:
    """Age of the container from its StartedAt/Created timestamp. On any parse
    failure returns 0.0 (treat as YOUNG -> keep it) so we never reap a running
    container we can't age."""
    import datetime as _dt
    import re as _re
    try:
        container.reload()
        st = (container.attrs.get("State") or {}).get("StartedAt") or container.attrs.get("Created")
        if not st:
            return 0.0
        s = _re.sub(r"(\.\d{6})\d+", r"\1", st.replace("Z", "+00:00"))
        started = _dt.datetime.fromisoformat(s)
        return (_dt.datetime.now(_dt.timezone.utc) - started).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0


def _pipeline_process_alive(container) -> bool:
    """True if a pipeline process is still running inside `container`.

    Uses the host-side Docker `top` API (no in-container binary dependency) so it
    works on minimal images. If we cannot determine it, we assume alive (never
    reap something that might be a live sibling run).
    """
    try:
        procs = container.top() or {}
        rows = procs.get("Processes") or []
        cmds = " ".join(" ".join(str(c) for c in row) for row in rows)
        return ("run_pipeline_" in cmds or "agent/config_" in cmds
                or "config_go.py" in cmds or "aider" in cmds)
    except Exception:  # noqa: BLE001 - container gone / API hiccup
        return True


def _sweep_orphaned_pipeline_containers(client, logger, keep_name: str) -> int:
    """Reap `kaiju.pipeline.*` containers orphaned by a killed host orchestrator.

    A container whose in-container pipeline finished but whose PID-1 keep-alive
    lingers (because the host process that owned the cleanup `finally` was killed
    with SIGKILL, terminal-close, etc.) shows up here. We remove:
      * any exited/dead `kaiju.pipeline.*` container, and
      * any running one whose pipeline process has exited (PID 1 lingering).
    Concurrency-safe: a genuinely-running sibling still has a live pipeline
    process, so `_pipeline_process_alive` keeps it. `keep_name` (this run's
    container) is always skipped. Returns the number reaped.
    """
    reaped = 0
    try:
        candidates = client.containers.list(
            all=True, filters={"label": "kaiju.harness=1"})
    except Exception as e:  # noqa: BLE001
        logger.debug("orphan sweep: list failed: %s", e)
        return 0
    for c in candidates:
        try:
            name = getattr(c, "name", "") or ""
            if name == keep_name or not name.startswith("kaiju.pipeline."):
                continue
            status = getattr(c, "status", "")
            if status == "running":
                if _pipeline_process_alive(c):
                    continue  # live run (this-or-another concurrent repo) — leave it
                if _container_age_seconds(c) < _SWEEP_GRACE_SEC:
                    continue  # young + no pipeline proc yet -> still in setup window
            logger.info("Reaping orphaned pipeline container %s (status=%s, "
                        "pipeline process not running)", name, status or "?")
            c.remove(force=True)
            reaped += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("orphan sweep: skip %s: %s", getattr(c, "name", "?"), e)
    if reaped:
        logger.info("Orphan sweep removed %d stale pipeline container(s)", reaped)
    return reaped


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

    if args.bridge_url:
        _bridge_url_container = args.bridge_url.replace("127.0.0.1", "host.docker.internal").replace("0.0.0.0", "host.docker.internal").replace("localhost", "host.docker.internal")
        if "host.docker.internal" in _bridge_url_container:
            try:
                client.containers.run(
                    "alpine:latest",
                    command=["sh", "-c", f"wget -q --timeout=5 -O- {_bridge_url_container}/healthz || exit 1"],
                    remove=True,
                    extra_hosts=_extra_hosts(),
                    stdout=True, stderr=True,
                )
                logger.info("Preflight OK: bridge %s reachable from Docker network", _bridge_url_container)
            except Exception as e:
                logger.error(
                    "PREFLIGHT FAILED: bridge %s NOT reachable from Docker network. "
                    "Fix: restart bridge with `KAIJU_CC_BRIDGE_HOST=0.0.0.0 bash scripts/claude_code_bridge.sh restart` "
                    "(or set KAIJU_CC_BRIDGE_HOST=0.0.0.0 in .env). Details: %s",
                    _bridge_url_container, e,
                )
                return 3

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
        "KAIJU_TEST_IDS_DIR": f"/opt/kaiju/outputs/{dataset_id}/datasets",
    }
    # Provider parity with the local pipeline: forward all host provider creds,
    # wire the bridge for anthropic/openai, and stage a Vertex/GCP key file.
    # Resolve a model alias (gpt55, opus48cc, …) to its full name so provider
    # detection sees the real provider. The pipeline gets the original arg and
    # re-resolves it itself (keeping MODEL_SHORT / branch naming consistent).
    resolved_model = _resolve_model_name(args.model)
    if resolved_model != args.model:
        logger.info("Model alias %r -> %s", args.model, resolved_model)
    # Resolve the bridge URL: explicit flag wins; otherwise auto per model.
    bridge_url = (args.bridge_url if args.bridge_url is not None
                  else _default_bridge_url(resolved_model))
    _prov_env, _gac_src = _build_provider_env(resolved_model, bridge_url, logger)
    env.update(_prov_env)
    _prov = "openai-bridge" if "OPENAI_API_BASE" in _prov_env and bridge_url else (
        "anthropic-bridge" if "ANTHROPIC_API_BASE" in _prov_env and bridge_url
        else "direct-creds")
    logger.info("Provider wiring: model=%s mode=%s bridge=%s forwarded=%s%s",
                args.model, _prov, bridge_url or "(none)", sorted(_prov_env),
                " +gcp-creds-file" if _gac_src else "")

    container = None
    rc = 1
    container_name = f"kaiju.pipeline.{repo_name}.{dataset_id[:8]}".lower()
    # A same-named container left behind by a CRASHED prior run (or a killed batch
    # task — the container is a child of dockerd, not of the killed python) makes
    # create_container raise 409 Conflict, which aborts THIS task before `container`
    # is ever assigned, so the finally-block cleanup can't reap anything. Proactively
    # remove any stale namesake first so a crash can't wedge every retry of this id.
    try:
        _stale = client.containers.get(container_name)
        logger.warning("Removing stale container %s (id=%s) before recreate",
                       container_name, _stale.id[:12])
        _stale.remove(force=True)
    except Exception:  # noqa: BLE001 - not-found (normal) or transient API error
        pass
    # Also reap any OTHER kaiju.pipeline.* container orphaned by a killed host
    # orchestrator (SIGKILL/terminal-close can't run our cleanup finally). Skips
    # this run's name and any concurrently-running sibling. Backstop for the case
    # where a prior run's host process died and left its container behind.
    if not args.keep_container:
        _sweep_orphaned_pipeline_containers(client, logger, keep_name=container_name)

    # Turn catchable termination signals into KeyboardInterrupt so the cleanup
    # `finally` runs (SIGINT already does this; SIGTERM/SIGHUP otherwise kill the
    # process WITHOUT running finally -> orphaned container). SIGKILL is
    # uncatchable and is covered by the orphan sweep + the self-destruct TTL below.
    def _sig_to_interrupt(signum, _frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    for _sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _sig_to_interrupt)
            except Exception:  # noqa: BLE001 - not on main thread / unsupported
                pass

    # PID-1 keep-alive with a self-destruct TTL: if the host orchestrator is
    # SIGKILLed (no cleanup possible), the container is auto-removed by dockerd
    # once this PID 1 exits after the TTL, instead of lingering forever. The TTL
    # is set safely above any real run (the pipeline's own wall-cap is 86400s and
    # the whole-pipeline hard cap is --eval-timeout), so it never cuts a live run.
    # `command -v timeout` guard: if `timeout` is missing, fall back to a plain
    # unbounded keep-alive (no regression) instead of exiting instantly.
    # --keep-container (debugging) opts out: keep the classic indefinite keep-alive
    # so the container persists for inspection until the user removes it.
    if args.keep_container:
        _keepalive_cmd = "tail -f /dev/null"
        _auto_remove = False
    else:
        # Size the TTL ABOVE the real worst-case legit runtime so it is a pure
        # safety net that never kills a live run: the pipeline's per-stage wall cap
        # is 86400s and there are 3 stages, so a legitimate run can last ~3*86400s.
        # (--eval-timeout is NOT enforced as a hard cap, so we can't rely on it.)
        _ttl = max(int(getattr(args, "eval_timeout", 0) or 0), 3 * 86400) + 3600
        _keepalive_cmd = [
            "sh", "-c",
            f"command -v timeout >/dev/null 2>&1 && exec timeout {_ttl} tail -f /dev/null "
            f"|| exec tail -f /dev/null",
        ]
        _auto_remove = True
    # Bind-mount the host output dirs into the container so the pipeline writes
    # trajectory data DIRECTLY to the host in real time. Without this, ALL data
    # lives inside the container until a single end-of-run copy-back, so an
    # orchestrator kill/crash mid-run (common on a long repo) strands and LOSES
    # everything. With the mount, data persists live and the copy-back is a no-op.
    host_out_dir = (Path("outputs") / dataset_id).resolve()
    host_out_dir.mkdir(parents=True, exist_ok=True)
    host_harbor_dir = Path("Harbor_Data").resolve()
    host_harbor_dir.mkdir(parents=True, exist_ok=True)
    _mount_volumes = {
        str(host_out_dir): {"bind": f"/opt/kaiju/outputs/{dataset_id}", "mode": "rw"},
        str(host_harbor_dir): {"bind": "/opt/kaiju/Harbor_Data", "mode": "rw"},
    }
    _mounted = False
    # Docker-socket mount (from teammate commit f1d92d1) so an in-container eval
    # could reach the host dockerd. SECURITY: this grants the container ROOT-
    # equivalent control of host docker, exposed to untrusted model-generated code.
    # It is NOT needed by this flow — the pipeline runs `--backend local_inplace`,
    # which evals in an in-container git worktree (no docker-in-docker), so the
    # socket is never used (see evaluate.py: docker path only when backend !=
    # local_inplace). DEFAULT OFF; opt in with KAIJU_MOUNT_DOCKER_SOCK=1 only if you
    # deliberately run an in-container `--backend docker` eval.
    _mount_sock = os.environ.get("KAIJU_MOUNT_DOCKER_SOCK") == "1"
    _sock = os.environ.get("KAIJU_DOCKER_SOCK", "/var/run/docker.sock")
    _volumes = ({_sock: {"bind": "/var/run/docker.sock", "mode": "rw"}}
                if (_mount_sock and Path(_sock).exists()) else None)
    if _volumes:
        logger.warning("Mounting docker socket %s -> /var/run/docker.sock "
                       "(KAIJU_MOUNT_DOCKER_SOCK=1; grants container root-equiv host docker control)", _sock)
    try:
        try:
            container = create_container(
                client=client, image_name=agent_tag,
                container_name=container_name,
                logger=logger, environment=env, extra_hosts=_extra_hosts(),
                # Merge BOTH mount sets: host output/Harbor bind-mount (trajectory
                # persistence, survives an orchestrator kill) + the docker socket.
                volumes={**_mount_volumes, **(_volumes or {})},
                # Bounded keep-alive + auto-remove: self-destruct if the host dies.
                command=_keepalive_cmd, auto_remove=_auto_remove,
            )
            container.start()
            _mounted = True
            logger.info("Bind-mounted host outputs/%s + Harbor_Data into container "
                        "(trajectory persists live; survives an orchestrator kill)", dataset_id)
        except Exception as _mnt_err:  # noqa: BLE001 - degrade gracefully if bind-mount unsupported
            logger.warning("Bind-mount failed (%s) — falling back to internal write + "
                           "end-of-run copy-back. Data will NOT survive an orchestrator "
                           "kill mid-run on this host (check Docker file sharing).", _mnt_err)
            try:
                if container is not None:
                    container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
            container = create_container(
                client=client, image_name=agent_tag,
                container_name=container_name,
                logger=logger, environment=env, extra_hosts=_extra_hosts(),
                volumes=(_volumes or None),  # keep the docker socket if present; drop the output mount
                # FALLBACK (no bind mount): the run's data lives INSIDE this
                # container and is only copied out at the end. So do NOT self-destruct
                # — a plain unbounded keepalive + auto_remove=False keeps an orphaned
                # fallback container (and its recoverable data via `docker cp`) around,
                # rather than nuking it after the TTL and losing the data entirely.
                command="tail -f /dev/null", auto_remove=False,
            )
            container.start()
            _mounted = False

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

        # The captured test-id inventory (+ any spec) lands in the CONSOLIDATED
        # datasets dir via copy_inference_inputs, NOT next to --dataset (which the
        # user typically passes from the repo root — globbing there finds no
        # inventory, or worse, unrelated *.bz2 like spec.pdf.bz2). Stage from the
        # canonical outputs/<uuid>/datasets/ so the in-container eval's
        # KAIJU_TEST_IDS_DIR lookup actually resolves. Fall back to --dataset's
        # dir only if the consolidated dir is absent.
        container_datasets_dir = Path(f"/opt/kaiju/outputs/{dataset_id}/datasets")
        host_datasets_dir = Path("outputs") / dataset_id / "datasets"
        if not host_datasets_dir.is_dir():
            host_datasets_dir = Path(args.dataset).parent
        # Ensure the canonical test-id inventory is present in the mount/stage dir.
        # If nothing is there yet (e.g. --skip-prepare, or a prepare that generated the
        # .bz2 only into commit0/data/test_ids/ but never staged it), copy it over so
        # the in-container agent's get_tests() resolves it instead of raising
        # FileNotFoundError — which crashes the agent worker and yields a degenerate
        # 0/0, $0, empty-pipeline_results run. The container image does NOT bake
        # commit0/data/test_ids/ (gitignored), so staging here is required.
        if not list(host_datasets_dir.glob("*_test_ids.bz2")):
            try:
                from kaiju.paths import normalize_test_ids_key
                _norm = normalize_test_ids_key(repo_name)
                _canon = Path("commit0/data/test_ids") / f"{_norm}.bz2"
                if _canon.is_file():
                    host_datasets_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_canon, host_datasets_dir / f"{_norm}_test_ids.bz2")
                    logger.info("Staged canonical test-id inventory %s -> %s/%s_test_ids.bz2",
                                _canon, host_datasets_dir, _norm)
                else:
                    logger.warning("No canonical test-id inventory at %s — eval will fall back "
                                   "to observed count (and a strict agent may crash)", _canon)
            except Exception as _e:  # noqa: BLE001
                logger.warning("Could not stage canonical test-id inventory: %s", _e)
        if _mounted:
            # outputs/<uuid>/ is host-mounted, so outputs/<uuid>/datasets/ (with the
            # captured inventory) is ALREADY visible inside the container. Copying it
            # in would copy the file onto itself through the mount and truncate it.
            n_inv = len(list(host_datasets_dir.glob("*_test_ids.bz2")))
            if n_inv:
                logger.info("Datasets host-mounted; %d inventory file(s) already in-container", n_inv)
            else:
                logger.warning("No *_test_ids.bz2 in %s — in-container eval will fall back "
                               "to the observed test count (no canonical denominator)", host_datasets_dir)
        else:
            _stream_exec(client, container.id, f"bash -c {shlex.quote('mkdir -p ' + str(container_datasets_dir))}")
            staged_any = False
            for src in host_datasets_dir.glob("*_test_ids.bz2"):
                copy_to_container(container, src, container_datasets_dir / src.name)
                logger.info("Staged inference input: %s -> %s", src.name, container_datasets_dir)
                staged_any = True
            if not staged_any:
                logger.warning("No *_test_ids.bz2 found in %s — the in-container eval will "
                               "fall back to the observed test count (no canonical denominator)",
                               host_datasets_dir)
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

        host_out = Path("outputs") / dataset_id
        if _mounted:
            # Data was written straight to the host via the bind mounts — no copy
            # needed, and it already survived even if this orchestrator had been
            # killed mid-run. Just report what landed.
            n_runs = len(list((host_out / "runs").rglob("pipeline_results.json"))) if (host_out / "runs").is_dir() else 0
            n_atif = len(list((Path("Harbor_Data") / "Trajectory").rglob("trajectory.json"))) if (Path("Harbor_Data") / "Trajectory").is_dir() else 0
            logger.info("Outputs host-mounted — persisted live to %s (%d result file(s)); "
                        "%d ATIF trajectory file(s) in Harbor_Data/Trajectory", host_out.resolve(), n_runs, n_atif)
            if n_atif == 0:
                logger.warning("No ATIF trajectory produced (Harbor_Data/Trajectory empty) — check the ATIF step")
        else:
            # Fallback (mount unavailable): copy outputs/<dataset-id>/ back at the end.
            try:
                with tempfile.TemporaryDirectory() as td:
                    staged = Path(td) / dataset_id
                    copy_from_container(
                        container, Path(f"/opt/kaiju/outputs/{dataset_id}"), staged)
                    shutil.copytree(staged, host_out, dirs_exist_ok=True)
                logger.info("Copied outputs to %s", host_out.resolve())
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not copy outputs/%s: %s", dataset_id, e)
            # ATIF artifact lives at /opt/kaiju/Harbor_Data/Trajectory/ (outside
            # outputs/<uuid>/); copy it to the host location local runs use.
            try:
                host_harbor = Path("Harbor_Data") / "Trajectory"
                with tempfile.TemporaryDirectory() as td:
                    staged = Path(td) / "Trajectory"
                    copy_from_container(
                        container, Path("/opt/kaiju/Harbor_Data/Trajectory"), staged)
                    if staged.exists() and any(staged.iterdir()):
                        host_harbor.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(staged, host_harbor, dirs_exist_ok=True)
                        logger.info("Copied ATIF trajectory artifacts to %s", host_harbor.resolve())
                    else:
                        logger.warning("No ATIF trajectory produced in the container "
                                       "(Harbor_Data/Trajectory empty) — check the ATIF step")
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not copy Harbor_Data/Trajectory: %s", e)
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
