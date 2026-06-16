import logging
import os
import re
from typing import Iterator

from commit0.harness.utils import clone_repo, load_dataset_from_config
from commit0.harness.constants_js import (
    JS_BASE_BRANCH,
    JS_DATASET_BRANCH,
    JS_GITIGNORE_ENTRIES,
    JS_SPLIT,
    JsRepoInstance,
)
from commit0.harness.split_utils import resolve_split


logger = logging.getLogger(__name__)

_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
) -> None:
    dataset: Iterator[JsRepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )
    normalized_name = dataset_name.lower()

    allowed_repos = set(resolve_split(repo_split, dataset, curated=JS_SPLIT))
    for example in dataset:
        repo_str = example["repo"]
        if not _REPO_NAME_RE.match(repo_str):
            logger.warning(
                "Skipping repo with invalid name: %r (expected owner/name)", repo_str
            )
            continue
        repo_name = repo_str.split("/")[-1]
        clone_url = f"https://github.com/{repo_str}.git"

        if repo_name not in allowed_repos:
            continue

        clone_dir = os.path.abspath(os.path.join(base_dir, repo_name))

        if normalized_name.endswith(".json") or os.sep in normalized_name:
            branch = JS_DATASET_BRANCH
        else:
            branch = normalized_name.split("/")[-1]

        repo = clone_repo(clone_url, clone_dir, branch, logger)

        if JS_BASE_BRANCH in repo.branches:
            repo.git.branch("-D", JS_BASE_BRANCH)
        repo.git.checkout("-b", JS_BASE_BRANCH)
        logger.info(f"Checked out the base branch: {JS_BASE_BRANCH}")

        try:
            exclude_path = os.path.join(clone_dir, ".git", "info", "exclude")
            os.makedirs(os.path.dirname(exclude_path), exist_ok=True)

            existing_lines: list[str] = []
            if os.path.exists(exclude_path):
                with open(exclude_path, "r") as f:
                    existing_lines = f.read().splitlines()

            added_lines: list[str] = [
                entry for entry in JS_GITIGNORE_ENTRIES if entry not in existing_lines
            ]

            if added_lines:
                with open(exclude_path, "a") as f:
                    for line in added_lines:
                        f.write(f"\n{line}")
                    f.write("\n")
                logger.info(
                    "Added %s to %s (worktree-local; no commit so HEAD stays "
                    "aligned with dataset base_commit)",
                    added_lines,
                    exclude_path,
                )
            else:
                logger.info(".git/info/exclude already has all JS exclusions")

        except Exception as e:
            logger.warning(f"Failed to update .git/info/exclude: {e}")
