"""Docker test runner for TypeScript repos.

Analogue of run_pytest_ids.py — runs Jest/Vitest tests inside Docker containers.
"""

import git
import logging
import os
import shlex
import sys
import traceback
from pathlib import Path

_module_logger = logging.getLogger(__name__)

from typing import Iterator, Union, cast
from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.constants_ts import RUN_TS_TEST_LOG_DIR
from commit0.harness.spec_ts import make_ts_spec
from commit0.harness.utils import (
    EvaluationError,
    get_hash_string,
    generate_patch_between_commits,
    setup_logger,
    close_logger,
    load_dataset_from_config,
)
from commit0.harness.execution_context import (
    ExecutionBackend,
    Docker,
)


def _inject_test_ids(eval_script: str, test_ids: str) -> str:
    """Append test file/ID filter to the test command line in the eval script.

    Finds the line containing ``--forceExit`` (Jest) or ``vitest`` and appends
    *test_ids* so only the requested tests are executed. Control characters
    (newline, carriage return, NUL) are stripped so a malicious or malformed
    test id cannot inject additional shell lines into the eval script.
    """
    if not test_ids:
        return eval_script

    # Tokenize test_ids and shlex-quote each so shell metacharacters in a
    # dataset-supplied id cannot escape argv into a new shell command.
    tokens = [
        t for t in test_ids.replace("\r", " ").replace("\x00", "").split()
        if t
    ]
    quoted = " ".join(shlex.quote(t) for t in tokens)

    lines = eval_script.split("\n")
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_jest_line = "--forceExit" in line
        is_vitest_line = " vitest " in " " + stripped + " "
        is_node_test_line = "--test-reporter=tap" in line and " node " in " " + stripped + " "
        if (is_jest_line or is_vitest_line or is_node_test_line) and ">" in line:
            line = line.rstrip() + " " + quoted
        new_lines.append(line)
    return "\n".join(new_lines)


def main(
    dataset_name: str,
    dataset_split: str,
    base_dir: str,
    repo_or_repo_dir: str,
    branch: str,
    test_ids: str,
    backend: str,
    timeout: int,
    num_cpus: int,
    rebuild_image: bool,
    verbose: int,
) -> None:
    """Run Jest/Vitest tests for a TypeScript repo inside a Docker container.

    Mirrors the workflow of ``run_pytest_ids.main`` but uses the TS spec
    pipeline (``make_ts_spec``) and injects test IDs into the Jest/Vitest
    command.
    """
    dataset: Iterator[Union[RepoInstance, SimpleInstance]] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    dataset_name = dataset_name.lower()
    absolute = True  # TS pipeline always uses absolute paths for Docker

    spec = None
    example: Union[RepoInstance, SimpleInstance, None] = None
    repo_name = None

    for example in dataset:
        if repo_or_repo_dir.endswith("/"):
            repo_or_repo_dir = repo_or_repo_dir[:-1]
        repo_name = example["repo"].split("/")[-1]
        target_basename = os.path.basename(repo_or_repo_dir)
        if target_basename == repo_name or repo_or_repo_dir.endswith("/" + repo_name):
            spec = make_ts_spec(cast(RepoInstance, example), absolute=absolute)
            break

    if spec is None:
        raise ValueError("No spec available")
    if example is None:
        raise ValueError("No example available")
    if repo_name is None:
        raise ValueError("No repo available")

    hashed_test_ids = get_hash_string(test_ids)
    # set up logging
    log_dir = RUN_TS_TEST_LOG_DIR / repo_name / branch / hashed_test_ids
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run_ts_tests.log"
    logger = setup_logger(repo_name, log_file, verbose=verbose)

    local_repo = None
    try:
        local_repo = git.Repo(repo_or_repo_dir)
        logger.info(f"Loaded a git repo from {repo_or_repo_dir}")
    except (git.exc.NoSuchPathError, git.exc.InvalidGitRepositoryError):  # type: ignore
        repo_dir = os.path.join(base_dir, repo_name)
        logger.error(f"{repo_or_repo_dir} is not a git dir, trying {repo_dir} again")
        try:
            local_repo = git.Repo(repo_dir)
            logger.info(f"Retried succeeded. Loaded a git repo from {repo_dir}")
        except git.exc.NoSuchPathError as e:  # type: ignore
            raise Exception(
                f"{repo_dir} and {repo_or_repo_dir} are not git directories.\n"
                "Usage: commit0 ts test {repo_dir} {branch} {test_ids}"
            ) from e

    # Resolve branch to commit SHA
    commit_id = ""
    if branch == "reference":
        commit_id = example["reference_commit"]
    else:
        if branch in local_repo.branches:
            commit_id = local_repo.commit(branch).hexsha
        else:
            found_remote_branch = False
            for remote in local_repo.remotes:
                remote.fetch()
                for ref in remote.refs:
                    if ref.remote_head == branch:
                        commit_id = local_repo.commit(ref.name).hexsha
                        found_remote_branch = True
                        break
                if found_remote_branch:
                    break
            if not found_remote_branch:
                logger.error(
                    "Branch %s does not exist locally or remotely for %s",
                    branch,
                    repo_name,
                )
                raise Exception(f"Branch {branch} does not exist locally or remotely.")

    # Generate patch between base_commit and resolved commit
    patch = generate_patch_between_commits(
        local_repo, example["base_commit"], commit_id
    )

    # Build eval script, inject test_ids if provided
    eval_script = spec.eval_script
    eval_script = _inject_test_ids(eval_script, test_ids)

    # Write eval.sh and patch.diff to log_dir
    patch_file = Path(log_dir / "patch.diff")
    patch_file.write_text(patch, encoding="utf-8", errors="ignore")
    eval_file = Path(log_dir / "eval.sh")
    eval_file.write_text(eval_script)

    backend = backend.upper()
    if ExecutionBackend(backend) != ExecutionBackend.LOCAL:
        raise ValueError(
            f"TS pipeline only supports LOCAL (Docker) backend, got {backend}. "
            f"Valid backends: {', '.join(EVAL_BACKENDS)}"
        )

    logger.info("Running locally via Docker")

    files_to_copy = Files(
        eval_script={
            "src": eval_file,
            "dest": Path("/eval.sh"),
        },
        patch={
            "src": patch_file,
            "dest": Path("/patch.diff"),
        },
    )
    files_to_collect = [
        "report.json",
        "report.tap",
        "test_exit_code.txt",
        "test_output.txt",
    ]

    eval_command = "/bin/bash /eval.sh"
    try:
        with Docker(
            spec,
            logger,
            timeout,
            num_cpus,
            log_dir,
            files_to_copy,
            files_to_collect,
            rebuild_image,
        ) as context:
            output, timed_out, total_runtime = context.exec_run_with_timeout(
                eval_command
            )
            logger.info(output)
            if timed_out:
                raise EvaluationError(
                    repo_name,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                    log_file=str(log_file),
                )
        if verbose > 0:
            test_output = Path(log_dir / "test_output.txt")
            if test_output.exists():
                print(test_output.read_text())
        exit_code_file = Path(log_dir / "test_exit_code.txt")
        _module_logger.debug("Reading test exit code from %s", exit_code_file)
        try:
            exit_code = int(exit_code_file.read_text().strip())
        except (FileNotFoundError, ValueError) as exc:
            _module_logger.warning(
                "Could not read exit code from %s: %s — defaulting to 1",
                exit_code_file,
                exc,
            )
            exit_code = 1
        sys.exit(exit_code)
    except EvaluationError as e:
        error_msg = (
            f"Error in running TS tests for {repo_name}: {e}\n"
            f"{traceback.format_exc()}\n"
            f"Check ({log_file}) for more information."
        )
        raise EvaluationError(
            repo_name, error_msg, logger, log_file=str(log_file)
        ) from e
    except Exception as e:
        error_msg = (
            f"General error: {e}\n"
            f"{traceback.format_exc()}\n"
            f"Check ({log_file}) for more information."
        )
        raise RuntimeError(error_msg) from e
    finally:
        if local_repo is not None:
            local_repo.close()
        close_logger(logger)
