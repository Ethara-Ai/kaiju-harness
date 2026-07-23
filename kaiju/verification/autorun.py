"""Automatic verification entry — called by the pipeline's verify step so every
produced trajectory is verified WITHOUT a manual step.

    python -m kaiju.verification.autorun outputs/<uuid> [--judge] [--build]

Default (no flags): runs the DETERMINISTIC gate on every run dir under the uuid
(offline, no model, no cost) and writes a report per run. `--build` authors +
freezes the TRUTH.md/rubric bundle (needs the golden diff + a model); `--judge`
runs the cross-family LLM judge on each run (needs a frozen bundle + a model) and
folds the Layer-2 verdicts into the report.

Never fatal: verification problems are reported, never break the pipeline.
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from .evaluator import verify_run
from .schemas import Gate


def _run_dirs(uuid_root: Path) -> list[Path]:
    return sorted({Path(p).parent for p in
                   glob.glob(str(uuid_root / "runs" / "*" / "agent" / "run_*" / "pipeline_results.json"))})


def verify_uuid(uuid_root: str | Path, *, judge: bool = False, build: bool = False,
                reexec: bool = False, pytest: bool = False) -> int:
    uuid_root = Path(uuid_root)
    runs = _run_dirs(uuid_root)
    if not runs:
        print(f"   [verify] no run dirs under {uuid_root}")
        return 0

    # Ensure the model bridges the LLM steps need are RUNNING (generation family +
    # the cross-family judge bridge). Started bridges are stopped at the end.
    cleanup = None
    if (build or judge) and not _no_bridges():
        cleanup = _ensure_bridges(runs, build=build, judge=judge)

    if build:
        _maybe_build_bundle(uuid_root, runs)

    quarantined = 0
    for run in runs:
        if reexec:
            _maybe_reexec(run)
        if pytest:
            _maybe_pytest(run)
        if judge:
            _maybe_judge(run)
        try:
            report = verify_run(run)
        except Exception as exc:  # never break the pipeline
            print(f"   [verify] ERROR on {run}: {type(exc).__name__}: {exc}")
            continue
        tag = run.relative_to(uuid_root)
        unenf = report.meta.get("unenforced_gating_concerns") or []
        caveat = f"  (UNVERIFIED: {len(unenf)} gating pending)" if unenf else ""
        score = "undef" if report.graded_score is None else report.graded_score
        print(f"   [verify] {tag}: GATE={report.gate.value.upper()} score={score}{caveat}")
        if report.gate is Gate.QUARANTINE:
            quarantined += 1
            print(f"            gating failures: {[r.concern_id for r in report.gating_failures]}")
    print(f"   [verify] {len(runs)} run(s): {quarantined} quarantined, "
          f"{len(runs) - quarantined} accepted")
    if cleanup is not None and _stop_bridges_after():
        cleanup()
    return quarantined


def _no_bridges() -> bool:
    import os
    return os.environ.get("KAIJU_VERIFY_NO_BRIDGES", "0") == "1"


def _stop_bridges_after() -> bool:
    import os
    # keep bridges up by default (a subsequent --judge/--pytest reuses them);
    # set KAIJU_VERIFY_STOP_BRIDGES=1 to tear down the ones we started.
    return os.environ.get("KAIJU_VERIFY_STOP_BRIDGES", "0") == "1"


def _ensure_bridges(runs: list[Path], *, build: bool, judge: bool):
    try:
        from .bridges import ensure_bridges, families_for_run
        model = _run_model(runs[0]) if runs else ""
        fams = families_for_run(model, generation=build, judge=judge)
        if not fams:
            return None
        _env, cleanup = ensure_bridges(fams)
        return cleanup
    except Exception as exc:
        print(f"   [bridge] ensure failed ({type(exc).__name__}: {exc}); "
              "LLM steps may skip if a bridge is down")
        return None


def _run_model(run: Path) -> str:
    import json
    try:
        d = json.loads((run / "pipeline_results.json").read_text())
        return str(d.get("model") or d.get("model_short") or "")
    except (OSError, ValueError):
        return ""


def _maybe_judge(run: Path) -> None:
    try:
        from .orchestrate import judge_run, load_bundle, uuid_root_of
        from .model_client import cross_family_judge_model
        if load_bundle(uuid_root_of(run)) is None:
            print(f"   [verify] JUDGE: no frozen bundle for {run.name} — build it first "
                  "(KAIJU_VERIFY_BUILD=1)")
            return
        judge_model = cross_family_judge_model(_run_model(run))
        print(f"   [verify] JUDGE: judging {run.name} with {judge_model} (cross-family)")
        res = judge_run(run)
        if res is not None:
            passed = sum(1 for v in res.verdicts if v.passed)
            print(f"   [verify] JUDGE: {passed}/{len(res.verdicts)} criteria passed "
                  "-> rubric_results.json written")
    except Exception as exc:
        print(f"   [verify] JUDGE FAILED for {run.name}: {type(exc).__name__}: {exc}\n"
              "            (is the cross-family bridge up? e.g. claude-code :8765 to judge a GPT run)")


def _maybe_reexec(run: Path) -> None:
    """Independent re-execution: re-run the frozen tests fresh and store the result
    so L0.INDEPENDENT_REEXEC can compare it to the recorded number."""
    try:
        from .reexec import run_independent_reexec
        from .reexec_runner import default_eval_runner
        run_independent_reexec(run, default_eval_runner(run))
    except Exception as exc:
        print(f"   [verify] reexec skipped for {run}: {type(exc).__name__}: {exc}")


def _maybe_pytest(run: Path) -> None:
    """Execute the generated pytest against golden/stub/agent-solution checkouts and
    store the held-out result for L1.HELDOUT_GAP. Needs the repo runtime (deps)."""
    try:
        from .pytest_exec import run_generated_pytest
        out = run_generated_pytest(run)
        if out is None:
            print(f"   [verify] PYTEST: skipped for {run.name} (no generated pytest or repo)")
            return
        a = out.get("analysis") or {}
        print(f"   [verify] PYTEST: {a.get('n_sound')} sound tests "
              f"(dropped {len(a.get('wrong_oracle_dropped') or [])} wrong-oracle); "
              f"solution {a.get('solution_pass_on_sound')} on sound (gap {a.get('gap_vs_golden')})")
    except Exception as exc:
        print(f"   [verify] PYTEST skipped for {run.name}: {type(exc).__name__}: {exc}")


def _maybe_build_bundle(uuid_root: Path, runs: list[Path]) -> None:
    from .orchestrate import verification_dir
    vdir = verification_dir(uuid_root)
    if (vdir / "TRUTH.md").exists():
        print(f"   [verify] BUILD: bundle already frozen at {vdir} (pre-registered)")
        return
    try:
        from .build_inputs import build_inputs_for_uuid, build_and_freeze_from_uuid
        from .model_client import generation_client_for_run
        inp = build_inputs_for_uuid(uuid_root)
        if inp is None:
            print(f"   [verify] BUILD SKIPPED for {uuid_root.name}: could not assemble inputs "
                  "(repo checkout not found on host, or golden diff empty). TRUTH.md/verifiers "
                  "CANNOT be generated without the repo + base/reference commits.")
            return
        client = generation_client_for_run(_run_model(runs[0]) if runs else "")
        print(f"   [verify] BUILD: authoring TRUTH.md + rubric + predicates with {client.model} "
              f"(golden diff {len(inp.golden_diff)} chars, {len(inp.stub_files)} stub file(s))")
        path = build_and_freeze_from_uuid(uuid_root, client)
        if path:
            print(f"   [verify] BUILD DONE — frozen under {vdir}: TRUTH.md, "
                  "verifiers/rubric/rubric.json, verifiers/pytest/predicates.json, "
                  "verifiers/coverage.json, target_manifest.json, meta_verification.json")
    except Exception as exc:
        print(f"   [verify] BUILD FAILED for {uuid_root.name}: {type(exc).__name__}: {exc}\n"
              "            (is a model bridge running for generation?)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kaiju.verification.autorun", description=__doc__)
    ap.add_argument("uuid_root", help="outputs/<uuid>")
    ap.add_argument("--judge", action="store_true", help="run the cross-family LLM judge")
    ap.add_argument("--build", action="store_true", help="author+freeze the TRUTH/rubric bundle")
    ap.add_argument("--reexec", action="store_true", help="independent re-run of the frozen tests")
    ap.add_argument("--pytest", action="store_true", help="execute the generated pytest suite")
    args = ap.parse_args(argv)
    verify_uuid(args.uuid_root, judge=args.judge, build=args.build, reexec=args.reexec,
                pytest=args.pytest)
    return 0  # never fatal


if __name__ == "__main__":
    sys.exit(main())
