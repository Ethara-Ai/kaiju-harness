"""Run Java tests inside a Docker container.

Follows the same pattern as run_pytest_ids.py: write eval script to file,
copy into container, execute via /bin/bash, collect results.
"""
import logging
import traceback
import sys as _sys  # F-A: emit timeout marker to stderr
from pathlib import Path
from typing import Dict, List, Optional

from commit0.harness.constants import Files, RUN_PYTEST_LOG_DIR
from commit0.harness.execution_context import Docker, ExecutionBackend, LocalInplace
from commit0.harness.spec_java import make_java_spec
from commit0.harness.java_test_parser import (
    parse_surefire_reports,
    summarize_results,
)
from commit0.harness.utils import setup_logger

logger = logging.getLogger(__name__)


def run_java_tests(
    instance: dict,
    test_ids: Optional[List[str]] = None,
    timeout: int = 600,
    num_cpus: int = 1,
    log_dir: Optional[str] = None,
    verbose: int = 0,
    backend: str = "local",
) -> Dict[str, str]:
    """Run Java tests inside a Docker container and return parsed results.

    Args:
    ----
        instance: Repo instance dict.
        test_ids: Specific test IDs to run. None = run all tests.
        timeout: Container execution timeout in seconds.
        num_cpus: CPU limit for the container.
        log_dir: Directory for logs and artifacts.
        verbose: Verbosity level. >0 prints test output.

    """
    spec = make_java_spec(instance, test_ids=test_ids)
    repo_name = instance.get("repo", instance.get("instance_id", "unknown"))

    log_path = Path(log_dir) if log_dir else RUN_PYTEST_LOG_DIR / repo_name
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "run_java_tests.log"
    test_logger = setup_logger(repo_name, log_file, verbose=verbose)

    eval_script_content = "\n".join(
        ["#!/bin/bash", "set -uxo pipefail"] + spec.make_eval_script_list() + [""]
    )
    eval_file = log_path / "eval.sh"
    eval_file.write_text(eval_script_content)

    patch_file = log_path / "patch.diff"
    if not patch_file.exists():
        patch_file.write_text("")

    report_dir = spec._get_report_dir()

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
        report_dir,
        "test_output.txt",
        "compile_output.txt",
        "test_exit_code.txt",
    ]

    _ctx = (LocalInplace
            if ExecutionBackend(backend.upper()) == ExecutionBackend.LOCAL_INPLACE
            else Docker)
    try:
        with _ctx(
            spec=spec,
            logger=test_logger,
            timeout=timeout,
            num_cpus=num_cpus,
            log_dir=log_path,
            files_to_copy=files_to_copy,
            files_to_collect=files_to_collect,
        ) as ctx:
            output, timed_out, elapsed = ctx.exec_run_with_timeout(
                "/bin/bash /eval.sh"
            )
            test_logger.info(output)

            if timed_out:
                # F-A audit fix: flush test output BEFORE returning so the caller
                # (aider captures stdout) sees WHAT the tests produced before the
                # timeout kill. Without this the agent only saw the log.error line
                # and had no signal to refine against.
                try:
                    _to = log_path / "test_output.txt"
                    if _to.exists():
                        print(_to.read_text(encoding="utf-8", errors="replace"))
                    else:
                        print(output or "(no test output captured before timeout)")
                except OSError:
                    pass
                print(
                    f"\n[TIMEOUT: java test process killed after {elapsed:.1f}s "
                    f"(bump via KAIJU_AGENT_TEST_TIMEOUT_SEC or --timeout)]",
                    file=_sys.stderr,
                )
                test_logger.error(
                    f"Tests timed out for {repo_name} after {elapsed:.1f}s"
                )
                return {"__TIMEOUT__": "ERROR"}

        # Docker.__exit__ closes test_logger and cleans up the container.

        exit_code_file = log_path / "test_exit_code.txt"
        if exit_code_file.exists():
            exit_content = exit_code_file.read_text().strip()
            if "COMPILATION_FAILED" in exit_content:
                logger.error(f"Compilation failed for {repo_name}")
                # Surface the javac errors to STDOUT so the test-refine agent (aider
                # captures the command's stdout) can actually fix them. logger.info
                # above only goes to the log FILE. Without this the agent saw nothing.
                _tail = "\n".join((output or "").splitlines()[-80:])
                print(
                    "Java COMPILATION FAILED — the changes do not compile. "
                    "javac/build output (tail):\n" + (_tail or "(no output captured)")
                )
                return {"__COMPILATION__": "FAILED"}

        xml_dir = log_path / report_dir
        if xml_dir.exists():
            results = parse_surefire_reports(str(xml_dir))
            summary = summarize_results(results)
            logger.info(
                f"{repo_name}: {summary.get('passed', 0)} passed, "
                f"{summary.get('failed', 0)} failed, "
                f"{summary.get('error', 0)} errors, "
                f"{summary.get('skipped', 0)} skipped"
            )
            return {
                test_id: result.value
                for test_id, result in results.items()
            }

        logger.warning(f"No test reports found for {repo_name}")
        # No surefire reports — a test crashed or the build produced no test output.
        # Print the raw build/test output so the agent has real signal to refine on.
        _tail = "\n".join((output or "").splitlines()[-80:])
        print(
            "Java tests produced NO surefire reports — a test likely crashed or the "
            "build produced no test output. Build/test output (tail):\n"
            + (_tail or "(no output captured)")
        )
        return {}

    except Exception as e:
        error_msg = (
            f"Error running Java tests for {repo_name}: {e}\n"
            f"{traceback.format_exc()}"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e
