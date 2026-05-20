import logging
import os

from typing import Iterator
from commit0.harness.utils import (
    clone_repo,
    load_dataset_from_config,
)
from commit0.harness.constants import BASE_BRANCH, RepoInstance, SPLIT
from commit0.harness.split_utils import resolve_split


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
) -> None:
    dataset: Iterator[RepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    dataset_name = dataset_name.lower()
    if (
        "humaneval" in dataset_name
        or "mbpp" in dataset_name
        or "bigcodebench" in dataset_name
        or "codecontests" in dataset_name
    ):
        logger.info("Skipping setup for simple dataset: %s", dataset_name)
        return
    allowed_repos = (
        set(resolve_split(repo_split, dataset, curated=SPLIT))
        if "swe" not in dataset_name
        else set()
    )
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        clone_url = f"https://github.com/{example['repo']}.git"
        if "swe" in dataset_name:
            if repo_split != "all" and repo_split not in example["instance_id"]:
                continue
            clone_dir = os.path.abspath(os.path.join(base_dir, example["instance_id"]))
            branch = example["base_commit"]
        else:
            if repo_name not in allowed_repos:
                continue
            clone_dir = os.path.abspath(os.path.join(base_dir, repo_name))
            # For HF datasets like "wentingzhao/commit0_combined", the last
            # segment is the branch name on the fork.  For local JSON files
            # (paths ending in .json or containing os.sep), fall back to
            # "commit0_all" which is the standard branch name.
            if dataset_name.endswith(".json") or os.sep in dataset_name:
                branch = "commit0_all"
            else:
                branch = dataset_name.split("/")[-1]
        repo = clone_repo(clone_url, clone_dir, branch, logger)
        if BASE_BRANCH in repo.branches:
            repo.git.branch("-D", BASE_BRANCH)
        repo.git.checkout("-b", BASE_BRANCH)
        logger.info(f"Checked out the base branch: {BASE_BRANCH}")

        try:
            gitignore_path = os.path.join(clone_dir, ".gitignore")
            existing_lines: list[str] = []
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    existing_lines = f.read().splitlines()
            added_lines: list[str] = []
            for entry in [".aider*", "logs/"]:
                if entry not in existing_lines:
                    added_lines.append(entry)
            if added_lines:
                with open(gitignore_path, "a") as f:
                    for line in added_lines:
                        f.write(f"\n{line}")
                    f.write("\n")
                repo.git.add(".gitignore")
                repo.git.commit("-m", "chore: add aider/logs to gitignore")
                logger.info(f"Added {added_lines} to .gitignore")
            else:
                logger.info(".gitignore already has aider/logs exclusions")
        except Exception as e:
            logger.warning(f"Failed to update .gitignore: {e}")


__all__ = []
