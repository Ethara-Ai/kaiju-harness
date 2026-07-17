"""Create and validate C++ dataset files for commit0.

Mirrors tools/create_dataset_java.py but for C++ repos with local JSON format
(no HuggingFace upload — same approach as Rust).

Usage:
    python -m tools.create_dataset_cpp entries.json --output cpp_dataset.json
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from kaiju.paths import datasets_dir
from typing import Dict, List
import uuid as _uuid_mod

logger = logging.getLogger(__name__)

# H10: hoist required-field lists to module-level constants for parity with
# create_dataset_{go,rust,js,ts,py}.py so schema drift is grep-visible.
# QC-C5-003: `original_repo` was previously absent from the required set even
# though every sibling language (c/go/rust/js/ts) requires it.
REQUIRED_FIELDS: List[str] = [
    "instance_id",
    "repo",
    "original_repo",
    "base_commit",
    "reference_commit",
]
REQUIRED_SETUP_FIELDS: List[str] = ["build_system"]
REQUIRED_TEST_FIELDS: List[str] = ["test_cmd"]
SUPPORTED_BUILD_SYSTEMS: tuple[str, ...] = ("cmake", "meson", "autotools", "make")
# QC-C5-003: runtime-version value check for the C++ standard (the cpp analog
# of SUPPORTED_PYTHON_VERSIONS / SUPPORTED_NODE_VERSIONS / SUPPORTED_JAVA_VERSIONS).
# prepare_repo_cpp.create_dataset_entry emits setup.cpp_standard (default "17").
SUPPORTED_CPP_STANDARDS: tuple[str, ...] = ("11", "14", "17", "20", "23")

# QC-C5-005: canonical commit-SHA shape guard, ported verbatim from
# create_dataset.py:75 (also in create_dataset_rust.py). Git SHA-1 is 40 hex
# chars; accept 7-64 to allow abbreviated inputs.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _validate_commit_shas(entry: dict, issues: List[str], prefix: str = "") -> None:
    """Append length (<7) + hex-shape issues for base_commit/reference_commit.

    Shared idiom ported from create_dataset.py:100-110 so C++/Java match the
    Python/Rust canonical defense instead of the presence-only check they had.
    """
    for _sha_field in ("base_commit", "reference_commit"):
        if _sha_field not in entry:
            continue
        _val = entry.get(_sha_field, "")
        if not isinstance(_val, str) or len(_val) < 7:
            issues.append(f"{prefix}{_sha_field} too short: {_val!r}")
        elif not _COMMIT_SHA_RE.match(_val):
            issues.append(
                f"{prefix}{_sha_field} is not a valid hex git SHA: {_val!r}"
            )


def validate_cpp_entry(entry: dict) -> List[str]:
    """Validate a single C++ dataset entry.

    Returns a list of issues (empty if valid).
    """
    issues: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in entry:
            issues.append(f"Missing required field: {field}")

    # QC-C5-005: commit-SHA length + hex-shape validation (parity with python/rust).
    _validate_commit_shas(entry, issues)

    # Check C++ specific fields
    setup = entry.get("setup", {})
    if not isinstance(setup, dict):
        issues.append("'setup' must be a dict")
    else:
        for _f in REQUIRED_SETUP_FIELDS:
            if not setup.get(_f):
                issues.append(f"setup.{_f} is required")
        if setup.get("build_system") and setup["build_system"] not in SUPPORTED_BUILD_SYSTEMS:
            issues.append(
                f"Unknown build_system: {setup['build_system']}. "
                f"Expected one of: {', '.join(SUPPORTED_BUILD_SYSTEMS)}"
            )
        # QC-C5-003: runtime-version value check (guarded — cpp_standard is
        # optional in older entries; a *wrong* value is still rejected).
        _cpp_std = setup.get("cpp_standard")
        if _cpp_std and str(_cpp_std) not in SUPPORTED_CPP_STANDARDS:
            issues.append(
                f"Unsupported C++ standard '{_cpp_std}'. "
                f"Supported: {', '.join(SUPPORTED_CPP_STANDARDS)}"
            )

    # QC-C5-003: language value check. DOCUMENTED DEVIATION: validated when
    # present rather than required, so legacy/stub entries that predate the
    # `language` field are not rejected while a *wrong* value still fails.
    if "language" in entry and entry["language"] not in ("cpp", "c++"):
        issues.append(
            f"language must be 'cpp', got '{entry.get('language')}'"
        )

    test = entry.get("test", {})
    if not isinstance(test, dict):
        issues.append("'test' must be a dict")
    elif not test.get("test_cmd"):
        issues.append("test.test_cmd is required")
    elif isinstance(test, dict):
        repo_root = Path(__file__).resolve().parents[1]
        scripts_dir = repo_root / "scripts"
        import sys as _sys
        if str(scripts_dir) not in _sys.path:
            _sys.path.insert(0, str(scripts_dir))
        try:
            from validate_cpp_dataset import lint_cmd
            for label, hint in lint_cmd(test["test_cmd"]):
                issues.append(f"test.test_cmd: {label} -- {hint.splitlines()[0]}")
        except ImportError:
            pass

    if not entry.get("src_dir"):
        issues.append("src_dir is required")

    return issues


def create_cpp_dataset(
    entries: List[dict],
    output_path: str,
    dataset_name: str = "commit0-cpp",
) -> None:
    """Write validated entries to a JSON dataset file.

    Invalid entries are logged and skipped. Caller may pre-assign each entry
    an ``id`` (uuid4); this function preserves ids passed in but does NOT
    generate them — the UUID lifecycle belongs to the create-dataset step
    (once per experiment), not this write helper.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    valid_entries: list[dict] = []
    for entry in entries:
        issues = validate_cpp_entry(entry)
        if issues:
            logger.warning(
                "Skipping %s: %s", entry.get("repo", "unknown"), issues
            )
            continue
        valid_entries.append(entry)

    # QC-C5-004: fail loud instead of silently writing an empty dataset when
    # every entry was rejected (parity with create_dataset_{c,go,rust,py}.py).
    if not valid_entries:
        logger.error("No valid entries — aborting")
        raise SystemExit(1)

    with open(output, "w") as f:
        json.dump(valid_entries, f, indent=2)
    logger.info("Created dataset with %d entries at %s", len(valid_entries), output)


def generate_cpp_split(entries: List[dict]) -> Dict[str, List[str]]:
    """Generate a split dict from dataset entries.

    Returns ``{"all": [...], "lite": [...first 5...]}``.
    """
    all_repos = [e["repo"] for e in entries]
    return {
        "all": all_repos,
        "lite": all_repos[:5],
    }


def merge_cpp_datasets(*paths: str, output_path: str) -> None:
    """Merge multiple C++ dataset JSON files into one.

    Deduplicates by instance_id (last entry wins).
    """
    seen: dict[str, dict] = {}
    for path in paths:
        p = Path(path)
        if not p.exists():
            logger.warning("Dataset file not found: %s", p)
            continue
        raw = p.read_text().strip()
        if not raw:
            continue
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for entry in data:
            iid = entry.get("instance_id", entry.get("repo", ""))
            seen[iid] = entry

    merged = list(seen.values())
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=2) + "\n")
    logger.info("Merged %d entries into %s", len(merged), out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Create/validate C++ dataset for commit0"
    )
    sub = parser.add_subparsers(dest="command")

    # create
    create_p = sub.add_parser("create", help="Create dataset from entries JSON")
    create_p.add_argument("entries", help="Path to JSON entries file")
    create_p.add_argument(
        "--output",
        default=None,
        help=(
            "Output dataset file. Default: auto-generated from the first "
            "entry's 'id' field (<uuid>.json), or 'cpp_dataset.json' if no "
            "id present."
        ),
    )

    # validate
    validate_p = sub.add_parser("validate", help="Validate an existing dataset")
    validate_p.add_argument("dataset", help="Path to dataset JSON")

    # merge
    merge_p = sub.add_parser("merge", help="Merge multiple datasets")
    merge_p.add_argument("datasets", nargs="+", help="Dataset files to merge")
    merge_p.add_argument(
        "--output",
        default="merged_cpp_dataset.json",
        help="Output file (default: merged_cpp_dataset.json)",
    )

    parser.add_argument(
        "--outputs-root",
        type=str,
        default=None,
        help="Root for consolidated outputs (overrides $KAIJU_OUTPUTS_ROOT; default: ./outputs)",
    )
    parser.add_argument(
        "--layout",
        choices=["flat", "consolidated"],
        default=None,
        help="Output layout: 'flat' (legacy) or 'consolidated' (outputs/<uuid>/…). Overrides $KAIJU_LOG_LAYOUT.",
    )

    args = parser.parse_args()

    if args.outputs_root is not None:
        os.environ["KAIJU_OUTPUTS_ROOT"] = args.outputs_root
    if args.layout is not None:
        os.environ["KAIJU_LOG_LAYOUT"] = args.layout
    _consolidated = os.environ.get("KAIJU_LOG_LAYOUT", "consolidated").lower() == "consolidated"

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "create":

        raw = Path(args.entries).read_text()
        entries = json.loads(raw)
        if isinstance(entries, dict):
            entries = [entries]
        annotated = [dict(e, id=e.get("id") or str(_uuid_mod.uuid4())) for e in entries]
        if args.output:
            output = args.output
        elif annotated:
            output = f"{annotated[0]['id']}.json"
        else:
            output = "cpp_dataset.json"
        create_cpp_dataset(annotated, output)

        # Consolidated layout: outputs/<uuid>/datasets/ must carry three files
        # for the containerized eval + agent to find them (commit0/data/ is
        # pruned from the agent image):
        #   1. <split>_test_ids.bz2 - frozen inventory (via copy_inference_inputs)
        #   2. <split>_spec.pdf.bz2  - scraped spec  (via copy_inference_inputs;
        #      ensure_spec_docs_cpp in run_pipeline_cpp.sh writes it to
        #      ${REPO_BASE}/{name}/spec.pdf.bz2 where REPO_BASE=${BASE_DIR}/repos)
        #   3. entries.json          - dataset entries for the run
        # Historical gap: entries.json was never emitted from create_dataset_cpp,
        # so tools reading outputs/<uuid>/datasets/entries.json (mirrors java/js)
        # saw an empty folder even on successful prep.
        if _consolidated and annotated and annotated[0].get("id"):
            _run_uuid = annotated[0]["id"]
            try:
                from kaiju.paths import copy_inference_inputs, datasets_dir
                for e in annotated:
                    copy_inference_inputs(
                        _run_uuid, e["repo"].split("/")[-1],
                        test_ids_subdir="cpp_test_ids", repo_base="repos",
                    )
                _entries_path = datasets_dir(_run_uuid) / "entries.json"
                _entries_path.write_text(json.dumps(annotated, indent=2))
                logger.info(
                    "Wrote %d entries to %s (consolidated)",
                    len(annotated), _entries_path,
                )
            except Exception as _e:  # noqa: BLE001
                logger.warning("consolidated staging failed: %s", _e)

    elif args.command == "validate":
        raw = Path(args.dataset).read_text()
        entries = json.loads(raw)
        if isinstance(entries, dict):
            entries = [entries]
        all_ok = True
        for entry in entries:
            issues = validate_cpp_entry(entry)
            if issues:
                all_ok = False
                print(f"INVALID {entry.get('repo', '?')}: {issues}")
            else:
                print(f"OK      {entry.get('repo', '?')}")
        if all_ok:
            print(f"\nAll {len(entries)} entries valid.")
        else:
            print("\nSome entries have issues.")

    elif args.command == "merge":
        merge_cpp_datasets(*args.datasets, output_path=args.output)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
