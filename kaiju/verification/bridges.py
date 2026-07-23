"""Ensure the model bridges the verifiers need are RUNNING before they run.

The pipeline starts only the run's own bridge; verification also needs the
CROSS-FAMILY bridge for the judge (a GPT run is judged by Claude and vice versa).
This starts/reuses both subscription bridges (codex :8788, claude-code :8765) and
returns the client env (base_url + key) so the litellm clients reach them.

    from kaiju.verification.bridges import ensure_bridges
    env, cleanup = ensure_bridges({"gpt", "claude"})   # merges into os.environ
    ...run verification...
    cleanup()                                           # stops bridges we started
"""
from __future__ import annotations

import os
import secrets
import subprocess
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
PORTS = {"gpt": 8788, "claude": 8765}
_LOG = "/tmp/kaiju_verify_bridge_{}.log"


def _healthy(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_healthy(port: int, tries: int = 40) -> bool:
    for _ in range(tries):
        if _healthy(port):
            return True
        time.sleep(1)
    return False


def _start_codex(secret: str):
    log = open(_LOG.format("codex"), "wb")
    env = {**os.environ, "KAIJU_CODEX_BRIDGE_SECRET": secret}
    return subprocess.Popen(
        ["python", "-m", "agent.openai_codex", "--host", "127.0.0.1", "--port", str(PORTS["gpt"])],
        cwd=str(_REPO), env=env, stdout=log, stderr=log, start_new_session=True)


def _start_cc(secret: str):
    log = open(_LOG.format("cc"), "wb")
    env = {**os.environ, "KAIJU_CC_BRIDGE_SECRET": secret, "KAIJU_CC_BRIDGE_HOST": "127.0.0.1"}
    # the script backgrounds itself + arms a watchdog; we just wait for /healthz.
    return subprocess.Popen(
        ["bash", "scripts/claude_code_bridge.sh", "start"],
        cwd=str(_REPO), env=env, stdout=log, stderr=log, start_new_session=True)


def _client_env(family: str, secret: str) -> dict:
    if family == "gpt":
        return {"OPENAI_BASE_URL": f"http://127.0.0.1:{PORTS['gpt']}", "OPENAI_API_KEY": secret}
    return {"ANTHROPIC_API_BASE": f"http://127.0.0.1:{PORTS['claude']}",
            "ANTHROPIC_API_KEY": secret}


def ensure_bridges(families: set[str]) -> tuple[dict, callable]:
    """Ensure a healthy bridge for each family. Reuses one already up; starts the
    rest. Returns (env-to-apply, cleanup()). cleanup stops only bridges WE started."""
    families = {f for f in families if f in PORTS}
    applied: dict = {}
    started: list = []
    for fam in sorted(families):
        port = PORTS[fam]
        if _healthy(port):
            # reuse — keep whatever secret the running bridge/env uses
            key = (os.environ.get("OPENAI_API_KEY") if fam == "gpt"
                   else os.environ.get("ANTHROPIC_API_KEY")) or "bridge"
            applied.update(_client_env(fam, key))
            print(f"   [bridge] reusing healthy {fam} bridge on :{port}")
            continue
        secret = os.environ.get("KAIJU_CODEX_BRIDGE_SECRET" if fam == "gpt"
                                else "KAIJU_CC_BRIDGE_SECRET") or secrets.token_hex(16)
        print(f"   [bridge] starting {fam} bridge on :{port} ...")
        proc = _start_codex(secret) if fam == "gpt" else _start_cc(secret)
        if _wait_healthy(port):
            applied.update(_client_env(fam, secret))
            started.append((fam, proc, port))
            print(f"   [bridge] {fam} bridge UP on :{port}")
        else:
            print(f"   [bridge] FAILED to start {fam} bridge on :{port} "
                  f"(see {_LOG.format('codex' if fam == 'gpt' else 'cc')})")

    os.environ.update(applied)

    def cleanup():
        for fam, proc, port in started:
            try:
                if fam == "claude":
                    subprocess.run(["bash", "scripts/claude_code_bridge.sh", "stop"],
                                   cwd=str(_REPO), capture_output=True, timeout=30)
                else:
                    proc.terminate()
            except Exception:
                pass
        if started:
            print(f"   [bridge] stopped {len(started)} bridge(s) we started")

    return applied, cleanup


def families_for_run(run_model: str, *, generation: bool, judge: bool) -> set[str]:
    """Which bridge families a verification of *run_model* needs. NOTE: the BUILD
    (generation) now validates the rubric by judging the golden/stub solutions, so
    it needs the JUDGE bridge in addition to the generation bridge."""
    from .model_client import model_family, cross_family_judge_model
    fams: set[str] = set()
    gen_fam = model_family(run_model)
    if gen_fam == "other":
        gen_fam = "claude"     # generation default falls back to Claude
    judge_fam = model_family(cross_family_judge_model(run_model))
    if generation:
        fams.add(gen_fam)
        fams.add(judge_fam)    # build judges golden/stub to validate the rubric
    if judge:
        fams.add(judge_fam)
    return fams
