"""Run C tests for a repository inside a Docker container."""

import git
import logging
import os
import re
import traceback
import sys as _sys  # F-A: emit timeout marker to stderr
from pathlib import Path

_module_logger = logging.getLogger(__name__)

from commit0.harness.constants import (
    EVAL_BACKENDS,
    Files,
)
from commit0.harness.constants_c import (
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
    LocalInplace,
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


def _compile_failure_masked_as_zero(log_dir, output: str) -> bool:
    """True when compilation failed but ``test_exit_code.txt`` recorded 0.

    The C test wrapper writes a 0 exit code on COMPILE_FAILED (the tests never
    ran) and reports the failure via ``compile_errors.txt`` / stdout instead.
    A raw 0 would tell the agent's stage-3 test-refine "all good", so ``main``
    uses this to surface the build failure as a non-zero exit — WITHOUT which the
    model never receives the compiler errors and a one-line fix scores 0%.
    Eval is unaffected (it scores from test_report.xml + the compile-error count).
    """
    ce_path = Path(log_dir) / "compile_errors.txt"
    ce_txt = (
        ce_path.read_text(errors="replace")
        if ce_path.exists() and ce_path.stat().st_size > 0
        else ""
    )
    no_report = not (Path(log_dir) / "test_report.xml").exists()
    return (
        "COMPILE_FAILED" in ce_txt
        or bool(_extract_build_errors(ce_txt))
        or (no_report and bool(_extract_build_errors(output or "")))
    )


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

        # F-A2 audit fix: always flush test output to stdout on the SUCCESS
        # path — the agent-side commit0 CLI (which spawns this module) needs
        # the test output regardless of --verbose. Prior `if verbose > 0:`
        # gate starved aider when local_inplace or a subprocess call passed
        # verbose=0.
        if True:
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
            else:
                # No junit report — a test crashed/aborted (e.g. a still-stubbed
                # function's abort()), ctest failed to run, or no test binary was
                # produced. logger.info(output) above only goes to the LOG FILE, so
                # without this the agent (aider captures stdout) sees NOTHING to
                # refine against and just asks for "the complete test output". Print
                # the raw build/test output to STDOUT so test-refine has real signal.
                _raw = (output or "").strip()
                # Surface the actual compiler diagnostics FIRST. The tail alone can
                # miss them (a `file:line: error:` near the top of a long build log),
                # and compile_errors.txt often holds only the COMPILE_FAILED sentinel
                # — so stage-3 test-refine would see "build failed" with no reason to
                # act on. _extract_build_errors pulls the gcc/clang/ld error lines
                # out of the full output regardless of position.
                _build_errs = _extract_build_errors(_raw)
                _tail = "\n".join(_raw.splitlines()[-80:]) if _raw else ""
                print(
                    "C tests produced NO test_report.xml — a test likely crashed/"
                    "aborted or ctest could not run.\n"
                    + (f"Compiler errors:\n{_build_errs}\n\n" if _build_errs else "")
                    + "Raw build/test output (tail):\n"
                    + f"{_tail if _tail else '(no output captured)'}"
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
        else:
            _module_logger.warning("test_exit_code.txt not found, assuming failure")
            return 1

        # A COMPILE failure makes the test wrapper write test_exit_code=0 (the tests
        # never ran), so a raw 0 tells the agent's stage-3 test-refine "all good" and
        # it does nothing — the exact bug that let a missing `#include` score a
        # permanent 0%. aider's cmd_test only hands output back to the model on a
        # NON-ZERO exit, so surface a build failure as exit 1. The compiler
        # diagnostics are already printed to stdout above, so the model then gets
        # them and can fix the build. Eval scoring is unaffected: evaluate_c derives
        # pass/fail from test_report.xml + the compile-errors count and only treats
        # exit codes OUTSIDE {0,1} specially.
        if exit_code == 0 and _compile_failure_masked_as_zero(log_dir, output):
            _module_logger.warning(
                "C compilation failed but test_exit_code.txt was 0 (tests never "
                "ran); returning non-zero so stage-3 test-refine receives the "
                "compiler errors."
            )
            return 1
        return exit_code
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
