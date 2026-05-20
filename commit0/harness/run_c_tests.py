"""Run C tests for a repository inside a Docker container."""

import git
import logging
import os
import re
import traceback
from pathlib import Path

_module_logger = logging.getLogger(__name__)

from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
)
from commit0.harness.constants_c import (
    CRepoInstance,
    RUN_C_TEST_LOG_DIR,
)
from commit0.harness.spec_c import make_c_spec
from commit0.harness.patch_utils_c import generate_c_patch
from commit0.harness.utils import (
    EvaluationError,
    get_hash_string,
    setup_logger,
    close_logger,
    load_dataset_from_config,
)
from commit0.harness.execution_context import (
    ExecutionBackend,
    Docker,
    Modal,
    E2B,
)


_BUILD_ERROR_PATTERNS = (
    re.compile(r":\d+:\d+:\s*(?:fatal\s+)?error:"),
    re.compile(r"\bundefined reference to\b"),
    re.compile(r"\bundefined symbol\b"),
    re.compile(r"\bld(?:\.lld)?: error:"),
    re.compile(r"\bld returned\s+\d+\s+exit status\b"),
    re.compile(r"\bSegmentation fault\b", re.IGNORECASE),
    re.compile(r"\b(?:cc1|clang|gcc|g\+\+):\s*(?:fatal\s+)?error"),
    re.compile(r"\bCMake Error\b"),
    re.compile(r"\bninja: error\b"),
    re.compile(r"\bmake(?:\[\d+\])?:\s+\*\*\*"),
)


def _extract_build_errors(raw_output: str, max_length: int = 4000) -> str:
    """Extract C compile / link errors from gcc/clang/cmake/ninja output."""
    errors: list[str] = []
    for line in raw_output.splitlines():
        text = line.rstrip()
        if not text.strip():
            continue
        if any(p.search(text) for p in _BUILD_ERROR_PATTERNS):
            errors.append(text)
    result = "\n".join(errors)
    if len(result) > max_length:
        result = result[:max_length] + "\n... (truncated)"
    return result


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

    dataset = load_dataset_from_config(dataset_name, split=dataset_split)
    dataset_name = dataset_name.lower()
    absolute = backend != "e2b"
    spec = None
    example = None
    repo_name = None

    for example in dataset:
        if repo_or_repo_dir.endswith("/"):
            repo_or_repo_dir = repo_or_repo_dir[:-1]
        repo_name = example["repo"].split("/")[-1]
        if repo_name == os.path.basename(repo_or_repo_dir) or repo_or_repo_dir.endswith(
            "/" + repo_name
        ):
            spec = make_c_spec(example, absolute=absolute)
            break

    if spec is None:
        raise ValueError("No C spec available — repo not found in dataset")
    if example is None:
        raise ValueError("No example available")
    if repo_name is None:
        raise ValueError("No repo available")

    hashed_test_ids = get_hash_string(test_ids)
    log_dir = RUN_C_TEST_LOG_DIR / repo_name / branch / hashed_test_ids
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run_c_tests.log"
    logger = setup_logger(repo_name, log_file, verbose=verbose)

    try:
        local_repo = git.Repo(repo_or_repo_dir)
        logger.info(f"Loaded a git repo from {repo_or_repo_dir}")
    except (git.exc.NoSuchPathError, git.exc.InvalidGitRepositoryError):  # type: ignore
        repo_dir = os.path.join(base_dir, repo_name)
        logger.error(f"{repo_or_repo_dir} is not a git dir, trying {repo_dir} again")
        try:
            local_repo = git.Repo(repo_dir)
            repo_or_repo_dir = repo_dir
            logger.info(f"Retried succeeded. Loaded a git repo from {repo_dir}")
        except git.exc.NoSuchPathError as e:  # type: ignore
            raise Exception(
                f"{repo_dir} and {repo_or_repo_dir} are not git directories."
            ) from e
        except Exception as e:
            raise e

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

    patch = generate_c_patch(repo_or_repo_dir, example["base_commit"], commit_id)
    eval_script = spec.eval_script

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
    elif ExecutionBackend(backend) == ExecutionBackend.E2B:
        logger.info("Running E2B")
        execution_context = E2B
    else:
        raise ValueError(
            f"Evaluation must be from {', '.join(EVAL_BACKENDS)}, but {backend} is provided."
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
        "test_report.xml",
        "test_output.txt",
        "compile_errors.txt",
        "test_exit_code.txt",
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
                raise EvaluationError(
                    repo_name,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                    log_file=str(log_file),
                )
        close_logger(logger)

        if verbose > 0:
            xml_path = Path(log_dir / "test_report.xml")
            if xml_path.exists():
                from commit0.harness.c_test_parser import parse_ctest_junit_with_summary

                results, summary = parse_ctest_junit_with_summary(
                    xml_path.read_text(errors="replace")
                )
                print(
                    f"C test results: {summary['passed']} passed, "
                    f"{summary['failed']} failed, {summary['skipped']} skipped, "
                    f"{summary['errored']} errored out of {summary['total']} tests"
                )

            compile_err_path = Path(log_dir / "compile_errors.txt")
            if compile_err_path.exists() and compile_err_path.stat().st_size > 0:
                raw = compile_err_path.read_text(errors="replace")
                build_errors = _extract_build_errors(raw)
                if build_errors:
                    print(f"\nBuild errors (compilation failed):\n{build_errors}")

        exit_code_file = Path(log_dir / "test_exit_code.txt")
        _module_logger.debug("Reading C test exit code from %s", exit_code_file)
        if exit_code_file.exists():
            exit_code = int(exit_code_file.read_text().strip() or "1")
            return exit_code
        else:
            _module_logger.warning("test_exit_code.txt not found, assuming failure")
            return 1
    except EvaluationError as e:
        error_msg = (
            f"Error in running C tests for {repo_name}: {e}\n"
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


__all__: list = []
