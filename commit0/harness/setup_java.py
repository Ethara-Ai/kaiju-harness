import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional

import git as gitpython
import yaml

from commit0.harness.constants_java import (
    JAVA_SPLIT,
    JAVA_SRC_CONVENTION,
    JAVA_BASE_BRANCH,
    JAVA_REMOTE_BRANCH,
)
from commit0.harness.utils import clone_repo, load_dataset_from_config
from commit0.harness.split_utils import resolve_split

logger = logging.getLogger(__name__)

JAVA_CONFIG_FILE = ".commit0.java.yaml"


def main(
    dataset_name: str,
    dataset_split: str,
    java_version: str,
    base_dir: str,
) -> None:
    repos_dir = Path(base_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)

    dataset = list(load_dataset_from_config(dataset_name, split=dataset_split))
    allowed_repos = set(resolve_split(dataset_split, dataset, curated=JAVA_SPLIT))

    for entry in dataset:
        repo_name = entry["repo"]
        original_repo = entry.get("original_repo", repo_name)
        basename = repo_name.split("/")[-1]
        original_basename = original_repo.split("/")[-1]
        if basename not in allowed_repos and original_basename not in allowed_repos:
            continue

        repo_short = repo_name.split("/")[-1]
        repo_dir = repos_dir / repo_short
        clone_url = f"https://github.com/{repo_name}.git"
        branch = JAVA_REMOTE_BRANCH

        if repo_dir.exists() and (repo_dir / ".git").exists():
            try:
                existing = gitpython.Repo(str(repo_dir))
                if JAVA_BASE_BRANCH in [b.name for b in existing.branches]:
                    existing.git.checkout(JAVA_BASE_BRANCH)
                    existing.git.checkout("--", ".")
                    existing.git.clean("-fd")
                    logger.info(f"{repo_short} already on '{JAVA_BASE_BRANCH}' — skipping clone")
                    continue
            except gitpython.exc.GitCommandError as e:
                logger.warning(f"Failed to check {repo_short}, will re-clone: {e}")

        logger.info(f"Cloning {repo_name} at {branch} -> {repo_dir}")
        try:
            repo = clone_repo(clone_url, str(repo_dir), branch, logger)
        except Exception as clone_err:
            logger.error(
                f"Failed to clone {repo_name}: {clone_err}. "
                f"The remote branch '{branch}' may not exist. "
                f"Run 'python tools/prepare_repo_java.py' for this repo first."
            )
            continue

        if JAVA_BASE_BRANCH in [b.name for b in repo.branches]:
            repo.git.branch("-D", JAVA_BASE_BRANCH)
        repo.git.checkout("-b", JAVA_BASE_BRANCH)
        logger.info(f"Checked out local branch: {JAVA_BASE_BRANCH}")

        try:
            gitignore_path = os.path.join(str(repo_dir), ".gitignore")
            existing_lines: list[str] = []
            if os.path.exists(gitignore_path):
                with open(gitignore_path, "r") as f:
                    existing_lines = f.read().splitlines()
            added_lines: list[str] = []
            for ignore_entry in [".aider*", "logs/"]:
                if ignore_entry not in existing_lines:
                    added_lines.append(ignore_entry)
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

    config = {
        "dataset_name": dataset_name,
        "dataset_split": dataset_split,
        "java_version": java_version,
        "build_system": "auto",
        "test_framework": "auto",
        "docker_timeout": 600,
        "parallel_builds": 4,
        "timeout": 600,
        "base_dir": base_dir,
        "repos_dir": base_dir,
        "patches_dir": "patches/java",
        "results_dir": "results/java",
        "agent": {
            "system_prompt": "agent/prompts/java_system_prompt.md",
            "file_extensions": [".java"],
            "skip_dirs": ["target", "build", ".gradle", ".mvn"],
            "skip_files": ["module-info.java", "package-info.java"],
        },
    }

    config_path = Path(JAVA_CONFIG_FILE)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    logger.info(f"Wrote Java config to {config_path}")


def _commit_spec(repo_dir: Path, repo_short: str) -> None:
    """Stage and commit spec.pdf.bz2 in the given repo directory."""
    import subprocess

    subprocess.run(
        ["git", "add", "spec.pdf.bz2"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", f"Add spec PDF for {repo_short}"],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    logger.info(f"Committed spec.pdf.bz2 for {repo_short}")


def _scrape_specs(
    dataset: list,
    repos_dir: Path,
    allowed_repos: Optional[set],
) -> None:
    specs_dir = repos_dir / "_specs"
    specs_dir.mkdir(parents=True, exist_ok=True)

    scrape_spec_sync = None
    try:
        from tools.scrape_pdf import scrape_spec_sync as _fn

        scrape_spec_sync = _fn
    except ImportError:
        logger.info("scrape_pdf not available — will use cached specs only")

    for entry in dataset:
        repo_name = entry["repo"]
        if allowed_repos is not None and repo_name not in allowed_repos:
            continue

        repo_short = repo_name.split("/")[-1]
        repo_dir = repos_dir / repo_short
        if not repo_dir.exists():
            logger.warning(f"Repo dir missing, skipping spec: {repo_dir}")
            continue

        dest = repo_dir / "spec.pdf.bz2"
        if dest.exists():
            logger.info(f"spec.pdf.bz2 already exists in {repo_short}, skipping")
            continue

        # Try cached spec from _specs/ first (avoids expensive Playwright scraping).
        cached = specs_dir / f"{repo_short}.pdf.bz2"
        if cached.exists():
            shutil.copy2(cached, dest)
            try:
                _commit_spec(repo_dir, repo_short)
            except Exception as e:
                logger.warning(f"Failed to commit cached spec for {repo_short}: {e}")
            continue

        spec_url = entry.get("setup", {}).get("specification", "")
        if not spec_url:
            logger.info(f"No spec URL for {repo_short}, skipping")
            continue

        if scrape_spec_sync is None:
            logger.warning(f"No cached spec and scraper unavailable for {repo_short}")
            continue

        logger.info(f"Scraping spec for {repo_short} from: {spec_url}")
        try:
            spec_path = scrape_spec_sync(
                base_url=spec_url,
                name=repo_short,
                output_dir=str(specs_dir),
                compress=True,
            )
            if not spec_path:
                logger.warning(f"Spec scraping returned no output for {repo_short}")
                continue

            shutil.copy2(spec_path, dest)
            _commit_spec(repo_dir, repo_short)
        except Exception as e:
            logger.warning(f"Spec scraping failed for {repo_short}: {e}")


def _find_java_source_dirs(repo_dir: Path) -> List[Path]:
    """Locate Java source directories, handling both standard and monorepo layouts."""
    standard = repo_dir / JAVA_SRC_CONVENTION
    if standard.exists():
        return [standard]

    candidates = list(repo_dir.rglob("src/main/java"))
    if candidates:
        return [c for c in candidates if "test" not in str(c).lower()]

    # Monorepo layout (e.g. guava): <submodule>/src/ with .java files
    monorepo_dirs = []
    for child in sorted(repo_dir.iterdir()):
        if child.is_dir() and (child / "src").is_dir():
            java_files = list((child / "src").rglob("*.java"))
            if java_files:
                monorepo_dirs.append(child / "src")
    return monorepo_dirs


def stub_repos(
    repos_dir: Path,
    repo_names: List[str],
) -> None:
    from git import Repo
    from tools.stub_java import stub_java_sources

    for repo_name in repo_names:
        repo_short = repo_name.split("/")[-1]
        repo_dir = repos_dir / repo_short
        if not repo_dir.exists():
            logger.warning(f"Repo dir missing, skipping stub: {repo_dir}")
            continue

        local_repo = Repo(str(repo_dir))
        has_stubs = any("stub" in c.message.lower() for c in local_repo.iter_commits(max_count=50))
        if has_stubs:
            logger.info(f"{repo_short} already stubbed, skipping")
            continue

        src_dirs = _find_java_source_dirs(repo_dir)
        if not src_dirs:
            logger.warning(f"No Java source dir found in {repo_dir}, skipping stub")
            continue

        try:
            total_stubs = 0
            total_files = 0
            for src_dir in src_dirs:
                result = stub_java_sources(src_dir=str(src_dir))
                total_stubs += result.get("totalStubs", 0)
                total_files += result.get("totalFiles", 0)
            logger.info(f"Stubbed {repo_name}: {total_stubs} stubs across {total_files} files")

            if total_stubs > 0:
                local_repo.git.add(A=True)
                local_repo.index.commit(f"chore: stub {total_stubs} methods across {total_files} files")
                logger.info(f"Committed stubbed files for {repo_name}")
        except Exception:
            logger.error(f"Failed to stub {repo_name}", exc_info=True)


def save_main(repo: Optional[str] = None) -> None:
    config_path = Path(JAVA_CONFIG_FILE)
    if not config_path.exists():
        raise FileNotFoundError(
            f"{JAVA_CONFIG_FILE} not found. Run 'commit0-java setup' first."
        )

    with open(config_path) as f:
        config = yaml.safe_load(f)

    repos_dir = Path(config.get("repos_dir", "repos/java"))
    patches_dir = Path(config.get("patches_dir", "patches/java"))
    patches_dir.mkdir(parents=True, exist_ok=True)

    if repo:
        repo_dirs = [repos_dir / repo.split("/")[-1]]
    else:
        repo_dirs = [d for d in repos_dir.iterdir() if d.is_dir() and (d / ".git").exists()]

    import subprocess
    for repo_dir in repo_dirs:
        if not repo_dir.exists():
            logger.warning(f"Repo directory not found: {repo_dir}")
            continue

        patch_file = patches_dir / f"{repo_dir.name}.patch"
        result = subprocess.run(
            ["git", "diff", JAVA_BASE_BRANCH],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            patch_file.write_text(result.stdout)
            logger.info(f"Saved patch: {patch_file}")
        else:
            logger.info(f"No changes to save for {repo_dir.name}")
