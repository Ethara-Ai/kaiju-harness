import logging
import os
from typing import Iterator

from commit0.harness.utils import clone_repo, load_dataset_from_config
from commit0.harness.constants_ts import (
    TS_BASE_BRANCH,
    TS_DATASET_BRANCH,
    TS_SPLIT,
    TS_GITIGNORE_ENTRIES,
    TsRepoInstance,
)
from commit0.harness.split_utils import resolve_split


logger = logging.getLogger(__name__)


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
) -> None:
    dataset: Iterator[TsRepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    normalized_name = dataset_name.lower()

    allowed_repos = set(resolve_split(repo_split, dataset, curated=TS_SPLIT))
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        clone_url = f"https://github.com/{example['repo']}.git"

        if repo_name not in allowed_repos:
            continue

        clone_dir = os.path.abspath(os.path.join(base_dir, repo_name))

        if normalized_name.endswith(".json") or os.sep in normalized_name:
            branch = TS_DATASET_BRANCH
        else:
            branch = normalized_name.split("/")[-1]

        repo = clone_repo(clone_url, clone_dir, branch, logger)

        if TS_BASE_BRANCH in repo.branches:
            repo.git.branch("-D", TS_BASE_BRANCH)
        repo.git.checkout("-b", TS_BASE_BRANCH)
        logger.info(f"Checked out the base branch: {TS_BASE_BRANCH}")

        try:
            gitignore_path = os.path.join(clone_dir, ".gitignore")

            existing_lines: list[str] = []
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    existing_lines = f.read().splitlines()

            added_lines: list[str] = []
            for entry in TS_GITIGNORE_ENTRIES:
                if entry not in existing_lines:
                    added_lines.append(entry)

            if added_lines:
                with open(gitignore_path, "a") as f:
                    for line in added_lines:
                        f.write(f"\n{line}")
                    f.write("\n")
                repo.git.add(".gitignore")
                repo.git.commit(
                    "-m", "chore: add node_modules/dist/aider/logs to gitignore"
                )
                logger.info(f"Added {added_lines} to .gitignore")
            else:
                logger.info(".gitignore already has all TS exclusions")

        except Exception as e:
            logger.warning(f"Failed to update .gitignore: {e}")
