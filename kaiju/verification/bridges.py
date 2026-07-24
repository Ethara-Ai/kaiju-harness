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
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
PORTS = {"gpt": 8788, "claude": 8765}
_LOG = "/tmp/kaiju_verify_bridge_{}.log"

# Secret persisted per port when WE start a bridge, so a LATER invocation can
# authenticate against a bridge a previous verify run left up. Without this,
# an orphaned bridge holds a secrets.token_hex() that died with its parent's
# environment and every future reuse is a guaranteed 401 until someone kills
# the process by hand (the exact failure: "kaiju-bridge: missing/invalid
# bridge secret" from a stale :8765 during BUILD).
_SECRET_STATE = "/tmp/kaiju_verify_bridge_secret_{}"

# Cheap authenticated probe per family — ported from run_trajectory.sh
# _bridge_secret_matches (B2 audit fix): a stale bridge passes /healthz but
# 401s every real request, so liveness alone must never green-light reuse.
_PROBE_PATHS = {"gpt": "/v1/responses", "claude": "/v1/messages"}


def _healthy(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# A 401 from the bridge's OWN auth gate carries one of these markers; a 401
# WITHOUT them is an upstream credential failure passed through on a fully
# authenticated request (revoked ChatGPT/Anthropic OAuth) — the secret was
# ACCEPTED and force-restarting would only destroy a working bridge without
# fixing the credentials.
_BRIDGE_AUTH_MARKERS = (b"bad OPENAI_API_KEY", b"missing/invalid bridge secret")

# run_trajectory.sh's well-known fixed secrets (run_trajectory.sh:290-293) —
# the NORMAL owner of a bridge already up on these shared ports. Without these
# in the candidate chain, every trajectory-owned bridge would be misclassified
# as stale and killed out from under a possibly-concurrent run.
_WELL_KNOWN_SECRETS = {"gpt": "kaiju-trajectory-fixed", "claude": "kaiju-cc-fixed"}


def _secret_matches(port: int, fam: str, secret: str, timeout: float = 5.0) -> bool | None:
    """Tri-state probe: True = the bridge accepted ``secret`` (any non-401
    response, or a 401 whose body is an UPSTREAM credential error); False =
    the bridge's own auth gate rejected it (401 with a bridge-auth marker);
    None = inconclusive (no response in time — a VALID secret's probe is fully
    proxied upstream and can legitimately outlive the timeout, so the caller
    must NEVER treat None as a mismatch). Sends both auth header forms (the
    codex bridge reads Bearer or x-api-key; the cc bridge likewise)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{_PROBE_PATHS[fam]}",
        data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {secret}", "x-api-key": secret,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return True
        try:
            body = e.read(4096)
        except Exception:
            body = b""
        return None if not body else (
            True if not any(m in body for m in _BRIDGE_AUTH_MARKERS) else False)
    except Exception:
        return None


def _read_persisted_secret(port: int) -> str | None:
    try:
        text = Path(_SECRET_STATE.format(port)).read_text().strip()
        return text or None
    except OSError:
        return None


def _persist_secret(port: int, secret: str) -> None:
    try:
        path = Path(_SECRET_STATE.format(port))
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(secret)
    except OSError:
        pass  # persistence is best-effort; same-process use still works


def _reusable_key(fam: str, port: int) -> str | None:
    """The first candidate secret the running bridge accepts, or None ONLY if
    every candidate was DEFINITIVELY rejected by the bridge's own auth gate
    (stale — caller may restart it). An inconclusive probe (timeout — e.g. a
    valid secret proxied to a slow/rate-limited upstream) is retried once with
    a longer window and then FAILS OPEN to reuse: killing a possibly-in-use
    bridge on the strength of a timeout would sever a concurrent run's
    streams, while a wrong reuse merely fails THIS verification with a clear
    401. Candidates: client-key env, bridge-secret env, the secret persisted
    by a previous verify run, run_trajectory.sh's well-known fixed secret, and
    a stub (accepted iff the bridge runs unauthenticated)."""
    env_key = (os.environ.get("OPENAI_API_KEY") if fam == "gpt"
               else os.environ.get("ANTHROPIC_API_KEY"))
    env_secret = os.environ.get("KAIJU_CODEX_BRIDGE_SECRET" if fam == "gpt"
                                else "KAIJU_CC_BRIDGE_SECRET")
    candidates = (env_key, env_secret, _read_persisted_secret(port),
                  _WELL_KNOWN_SECRETS[fam], "bridge")
    seen: set[str] = set()
    first_inconclusive: str | None = None
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        verdict = _secret_matches(port, fam, cand)
        if verdict is None:
            verdict = _secret_matches(port, fam, cand, timeout=20.0)
        if verdict is True:
            return cand
        if verdict is None and first_inconclusive is None:
            first_inconclusive = cand
    return first_inconclusive  # None only when ALL candidates got a hard 401


def _live_bridge_users(port: int) -> list[int]:
    """Live PIDs registered in run_trajectory.sh's shared-bridge ref-count dir
    (${TMPDIR:-/tmp}/kaiju_bridge_<port>.users — one file per run, named by
    pid). A non-empty answer means a concurrent trajectory run is actively
    using this bridge and it must NOT be killed."""
    users_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"kaiju_bridge_{port}.users"
    live: list[int] = []
    try:
        for entry in users_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                os.kill(pid, 0)
                live.append(pid)
            except OSError:
                pass  # dead registrant; run_trajectory prunes these itself
    except OSError:
        pass
    return live


def _port_has_listener(port: int) -> bool:
    try:
        out = subprocess.run(["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=10).stdout
        return any(p.strip().isdigit() for p in out.split())
    except Exception:
        return False


def _force_free_port(port: int, fam: str) -> None:
    """Stop a stale bridge holding ``port``. For claude, the stop script first
    (it tears down the auto-restart monitor — killing just the listener would
    get it resurrected with the same unusable secret); then SIGCONT+TERM->KILL
    any remaining listener (a T-state process holds its port but never answers
    — the suspended-bridge failure mode)."""
    if fam == "claude":
        try:
            subprocess.run(["bash", "scripts/claude_code_bridge.sh", "stop"],
                           cwd=str(_REPO), capture_output=True, timeout=30)
        except Exception:
            pass
    for attempt in range(2):
        try:
            out = subprocess.run(["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return
        pids = [int(p) for p in out.split() if p.strip().isdigit()]
        if not pids:
            return
        for pid in pids:
            for sig in ((signal.SIGCONT, signal.SIGTERM) if attempt == 0
                        else (signal.SIGKILL,)):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
        deadline = time.time() + 5
        while time.time() < deadline and _healthy(port, timeout=1.0):
            time.sleep(0.5)


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
            # Reuse ONLY if the running bridge accepts a secret we can hand to
            # the client (B2: /healthz liveness alone must never green-light
            # reuse — a stale bridge 401s every request and BUILD/JUDGE fail).
            key = _reusable_key(fam, port)
            if key is not None:
                _persist_secret(port, key)  # remember the working key too
                applied.update(_client_env(fam, key))
                print(f"   [bridge] reusing healthy {fam} bridge on :{port} (secret validated)")
                continue
            # Every candidate got a hard 401 from the bridge's own auth gate:
            # stale, started by a dead process with a random secret nobody can
            # reproduce. Unusable by any client we can construct — replace it,
            # UNLESS a live trajectory run is registered as a user (then the
            # kill would sever its in-flight streams; it can authenticate, we
            # just can't — surface that instead of destroying its bridge).
            users = _live_bridge_users(port)
            if users:
                print(f"   [bridge] {fam} bridge on :{port} rejects every known "
                      f"secret BUT live run(s) {users} are using it — NOT "
                      "restarting; verification calls will fail 401 (rerun "
                      "after those runs finish, or export the bridge secret)")
                applied.update(_client_env(fam, "bridge"))
                continue
            print(f"   [bridge] {fam} bridge on :{port} rejects every known secret "
                  "(stale from a previous session) — restarting it")
            _force_free_port(port, fam)
        elif _port_has_listener(port):
            # Unhealthy but the port IS held — a suspended (T-state) or wedged
            # bridge: /healthz never answers, a fresh start can't bind. Free it
            # first (the SIGCONT in _force_free_port handles the T-state case).
            print(f"   [bridge] :{port} held by an unresponsive process — freeing it")
            _force_free_port(port, fam)
        secret = os.environ.get("KAIJU_CODEX_BRIDGE_SECRET" if fam == "gpt"
                                else "KAIJU_CC_BRIDGE_SECRET") or secrets.token_hex(16)
        print(f"   [bridge] starting {fam} bridge on :{port} ...")
        proc = _start_codex(secret) if fam == "gpt" else _start_cc(secret)
        if not _wait_healthy(port):
            print(f"   [bridge] FAILED to start {fam} bridge on :{port} "
                  f"(see {_LOG.format('codex' if fam == 'gpt' else 'cc')})")
            continue
        # Post-start gate: /healthz alone can be answered by a FOREIGN bridge
        # that survived _force_free_port or won a concurrent bind race — then
        # persisting/applying OUR secret would poison the state file and 401
        # every call under a deceptive "UP" log. Only a hard rejection fails
        # the start (None = our own bridge accepted the secret locally and the
        # probe's proxied upstream call was just slow).
        if _secret_matches(port, fam, secret) is False:
            print(f"   [bridge] :{port} answers /healthz but rejects OUR secret "
                  f"— a foreign {fam} bridge holds the port (bind race or "
                  "unkillable stale process); NOT registering it")
            continue
        _persist_secret(port, secret)  # let the NEXT invocation reuse it
        applied.update(_client_env(fam, secret))
        started.append((fam, proc, port))
        print(f"   [bridge] {fam} bridge UP on :{port}")

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
