"""Create a dataset JSON from prepared TypeScript repo entries.

Mirrors tools/create_dataset.py but with TypeScript-specific validation:
- Node.js versions instead of Python versions
- Test framework detection (jest/vitest)
- Language field requirement

Usage:
    python -m tools.create_dataset_ts ts_entries.json --output ts_dataset.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TS_REQUIRED_FIELDS = {
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

TS_SETUP_FIELDS = {"node", "install", "packages", "pre_install", "specification"}
TS_TEST_FIELDS = {"test_cmd", "test_dir"}
from commit0.harness.constants_ts import SUPPORTED_NODE_VERSIONS

SUPPORTED_TEST_FRAMEWORKS = {"jest", "vitest"}


def generate_ts_split_constants(
    entries: list[dict], split_name: str = "custom_ts"
) -> str:
    """Generate Python code to extend ``TS_SPLIT`` in ``constants_ts.py``.

    Mirrors ``generate_go_split_constants`` in ``create_dataset_go.py`` but
    targets the TypeScript ``TS_SPLIT`` dict rather than Python ``SPLIT``.
    """
    repo_names = sorted(entry["repo"] for entry in entries)

    lines = [
        f"# TS split: {split_name} ({len(entries)} repos)",
        f'TS_SPLIT["{split_name}"] = {{',
    ]
    for name in repo_names:
        lines.append(f'    "{name}",')
    lines.append("}")

    lines.append("")
    lines.append("# Individual TS repo splits")
    for name in repo_names:
        short = name.split("/")[-1]
        lines.append(f'TS_SPLIT["{short}"] = {{"{name}"}}')

    return "\n".join(lines)


def generate_commit0_ts_yaml(
    entries: list[dict], split_name: str, dataset_name: str
) -> str:
    """Generate ``.commit0.<split>.ts.yaml`` content for a TS dataset."""
    repo_names = sorted(entry["repo"] for entry in entries)

    yaml_content = f"""# commit0 TypeScript config for dataset: {split_name}
dataset_name: {dataset_name}
dataset_split: test
repo_split: {split_name}
language: typescript
base_dir: repos

# Repos in this split ({len(entries)}):
"""
    for name in repo_names:
        yaml_content += f"#   - {name}\n"

    return yaml_content


def validate_ts_entry(entry: dict, index: int) -> list[str]:
    """Validate a single TypeScript dataset entry. Returns list of issues."""
    issues: list[str] = []

    for field, ftype in TS_REQUIRED_FIELDS.items():
        if field not in entry:
            issues.append(f"[{index}] Missing field: {field}")
        elif not isinstance(entry[field], ftype):
            issues.append(
                f"[{index}] {field}: expected {ftype.__name__}, "
                f"got {type(entry[field]).__name__}"
            )

    if "setup" in entry and isinstance(entry["setup"], dict):
        missing_setup = TS_SETUP_FIELDS - set(entry["setup"].keys())
        if missing_setup:
            issues.append(f"[{index}] setup missing fields: {missing_setup}")

    if "test" in entry and isinstance(entry["test"], dict):
        missing_test = TS_TEST_FIELDS - set(entry["test"].keys())
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

    if "setup" in entry and isinstance(entry["setup"], dict):
        node_version = entry["setup"].get("node_version")
        if node_version and node_version not in SUPPORTED_NODE_VERSIONS:
            issues.append(
                f"[{index}] Unsupported Node.js version '{node_version}'. "
                f"Supported: {sorted(SUPPORTED_NODE_VERSIONS)}"
            )

    if "language" in entry and entry["language"] != "typescript":
        issues.append(
            f"[{index}] language must be 'typescript', got '{entry['language']}'"
        )

    test_framework = entry.get("test_framework")
    if test_framework and test_framework not in SUPPORTED_TEST_FRAMEWORKS:
        issues.append(
            f"[{index}] Unsupported test framework '{test_framework}'. "
            f"Supported: {sorted(SUPPORTED_TEST_FRAMEWORKS)}"
        )

    if "setup" in entry and isinstance(entry["setup"], dict):
        install_cmd = entry["setup"].get("install", "")
        if install_cmd:
            first_word = install_cmd.split()[0] if install_cmd.split() else ""
            if first_word not in {"npm", "yarn", "pnpm", "bun"}:
                issues.append(
                    f"[{index}] Invalid package manager in setup.install: "
                    f"'{first_word}'"
                )
            # Reject shell metacharacters in install_cmd
            _SHELL_DANGER = set(";&|`$(){}!><")
            if any(c in _SHELL_DANGER for c in install_cmd):
                issues.append(
                    f"[{index}] setup.install contains shell metacharacters: "
                    f"'{install_cmd}'. Only simple package manager commands allowed."
                )

    test_info = entry.get("test", {})
    if isinstance(test_info, dict):
        test_cmd = test_info.get("test_cmd", "")
        if test_cmd:
            first_word = test_cmd.split()[0] if test_cmd.split() else ""
            # Accept "npx", "pnpm", "yarn", "bunx", "node", and direct framework names
            allowed_test_prefixes = {
                "npx",
                "pnpm",
                "yarn",
                "bunx",
                "node",
                "jest",
                "vitest",
            }
            if first_word not in allowed_test_prefixes:
                issues.append(
                    f"[{index}] Unrecognized test command prefix in test.test_cmd: "
                    f"'{first_word}'. Allowed: {sorted(allowed_test_prefixes)}"
                )

    return issues


def validate_ts_dataset(
    entries: list[dict],
) -> tuple[list[dict], list[str]]:
    """Validate all TS entries. Returns (valid_entries, all_issues)."""
    all_issues: list[str] = []
    valid: list[dict] = []

    for i, entry in enumerate(entries):
        issues = validate_ts_entry(entry, i)
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


def create_ts_hf_dataset_dict(entries: list[dict]) -> list[dict]:
    """Convert TS entries to HuggingFace-compatible format (10 fields)."""
    hf_entries: list[dict] = []

    for entry in entries:
        hf_entry = {
            "instance_id": entry["instance_id"],
            "repo": entry["repo"],
            "original_repo": entry["original_repo"],
            "base_commit": entry["base_commit"],
            "reference_commit": entry["reference_commit"],
            "setup": entry["setup"],
            "test": entry["test"],
            "src_dir": entry["src_dir"],
            "language": entry["language"],
            "test_framework": entry.get("test_framework", "jest"),
        }
        hf_entries.append(hf_entry)

    return hf_entries


def upload_ts_to_huggingface(
    entries: list[dict], repo_id: str, token: str | None = None
) -> None:
    """Upload TS dataset to HuggingFace Hub (10 fields including language/test_framework)."""
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
            "language": entry.get("language", "typescript"),
            "test_framework": entry.get("test_framework", "jest"),
        }
        flat_entries.append(flat)

    ds = Dataset.from_list(flat_entries)
    logger.info("Uploading to %s...", repo_id)
    ds.push_to_hub(repo_id, split="test", token=token)
    logger.info("Upload complete: https://huggingface.co/datasets/%s", repo_id)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create dataset from prepared TypeScript entries"
    )
    parser.add_argument(
        "entries_file",
        help="Input ts_entries.json from prepare_repo_ts.py",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="ts_custom_dataset.json",
        help="Output dataset JSON file (default: ts_custom_dataset.json)",
    )
    parser.add_argument(
        "--split-name",
        type=str,
        default="custom_ts",
        help="Name for the SPLIT constant (default: custom_ts)",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload to HuggingFace Hub",
    )
    parser.add_argument(
        "--hf-repo",
        type=str,
        default="Ethara-Ai/commit0_typescript",
        help="HuggingFace repo ID (default: Ethara-Ai/commit0_typescript)",
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
        help="Generate Python code to add to constants_ts.py",
    )
    parser.add_argument(
        "--generate-yaml",
        action="store_true",
        help="Generate .commit0.yaml for the custom TS dataset",
    )

    args = parser.parse_args()

    entries = json.loads(Path(args.entries_file).read_text())
    logger.info("Loaded %d entries from %s", len(entries), args.entries_file)

    valid, issues = validate_ts_dataset(entries)
    if issues:
        logger.warning("%d validation issues found", len(issues))
    logger.info("%d / %d entries valid", len(valid), len(entries))

    if not valid:
        logger.error("No valid entries — aborting")
        return

    hf_entries = create_ts_hf_dataset_dict(valid)

    output_path = Path(args.output)
    output_path.write_text(json.dumps(hf_entries, indent=2))
    logger.info("Saved dataset to %s", output_path)

    print(f"\n{'=' * 80}")
    print(f"TS DATASET: {len(valid)} entries")
    print(f"{'=' * 80}")
    for i, e in enumerate(valid, 1):
        fw = e.get("test_framework", "?")
        print(f"  {i:>3}. {e['instance_id']:<35} [{fw}] ({e['original_repo']})")
    print(f"{'=' * 80}\n")

    if args.patch_constants:
        constants_code = generate_ts_split_constants(valid, args.split_name)
        constants_file = Path(f"split_{args.split_name}_ts.py")
        constants_file.write_text(constants_code)
        logger.info("TS_SPLIT constants written to %s", constants_file)
        print(f"\n# Add to constants_ts.py:\n{constants_code}\n")

    if args.generate_yaml:
        yaml_content = generate_commit0_ts_yaml(valid, args.split_name, args.hf_repo)
        yaml_file = Path(f".commit0.{args.split_name}.ts.yaml")
        yaml_file.write_text(yaml_content)
        logger.info("Config written to %s", yaml_file)
        print(yaml_content)

    if args.upload:
        token = args.hf_token or os.environ.get("HF_TOKEN")
        if not token:
            raise EnvironmentError(
                "HF_TOKEN is required for upload. Pass --hf-token or export HF_TOKEN."
            )
        upload_ts_to_huggingface(hf_entries, args.hf_repo, token=token)


if __name__ == "__main__":
    main()
