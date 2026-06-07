"""Clone and set up Go repos for the commit0 Go pipeline.

Mirrors commit0/harness/setup.py but uses GO_SPLIT for filtering.
Does NOT modify the original setup.py.
"""

import logging
import os
from typing import Iterator

from commit0.harness.constants import BASE_BRANCH
from commit0.harness.constants_go import GoRepoInstance, GO_SPLIT
from commit0.harness.utils import clone_repo, load_dataset_from_config
from commit0.harness.split_utils import resolve_split

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
) -> None:
    """Clone and prepare Go repos for evaluation.

    Parameters
    ----------
    dataset_name : str
        Name or path of the Go dataset.
    dataset_split : str
        HuggingFace split or "test" for local JSON.
    repo_split : str
        Key from GO_SPLIT, a repo name, or "all".
    base_dir : str
        Local directory to clone repos into.

    """
    dataset: Iterator[GoRepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore

    allowed_repos = set(resolve_split(repo_split, dataset, curated=GO_SPLIT))
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in allowed_repos:
            continue

        clone_url = f"https://github.com/{example['repo']}.git"
        clone_dir = os.path.abspath(os.path.join(base_dir, repo_name))

        dataset_lower = dataset_name.lower()
        if dataset_lower.endswith(".json") or os.sep in dataset_lower:
            branch = "commit0_all"
        else:
            branch = dataset_lower.split("/")[-1]

        repo = clone_repo(clone_url, clone_dir, branch, logger)

        if BASE_BRANCH in repo.branches:
            repo.git.branch("-D", BASE_BRANCH)
        repo.git.checkout("-b", BASE_BRANCH)
        logger.info("Checked out base branch: %s for %s", BASE_BRANCH, repo_name)

        try:
            gitignore_path = os.path.join(clone_dir, ".gitignore")
            existing_lines: list[str] = []
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    existing_lines = f.read().splitlines()
            added_lines: list[str] = []
            for entry in [".aider*", "logs/", "vendor/"]:
                if entry not in existing_lines:
                    added_lines.append(entry)
            if added_lines:
                with open(gitignore_path, "a") as f:
                    for line in added_lines:
                        f.write(f"\n{line}")
                    f.write("\n")
                repo.git.add(".gitignore")
                repo.git.commit("-m", "chore: add aider/logs/vendor to gitignore")
                logger.info("Added %s to .gitignore", added_lines)
            else:
                logger.info(".gitignore already has needed exclusions")
        except Exception as e:
            logger.warning("Failed to update .gitignore: %s", e)


__all__: list[str] = []
