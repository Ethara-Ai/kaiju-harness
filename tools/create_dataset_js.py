"""Create a JS dataset JSON from prepared repo entries and (optionally) upload.

Usage:
    python -m tools.create_dataset_js js_entries.json --output js_dataset.json
    python -m tools.create_dataset_js js_entries.json --upload --hf-token $HF_TOKEN
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from kaiju.paths import datasets_dir
import uuid as _uuid_mod

from commit0.harness.constants_js import (
    JS_SHELL_METACHARS,
    SUPPORTED_NODE_VERSIONS,
    SUPPORTED_PACKAGE_MANAGERS,
    SUPPORTED_TEST_FRAMEWORKS,
)

# TODO(JS-PLAN §11.4): confirm with dataset owner
DEFAULT_HF_REPO: str = "wentingzhao/commit0_js"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


JS_REQUIRED_FIELDS: dict[str, type] = {
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

JS_SETUP_FIELDS: frozenset[str] = frozenset(
    {"node_version", "install", "packages", "pre_install", "specification"}
)
JS_TEST_FIELDS: frozenset[str] = frozenset({"test_cmd", "test_dir"})

_INSTALL_PM_PREFIXES: frozenset[str] = frozenset({"npm", "pnpm", "yarn", "bun"})
_TEST_CMD_PREFIXES: frozenset[str] = frozenset(
    {"npx", "pnpm", "yarn", "bunx", "node", "jest", "vitest", "mocha"}
)
_INSTALL_SHELL_DANGER: frozenset[str] = JS_SHELL_METACHARS


def generate_js_split_constants(
    entries: list[dict],
    split_name: str = "custom_js",
) -> str:
    """Generate Python code to extend `JS_SPLIT` in constants_js.py."""
    repo_names = sorted(entry["repo"] for entry in entries)
    lines = [
        f"# JS split: {split_name} ({len(entries)} repos)",
        f'JS_SPLIT["{split_name}"] = [',
    ]
    for name in repo_names:
        lines.append(f'    "{name}",')
    lines.append("]")
    lines.append("")
    lines.append("# Individual JS repo splits")
    for name in repo_names:
        short = name.split("/")[-1]
        lines.append(f'JS_SPLIT["{short}"] = ["{name}"]')
    return "\n".join(lines)


def generate_commit0_js_yaml(
    entries: list[dict],
    split_name: str,
    dataset_name: str,
) -> str:
    """Generate .commit0.<split>.js.yaml content for a JS dataset."""
    repo_names = sorted(entry["repo"] for entry in entries)
    body = (
        f"# commit0 JavaScript config for dataset: {split_name}\n"
        f"dataset_name: {dataset_name}\n"
        "dataset_split: test\n"
        f"repo_split: {split_name}\n"
        "language: javascript\n"
        "base_dir: repos\n"
        "\n"
        f"# Repos in this split ({len(entries)}):\n"
    )
    for name in repo_names:
        body += f"#   - {name}\n"
    return body


def validate_js_entry(entry: dict, index: int) -> list[str]:
    """Validate a single JS dataset entry; returns list of issues."""
    issues: list[str] = []

    for field, ftype in JS_REQUIRED_FIELDS.items():
        if field not in entry:
            issues.append(f"[{index}] Missing field: {field}")
        elif not isinstance(entry[field], ftype):
            issues.append(
                f"[{index}] {field}: expected {ftype.__name__}, "
                f"got {type(entry[field]).__name__}"
            )

    if "setup" in entry and isinstance(entry["setup"], dict):
        missing = JS_SETUP_FIELDS - set(entry["setup"].keys())
        if missing:
            issues.append(f"[{index}] setup missing fields: {sorted(missing)}")

    if "test" in entry and isinstance(entry["test"], dict):
        missing = JS_TEST_FIELDS - set(entry["test"].keys())
        if missing:
            issues.append(f"[{index}] test missing fields: {sorted(missing)}")

    if "base_commit" in entry and len(entry.get("base_commit", "")) < 7:
        issues.append(f"[{index}] base_commit too short: {entry.get('base_commit', '')}")

    if "reference_commit" in entry and len(entry.get("reference_commit", "")) < 7:
        issues.append(
            f"[{index}] reference_commit too short: "
            f"{entry.get('reference_commit', '')}"
        )

    if "language" in entry and entry["language"] not in ("javascript", "js"):
        issues.append(
            f"[{index}] language must be 'javascript' or 'js', got '{entry['language']}'"
        )

    setup = entry.get("setup")
    if isinstance(setup, dict):
        node_version = setup.get("node_version")
        if node_version is not None:
            try:
                nv = int(node_version)
            except (TypeError, ValueError):
                issues.append(f"[{index}] node_version not an int: {node_version!r}")
            else:
                if nv not in SUPPORTED_NODE_VERSIONS:
                    issues.append(
                        f"[{index}] Unsupported Node.js version {nv}. "
                        f"Supported: {sorted(SUPPORTED_NODE_VERSIONS)}"
                    )
        install_cmd = str(setup.get("install", ""))
        if install_cmd:
            tokens = install_cmd.split()
            first = tokens[0] if tokens else ""
            if first not in _INSTALL_PM_PREFIXES:
                issues.append(
                    f"[{index}] Invalid package manager in setup.install: '{first}'. "
                    f"Allowed: {sorted(_INSTALL_PM_PREFIXES)}"
                )
            if any(c in _INSTALL_SHELL_DANGER for c in install_cmd):
                issues.append(
                    f"[{index}] setup.install contains shell metacharacters: "
                    f"'{install_cmd}'. Only simple package manager commands allowed."
                )

    test_framework = entry.get("test_framework")
    if test_framework and test_framework not in SUPPORTED_TEST_FRAMEWORKS:
        issues.append(
            f"[{index}] Unsupported test_framework '{test_framework}'. "
            f"Supported: {sorted(SUPPORTED_TEST_FRAMEWORKS)}"
        )

    pm = entry.get("package_manager")
    if pm and pm not in SUPPORTED_PACKAGE_MANAGERS:
        issues.append(
            f"[{index}] Unsupported package_manager '{pm}'. "
            f"Supported: {sorted(SUPPORTED_PACKAGE_MANAGERS)}"
        )

    test_info = entry.get("test")
    if isinstance(test_info, dict):
        test_cmd = str(test_info.get("test_cmd", ""))
        if test_cmd:
            tokens = test_cmd.split()
            first = tokens[0] if tokens else ""
            if first not in _TEST_CMD_PREFIXES:
                issues.append(
                    f"[{index}] Unrecognized test_cmd prefix '{first}'. "
                    f"Allowed: {sorted(_TEST_CMD_PREFIXES)}"
                )
            if any(c in JS_SHELL_METACHARS for c in test_cmd):
                issues.append(
                    f"[{index}] test.test_cmd contains shell metacharacters: "
                    f"'{test_cmd}'. Only simple runner commands allowed."
                )

    return issues


def validate_js_dataset(entries: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate all JS entries; returns (valid_entries, all_issues)."""
    all_issues: list[str] = []
    valid: list[dict] = []
    for i, entry in enumerate(entries):
        issues = validate_js_entry(entry, i)
        if issues:
            all_issues.extend(issues)
            logger.warning("Entry %d (%s) has issues:", i, entry.get("instance_id", "?"))
            for issue in issues:
                logger.warning("  %s", issue)
        else:
            valid.append(entry)
    return valid, all_issues


def create_js_hf_dataset_dict(entries: list[dict]) -> list[dict]:
    """Project entries into the JS HuggingFace row schema.

    Assigns a fresh UUID4 to each entry's ``id`` field — each dataset build
    represents a distinct experiment instance, so the id is generated here
    (not in prepare_repo_js.py where entries.json is a reusable source).
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
            "test_framework": entry.get("test_framework", "jest"),
            "package_manager": entry.get("package_manager", "npm"),
        }
        hf_entries.append(hf_entry)
    return hf_entries


def upload_js_to_huggingface(
    entries: list[dict],
    repo_id: str,
    token: str | None = None,
) -> None:
    """Upload the JS dataset to HuggingFace Hub."""
    try:
        from datasets import Dataset
    except ImportError:
        logger.error("Install 'datasets' package: pip install datasets")
        return

    logger.info("Creating HuggingFace dataset with %d entries...", len(entries))

    flat_entries: list[dict] = []
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
            "language": entry.get("language", "javascript"),
            "test_framework": entry.get("test_framework", "jest"),
            "package_manager": entry.get("package_manager", "npm"),
        }
        flat_entries.append(flat)

    ds = Dataset.from_list(flat_entries)
    logger.info("Uploading to %s...", repo_id)
    ds.push_to_hub(repo_id, split="test", token=token)
    logger.info("Upload complete: https://huggingface.co/datasets/%s", repo_id)


def main() -> None:
    """CLI entry: build a JS dataset JSON from prepared entries."""
    parser = argparse.ArgumentParser(
        description="Create dataset from prepared JavaScript entries"
    )
    parser.add_argument("entries_file", help="Input js_entries.json from prepare_repo_js.py")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=(
            "Output dataset JSON file. Default: auto-generated from the "
            "first entry's 'id' field (<uuid>.json), or 'js_custom_dataset.json' "
            "if no id present."
        ),
    )
    parser.add_argument(
        "--split-name", type=str, default="custom_js", help="Name for the JS_SPLIT entry"
    )
    parser.add_argument("--upload", action="store_true", help="Upload to HuggingFace Hub")
    parser.add_argument(
        "--hf-repo",
        type=str,
        default=DEFAULT_HF_REPO,
        help=f"HuggingFace repo ID (default: {DEFAULT_HF_REPO})",
    )
    parser.add_argument(
        "--hf-token", type=str, default=None, help="HuggingFace token (or set HF_TOKEN env)"
    )
    parser.add_argument(
        "--patch-constants",
        action="store_true",
        help="Generate Python code to add to constants_js.py",
    )
    parser.add_argument(
        "--generate-yaml",
        action="store_true",
        help="Generate .commit0.<split>.js.yaml for the custom JS dataset",
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

    entries = json.loads(Path(args.entries_file).read_text(encoding="utf-8"))
    logger.info("Loaded %d entries from %s", len(entries), args.entries_file)

    valid, issues = validate_js_dataset(entries)
    if issues:
        logger.warning("%d validation issues found", len(issues))
    logger.info("%d / %d entries valid", len(valid), len(entries))

    if not valid:
        logger.error("No valid entries — aborting")
        return

    hf_entries = create_js_hf_dataset_dict(valid)

    # An explicit --output ALWAYS wins (it's a documented, deliberate override);
    # only fall back to the consolidated UUID path when the caller didn't ask for a
    # specific file. The consolidated side-effects below (copy_inference_inputs) run
    # regardless, so honoring --output here does not disturb the consolidated layout.
    if args.output:
        output_path = Path(args.output)
    elif _consolidated and hf_entries and hf_entries[0].get("id"):
        output_path = datasets_dir(hf_entries[0]["id"]) / "dataset.json"
    elif hf_entries and hf_entries[0].get("id"):
        output_path = Path(f"{hf_entries[0]['id']}.json")
    else:
        output_path = Path("js_custom_dataset.json")
    output_path.write_text(json.dumps(hf_entries, indent=2), encoding="utf-8")
    logger.info("Saved dataset to %s", output_path)

    if _consolidated and hf_entries and hf_entries[0].get("id"):
        try:
            from kaiju.paths import copy_inference_inputs as _cii
            for e in hf_entries:
                _cii(hf_entries[0]["id"], e["repo"].split("/")[-1], test_ids_subdir="test_ids", repo_base="repos_js")
        except Exception as _e:
            logger.warning("copy_inference_inputs failed: %s", _e)

    bar = "=" * 80
    print(f"\n{bar}")
    print(f"JS DATASET: {len(valid)} entries")
    print(bar)
    for i, e in enumerate(valid, 1):
        fw = e.get("test_framework", "?")
        pm = e.get("package_manager", "?")
        print(f"  {i:>3}. {e['instance_id']:<35} [{fw}/{pm}] ({e['original_repo']})")
    print(f"{bar}\n")

    if args.patch_constants:
        code = generate_js_split_constants(valid, args.split_name)
        constants_file = Path(f"split_{args.split_name}_js.py")
        constants_file.write_text(code, encoding="utf-8")
        logger.info("JS_SPLIT constants written to %s", constants_file)
        print(f"\n# Add to constants_js.py:\n{code}\n")

    if args.generate_yaml:
        yaml_text = generate_commit0_js_yaml(valid, args.split_name, args.hf_repo)
        yaml_file = Path(f".commit0.{args.split_name}.js.yaml")
        yaml_file.write_text(yaml_text, encoding="utf-8")
        logger.info("Config written to %s", yaml_file)
        print(yaml_text)

    if args.upload:
        token = args.hf_token or os.environ.get("HF_TOKEN")
        if not token:
            raise OSError(
                "HF_TOKEN is required for upload. Pass --hf-token or export HF_TOKEN."
            )
        upload_js_to_huggingface(hf_entries, args.hf_repo, token=token)


if __name__ == "__main__":
    main()
