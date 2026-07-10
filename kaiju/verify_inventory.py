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
        print(
            "verify_inventory: WARNING could not enumerate repos "
            f"(language={args.language}, dataset={args.dataset}, split={args.repo_split}); "
            "skipping inventory guard.",
            file=sys.stderr,
        )
        return 0

    missing = missing_inventory(args.language, repos)
    if not missing:
        print(
            f"verify_inventory: OK all {len(repos)} {args.language} repo(s) have a "
            "frozen test-id inventory."
        )
        return 0

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
