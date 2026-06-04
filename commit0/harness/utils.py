import git
import git.exc
import hashlib
import json
import logging
import os
import time
import re
import sys
from pathlib import Path
from typing import Any, List, Optional, Union

from fastcore.net import HTTP404NotFoundError, HTTP403ForbiddenError  # type: ignore
from ghapi.core import GhApi


_PROTECTED_TEST_PATHSPECS: tuple[str, ...] = (
    ":(exclude,icase)tests/**",
    ":(exclude,icase)test/**",
    ":(exclude,icase)test_*.py",
    ":(exclude,icase)**/test_*.py",
    ":(exclude,icase)*_test.py",
    ":(exclude,icase)**/*_test.py",
    ":(exclude,icase)conftest.py",
    ":(exclude,icase)**/conftest.py",
    ":(exclude,icase)pytest.ini",
    ":(exclude,icase)setup.cfg",
    ":(exclude,icase)tox.ini",
    ":(exclude,icase)pyproject.toml",
    ":(exclude,icase).coveragerc",
    ":(exclude,icase)*.test.ts",
    ":(exclude,icase)*.test.tsx",
    ":(exclude,icase)*.test.js",
    ":(exclude,icase)*.test.jsx",
    ":(exclude,icase)*.test.mjs",
    ":(exclude,icase)*.test.cjs",
    ":(exclude,icase)**/*.test.ts",
    ":(exclude,icase)**/*.test.tsx",
    ":(exclude,icase)**/*.test.js",
    ":(exclude,icase)**/*.test.jsx",
    ":(exclude,icase)**/*.test.mjs",
    ":(exclude,icase)**/*.test.cjs",
    ":(exclude,icase)*.spec.ts",
    ":(exclude,icase)*.spec.tsx",
    ":(exclude,icase)*.spec.js",
    ":(exclude,icase)*.spec.jsx",
    ":(exclude,icase)**/*.spec.ts",
    ":(exclude,icase)**/*.spec.tsx",
    ":(exclude,icase)**/*.spec.js",
    ":(exclude,icase)**/*.spec.jsx",
    ":(exclude,icase)__tests__/**",
    ":(exclude,icase)**/__tests__/**",
    ":(exclude,icase)jest.config.*",
    ":(exclude,icase)vitest.config.*",
    ":(exclude,icase)vitest.workspace.*",
    ":(exclude,icase)babel.config.*",
    ":(exclude,icase).mocharc.*",
    ":(exclude,icase)*_test.go",
    ":(exclude,icase)**/*_test.go",
    ":(exclude,icase)benches/**",
    ":(exclude,icase)src/test/**",
    ":(exclude,icase)src/androidTest/**",
    ":(exclude,icase)src/integrationTest/**",
    ":(exclude,icase)test_*.c",
    ":(exclude,icase)test_*.cpp",
    ":(exclude,icase)test_*.cc",
    ":(exclude,icase)test_*.h",
    ":(exclude,icase)*_test.c",
    ":(exclude,icase)*_test.cpp",
    ":(exclude,icase)*_test.cc",
    ":(exclude,icase)**/test_*.c",
    ":(exclude,icase)**/test_*.cpp",
    ":(exclude,icase)**/test_*.cc",
    ":(exclude,icase)**/test_*.h",
    ":(exclude,icase)**/*_test.c",
    ":(exclude,icase)**/*_test.cpp",
    ":(exclude,icase)**/*_test.cc",
    ":(exclude,icase)googletest/**",
    ":(exclude,icase)gtest/**",
    ":(exclude,icase)**/tests/**",
    ":(exclude,icase)**/test/**",
    ":(exclude,icase)**/src/test/**",
    ":(exclude,icase)**/src/androidTest/**",
    ":(exclude,icase)**/src/integrationTest/**",
    ":(exclude,icase)**/benches/**",
    ":(exclude,icase)**/googletest/**",
    ":(exclude,icase)**/gtest/**",
    ":(exclude,icase)**/pytest.ini",
    ":(exclude,icase)**/setup.cfg",
    ":(exclude,icase)**/tox.ini",
    ":(exclude,icase)**/pyproject.toml",
    ":(exclude,icase)**/.coveragerc",
    ":(exclude,icase)noxfile.py",
    ":(exclude,icase)**/noxfile.py",
    ":(exclude,icase)Cargo.toml",
    ":(exclude,icase)**/Cargo.toml",
    ":(exclude,icase)Cargo.lock",
    ":(exclude,icase)**/Cargo.lock",
    ":(exclude,icase)pom.xml",
    ":(exclude,icase)**/pom.xml",
    ":(exclude,icase)build.gradle",
    ":(exclude,icase)build.gradle.kts",
    ":(exclude,icase)**/build.gradle",
    ":(exclude,icase)**/build.gradle.kts",
    ":(exclude,icase)settings.gradle",
    ":(exclude,icase)settings.gradle.kts",
    ":(exclude,icase)**/settings.gradle",
    ":(exclude,icase)**/settings.gradle.kts",
    ":(exclude,icase)cypress.config.*",
    ":(exclude,icase)playwright.config.*",
    ":(exclude,icase)karma.conf.*",
    ":(exclude,icase)**/cypress.config.*",
    ":(exclude,icase)**/playwright.config.*",
    ":(exclude,icase)**/karma.conf.*",
    ":(exclude,icase)**/jest.config.*",
    ":(exclude,icase)**/vitest.config.*",
    ":(exclude,icase)**/vitest.workspace.*",
    ":(exclude,icase)**/babel.config.*",
    ":(exclude,icase)**/.mocharc.*",
    ":(exclude,icase)CMakeLists.txt",
    ":(exclude,icase)**/CMakeLists.txt",
    ":(exclude,icase)meson.build",
    ":(exclude,icase)**/meson.build",
    ":(exclude,icase)BUILD.bazel",
    ":(exclude,icase)**/BUILD.bazel",
    ":(exclude,icase)WORKSPACE",
    ":(exclude,icase)**/WORKSPACE",
    ":(exclude,icase)MODULE.bazel",
    ":(exclude,icase)**/MODULE.bazel",
    ":(exclude,icase)Makefile",
    ":(exclude,icase)**/Makefile",
    ":(exclude,icase)GNUmakefile",
    ":(exclude,icase)**/GNUmakefile",
    ":(exclude,icase)configure.ac",
    ":(exclude,icase)**/configure.ac",
    ":(exclude,icase)configure.in",
    ":(exclude,icase)**/configure.in",
    ":(exclude,icase)e2e/**",
    ":(exclude,icase)**/e2e/**",
    ":(exclude,icase)cypress/**",
    ":(exclude,icase)**/cypress/**",
    ":(exclude,icase)playwright/**",
    ":(exclude,icase)**/playwright/**",
    ":(exclude,icase)integration/**",
    ":(exclude,icase)**/integration/**",
    ":(exclude,icase)*Test.java",
    ":(exclude,icase)**/*Test.java",
    ":(exclude,icase)*Tests.java",
    ":(exclude,icase)**/*Tests.java",
    ":(exclude,icase)*IT.java",
    ":(exclude,icase)**/*IT.java",
    ":(exclude,icase)*Spec.groovy",
    ":(exclude,icase)**/*Spec.groovy",
    ":(exclude,icase)testdata/**",
    ":(exclude,icase)**/testdata/**",
    ":(exclude,icase)test-data/**",
    ":(exclude,icase)**/test-data/**",
    ":(exclude,icase)test_data/**",
    ":(exclude,icase)**/test_data/**",
    ":(exclude,icase)fixtures/**",
    ":(exclude,icase)**/fixtures/**",
    ":(exclude,icase)*.feature",
    ":(exclude,icase)**/*.feature",
    ":(exclude,icase)features/**",
    ":(exclude,icase)**/features/**",
    # ── Issue 3 extension: Python startup hooks, env, git metadata ──
    ":(exclude,icase)sitecustomize.py",
    ":(exclude,icase)**/sitecustomize.py",
    ":(exclude,icase)usercustomize.py",
    ":(exclude,icase)**/usercustomize.py",
    ":(exclude,icase)*.pth",
    ":(exclude,icase)**/*.pth",
    ":(exclude,icase).env",
    ":(exclude,icase)**/.env",
    ":(exclude,icase).gitmodules",
    ":(exclude,icase)**/.gitmodules",
    ":(exclude,icase).gitattributes",
    ":(exclude,icase)**/.gitattributes",
)


class EvaluationError(Exception):
    def __init__(
        self, repo: str, message: str, logger: logging.Logger, log_file: str = ""
    ):
        super().__init__(message)
        self.super_str = super().__str__()
        self.repo = repo
        self.log_file = log_file
        self.logger = logger

    def __str__(self):
        return (
            f"Evaluation error for {self.repo}: {self.super_str}\n"
            f"Check ({self.log_file}) for more information."
        )


def setup_logger(
    repo: str, log_file: Path, mode: str = "w", verbose: int = 1
) -> logging.Logger:
    """Used for logging the build process of images and running containers.
    It writes logs to the log file.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"{repo}.{log_file.name}")
    handler = logging.FileHandler(log_file, mode=mode)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if verbose == 2:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        logger.addHandler(stdout_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    setattr(logger, "log_file", log_file)
    return logger


def close_logger(logger: logging.Logger) -> None:
    """Closes all handlers associated with the given logger to prevent too many open files."""
    # To avoid too many open files
    for handler in logger.handlers:
        handler.close()
        logger.removeHandler(handler)


def get_hash_string(input_string: str) -> str:
    # Create a new SHA-256 hash object
    sha256 = hashlib.sha256()
    # Update the hash object with the bytes of the input string
    sha256.update(input_string.encode("utf-8"))
    # Obtain the hexadecimal digest of the hash
    hash_hex = sha256.hexdigest()[:22]
    return hash_hex


def extract_test_output(ss: str, pattern: str) -> str:
    s = ss.split("\n")
    out = []
    append = False
    for one in s:
        if one.startswith("+") and pattern in one:
            append = True
        # the next command started here, so we finished reading test output
        elif append and one.startswith("+"):
            # remove the first element "+ {command}"
            out = out[1:]
            return "\n".join(out).strip()
        if append:
            out.append(one)
    return ""


def clone_repo(
    clone_url: str, clone_dir: str, branch: str, logger: logging.Logger
) -> git.Repo:
    """Clone repo into the specified directory if it does not already exist.

    If the repository already exists in the specified directory,
    it fetches the latest changes and checks out the specified commit.

    Parameters
    ----------
    clone_url : str
        URL of the repository to clone.
    clone_dir : str
        Directory where the repository will be cloned.
    branch : str
        The branch/tag name to checkout.
    logger : logging.Logger
        The logger object.

    Returns
    -------
    git.Repo
        The cloned repository object.

    Raises
    ------
    RuntimeError
        If cloning or checking out the repository fails.

    """
    # Check if the repository already exists
    if os.path.exists(clone_dir):
        logger.info(f"Repository already exists at {clone_dir}. Fetching updates.")
        try:
            repo = git.Repo(clone_dir)
            repo.git.fetch()
        except git.exc.GitCommandError as e:
            logger.error("Failed to fetch updates for %s: %s", clone_dir, e)
            raise RuntimeError(f"Failed to fetch updates for repository: {e}") from e
    else:
        logger.info(f"Cloning {clone_url} into {clone_dir}")
        try:
            repo = git.Repo.clone_from(clone_url, clone_dir)
        except git.exc.GitCommandError as e:
            logger.error("Failed to clone %s: %s", clone_url, e)
            raise RuntimeError(f"Failed to clone repository: {e}") from e

    try:
        repo.git.checkout(branch)
    except git.exc.GitCommandError as e:
        logger.error("Failed to check out branch %s in %s: %s", branch, clone_dir, e)
        raise RuntimeError(f"Failed to check out {branch}: {e}") from e

    return repo


def create_repo_on_github(
    organization: str,
    repo: str,
    logger: logging.Logger,
    token: Optional[str] = None,
    max_retries: int = 10,
) -> None:
    api = GhApi(token=token)
    for attempt in range(max_retries):
        try:
            api.repos.get(owner=organization, repo=repo)  # type: ignore
            logger.info(f"{organization}/{repo} already exists")
            return
        except HTTP403ForbiddenError:
            for _ in range(60):
                rl = api.rate_limit.get()  # type: ignore
                logger.info(
                    f"Rate limit exceeded, waiting. Remaining: {rl.resources.core.remaining}"
                )
                if rl.resources.core.remaining > 0:
                    break
                time.sleep(60 * 5)
            else:
                raise RuntimeError(
                    f"Rate limit not recovered after 5 hours for {organization}/{repo}"
                )
        except HTTP404NotFoundError:
            api.repos.create_in_org(org=organization, name=repo)  # type: ignore
            logger.info(f"Created {organization}/{repo} on GitHub")
            return
    raise RuntimeError(
        f"Failed to create/access {organization}/{repo} after {max_retries} retries"
    )


def generate_patch_between_commits(
    repo: git.Repo, old_commit: str, new_commit: str
) -> str:
    """Generate a patch string by comparing two specified commits.

    Args:
    ----
        repo (git.Repo): An instance of the git.Repo object representing the repository.
        old_commit (str): The hash or reference to the old commit.
        new_commit (str): The hash or reference to the new commit.

    Returns:
    -------
        patch (str): A string containing the patch in the diff format between the two commits

    Raises:
    ------
        git.GitCommandError: If there is an error while running git commands.

    """
    try:
        patch = repo.git.diff(
            "--no-renames",
            old_commit,
            new_commit,
            "--",
            ".",
            ":(exclude)spec.pdf.bz2",
            ":(exclude)spec.pdf",
            ":(exclude).aider*",
            ":(exclude)*.pdf",
            ":(exclude)*.pdf.bz2",
            *_PROTECTED_TEST_PATHSPECS,
        )
        return patch + "\n\n"
    except git.GitCommandError as e:
        raise Exception(f"Error generating patch: {e}") from e


def get_active_branch(repo_path: Union[str, Path]) -> str:
    """Retrieve the current active branch of a Git repository.

    Args:
    ----
        repo_path (Path): The path to git repo.

    Returns:
    -------
        str: The name of the active branch.

    Raises:
    ------
        Exception: If the repository is in a detached HEAD state.

    """
    repo = git.Repo(repo_path)
    try:
        # Get the current active branch
        branch = repo.active_branch.name
    except TypeError as e:
        raise Exception(
            f"{e}\nThis means the repository is in a detached HEAD state. "
            "To proceed, please specify a valid branch by using --branch {branch}."
        ) from e

    return branch


def extract_code_blocks(text: str) -> List[str]:
    """Extract Python code blocks from a given text wrapped in markdown markers.

    This function identifies and extracts all Python code blocks within a provided
    text. The code blocks should be surrounded by markdown-style markers, such as
    ```python ... ```.

    Args:
    ----
        text (str): The input text containing Python code blocks marked with
                    ```python ... ```.

    Returns:
    -------
        List[str]: A list of strings, each containing a Python code block extracted
                   from the text.

    """
    pattern = r"```python\n(.*?)```"
    matches = re.finditer(pattern, text, re.DOTALL)
    return [match.group(1).strip() for match in matches]


def load_dataset_from_config(dataset_name: str, split: str = "test") -> Any:
    """Load dataset from a local JSON file path or a HuggingFace dataset identifier."""
    local_path = None
    if dataset_name.endswith(".json"):
        local_path = dataset_name
    elif os.sep in dataset_name or "/" in dataset_name:
        # HF identifiers are "org/dataset" (one slash, no extension) — anything else is a path
        parts = dataset_name.split("/")
        if len(parts) != 2 or "." in parts[-1] or os.path.exists(dataset_name):
            local_path = dataset_name

    if local_path is not None:
        resolved = Path(local_path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"Local dataset file not found: {resolved}\n"
                f"If this is a HuggingFace dataset, remove the file extension."
            )
        with open(resolved) as f:
            data = json.load(f)
        if isinstance(data, dict) and "data" in data:
            entries = data["data"]
        elif isinstance(data, list):
            entries = data
        else:
            raise ValueError(
                f"Unexpected JSON structure in {resolved}. "
                f"Expected a list or an object with a 'data' key."
            )
        logging.getLogger(__name__).info(
            f"Loaded {len(entries)} entries from local dataset: {resolved}"
        )
        return entries

    from datasets import load_dataset

    return list(load_dataset(dataset_name, split=split))  # type: ignore


__all__ = []
