import argparse
import json
import logging
import re
import uuid as _uuid_mod
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Supported Java runtime versions — imported from the canonical harness
# constant so create-dataset validation and the Docker/spec layer agree on the
# same set (parity with create_dataset.py importing SUPPORTED_PYTHON_VERSIONS
# and create_dataset_{js,ts}.py importing SUPPORTED_NODE_VERSIONS).
try:  # pragma: no cover - import shim
    from commit0.harness.constants_java import SUPPORTED_JAVA_VERSIONS
except Exception:  # noqa: BLE001 - keep validation usable if harness unavailable
    SUPPORTED_JAVA_VERSIONS = {"8", "11", "17", "21"}


# H10: hoist required-field schema to a module-level typed dict for parity with
# create_dataset_{go,rust,js,ts,cpp,py}.py so schema drift is grep-visible.
# QC-C5-003: `original_repo`, `setup`, `test`, `src_dir` were previously absent
# from Java's required set, letting schema-incomplete entries pass silently.
REQUIRED_FIELDS: Dict[str, type] = {
    "instance_id": str,
    "repo": str,
    "original_repo": str,
    "base_commit": str,
    "reference_commit": str,
    "setup": dict,
    "test": dict,
    "src_dir": str,
}
REQUIRED_SETUP_FIELDS: List[str] = ["build_system"]

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


def validate_java_entry(entry: dict) -> List[str]:
    """Validate a single Java dataset entry; returns a list of issues.

    Brought to parity with the c/go/rust/js/ts validators (QC-C5-003/005):
    required-field presence+type, setup/test shape, commit-SHA shape, and a
    guarded runtime-version + language value check.
    """
    issues: List[str] = []

    for field, ftype in REQUIRED_FIELDS.items():
        if field not in entry:
            issues.append(f"Missing required field: {field}")
        elif not isinstance(entry[field], ftype):
            issues.append(
                f"{field}: expected {ftype.__name__}, "
                f"got {type(entry[field]).__name__}"
            )

    setup = entry.get("setup")
    if isinstance(setup, dict):
        for _f in REQUIRED_SETUP_FIELDS:
            if not setup.get(_f):
                issues.append(f"setup.{_f} is required")
        # java_version can live top-level or under setup (spec_java reads both).
        java_version = entry.get("java_version") or setup.get("java_version")
        if java_version is not None:
            if str(java_version) not in SUPPORTED_JAVA_VERSIONS:
                issues.append(
                    f"Unsupported Java version '{java_version}'. "
                    f"Supported: {sorted(SUPPORTED_JAVA_VERSIONS)}"
                )

    _validate_commit_shas(entry, issues)

    # DOCUMENTED DEVIATION: `language` is validated when present rather than
    # required. Existing Java dataset entries (e.g. JSON-java_dataset.json)
    # predate the explicit `language` field; enforcing presence would reject
    # them. A *wrong* value — the silent-corruption risk — is still rejected,
    # matching the guarded js/ts style.
    if "language" in entry and entry["language"] != "java":
        issues.append(
            f"language must be 'java', got '{entry.get('language')}'"
        )

    return issues


def create_java_dataset(
    entries: List[dict],
    output_path: str,
    dataset_name: str = "commit0-java",
) -> None:
    """Validate + write Java dataset entries, assigning a UUID `id` to each.

    QC-C5-004: previously wrote ``valid_entries`` directly with no UUID
    assignment and no empty-dataset guard, so a run where every entry failed
    validation silently emitted ``[]``. Now each written entry carries a stable
    ``id`` (preserved if already present) and a zero-valid-entries result fails
    loud via ``SystemExit(1)`` — mirroring create_dataset_{c,cpp,go,rust}.py.
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    valid_entries: List[dict] = []
    for entry in entries:
        issues = validate_java_entry(entry)
        if issues:
            logger.warning("Skipping %s: %s", entry.get("repo", "unknown"), issues)
            continue
        # UUID lifecycle: preserve a caller-assigned id, otherwise mint one so
        # the module can never emit an un-ided entry (parity with cpp:217).
        valid_entries.append(dict(entry, id=entry.get("id") or str(_uuid_mod.uuid4())))

    if not valid_entries:
        logger.error("No valid entries — aborting")
        raise SystemExit(1)

    with open(output, "w") as f:
        json.dump(valid_entries, f, indent=2)
    logger.info("Created dataset with %d entries at %s", len(valid_entries), output)


def generate_java_split(entries: List[dict]) -> Dict[str, List[str]]:
    all_repos = [e["repo"] for e in entries]
    return {
        "all": all_repos,
        "lite": all_repos[:10],
    }


def upload_java_to_huggingface(
    entries: List[dict],
    repo_id: str,
    token: Optional[str] = None,
) -> None:
    try:
        from datasets import Dataset
    except ImportError:
        logger.error("Install 'datasets' package: pip install datasets")
        return

    logger.info("Creating HuggingFace dataset with %d Java entries...", len(entries))

    flat_entries = []
    for entry in entries:
        flat = {
            "instance_id": entry.get("instance_id", ""),
            "repo": entry.get("repo", ""),
            "base_commit": entry.get("base_commit", ""),
            "reference_commit": entry.get("reference_commit", ""),
            "build_system": entry.get("build_system", ""),
            "java_version": entry.get("java_version", ""),
            "test_framework": entry.get("test_framework", ""),
            "setup": json.dumps(entry.get("setup", {})),
            "test": json.dumps(entry.get("test", {})),
            "src_dir": entry.get("src_dir", "src/main/java"),
        }
        flat_entries.append(flat)

    ds = Dataset.from_list(flat_entries)
    logger.info("Uploading to %s...", repo_id)
    ds.push_to_hub(repo_id, split="test", token=token)
    logger.info("Upload complete: https://huggingface.co/datasets/%s", repo_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Minimal create/validate CLI for parity with the sibling create_dataset
    tools. DOCUMENTED DEVIATION: no consolidated outputs/<uuid>/ staging (that
    lives in create_dataset_cpp/go/rust); Java's write path is currently only
    driven library-side, so this exposes a non-zero exit on empty/invalid
    input without coupling to kaiju.paths.
    """
    parser = argparse.ArgumentParser(
        description="Create/validate Java dataset for commit0"
    )
    sub = parser.add_subparsers(dest="command")

    create_p = sub.add_parser("create", help="Create dataset from entries JSON")
    create_p.add_argument("entries", help="Path to JSON entries file")
    create_p.add_argument(
        "--output",
        default="java_dataset.json",
        help="Output dataset file (default: java_dataset.json)",
    )

    validate_p = sub.add_parser("validate", help="Validate an existing dataset")
    validate_p.add_argument("dataset", help="Path to dataset JSON")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.command == "create":
        entries = json.loads(Path(args.entries).read_text())
        if isinstance(entries, dict):
            entries = [entries]
        create_java_dataset(entries, args.output)
    elif args.command == "validate":
        entries = json.loads(Path(args.dataset).read_text())
        if isinstance(entries, dict):
            entries = [entries]
        all_ok = True
        for entry in entries:
            issues = validate_java_entry(entry)
            if issues:
                all_ok = False
                print(f"INVALID {entry.get('repo', '?')}: {issues}")
            else:
                print(f"OK      {entry.get('repo', '?')}")
        if not all_ok:
            print("\nSome entries have issues.")
            raise SystemExit(1)
        print(f"\nAll {len(entries)} entries valid.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
