"""Pre-eval guard: verify the FROZEN test-id inventory is present for every repo.

Why this exists
---------------
The scoring denominator for a run is the frozen per-repo test-id inventory
(``commit0/data/<subdir>/<repo>.bz2``, staged into
``outputs/<uuid>/datasets/<repo>_test_ids.bz2`` and resolved at eval time via
``kaiju.paths.find_test_ids_file``). When that inventory is MISSING, several
language evaluators SILENTLY fall back to "compare against all discovered
tests" — a wrong, non-reproducible denominator. Real log:

    commit0.harness.get_go_test_ids - WARNING - No Go test ID files found for
    lego ... Returning empty test IDs — evaluation will compare against all
    discovered tests.

This module makes that condition LOUD and FAIL-FAST *before* any eval runs.
It resolves the inventory with the SAME function the eval uses
(``find_test_ids_file``), so a "present" verdict here means the eval will
actually find it — no drift between guard and eval.

CLI
---
    python -m kaiju.verify_inventory \
        --language <c|cpp|go|js|ts|rust|java|python> \
        [--dataset <path-or-hub-id>] \
        [--repo-split <split>] \
        [--datasets-dir <outputs/<uuid>/datasets>] \
        [--strict | --no-strict]

Exit status:
    0  every enumerated repo has a resolvable frozen inventory
    2  one or more repos are missing it AND --strict (the default)
    0  missing repos, but --no-strict (warn-only; prints the list)

Set env ``KAIJU_REQUIRE_INVENTORY=0`` to force warn-only (overrides --strict);
``KAIJU_TEST_IDS_DIR`` (set by the containerized run to the staged datasets
dir) is honored transparently through ``find_test_ids_file``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List

# Per-language commit0/data/<subdir>/ that holds the frozen inventory. MUST stay
# in lockstep with agent/container/run_pipeline_containerized.py::_TEST_IDS_SUBDIR
# and each evaluate_<lang>.py's find_test_ids_file(subdir=...) call.
TEST_IDS_SUBDIR = {
    "go": "test_ids",
    "python": "test_ids",
    "ts": "test_ids",
    "js": "test_ids",
    "c": "c_test_ids",
    "cpp": "cpp_test_ids",
    "rust": "rust_test_ids",
    "java": "java_test_ids",
}

# Per-language SPLIT dict used to enumerate repos when a local dataset JSON is
# not available (e.g. a hub dataset id like wentingzhao/commit0_c).
_SPLIT_IMPORT = {
    "c": ("commit0.harness.constants_c", "C_SPLIT"),
    "cpp": ("commit0.harness.constants_cpp", "CPP_SPLIT"),
    "go": ("commit0.harness.constants_go", "GO_SPLIT"),
    "js": ("commit0.harness.constants_js", "JS_SPLIT"),
    "ts": ("commit0.harness.constants_ts", "TS_SPLIT"),
    "rust": ("commit0.harness.constants_rust", "RUST_SPLIT"),
    "java": ("commit0.harness.constants_java", "JAVA_SPLIT"),
    "python": ("commit0.harness.constants", "SPLIT"),
}


def _repos_from_dataset(dataset_path: str) -> List[str]:
    with open(dataset_path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    repos = []
    for item in data:
        repo = item.get("repo") or item.get("repo_name") or ""
        if repo:
            repos.append(repo.split("/")[-1])
    return repos


def _repos_from_split(language: str, repo_split: str) -> List[str]:
    mod_name, attr = _SPLIT_IMPORT[language]
    try:
        mod = __import__(mod_name, fromlist=[attr])
        split = getattr(mod, attr, {}) or {}
    except Exception:  # noqa: BLE001
        return []
    return sorted(split.get(repo_split, []) or [])


def enumerate_repos(language: str, dataset: str | None, repo_split: str | None) -> List[str]:
    """Return the repo basenames the eval will score, mirroring verify_spec_docs_*."""
    if dataset and not dataset.startswith("wentingzhao/") and Path(dataset).is_file():
        try:
            return _repos_from_dataset(dataset)
        except Exception:  # noqa: BLE001
            pass
    if repo_split and language in _SPLIT_IMPORT:
        return _repos_from_split(language, repo_split)
    return []


def missing_inventory(language: str, repos: List[str]) -> List[str]:
    """Return the subset of *repos* whose frozen inventory does NOT resolve.

    Uses the SAME resolver the eval uses (find_test_ids_file), honoring
    KAIJU_TEST_IDS_DIR, so this verdict matches what the eval will see.
    """
    import commit0
    from kaiju.paths import find_test_ids_file

    commit0_path = os.path.dirname(commit0.__file__)
    subdir = TEST_IDS_SUBDIR.get(language, "test_ids")
    missing: List[str] = []
    for repo in repos:
        if not repo:
            continue
        # A composite "a__b" instance keys off two files (fail/pass); requiring the
        # fail_to_pass side is sufficient to detect a wholly-missing inventory.
        if "__" in repo:
            fname = f"{repo}#fail_to_pass.bz2"
        else:
            fname = f"{repo}.bz2"
        if find_test_ids_file(commit0_path, subdir, fname) is None:
            missing.append(repo)
    return missing


def _dataset_reference_commits(dataset: str | None) -> dict[str, str]:
    """Map repo basename -> reference_commit from a local dataset JSON.

    Returns {} for a hub-id dataset or when the file cannot be read, in which
    case the freshness check degrades to integrity-only (it cannot compare
    against the canonical reference_commit without the dataset).
    """
    if not dataset or dataset.startswith("wentingzhao/") or not Path(dataset).is_file():
        return {}
    try:
        with open(dataset) as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    ref: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        repo = item.get("repo") or item.get("repo_name") or ""
        rc = item.get("reference_commit") or ""
        if repo and rc:
            ref[repo.split("/")[-1]] = rc
    return ref


def stale_inventory(
    language: str, repos: List[str], dataset: str | None
) -> tuple[List[str], List[str], List[str]]:
    """Freshness / provenance cross-check for the frozen inventory (QC-C8-011).

    For each resolvable ``<repo>.bz2`` we look for a sibling
    ``<repo>.bz2.meta.json`` provenance sidecar (``{reference_commit,
    content_sha256, ...}``). When present we (a) recompute the bz2 content hash
    and (b) compare its ``reference_commit`` to the dataset instance. The
    pre-eval guard verifies only EXISTENCE, so a bz2 frozen from a stale
    reference_commit (or silently corrupted) would pass unnoticed and score the
    run against a wrong denominator.

    Returns ``(corrupt, drifted, no_provenance)``:
      * ``corrupt``      — sidecar present but content_sha256 mismatches (or the
                           sidecar is unreadable): the bz2 does not match what
                           was frozen.
      * ``drifted``      — sidecar.reference_commit != dataset.reference_commit.
      * ``no_provenance``— no sidecar found (cannot verify freshness).

    ``corrupt``/``drifted`` are hard failures under --strict; ``no_provenance``
    is always warn-only because existing inventories predate the sidecar and
    failing on it would break every current run (the generators emit the sidecar
    going forward).
    """
    import hashlib

    import commit0
    from kaiju.paths import find_test_ids_file

    commit0_path = os.path.dirname(commit0.__file__)
    subdir = TEST_IDS_SUBDIR.get(language, "test_ids")
    ref_commits = _dataset_reference_commits(dataset)

    corrupt: List[str] = []
    drifted: List[str] = []
    no_provenance: List[str] = []
    for repo in repos:
        if not repo:
            continue
        fname = f"{repo}#fail_to_pass.bz2" if "__" in repo else f"{repo}.bz2"
        bz2_path = find_test_ids_file(commit0_path, subdir, fname)
        if bz2_path is None:
            # Wholly missing — reported by missing_inventory(); not our concern.
            continue
        meta_path = bz2_path.with_name(bz2_path.name + ".meta.json")
        if not meta_path.exists():
            no_provenance.append(repo)
            continue
        try:
            meta = json.loads(meta_path.read_text())
            recorded_hash = str(meta.get("content_sha256", ""))
            actual_hash = hashlib.sha256(bz2_path.read_bytes()).hexdigest()
            if not recorded_hash or recorded_hash != actual_hash:
                corrupt.append(repo)
                continue
        except Exception:  # noqa: BLE001
            corrupt.append(repo)
            continue
        expected_rc = ref_commits.get(repo)
        recorded_rc = str(meta.get("reference_commit", ""))
        if expected_rc and recorded_rc and expected_rc != recorded_rc:
            drifted.append(repo)
    return corrupt, drifted, no_provenance


def _report_freshness(args, repos: List[str]) -> int:
    """Run the QC-C8-011 freshness cross-check and report. Returns exit code."""
    corrupt, drifted, no_provenance = stale_inventory(
        args.language, repos, args.dataset
    )
    if no_provenance:
        print(
            f"verify_inventory: NOTE {len(no_provenance)}/{len(repos)} "
            f"{args.language} inventory file(s) carry no provenance sidecar "
            "(.bz2.meta.json); freshness vs reference_commit not verifiable for "
            "them. Regenerate with the provenance-emitting generator to enable "
            "the check.",
            file=sys.stderr,
        )
    if not corrupt and not drifted:
        return 0

    hdr = "FATAL" if args.strict else "WARNING"
    lines = [
        "",
        "======================================================================",
        f"{hdr}: {len(corrupt) + len(drifted)}/{len(repos)} {args.language} "
        "repo(s) have a STALE or CORRUPTED frozen test-id inventory.",
        "  The bz2 exists but does not match its recorded provenance, so the eval",
        "  denominator would be silently wrong / non-reproducible.",
    ]
    for repo in corrupt:
        lines.append(
            f"    - {repo}  (content hash mismatch — bz2 differs from what was frozen)"
        )
    for repo in drifted:
        lines.append(
            f"    - {repo}  (reference_commit drift — bz2 frozen from a different commit "
            "than the dataset)"
        )
    lines += [
        "  Fix: regenerate the inventory (tools/generate_test_ids_<lang>.py) from",
        "  the dataset's reference_commit and re-stage it.",
        "  Override (score against a possibly-stale inventory — NON-CANONICAL):",
        "  re-run with --no-strict or KAIJU_REQUIRE_INVENTORY=0.",
        "======================================================================",
    ]
    print("\n".join(lines), file=sys.stderr)
    return 2 if args.strict else 0


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify frozen test-id inventory presence.")
    p.add_argument("--language", required=True, choices=sorted(TEST_IDS_SUBDIR))
    p.add_argument("--dataset", default=None)
    p.add_argument("--repo-split", default=None)
    p.add_argument(
        "--datasets-dir",
        default=None,
        help="Staged outputs/<uuid>/datasets dir; sets KAIJU_TEST_IDS_DIR for resolution.",
    )
    strict = p.add_mutually_exclusive_group()
    strict.add_argument("--strict", dest="strict", action="store_true", default=True)
    strict.add_argument("--no-strict", dest="strict", action="store_false")
    args = p.parse_args(argv)

    # KAIJU_REQUIRE_INVENTORY=0 forces warn-only regardless of --strict.
    if os.environ.get("KAIJU_REQUIRE_INVENTORY", "1") == "0":
        args.strict = False

    if args.datasets_dir and os.path.isdir(args.datasets_dir):
        os.environ["KAIJU_TEST_IDS_DIR"] = args.datasets_dir

    repos = enumerate_repos(args.language, args.dataset, args.repo_split)
    if not repos:
        # QC-C8-007: an empty enumeration is EXACTLY the misconfiguration this
        # guard exists to catch — a run that resolved zero repos would score
        # every repo against ALL discovered tests (a wrong, non-reproducible
        # denominator) with no error. Under --strict this must FAIL LOUD, not
        # silently return 0. Only warn-only mode (--no-strict /
        # KAIJU_REQUIRE_INVENTORY=0) may downgrade to a warning + pass. Mirrors
        # the missing-inventory branch below (return 2 if strict else 0).
        hdr = "FATAL" if args.strict else "WARNING"
        print(
            "\n".join(
                [
                    "",
                    "======================================================================",
                    f"{hdr}: verify_inventory could not enumerate ANY repos "
                    f"(language={args.language}, dataset={args.dataset}, "
                    f"split={args.repo_split}).",
                    "  With no resolved repos the inventory guard cannot run, and the eval",
                    "  would SILENTLY score against ALL discovered tests for every repo — a",
                    "  wrong, non-reproducible denominator. This usually means a bad/missing",
                    "  --dataset or --repo-split, or a hub-id dataset with no --repo-split.",
                    "  Fix: pass a resolvable --dataset (local JSON) or --repo-split.",
                    "  Override (NON-CANONICAL, warn-only): re-run with --no-strict or",
                    "  KAIJU_REQUIRE_INVENTORY=0.",
                    "======================================================================",
                ]
            ),
            file=sys.stderr,
        )
        return 2 if args.strict else 0

    missing = missing_inventory(args.language, repos)
    if not missing:
        print(
            f"verify_inventory: OK all {len(repos)} {args.language} repo(s) have a "
            "frozen test-id inventory."
        )
        # QC-C8-011: existence is necessary but not sufficient — a bz2 frozen
        # from a STALE reference_commit (or silently corrupted) still "exists".
        # Cross-check the provenance sidecar when present.
        return _report_freshness(args, repos)

    subdir = TEST_IDS_SUBDIR.get(args.language, "test_ids")
    hdr = "FATAL" if args.strict else "WARNING"
    lines = [
        "",
        "======================================================================",
        f"{hdr}: {len(missing)}/{len(repos)} {args.language} repo(s) are MISSING the "
        "frozen test-id inventory.",
        "  Without it the eval SILENTLY scores against ALL discovered tests — a",
        "  wrong, non-reproducible denominator. Missing repos:",
    ]
    for repo in missing:
        lines.append(f"    - {repo}  (expected commit0/data/{subdir}/{repo}.bz2)")
    lines += [
        "  Fix: generate the inventory (tools/generate_test_ids_<lang>.py) and/or",
        "  ensure it is staged into outputs/<uuid>/datasets/<repo>_test_ids.bz2.",
        "  Override (score against discovered — NON-CANONICAL): re-run with",
        "  --no-strict or KAIJU_REQUIRE_INVENTORY=0.",
        "======================================================================",
    ]
    print("\n".join(lines), file=sys.stderr)
    return 2 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
