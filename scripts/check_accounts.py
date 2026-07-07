#!/usr/bin/env python3
"""Pre-flight account freshness checker for the Claude Code multi-account pool.

Before starting a trajectory-generation batch, verify that every account in
``KAIJU_CC_ACCOUNT_POOL`` is (a) reachable/valid, (b) fresh (low 5-hour rolling
utilization), and (c) a DISTINCT Anthropic org (independent quota). Two accounts
sharing an org share one quota, so capping one caps both -- which silently
breaks rotation. This catches that, plus already-used or dead accounts, BEFORE a
run wastes real quota.

Parsing reuses the bridge's own ``load_account_pool`` so the account list is
exactly what the bridge will use (file paths, ``keychain:<service>``, or
``default``, colon-separated).

Usage:
    .venv/bin/python scripts/check_accounts.py                 # read KAIJU_CC_ACCOUNT_POOL from env
    .venv/bin/python scripts/check_accounts.py --pool "/a.json:/b.json"
    .venv/bin/python scripts/check_accounts.py --threshold 0.10   # "fresh" = util < 10%
    .venv/bin/python scripts/check_accounts.py --env .env         # load pool from a .env file

Exit code: 0 if ALL accounts are healthy, fresh, and distinct orgs; non-zero
otherwise (so it can gate a batch launcher: `check_accounts.py && launch...`).
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

# Import the REAL pool loader so parsing matches the bridge exactly.
try:
    from agent.claude_code.credentials import load_account_pool
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent.claude_code.credentials import load_account_pool

ANTHROPIC = "https://api.anthropic.com/v1/messages"
# A valid current model is required to get a 200 (and thus the utilization
# header). An invalid model returns 404 -- it still carries the org-id but NOT
# the utilization figure, so we use a real model.
PROBE_MODEL = "claude-haiku-4-5-20251001"
SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _load_pool_spec(args) -> str:
    if args.pool:
        return args.pool
    spec = os.environ.get("KAIJU_CC_ACCOUNT_POOL", "").strip()
    if spec:
        return spec
    # fall back to a .env file
    env_path = args.env or ".env"
    if os.path.isfile(env_path):
        for line in open(env_path):
            line = line.strip()
            if line.startswith("KAIJU_CC_ACCOUNT_POOL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def probe(token: str, timeout: float) -> dict:
    """Probe Anthropic once; return org-id, 5h utilization, status, http code."""
    out = {"http": None, "org": None, "util": None, "status": None, "reset": None,
           "error": None}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.post(
                ANTHROPIC,
                headers={
                    "authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20,claude-code-20250219",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": PROBE_MODEL,
                    "max_tokens": 1,
                    "system": SYSTEM_PREFIX,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        h = r.headers
        out["http"] = r.status_code
        out["org"] = h.get("anthropic-organization-id")
        u = h.get("anthropic-ratelimit-unified-5h-utilization")
        out["util"] = float(u) if u is not None else None
        out["status"] = h.get("anthropic-ratelimit-unified-5h-status")
        out["reset"] = h.get("anthropic-ratelimit-unified-5h-reset")
    except httpx.HTTPError as e:
        out["error"] = str(e)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Pre-flight multi-account freshness checker")
    p.add_argument("--pool", help="Pool spec (default: $KAIJU_CC_ACCOUNT_POOL or .env)")
    p.add_argument("--env", help="Path to .env to read the pool from (default: .env)")
    p.add_argument("--threshold", type=float, default=0.10,
                   help="'fresh' = 5h utilization below this (default 0.10 = 10%%)")
    p.add_argument("--timeout", type=float, default=30.0)
    args = p.parse_args(argv)

    spec = _load_pool_spec(args)
    if not spec:
        print("ERROR: no pool spec (set KAIJU_CC_ACCOUNT_POOL, --pool, or .env)",
              file=sys.stderr)
        return 2

    pool = load_account_pool(spec)
    if pool is None:
        print(f"ERROR: pool spec parsed to zero accounts: {spec!r}", file=sys.stderr)
        return 2

    slots = pool._slots  # noqa: SLF001 - read-only inspection
    print(f"Pool: {len(slots)} account(s)\n")
    print(f"{'#':<3}{'label':<46}{'http':<6}{'util5h':<9}{'org-id':<38}verdict")
    print("-" * 118)

    rows = []
    for idx, slot in enumerate(slots):
        try:
            token = slot.provider.get_access_token()
        except Exception as e:  # noqa: BLE001
            rows.append({"idx": idx, "label": slot.label, "load_error": str(e)})
            continue
        r = probe(token, args.timeout)
        r["idx"] = idx
        r["label"] = slot.label
        rows.append(r)

    orgs: dict[str, list[int]] = {}
    all_ok = True
    for r in rows:
        idx, label = r["idx"], r["label"]
        short = (label[:44] + "..") if len(label) > 44 else label
        if r.get("load_error"):
            print(f"{idx:<3}{short:<46}{'--':<6}{'--':<9}{'--':<38}DEAD (cred load failed)")
            all_ok = False
            continue
        http, util, org = r["http"], r["util"], r["org"]
        util_s = f"{util:.2f}" if util is not None else "?"
        org_s = org or "?"
        if org:
            orgs.setdefault(org, []).append(idx)
        # verdicts
        if r["error"] or http is None:
            verdict, ok = f"UNREACHABLE ({r['error']})", False
        elif http == 401:
            verdict, ok = "INVALID/EXPIRED token (401)", False
        elif http == 429:
            verdict, ok = "CAPPED now (429, rate-limited)", False
        elif http not in (200, 404):
            verdict, ok = f"HTTP {http}", False
        elif util is None:
            verdict, ok = "reachable, util unknown", True
        elif util >= args.threshold:
            verdict, ok = f"USED ({util*100:.0f}% >= {args.threshold*100:.0f}%)", False
        else:
            verdict, ok = "FRESH", True
        all_ok &= ok
        print(f"{idx:<3}{short:<46}{str(http):<6}{util_s:<9}{org_s:<38}{verdict}")

    # duplicate-org detection (shared quota = rotation breaks)
    dupes = {o: idxs for o, idxs in orgs.items() if len(idxs) > 1}
    print()
    if dupes:
        all_ok = False
        for o, idxs in dupes.items():
            print(f"DUPLICATE ORG: slots {idxs} all map to org {o} "
                  f"-> they SHARE quota; capping one caps all. Rotation will NOT work.")
    else:
        distinct = len(orgs)
        print(f"Org distinctness: {distinct} distinct org(s) across "
              f"{len([r for r in rows if r.get('org')])} reachable account(s) -> "
              f"{'OK (independent quotas)' if distinct == len([r for r in rows if r.get('org')]) else 'check above'}")

    print()
    print("RESULT:", "ALL ACCOUNTS FRESH & DISTINCT -> safe to launch"
          if all_ok else "PROBLEMS FOUND -> review before launching")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
