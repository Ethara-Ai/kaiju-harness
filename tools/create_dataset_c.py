"""Create a HuggingFace-shaped dataset from prepared C repo entries.

Takes the output of ``prepare_repo_c.py`` (``dataset_entries.json``) and:
1. Validates each entry against the ``CRepoInstance`` schema.
2. Enforces ``setup.apt`` against ``ALLOWED_APT_PACKAGES`` (CR-5 supply chain).
3. Emits a dataset JSON suitable for ``commit0/cli_c.py``.
4. Optionally uploads to HuggingFace Hub.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from kaiju.paths import datasets_dir
import uuid as _uuid_mod

from commit0.harness.constants_c import ALLOWED_APT_PACKAGES

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

SETUP_OPTIONAL_FIELDS = {
    "apt",
    "build_system",
    "cmake_flags",
    "pre_install",
    "specification",
}

TEST_REQUIRED_FIELDS = {"framework", "test_cmd"}

ALLOWED_BUILD_SYSTEMS = frozenset({"cmake"})
ALLOWED_TEST_FRAMEWORKS = frozenset({"ctest"})


def validate_entry(entry: dict, index: int) -> list[str]:
    issues: list[str] = []

    for field, ftype in REQUIRED_FIELDS.items():
        if field not in entry:
            issues.append(f"[{index}] Missing field: {field}")
        elif not isinstance(entry[field], ftype):
            issues.append(
                f"[{index}] {field}: expected {ftype.__name__}, "
                f"got {type(entry[field]).__name__}"
            )

    if entry.get("language") != "c":
        issues.append(
            f"[{index}] language must be 'c', got '{entry.get('language')}'"
        )

    setup = entry.get("setup") or {}
    if isinstance(setup, dict):
        build_system = setup.get("build_system", "cmake")
        if build_system not in ALLOWED_BUILD_SYSTEMS:
            issues.append(
                f"[{index}] setup.build_system={build_system!r} unsupported in MVP. "
                f"Allowed: {sorted(ALLOWED_BUILD_SYSTEMS)}"
            )
        apt = setup.get("apt") or []
        if not isinstance(apt, list):
            issues.append(f"[{index}] setup.apt must be a list, got {type(apt).__name__}")
        else:
            for pkg in apt:
                if not isinstance(pkg, str):
                    issues.append(
                        f"[{index}] setup.apt entry not a string: {pkg!r}"
                    )
                    continue
                if pkg not in ALLOWED_APT_PACKAGES:
                    issues.append(
                        f"[{index}] setup.apt entry {pkg!r} not in ALLOWED_APT_PACKAGES "
                        f"(see commit0/harness/constants_c.py)"
                    )
        unknown_setup = set(setup.keys()) - SETUP_OPTIONAL_FIELDS
        if unknown_setup:
            issues.append(
                f"[{index}] setup unknown fields: {sorted(unknown_setup)}"
            )

    test = entry.get("test") or {}
    if isinstance(test, dict):
        missing_test = TEST_REQUIRED_FIELDS - set(test.keys())
        if missing_test:
            issues.append(f"[{index}] test missing fields: {sorted(missing_test)}")
        framework = test.get("framework", "ctest")
        if framework not in ALLOWED_TEST_FRAMEWORKS:
            issues.append(
                f"[{index}] test.framework={framework!r} unsupported in MVP. "
                f"Allowed: {sorted(ALLOWED_TEST_FRAMEWORKS)}"
            )

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


def create_hf_dataset_dict(entries: list[dict]) -> list[dict]:
    """Convert entries to HuggingFace-compatible format.

    Assigns a fresh UUID4 to each entry's ``id`` field — each dataset build
    represents a distinct experiment instance, so the id is generated here
    (not in prepare_repo_c.py where entries.json is a reusable source).
    """

    hf_entries: list[dict] = []
    for entry in entries:
        hf_entries.append(
            {
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
        )
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
        flat_entries.append(
            {
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
        )

    ds = Dataset.from_list(flat_entries)
    logger.info("Uploading to %s...", repo_id)
    ds.push_to_hub(repo_id, split="test", token=token)
    logger.info("Upload complete: https://huggingface.co/datasets/%s", repo_id)


def generate_commit0_c_yaml(
    entries: list[dict], split_name: str, dataset_name: str
) -> str:
    repo_names = sorted(entry["repo"] for entry in entries)
    yaml_content = (
        f"# commit0 C config for dataset: {split_name}\n"
        f"dataset_name: {dataset_name}\n"
        f"dataset_split: test\n"
        f"repo_split: {split_name}\n"
        f"base_dir: repos\n"
    )
    for name in repo_names:
        yaml_content += f"#   - {name}\n"
    return yaml_content


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create dataset JSON from prepared C entries"
    )
    parser.add_argument(
        "entries_file", help="Input dataset_entries.json from prepare_repo_c.py"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Output dataset JSON file. Default: auto-generated from the "
            "first entry's 'id' field (<uuid>.json), or 'c_dataset.json' "
            "if no id present."
        ),
    )
    parser.add_argument(
        "--split-name",
        type=str,
        default="custom_c",
        help="Name for the C_SPLIT constant",
    )
    parser.add_argument(
        "--upload", action="store_true", help="Upload to HuggingFace Hub"
    )
    parser.add_argument(
        "--hf-repo",
        type=str,
        default="Ethara-Ai/commit0_c",
        help="HuggingFace repo ID for upload",
    )
    parser.add_argument(
        "--hf-token", type=str, default=None, help="HuggingFace token"
    )
    parser.add_argument(
        "--generate-yaml",
        action="store_true",
        help="Generate .commit0.c.yaml for the custom dataset",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any entry fails validation",
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

    entries = json.loads(Path(args.entries_file).read_text())
    logger.info("Loaded %d entries from %s", len(entries), args.entries_file)

    valid, issues = validate_dataset(entries)
    if issues:
        logger.warning("%d validation issues found", len(issues))
    logger.info("%d / %d entries valid", len(valid), len(entries))
    if args.strict and issues:
        raise SystemExit(1)
    if not valid:
        logger.error("No valid entries — aborting")
        raise SystemExit(1)

    hf_entries = create_hf_dataset_dict(valid)
    if _consolidated and hf_entries and hf_entries[0].get("id"):
        output_path = datasets_dir(hf_entries[0]["id"]) / "dataset.json"
    elif args.output:
        output_path = Path(args.output)
    elif hf_entries and hf_entries[0].get("id"):
        output_path = Path(f"{hf_entries[0]['id']}.json")
    else:
        output_path = Path("c_dataset.json")
    output_path.write_text(json.dumps(hf_entries, indent=2))
    logger.info("Saved dataset to %s", output_path)

    if _consolidated and hf_entries and hf_entries[0].get("id"):
        try:
            from kaiju.paths import copy_inference_inputs as _cii
            for e in hf_entries:
                _cii(hf_entries[0]["id"], e["repo"].split("/")[-1], test_ids_subdir="c_test_ids", repo_base="repos")
        except Exception as _e:
            logger.warning("copy_inference_inputs failed: %s", _e)

    print(f"\n{'=' * 80}")
    print(f"C DATASET: {len(valid)} entries")
    print(f"{'=' * 80}")
    for i, e in enumerate(valid, 1):
        print(f"  {i:>3}. {e['instance_id']:<35} ({e['original_repo']})")
    print(f"{'=' * 80}\n")

    if args.generate_yaml:
        yaml_content = generate_commit0_c_yaml(valid, args.split_name, args.hf_repo)
        yaml_file = Path(f".commit0.c.{args.split_name}.yaml")
        yaml_file.write_text(yaml_content)
        logger.info("Config written to %s", yaml_file)
        print(yaml_content)

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
