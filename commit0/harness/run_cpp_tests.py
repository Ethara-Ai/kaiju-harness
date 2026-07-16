from __future__ import annotations

import git
import logging
import os
import traceback
import sys as _sys  # F-A: emit timeout marker to stderr
from pathlib import Path
from typing import Iterator, Union

from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
    RepoInstance,
)
from commit0.harness.constants_cpp import (
    RUN_CPP_TESTS_LOG_DIR,
    CppRepoInstance,
)
from commit0.harness.spec_cpp import make_cpp_spec
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
    LocalInplace,
    Modal,
    E2B,
)

_module_logger = logging.getLogger(__name__)


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
) -> int:
    dataset: Iterator[Union[RepoInstance, CppRepoInstance]] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    absolute = backend != "e2b"
    spec = None
    example = None
    repo_name = None

    for example in dataset:
        if repo_or_repo_dir.endswith("/"):
            repo_or_repo_dir = repo_or_repo_dir[:-1]
        repo_name = example["repo"].split("/")[-1]
        if repo_name in os.path.basename(repo_or_repo_dir) or repo_or_repo_dir.endswith(
            repo_name
        ):
            spec = make_cpp_spec(example, absolute)
            break

    if spec is None:
        raise ValueError("No matching C++ repo found in dataset")
    if example is None:
        raise ValueError("No example found in dataset")
    if repo_name is None:
        raise ValueError("No repo_name resolved")

    hashed_test_ids = get_hash_string(test_ids)
    log_dir = RUN_CPP_TESTS_LOG_DIR / repo_name / branch / hashed_test_ids
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run_cpp_tests.log"
    logger = setup_logger(repo_name, log_file, verbose=verbose)

    try:
        local_repo = git.Repo(repo_or_repo_dir)
        logger.info(f"Loaded git repo from {repo_or_repo_dir}")
    except (git.exc.NoSuchPathError, git.exc.InvalidGitRepositoryError):  # type: ignore
        repo_dir = os.path.join(base_dir, repo_name)
        logger.error(f"{repo_or_repo_dir} is not a git dir, trying {repo_dir}")
        try:
            local_repo = git.Repo(repo_dir)
            logger.info(f"Retry succeeded. Loaded git repo from {repo_dir}")
        except git.exc.NoSuchPathError as e:  # type: ignore
            raise Exception(
                f"{repo_dir} and {repo_or_repo_dir} are not git directories."
            ) from e

    commit_id = ""
    if branch == "reference":
        commit_id = example["reference_commit"]
    else:
        if branch in local_repo.branches:
            commit_id = local_repo.commit(branch).hexsha
        else:
            found_remote = False
            for remote in local_repo.remotes:
                remote.fetch()
                for ref in remote.refs:
                    if ref.remote_head == branch:
                        commit_id = local_repo.commit(ref.name).hexsha
                        found_remote = True
                        break
                if found_remote:
                    break
            if not found_remote:
                logger.error("Branch %s does not exist for %s", branch, repo_name)
                raise Exception(f"Branch {branch} does not exist locally or remotely.")

    patch = generate_patch_between_commits(
        local_repo, example["base_commit"], commit_id
    )

    eval_script = spec.eval_script.replace("{test_ids}", test_ids)

    patch_file = Path(log_dir / "patch.diff")
    patch_file.write_text(patch, encoding="utf-8", errors="ignore")
    eval_file = Path(log_dir / "eval.sh")
    eval_file.write_text(eval_script)

    backend = backend.upper()
    if ExecutionBackend(backend) == ExecutionBackend.MODAL:
        logger.info("Running on Modal")
        execution_context = Modal
    elif ExecutionBackend(backend) == ExecutionBackend.LOCAL:
        logger.info("Running locally")
        execution_context = Docker
    elif ExecutionBackend(backend) == ExecutionBackend.LOCAL_INPLACE:
        logger.info("Running locally in-place (git worktree, no new container)")
        execution_context = LocalInplace
    elif ExecutionBackend(backend) == ExecutionBackend.E2B:
        logger.info("Running on E2B")
        execution_context = E2B
    else:
        raise ValueError(
            f"Backend must be from {', '.join(EVAL_BACKENDS)}, got {backend}."
        )

    files_to_copy = Files(
        eval_script={
            "src": eval_file,
            "dest": Path("/eval.sh" if absolute else "eval.sh"),
        },
        patch={
            "src": patch_file,
            "dest": Path("/patch.diff" if absolute else "patch.diff"),
        },
    )
    files_to_collect = [
        "test_exit_code.txt",
        "test_output.txt",
    ]

    eval_command = (
        "/bin/bash /eval.sh"
        if ExecutionBackend(backend) != ExecutionBackend.E2B
        else "/bin/bash eval.sh"
    )

    try:
        with execution_context(
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
                # F-A audit fix: flush test output to stdout BEFORE raising so
                # the caller (aider captures the eval subprocess stdout) sees
                # WHAT the tests produced before the timeout kill. Without this
                # the agent only saw the "Test timed out after Ns" line and had
                # zero signal to refine against.
                try:
                    _to = Path(log_dir / "test_output.txt")
                    if _to.exists():
                        print(_to.read_text(encoding="utf-8", errors="replace"))
                    else:
                        print(output or "(no test output captured before timeout)")
                except OSError:
                    pass
                print(
                    f"\n[TIMEOUT: test process killed after {timeout}s "
                    f"(bump via KAIJU_AGENT_TEST_TIMEOUT_SEC or --timeout)]",
                    file=_sys.stderr,
                )
                raise EvaluationError(
                    repo_name,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                    log_file=str(log_file),
                )
        close_logger(logger)
        # F-A2 audit fix: always flush test output to stdout on the
        # SUCCESS path — the agent-side commit0 CLI (which spawns this
        # module) needs the test output regardless of --verbose. Prior
        # `if verbose > 0:` gate starved aider when local_inplace or a
        # subprocess call passed verbose=0.
        if True:
            test_output = Path(log_dir / "test_output.txt")
            print(test_output.read_text())
        exit_code_file = Path(log_dir / "test_exit_code.txt")
        _module_logger.debug("Reading C++ test exit code from %s", exit_code_file)
        exit_code = int(exit_code_file.read_text().strip())
        return exit_code
    except EvaluationError as e:
        error_msg = (
            f"Error running C++ tests for {repo_name}: {e}\n"
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


__all__ = ["main"]
