import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# H10: hoist required-field list to module-level for parity with
# create_dataset_{go,rust,js,ts,cpp,py}.py so schema drift is grep-visible.
REQUIRED_FIELDS: List[str] = ["instance_id", "repo", "base_commit", "reference_commit"]


def validate_java_entry(entry: dict) -> List[str]:
    issues: List[str] = []
    for field in REQUIRED_FIELDS:
        if field not in entry:
            issues.append(f"Missing required field: {field}")
    return issues


def create_java_dataset(
    entries: List[dict],
    output_path: str,
    dataset_name: str = "commit0-java",
) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    valid_entries = []
    for entry in entries:
        issues = validate_java_entry(entry)
        if issues:
            logger.warning(f"Skipping {entry.get('repo', 'unknown')}: {issues}")
            continue
        valid_entries.append(entry)

    with open(output, "w") as f:
        json.dump(valid_entries, f, indent=2)
    logger.info(f"Created dataset with {len(valid_entries)} entries at {output}")


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
