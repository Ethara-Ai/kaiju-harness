"""Create a HuggingFace dataset from prepared Go repo entries.

Takes output of prepare_repo_go.py (dataset_entries.json) and:
1. Validates entries match GoRepoInstance schema
2. Adds entries to GO_SPLIT constants
3. Uploads to HuggingFace (optional)
4. Generates commit0 config files (.commit0.go.yaml)

Usage:
    python -m tools.create_dataset_go dataset_entries.json --output custom_dataset.json
    python -m tools.create_dataset_go dataset_entries.json --upload --hf-repo Ethara-Ai/commit0_go
    python -m tools.create_dataset_go dataset_entries.json --patch-constants
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import uuid as _uuid_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REQUIRED_FIELDS = {
    "instance_id": str,
    "repo": str,
    "original_repo": str,
    "base_commit": str,
    "reference_commit": str,
    "setup": dict,
    "test": dict,
    "src_dir": str,
    "language": str,
}

SETUP_FIELDS = {
    "install",
    "packages",
    "pip_packages",
    "pre_install",
    "go_version",
    "specification",
}
TEST_FIELDS = {"test_cmd", "test_dir"}


def validate_entry(entry: dict, index: int) -> list[str]:
    issues: list[str] = []

    for field, ftype in REQUIRED_FIELDS.items():
        if field not in entry:
            issues.append(f"[{index}] Missing field: {field}")
        elif not isinstance(entry[field], ftype):
            issues.append(
                f"[{index}] {field}: expected {ftype.__name__}, got {type(entry[field]).__name__}"
            )

    if entry.get("language") != "go":
        issues.append(f"[{index}] language must be 'go', got '{entry.get('language')}'")

    if "setup" in entry and isinstance(entry["setup"], dict):
        missing_setup = SETUP_FIELDS - set(entry["setup"].keys())
        if missing_setup:
            issues.append(f"[{index}] setup missing fields: {missing_setup}")

    if "test" in entry and isinstance(entry["test"], dict):
        missing_test = TEST_FIELDS - set(entry["test"].keys())
        if missing_test:
            issues.append(f"[{index}] test missing fields: {missing_test}")

    if "base_commit" in entry and len(entry.get("base_commit", "")) < 7:
        issues.append(
            f"[{index}] base_commit too short: {entry.get('base_commit', '')}"
        )

    if "reference_commit" in entry and len(entry.get("reference_commit", "")) < 7:
        issues.append(
            f"[{index}] reference_commit too short: {entry.get('reference_commit', '')}"
        )

    return issues


def validate_dataset(entries: list[dict]) -> tuple[list[dict], list[str]]:
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


def generate_go_split_constants(
    entries: list[dict], split_name: str = "custom_go"
) -> str:
    repo_names = sorted(entry["repo"] for entry in entries)

    lines = [
        f"# Go split: {split_name} ({len(entries)} repos)",
        f'GO_SPLIT["{split_name}"] = {{',
    ]
    for name in repo_names:
        lines.append(f'    "{name}",')
    lines.append("}")

    lines.append("")
    lines.append("# Individual Go repo splits")
    for name in repo_names:
        short = name.split("/")[-1]
        lines.append(f'GO_SPLIT["{short}_go"] = ["{short}"]')

    return "\n".join(lines)


def create_hf_dataset_dict(entries: list[dict]) -> list[dict]:
    """Convert entries to HuggingFace-compatible format.

    Assigns a fresh UUID4 to each entry's ``id`` field — each dataset build
    represents a distinct experiment instance, so the id is generated here
    (not in prepare_repo_go.py where entries.json is a reusable source).
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
            "language": entry["language"],
        }
        hf_entries.append(hf_entry)
    return hf_entries


def upload_to_huggingface(
    entries: list[dict], repo_id: str, token: str | None = None
) -> None:
    try:
        from datasets import Dataset
    except ImportError:
        logger.error("Install 'datasets' package: pip install datasets")
        return

    logger.info("Creating HuggingFace dataset with %d entries...", len(entries))

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
            "language": entry["language"],
        }
        flat_entries.append(flat)

    ds = Dataset.from_list(flat_entries)
    logger.info("Uploading to %s...", repo_id)
    ds.push_to_hub(repo_id, split="test", token=token)
    logger.info("Upload complete: https://huggingface.co/datasets/%s", repo_id)


def generate_commit0_go_yaml(
    entries: list[dict], split_name: str, dataset_name: str
) -> str:
    repo_names = sorted(entry["repo"] for entry in entries)

    yaml_content = f"""# commit0 Go config for dataset: {split_name}
dataset_name: {dataset_name}
dataset_split: test
repo_split: {split_name}
base_dir: repos
"""
    for name in repo_names:
        yaml_content += f"#   - {name}\n"

    return yaml_content


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create HuggingFace dataset from prepared Go entries"
    )
    parser.add_argument(
        "entries_file",
        help="Input dataset_entries.json from prepare_repo_go.py",
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
        help="Name for the GO_SPLIT constant (default: custom)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload to HuggingFace Hub",
    )
    parser.add_argument(
        "--hf-repo",
        type=str,
        default="Ethara-Ai/commit0_go",
        help="HuggingFace repo ID for upload (default: Ethara-Ai/commit0_go)",
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
        help="Generate Python code to add to constants_go.py",
    )
    parser.add_argument(
        "--generate-yaml",
        action="store_true",
        help="Generate .commit0.go.yaml for the custom dataset",
    )

    args = parser.parse_args()

    entries = json.loads(Path(args.entries_file).read_text())
    logger.info("Loaded %d entries from %s", len(entries), args.entries_file)

    valid, issues = validate_dataset(entries)
    if issues:
        logger.warning("%d validation issues found", len(issues))
    logger.info("%d / %d entries valid", len(valid), len(entries))

    if not valid:
        logger.error("No valid entries — aborting")
        return

    hf_entries = create_hf_dataset_dict(valid)

    if args.output:
        output_path = Path(args.output)
    elif hf_entries and hf_entries[0].get("id"):
        output_path = Path(f"{hf_entries[0]['id']}.json")
    else:
        output_path = Path("custom_dataset.json")
    output_path.write_text(json.dumps(hf_entries, indent=2))
    logger.info("Saved dataset to %s", output_path)

    print(f"\n{'=' * 80}")
    print(f"GO DATASET: {len(valid)} entries")
    print(f"{'=' * 80}")
    for i, e in enumerate(valid, 1):
        print(f"  {i:>3}. {e['instance_id']:<35} ({e['original_repo']})")
    print(f"{'=' * 80}\n")

    if args.patch_constants:
        constants_code = generate_go_split_constants(valid, args.split_name)
        constants_file = Path(f"go_split_{args.split_name}.py")
        constants_file.write_text(constants_code)
        logger.info("GO_SPLIT constants written to %s", constants_file)
        print(f"\n# Add to commit0/harness/constants_go.py:\n{constants_code}\n")

    if args.generate_yaml:
        yaml_content = generate_commit0_go_yaml(valid, args.split_name, args.hf_repo)
        yaml_file = Path(f".commit0.go.{args.split_name}.yaml")
        yaml_file.write_text(yaml_content)
        logger.info("Config written to %s", yaml_file)
        print(yaml_content)

    if args.upload:
        import os

        token = args.hf_token or os.environ.get("HF_TOKEN")
        if not token:
            raise EnvironmentError(
                "HF_TOKEN is required for upload but not set. "
                "Pass --hf-token or export HF_TOKEN."
            )
        upload_to_huggingface(hf_entries, args.hf_repo, token=token)


if __name__ == "__main__":
    main()
