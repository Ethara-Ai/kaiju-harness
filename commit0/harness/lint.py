import logging
import shutil
import subprocess
import sys
import os
from pathlib import Path
from typing import Iterator, Union, List

from commit0.harness.constants import (
    RepoInstance,
)
from commit0.harness.utils import load_dataset_from_config, relativize

logger = logging.getLogger(__name__)

_CONFIG_FULL = """repos:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.3.0
  hooks:
  - id: check-case-conflict
  - id: mixed-line-ending

- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.6.1
  hooks:
    - id: ruff
      args: [ --fix ]
    - id: ruff-format

- repo: https://github.com/RobertCraigie/pyright-python
  rev: v1.1.376
  hooks:
    - id: pyright"""

_CONFIG_NO_PYRIGHT = """repos:
- repo: https://github.com/pre-commit/pre-commit-hooks
  rev: v4.3.0
  hooks:
  - id: check-case-conflict
  - id: mixed-line-ending

- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.6.1
  hooks:
    - id: ruff
      args: [ --fix ]
    - id: ruff-format
"""


def _check_pyright_available() -> bool:
    """Check if pyright's Node.js binary can execute on this host."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyright", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.debug("pyright availability check failed: %s", e)
        return False


def main(
    dataset_name: str,
    dataset_split: str,
    repo_or_repo_dir: str,
    files: Union[List[Path], None],
    base_dir: str,
) -> None:
    dataset: Iterator[RepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    example = None
    repo_name = None
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if repo_or_repo_dir.endswith("/"):
            repo_or_repo_dir = repo_or_repo_dir[:-1]
        if repo_name in os.path.basename(repo_or_repo_dir):
            logger.debug("Found matching repo: %s", repo_name)
            break
    assert example is not None, "No example available"
    assert repo_name is not None, "No repo available"

    if files is None:
        repo_dir = os.path.join(base_dir, repo_name)
        if os.path.isdir(repo_or_repo_dir):
            repo = repo_or_repo_dir
        elif os.path.isdir(repo_dir):
            repo = repo_dir
        else:
            logger.error(
                "Neither %s nor %s is a valid path", repo_dir, repo_or_repo_dir
            )
            raise Exception(
                f"Neither {repo_dir} nor {repo_or_repo_dir} is a valid path.\nUsage: commit0 lint {{repo_or_repo_dir}}"
            )

        files = []
        repo = os.path.join(repo, example["src_dir"])
        for root, dirs, fs in os.walk(repo):
            for file in fs:
                if file.endswith(".py"):
                    files.append(Path(os.path.join(root, file)))

    config_file = Path(".commit0.pre-commit-config.yaml")
    pyright_ok = _check_pyright_available()
    if not pyright_ok:
        logger.warning(
            "pyright is not available on this host (likely missing libatomic1). "
            "Skipping pyright hook — only ruff checks will run. "
            "Fix: sudo apt-get install -y libatomic1"
        )
    config_file.write_text(_CONFIG_FULL if pyright_ok else _CONFIG_NO_PYRIGHT)
    # Find pre-commit executable: prefer venv, then PATH
    pre_commit_bin = os.path.join(os.path.dirname(sys.executable), "pre-commit")
    if not os.path.isfile(pre_commit_bin):
        pre_commit_bin = shutil.which("pre-commit")
    if not pre_commit_bin:
        logger.error("pre-commit command not found")
        raise FileNotFoundError(
            "Error: pre-commit command not found. "
            "Ensure it is installed in the active virtual environment."
        )
    command = [pre_commit_bin, "run", "--config", str(config_file), "--files"] + [
        str(f) for f in files
    ]

    known_deps: set[str] = set()
    if example is not None:
        setup = (
            example.get("setup", {})
            if isinstance(example, dict)
            else getattr(example, "setup", {})
        )
        if isinstance(setup, dict):
            known_deps = {
                p.lower()
                .split("[")[0]
                .split(">")[0]
                .split("<")[0]
                .split("=")[0]
                .strip()
                for p in setup.get("pip_packages", [])
            }
    project_package = (repo_name or "").replace("-", "_").lower()

    from commit0.harness.lint_filter import filter_lint_output

    logger.info("Using pre-commit binary: %s", relativize(pre_commit_bin))
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        lint_result = filter_lint_output(result.stdout, project_package, known_deps)
        print(lint_result.output)
        logger.info("Lint completed successfully")
        sys.exit(result.returncode)
    except subprocess.CalledProcessError as e:
        raw = e.output or ""
        lint_result = filter_lint_output(raw, project_package, known_deps)
        print(lint_result.output)
        logger.error("Lint failed (exit code %d)", e.returncode)
        if lint_result.code_error_count == 0 and lint_result.suppressed_count > 0:
            logger.info("All %d lint errors were suppressed (third-party/config), exiting 0", lint_result.suppressed_count)
            sys.exit(0)
        sys.exit(e.returncode)
    except FileNotFoundError as e:
        logger.error("pre-commit binary not found: %s", e)
        raise FileNotFoundError(f"Error running pre-commit: {e}") from e
    except Exception as e:
        logger.error("Unexpected error running pre-commit: %s", e, exc_info=True)
        raise Exception(f"An unexpected error occurred: {e}") from e


__all__ = []
