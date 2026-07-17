"""Create a HuggingFace dataset from prepared repo entries.

Takes output of prepare_repo.py (dataset_entries.json) and:
1. Validates entries match RepoInstance schema
2. Adds entries to commit0's SPLIT constants
3. Uploads to HuggingFace (optional)
4. Generates commit0 config files (.commit0.yaml)

Usage:
    # Create local dataset file:
    python -m tools.create_dataset dataset_entries.json --output custom_dataset.json

    # Upload to HuggingFace:
    python -m tools.create_dataset dataset_entries.json --upload --hf-repo Ethara-Ai/commit0_custom

    # Generate constants.py patch:
    python -m tools.create_dataset dataset_entries.json --patch-constants
"""

from __future__ import annotations

import os

import argparse
import json
import logging
import re
from pathlib import Path
from kaiju.paths import datasets_dir
import uuid as _uuid_mod

from commit0.harness.constants import SUPPORTED_PYTHON_VERSIONS

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# RepoInstance schema (from commit0.harness.constants)
REQUIRED_FIELDS = {
    "instance_id": str,
    "repo": str,
    "original_repo": str,
    "base_commit": str,
    "reference_commit": str,
    "setup": dict,
    "test": dict,
    "src_dir": str,
}

# Required setup fields — every entry must have these.
REQUIRED_SETUP_FIELDS = {
    "install",
    "packages",
    "pip_packages",
    "pre_install",
    "python",
    "specification",
}
# Optional setup fields produced by newer pipeline runs. Missing is OK; the
# validator only flags presence-with-wrong-type.
OPTIONAL_SETUP_FIELDS = {
    "version_source",
    "version_conflicts",
    "system_deps_hint",
    "test_collection_status",
}
SETUP_FIELDS = REQUIRED_SETUP_FIELDS | OPTIONAL_SETUP_FIELDS
TEST_FIELDS = {"test_cmd", "test_dir"}

# T8: git commit SHAs are lowercase hex, 7-64 chars (SHA-1 to SHA-256).
# Previously validation only checked length (>=7), so 'not_a_sha' or
# 'zzzzzzz' would pass and later fail deep in the pipeline with an
# opaque git error. Enforce hex-only shape here so bad dataset rows
# are rejected at the dataset-boundary.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def validate_entry(entry: dict, index: int) -> list[str]:
    """Validate a single dataset entry. Returns list of issues."""
    issues: list[str] = []

    for field, ftype in REQUIRED_FIELDS.items():
        if field not in entry:
            issues.append(f"[{index}] Missing field: {field}")
        elif not isinstance(entry[field], ftype):
            issues.append(
                f"[{index}] {field}: expected {ftype.__name__}, got {type(entry[field]).__name__}"
            )

    if "setup" in entry and isinstance(entry["setup"], dict):
        missing_setup = REQUIRED_SETUP_FIELDS - set(entry["setup"].keys())
        if missing_setup:
            issues.append(f"[{index}] setup missing fields: {missing_setup}")

    if "test" in entry and isinstance(entry["test"], dict):
        missing_test = TEST_FIELDS - set(entry["test"].keys())
        if missing_test:
            issues.append(f"[{index}] test missing fields: {missing_test}")

    for _sha_field in ("base_commit", "reference_commit"):
        _val = entry.get(_sha_field, "")
        if _sha_field in entry:
            if len(_val) < 7:
                issues.append(
                    f"[{index}] {_sha_field} too short: {_val!r}"
                )
            elif not _COMMIT_SHA_RE.match(_val):
                issues.append(
                    f"[{index}] {_sha_field} is not a valid hex git SHA: {_val!r}"
                )

    if "setup" in entry and isinstance(entry["setup"], dict):
        py_version = entry["setup"].get("python")
        if py_version and py_version not in SUPPORTED_PYTHON_VERSIONS:
            issues.append(
                f"[{index}] Unsupported Python version '{py_version}'. "
                f"Supported: {sorted(SUPPORTED_PYTHON_VERSIONS)}"
            )

    return issues


def validate_dataset(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate all entries. Returns (valid_entries, all_issues)."""
    all_issues: list[str] = []
    valid: list[dict] = []

    for i, entry in enumerate(entries):
        issues = validate_entry(entry, i)
        if issues:
            all_issues.extend(issues)
            logger.warning(
                "Entry %d (%s) has issues:", i, entry.get("instance_id", "?")
            )
            for issue in issues:
                logger.warning("  %s", issue)
        else:
            valid.append(entry)

    return valid, all_issues


def generate_split_constants(entries: list[dict], split_name: str = "custom") -> str:
    """Generate Python code for SPLIT constant additions."""
    repo_names = sorted(entry["repo"] for entry in entries)

    lines = [
        f"# Custom split: {split_name} ({len(entries)} repos)",
        f'SPLIT["{split_name}"] = {{',
    ]
    for name in repo_names:
        lines.append(f'    "{name}",')
    lines.append("}")

    # Also add individual repo entries
    lines.append("")
    lines.append("# Individual repo splits")
    for name in repo_names:
        lines.append(f'SPLIT["{name}"] = {{"{name}"}}')

    return "\n".join(lines)


def create_hf_dataset_dict(entries: list[dict]) -> list[dict]:
    """Convert entries to HuggingFace-compatible format.

    Assigns a fresh UUID4 to each entry's ``id`` field — each dataset build
    represents a distinct experiment instance, so the id is generated here
    (not in prepare_repo.py where entries.json is a reusable source).
    """

    hf_entries: list[dict] = []

    for entry in entries:
        hf_entry = {
            "instance_id": entry["instance_id"],
            "id": entry.get("id") or str(_uuid_mod.uuid4()),
            "repo": entry["repo"],
            "original_repo": entry["original_repo"],
            "base_commit": entry["base_commit"],
            "reference_commit": entry["reference_commit"],
            "setup": entry["setup"],
            "test": entry["test"],
            "src_dir": entry["src_dir"],
        }
        hf_entries.append(hf_entry)

    return hf_entries


def upload_to_huggingface(
    entries: list[dict], repo_id: str, token: str | None = None
) -> None:
    """Upload dataset to HuggingFace Hub."""
    try:
        from datasets import Dataset
    except ImportError:
        logger.error("Install 'datasets' package: pip install datasets")
        return

    logger.info("Creating HuggingFace dataset with %d entries...", len(entries))

    # datasets library needs flat structures — serialize nested dicts
    flat_entries = []
    for entry in entries:
        flat = {
            "instance_id": entry["instance_id"],
            "repo": entry["repo"],
            "original_repo": entry["original_repo"],
            "base_commit": entry["base_commit"],
            "reference_commit": entry["reference_commit"],
            "setup": json.dumps(entry["setup"]),
            "test": json.dumps(entry["test"]),
            "src_dir": entry["src_dir"],
        }
        flat_entries.append(flat)

    ds = Dataset.from_list(flat_entries)
    logger.info("Uploading to %s...", repo_id)
    ds.push_to_hub(repo_id, split="test", token=token)
    logger.info("Upload complete: https://huggingface.co/datasets/%s", repo_id)


def generate_commit0_yaml(
    entries: list[dict], split_name: str, dataset_name: str
) -> str:
    """Generate .commit0.yaml content for using the custom dataset."""
    repo_names = sorted(entry["repo"] for entry in entries)

    yaml_content = f"""# commit0 config for custom dataset: {split_name}
dataset_name: {dataset_name}
dataset_split: test
repo_split: {split_name}
base_dir: repos

# Repos in this split ({len(entries)}):
"""
    for name in repo_names:
        yaml_content += f"#   - {name}\n"

    return yaml_content


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create HuggingFace dataset from prepared entries"
    )
    parser.add_argument(
        "entries_file",
        help="Input dataset_entries.json from prepare_repo.py",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Output dataset JSON file. Default: auto-generated from the "
            "first entry's 'id' field (<uuid>.json), or 'custom_dataset.json' "
            "if no id present."
        ),
    )
    parser.add_argument(
        "--split-name",
        type=str,
        default="custom",
        help="Name for the SPLIT constant (default: custom)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload to HuggingFace Hub",
    )
    parser.add_argument(
        "--hf-repo",
        type=str,
        default="Ethara-Ai/commit0_custom",
        help="HuggingFace repo ID for upload (default: Ethara-Ai/commit0_custom)",
    )
    parser.add_argument(
        "--hf-token",
        type=str,
        default=None,
        help="HuggingFace token (or set HF_TOKEN env var)",
    )
    parser.add_argument(
        "--patch-constants",
        action="store_true",
        help="Generate Python code to add to constants.py",
    )
    parser.add_argument(
        "--generate-yaml",
        action="store_true",
        help="Generate .commit0.yaml for the custom dataset",
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

    # Load entries
    # T3 fix: guard against malformed entries file with a clean error.
    try:
        entries = json.loads(Path(args.entries_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        parser.error(f"Entries file not found: {args.entries_file}")
        return
    except json.JSONDecodeError as e:
        parser.error(f"Entries JSON at {args.entries_file} is malformed: {e}")
        return
    logger.info("Loaded %d entries from %s", len(entries), args.entries_file)

    # Validate
    valid, issues = validate_dataset(entries)
    if issues:
        logger.warning("%d validation issues found", len(issues))
    logger.info("%d / %d entries valid", len(valid), len(entries))

    if not valid:
        logger.error("No valid entries — aborting")
        return

    # Create HF-compatible dataset
    hf_entries = create_hf_dataset_dict(valid)

    # Save local dataset
    if _consolidated and hf_entries and hf_entries[0].get("id"):
        output_path = datasets_dir(hf_entries[0]["id"]) / "dataset.json"
    elif args.output:
        output_path = Path(args.output)
    elif hf_entries and hf_entries[0].get("id"):
        output_path = Path(f"{hf_entries[0]['id']}.json")
    else:
        output_path = Path("custom_dataset.json")
    output_path.write_text(json.dumps(hf_entries, indent=2), encoding="utf-8")
    logger.info("Saved dataset to %s", output_path)

    if _consolidated and hf_entries and hf_entries[0].get("id"):
        try:
            from kaiju.paths import copy_inference_inputs as _cii
            for e in hf_entries:
                _cii(hf_entries[0]["id"], e["repo"].split("/")[-1], test_ids_subdir="test_ids", repo_base="repos")
        except Exception as _e:
            logger.warning("copy_inference_inputs failed: %s", _e)

    # Print summary
    print(f"\n{'=' * 80}")
    print(f"DATASET: {len(valid)} entries")
    print(f"{'=' * 80}")
    for i, e in enumerate(valid, 1):
        print(f"  {i:>3}. {e['instance_id']:<35} ({e['original_repo']})")
    print(f"{'=' * 80}\n")

    # Generate SPLIT constants
    if args.patch_constants:
        constants_code = generate_split_constants(valid, args.split_name)
        constants_file = Path(f"split_{args.split_name}.py")
        constants_file.write_text(constants_code, encoding="utf-8")
        logger.info("SPLIT constants written to %s", constants_file)
        print(f"\n# Add to commit0/harness/constants.py:\n{constants_code}\n")

    # Generate .commit0.yaml
    if args.generate_yaml:
        yaml_content = generate_commit0_yaml(valid, args.split_name, args.hf_repo)
        yaml_file = Path(f".commit0.{args.split_name}.yaml")
        yaml_file.write_text(yaml_content, encoding="utf-8")
        logger.info("Config written to %s", yaml_file)
        print(yaml_content)

    # Upload to HuggingFace
    if args.upload:
        token = args.hf_token or os.environ.get("HF_TOKEN")
        if not token:
            raise EnvironmentError(
                "HF_TOKEN is required for upload but not set. "
                "Pass --hf-token or export HF_TOKEN."
            )
        upload_to_huggingface(hf_entries, args.hf_repo, token=token)


if __name__ == "__main__":
    main()
