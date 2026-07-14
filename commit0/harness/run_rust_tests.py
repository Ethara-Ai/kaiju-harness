from __future__ import annotations

import git
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Iterator, Union

# Characters allowed in `test_ids` (cargo/nextest filter args). Alphanumerics,
# HORIZONTAL whitespace only, and the punctuation that appears in test paths /
# filter exprs. Deliberately excludes shell metacharacters: ; | & $ ` ( ) < > " ' \
# NOTE: only ` ` and `\t` are allowed for whitespace, NOT `\s` -- `\s` matches
# newline, and Python's `$` matches before an embedded final newline, so a value
# like "foo\nrm -rf /x" (every char otherwise allowed) would pass and, once
# spliced into `cargo test __TEST_IDS__`, run as a SECOND shell command. `\A..\Z`
# anchors the whole string (not per-line) as a second line of defense.
_TEST_IDS_RE = re.compile(r"\A[\w \t:./@#=+*\-\[\],~^!]*\Z")

from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
    RepoInstance,
)
from commit0.harness.constants_rust import (
    RUN_RUST_TESTS_LOG_DIR,
    RustRepoInstance,
)
from commit0.harness.spec_rust import make_rust_spec
from commit0.harness.patch_utils_rust import filter_rust_patch
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
) -> None:
    dataset: Iterator[Union[RepoInstance, RustRepoInstance]] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    absolute = backend != "e2b"
    spec = None
    example = None
    repo_name = None

    target_base = os.path.basename(repo_or_repo_dir.rstrip("/"))
    for example in dataset:
        # N21 defensive: a malformed dataset row (missing / empty / non-str repo)
        # would previously have `repo_name` degenerate silently — mostly benign
        # because target_base would never match "", but skip explicitly so the
        # loop can't spend time on obviously bad rows.
        repo_field = example.get("repo", "")
        if not isinstance(repo_field, str) or not repo_field:
            continue
        repo_name = repo_field.split("/")[-1]
        # Exact basename match only. A substring/endswith test mis-resolves any
        # repo whose name is a substring of another (`serde` vs `serde_json`,
        # `time` vs `runtime`), silently running the wrong base commit/test cmd.
        if repo_name == target_base:
            spec = make_rust_spec(example, absolute)
            break

    if spec is None:
        raise ValueError("No matching Rust repo found in dataset")
    if example is None:
        raise ValueError("No example found in dataset")
    if repo_name is None:
        raise ValueError("No repo_name resolved")

    hashed_test_ids = get_hash_string(test_ids)
    log_dir = RUN_RUST_TESTS_LOG_DIR / repo_name / branch / hashed_test_ids
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run_rust_tests.log"
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
    # Strip target/ build artefacts so they aren't applied into the eval tree.
    patch = filter_rust_patch(patch)

    # `test_ids` is appended verbatim as cargo CLI args. Reject shell
    # metacharacters so a dataset/CLI value can't break out of the eval script.
    # (`::`, paths, globs and filter expressions remain allowed.)
    if not _TEST_IDS_RE.match(test_ids):
        raise ValueError(f"Unsafe characters in test_ids: {test_ids!r}")
    # Plain string replace (NOT str.format) so literal braces / `${...}` in the
    # script and test_cmd survive untouched.
    eval_script = spec.eval_script.replace("__TEST_IDS__", test_ids)

    patch_file = Path(log_dir / "patch.diff")
    # surrogateescape round-trips any non-UTF8 bytes git smuggled into the diff
    # rather than silently dropping them (errors="ignore") and corrupting it.
    patch_file.write_text(patch, encoding="utf-8", errors="surrogateescape")
    eval_file = Path(log_dir / "eval.sh")
    eval_file.write_text(eval_script, encoding="utf-8")

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
        "cargo_test_exit_code.txt",
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
                raise EvaluationError(
                    repo_name,
                    f"Test timed out after {timeout} seconds.",
                    logger,
                    log_file=str(log_file),
                )
        if verbose > 0:
            test_output = Path(log_dir / "test_output.txt")
            # cargo output can contain non-UTF8 bytes (panic payloads, locale
            # text); replace rather than raise UnicodeDecodeError.
            print(test_output.read_text(encoding="utf-8", errors="replace"))
        exit_code_file = Path(log_dir / "cargo_test_exit_code.txt")
        _module_logger.debug("Reading cargo test exit code from %s", exit_code_file)
        exit_code = int(
            exit_code_file.read_text(encoding="utf-8", errors="replace").strip()
        )
        sys.exit(exit_code)
    except EvaluationError as e:
        error_msg = (
            f"Error running Rust tests for {repo_name}: {e}\n"
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
        # Always release the per-repo log handle and git repo, even on the
        # timeout/error paths -- otherwise FDs leak under the evaluate_rust
        # ThreadPoolExecutor across a large dataset. (SystemExit from the
        # success path passes through finally unharmed.)
        close_logger(logger)
        try:
            local_repo.close()
        except (OSError, ValueError) as e:
            logger.debug("local_repo.close() best-effort cleanup failed: %s", e)


__all__ = ["main"]
