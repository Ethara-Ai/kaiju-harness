"""Run Go tests for a repository inside a Docker container."""

import git
import logging
import os
import re
import traceback
from pathlib import Path

_module_logger = logging.getLogger(__name__)

# Defense-in-depth guard for `test_ids`. On the Go path the eval command is built
# entirely from the dataset `test_cmd` (validated in spec_go); `test_ids` is
# currently used ONLY as a log-path/hash component and is NEVER spliced into the
# `go test` shell command. We still reject shell metacharacters here so (a) a
# malformed dataset/CLI value fails fast instead of poisoning a log path, and
# (b) if a future change ever splices test_ids into the command it is already
# guarded. Mirrors run_rust_tests._TEST_IDS_RE: only horizontal whitespace (` `,
# `\t`) is allowed — NOT `\s` (a newline could inject a second shell line) — and
# `\A..\Z` anchors the WHOLE string, not per-line.
_TEST_IDS_RE = re.compile(r"\A[\w \t:./@#=+*\-\[\],~^!]*\Z")

from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
)
from commit0.harness.constants_go import (
    RUN_GO_TEST_LOG_DIR,
)
from commit0.harness.spec_go import make_go_spec
from commit0.harness.patch_utils_go import generate_go_patch
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
    LocalInplace,
    Modal,
    E2B,
)


def _extract_build_errors(raw_json_output: str, max_length: int = 4000) -> str:
    """Extract Go compilation errors from go test -json output."""
    import json as _json

    errors = []
    for line in raw_json_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        action = event.get("Action")
        output = event.get("Output", "")
        if action in ("output", "build-output") and output.strip():
            text = output.strip()
            if (
                ".go:" in text
                or "build failed" in text.lower()
                or "cannot " in text
                or "undefined:" in text
                or "imported and not used" in text
                or "redeclared" in text
                or "syntax error" in text
            ):
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

    if test_ids is not None and not _TEST_IDS_RE.match(test_ids):
        raise ValueError(f"Unsafe characters in test_ids: {test_ids!r}")

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
            spec = make_go_spec(example, absolute=absolute)
            break

    if spec is None:
        raise ValueError("No Go spec available — repo not found in dataset")
    if example is None:
        raise ValueError("No example available")
    if repo_name is None:
        raise ValueError("No repo available")

    hashed_test_ids = get_hash_string(test_ids)
    log_dir = RUN_GO_TEST_LOG_DIR / repo_name / branch / hashed_test_ids
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run_go_tests.log"
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

    patch = generate_go_patch(repo_or_repo_dir, example["base_commit"], commit_id)
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
    elif ExecutionBackend(backend) == ExecutionBackend.LOCAL_INPLACE:
        logger.info("Running locally in-place (git worktree, no new container)")
        execution_context = LocalInplace
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
        "test_output.json",
        "test_stderr.txt",
        "go_test_exit_code.txt",
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
            test_output = Path(log_dir / "test_output.json")
            if test_output.exists():
                from commit0.harness.go_test_parser import parse_go_test_json

                raw = test_output.read_text()
                results = parse_go_test_json(raw)
                passed = sum(1 for s in results.values() if s.value == "PASSED")
                failed = sum(1 for s in results.values() if s.value == "FAILED")
                skipped = sum(1 for s in results.values() if s.value == "SKIPPED")
                print(
                    f"Go test results: {passed} passed, {failed} failed, "
                    f"{skipped} skipped out of {len(results)} tests"
                )

                if len(results) == 0:
                    build_errors = _extract_build_errors(raw)
                    if build_errors:
                        print(f"\nBuild errors (compilation failed):\n{build_errors}")

        go_exit_code_file = Path(log_dir / "go_test_exit_code.txt")
        _module_logger.debug("Reading go test exit code from %s", go_exit_code_file)
        if go_exit_code_file.exists():
            # N7: previously `int(read_text().strip())` propagated ValueError
            # on an OOM-truncated / empty / non-numeric file, killing the
            # evaluator instead of being treated as a run failure. Treat any
            # unparseable exit code as a generic failure (1) with a warning —
            # the surrounding evaluate_go path will classify based on the
            # test_output.json content, so returning 1 here just says 'not 0'.
            raw = go_exit_code_file.read_text().strip()
            try:
                return int(raw)
            except ValueError:
                _module_logger.warning(
                    "go_test_exit_code.txt has non-numeric content %r; treating as failure",
                    raw[:64],
                )
                return 1
        else:
            _module_logger.warning("go_test_exit_code.txt not found, assuming failure")
            return 1
    except EvaluationError as e:
        error_msg = (
            f"Error in running Go tests for {repo_name}: {e}\n"
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
