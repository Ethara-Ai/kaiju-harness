"""CLI: verify a produced trajectory's run directory.

    python -m kaiju.verification <run_dir> [--out PATH] [--json] [--quiet]

Exit code is 0 when the gate ACCEPTs, 1 when it QUARANTINEs — so the pipeline's
verify step can branch on it.
"""
from __future__ import annotations

import argparse
import json
import sys

from .evaluator import verify_run
from .schemas import CheckStatus, Gate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kaiju.verification", description=__doc__)
    ap.add_argument("run_dir", help="outputs/<uuid>/runs/<model>/agent/run_<N>")
    ap.add_argument("--out", default=None, help="explicit report path (else consolidated default)")
    ap.add_argument("--json", action="store_true", help="print the full report JSON to stdout")
    ap.add_argument("--quiet", action="store_true", help="only print the gate + score line")
    args = ap.parse_args(argv)

    try:
        report = verify_run(args.run_dir, out_path=args.out)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif not args.quiet:
        _print_human(report)
    score = "undefined" if report.graded_score is None else report.graded_score
    unenforced = report.meta.get("unenforced_gating_concerns") or []
    caveat = f"  (UNVERIFIED: {len(unenforced)} gating concern(s) not yet enforced)" if unenforced else ""
    print(f"GATE={report.gate.value.upper()}  score={score}  "
          f"report={report.meta.get('report_path', '(not written)')}{caveat}")
    return 0 if report.gate is Gate.ACCEPT else 1


def _print_human(report) -> None:
    icon = {
        CheckStatus.PASS: "PASS", CheckStatus.FAIL: "FAIL",
        CheckStatus.NOT_APPLICABLE: "n/a ", CheckStatus.PENDING: "pend",
        CheckStatus.ERROR: "ERR ",
    }
    for r in report.results:
        gate_mark = "*" if r.gating else " "
        print(f"  [{icon[r.status]}]{gate_mark} {r.concern_id:<32} {r.summary}")
    print(f"  status tally: {report.meta.get('status_tally')}")


if __name__ == "__main__":
    sys.exit(main())
