import logging
import os

import git

from typing import Iterator
from commit0.harness.constants import RepoInstance, SPLIT
from commit0.harness.split_utils import resolve_split
from commit0.harness.utils import create_repo_on_github, load_dataset_from_config


logger = logging.getLogger(__name__)


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
    owner: str,
    branch: str,
    github_token: str,
) -> None:
    if github_token is None:
        # Get GitHub token from environment variable if not provided
        github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        raise EnvironmentError(
            "GITHUB_TOKEN is required but not set. "
            "Set it via --github-token flag or GITHUB_TOKEN environment variable."
        )
    dataset: Iterator[RepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    allowed_repos: set[str] = set()
    if "swe" not in dataset_name.lower():
        allowed_repos = set(resolve_split(repo_split, dataset, curated=SPLIT))
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if "swe" in dataset_name.lower():
            if repo_split != "all" and repo_split not in example["instance_id"]:
                continue
        else:
            if repo_name not in allowed_repos:
                continue
        local_repo_path = f"{base_dir}/{repo_name}"
        github_repo_url = f"https://github.com/{owner}/{repo_name}.git"
        github_repo_url = github_repo_url.replace(
            "https://", f"https://x-access-token:{github_token}@"
        )
        _safe_url = github_repo_url.replace(github_token, "***")

        # Initialize the local repository if it is not already initialized
        if not os.path.exists(local_repo_path):
            logger.error("Local repo path does not exist: %s", local_repo_path)
            raise OSError(f"{local_repo_path} does not exists")
        else:
            repo = git.Repo(local_repo_path)

        # create Github repo
        create_repo_on_github(
            organization=owner, repo=repo_name, logger=logger, token=github_token
        )
        # Add your remote repository URL
        remote_name = "progress-tracker"
        if remote_name not in [remote.name for remote in repo.remotes]:
            repo.create_remote(remote_name, url=github_repo_url)
        else:
            logger.info(
                f"Remote {remote_name} already exists, replacing it with {_safe_url}"
            )
            repo.remote(name=remote_name).set_url(github_repo_url)

        # Check if the branch already exists
        if branch in repo.heads:
            repo.git.checkout(branch)
        else:
            raise ValueError(f"The branch {branch} you want save does not exist.")

        # Add all files to the repo and commit if not already committed
        if repo.is_dirty(untracked_files=True):
            repo.git.add(A=True)
            repo.index.commit("AI generated code.")

        # Push to the GitHub repository
        origin = repo.remote(name=remote_name)
        try:
            origin.push(refspec=f"{branch}:{branch}")
            logger.info(f"Pushed to {_safe_url} on branch {branch}")
        except Exception as e:
            logger.error(f"Push {branch} to {owner}/{repo_name} fails.\n{str(e)}")
            continue
            # raise Exception(f"Push {branch} to {owner}/{repo_name} fails.\n{str(e)}")


__all__ = []
