"""C++ evaluation pipeline -- mirrors ``evaluate.py`` for C++ repositories.

Uses ``run_cpp_tests.main`` as the per-repo test runner and parses
C++ test framework output for result aggregation.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator, Union

from tqdm import tqdm

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_cpp import (
    CPP_SPLIT,
    RUN_CPP_TESTS_LOG_DIR,
)
from commit0.harness.run_cpp_tests import main as run_cpp_tests
from commit0.harness.cpp_test_parser import parse_cpp_test_output
from commit0.harness.utils import (
    get_hash_string,
    get_active_branch,
    load_dataset_from_config,
    relativize,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_CPP_FAILURE_EXIT_CODES = {1, 42, 200, 201}


def evaluate_single_repo(
    instance: dict,
    patch_path: str,
    timeout: int = 7200,
    backend: str = "local",
    num_cpus: int = 1,
    rebuild_image: bool = False,
) -> dict:
    """Evaluate a single C++ repo given an instance dict and patch file path.

    Returns a dict mapping test_name -> TestStatus.
    """
    from commit0.harness.constants import TestStatus
    from commit0.harness.spec_cpp import make_cpp_spec
    from commit0.harness.execution_context import Docker, Modal, E2B, ExecutionBackend
    from pathlib import Path
    import tempfile

    absolute = backend != "e2b"
    spec = make_cpp_spec(instance, absolute)

    repo_name = instance["repo"].split("/")[-1]
    test_info = instance.get("test", {})
    test_ids = test_info.get("test_dir", "") if isinstance(test_info, dict) else str(test_info)

    with tempfile.TemporaryDirectory(prefix=f"cpp_eval_{repo_name}_") as _tmp_dir:
        log_dir = Path(_tmp_dir)
        eval_logger = logging.getLogger(f"eval.{repo_name}")

        patch_content = Path(patch_path).read_text()
        eval_script = spec.eval_script.format(test_ids=test_ids)

        patch_file = log_dir / "patch.diff"
        patch_file.write_text(patch_content, encoding="utf-8", errors="ignore")
        eval_file = log_dir / "eval.sh"
        eval_file.write_text(eval_script)

        from commit0.harness.constants import Files
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
        files_to_collect = ["test_exit_code.txt", "test_output.txt"]

        backend_upper = backend.upper()
        if ExecutionBackend(backend_upper) == ExecutionBackend.MODAL:
            execution_context = Modal
        elif ExecutionBackend(backend_upper) == ExecutionBackend.E2B:
            execution_context = E2B
        else:
            execution_context = Docker

        eval_command = (
            "/bin/bash /eval.sh"
            if ExecutionBackend(backend_upper) != ExecutionBackend.E2B
            else "/bin/bash eval.sh"
        )

        try:
            with execution_context(
                spec,
                eval_logger,
                timeout,
                num_cpus,
                log_dir,
                files_to_copy,
                files_to_collect,
                rebuild_image,
            ) as context:
                output, timed_out, total_runtime = context.exec_run_with_timeout(eval_command)
                if timed_out:
                    logger.warning("Evaluation timed out for %s after %ds", repo_name, timeout)
        except Exception as e:
            logger.error("Evaluation failed for %s: %s", repo_name, e, exc_info=True)
            return {"__error__": str(e)}

        test_output_file = log_dir / "test_output.txt"
        exit_code_file = log_dir / "test_exit_code.txt"

        exit_code = -1
        if exit_code_file.exists():
            try:
                exit_code = int(exit_code_file.read_text().strip())
            except (ValueError, OSError):
                pass

        if not test_output_file.exists():
            return {}

        content = test_output_file.read_text()
        report = parse_cpp_test_output(content, exit_code)
        tests = report.get("tests", [])

        results = {}
        for t in tests:
            name = t.get("name", "unknown")
            status = t.get("outcome", "FAILED")
            if status.upper() == "PASSED":
                results[name] = TestStatus.PASSED
            elif status.upper() == "SKIPPED":
                results[name] = TestStatus.SKIPPED
            else:
                results[name] = TestStatus.FAILED

        return results


def _aggregate_cpp_results(log_dir: str, name: str, out: list) -> None:
    """Parse C++ test results from *log_dir* and append a summary dict to *out*.

    Looks for ``test_output.txt`` and ``test_exit_code.txt`` in the log
    directory.  Auto-detects the test framework (GTest, Catch2, doctest,
    Boost.Test, CTest) and delegates to the appropriate parser.
    """
    test_output_file = os.path.join(log_dir, "test_output.txt")
    exit_code_file = os.path.join(log_dir, "test_exit_code.txt")

    if not os.path.exists(test_output_file):
        logger.warning(
            "%s: missing test_output.txt -- check %s", name, log_dir
        )
        out.append(
            {
                "name": name,
                "sum": 0,
                "passed": 0,
                "num_passed": 0,
                "num_tests": 0,
            }
        )
        return

    exit_code = -1
    if os.path.exists(exit_code_file):
        try:
            exit_code = int(Path(exit_code_file).read_text().strip())
        except (ValueError, OSError):
            pass

    try:
        with open(test_output_file, "r") as f:
            content = f.read()
    except OSError as exc:
        logger.warning("Failed to read %s: %s", test_output_file, exc)
        out.append(
            {
                "name": name,
                "sum": 0,
                "passed": 0,
                "num_passed": 0,
                "num_tests": 0,
            }
        )
        return

    report = parse_cpp_test_output(content, exit_code)
    tests = report.get("tests", [])
    summary = report.get("summary", {})

    num_passed = summary.get("passed", 0)
    num_tests = summary.get("total", 0)
    total_runtime = sum(t.get("duration", 0) for t in tests)
    passed_rate = num_passed / num_tests if num_tests > 0 else 0.0

    out.append(
        {
            "name": name,
            "sum": total_runtime,
            "passed": passed_rate,
            "num_passed": num_passed,
            "num_tests": num_tests,
        }
    )


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
    branch: Union[str, None],
    backend: str,
    timeout: int,
    num_cpus: int,
    num_workers: int,
    rebuild_image: bool,
) -> None:
    """Evaluate C++ repositories by running tests and aggregating results."""
    split_dict = CPP_SPLIT
    log_base_dir = RUN_CPP_TESTS_LOG_DIR

    dataset: Iterator[RepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore
    dataset_list = list(dataset) if not isinstance(dataset, list) else dataset
    logger.info(
        "Loaded %d entries from dataset=%s, split=%s, repo_split=%s",
        len(dataset_list),
        relativize(dataset_name),
        dataset_split,
        repo_split,
    )

    cpp_repo_names = set()
    if repo_split == "all":
        for repos in split_dict.values():
            cpp_repo_names.update(r.split("/")[-1] for r in repos)
    elif repo_split in split_dict:
        cpp_repo_names = {r.split("/")[-1] for r in split_dict[repo_split]}

    accept_all_when_split_empty = (repo_split == "all" and not cpp_repo_names)

    repos = []
    if repo_split == "all" or repo_split in split_dict:
        repos = list(cpp_repo_names)
    else:
        repos = [repo_split]

    triples = []
    log_dirs = []
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_split == "all":
            if not accept_all_when_split_empty and repo_name not in cpp_repo_names:
                continue
        elif repo_split in split_dict:
            if repo_name not in cpp_repo_names:
                continue
        else:
            if repo_name.replace("-", "_") != repo_split.replace("-", "_"):
                continue

        test_dir = example["test"]["test_dir"]
        hashed_test_ids = get_hash_string(test_dir)
        repo_branch = branch
        if repo_branch is None:
            git_path = os.path.join(base_dir, repo_name)
            repo_branch = get_active_branch(git_path)
            logger.debug(
                "Branch not specified for %s, resolved to: %s", repo_name, repo_branch
            )
        log_dir = (
            log_base_dir
            / repo_name
            / repo_branch
            / hashed_test_ids
        )
        log_dirs.append(str(log_dir))
        triples.append(
            (example["repo"], test_dir, repo_branch)
        )

    if not triples:
        logger.error(
            "No C++ repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.yaml repo_split matches C++ repo names in CPP_SPLIT.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d C++ repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating C++ repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {}
            for repo, test_dir, repo_branch in triples:
                future = executor.submit(
                    run_cpp_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    repo_branch,
                    test_dir,
                    backend,
                    timeout,
                    num_cpus,
                    rebuild_image,
                    0,
                )
                futures[future] = repo
            for future in as_completed(futures):
                pbar.update(1)
                repo_name = futures[future]
                try:
                    exit_code = future.result()
                    if exit_code not in (0,) and exit_code not in _CPP_FAILURE_EXIT_CODES:
                        logger.warning(
                            "C++ evaluation for %s exited with code %s",
                            repo_name,
                            exit_code,
                        )
                except Exception as e:
                    logger.error(
                        "C++ evaluation failed for %s: %s", repo_name, e, exc_info=True
                    )

    out = []
    for log_path in tqdm(log_dirs):
        log_name = os.path.basename(os.path.dirname(os.path.dirname(log_path)))
        if not log_name:
            log_name = log_path.split("/")[2] if len(log_path.split("/")) > 2 else "unknown"
        _aggregate_cpp_results(log_path, log_name, out)

    print("repo,runtime,num_passed/num_tests")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        print(f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']}")
    total_runtime = sum(x["sum"] for x in out)
    averaged_passed = sum(x["passed"] for x in out) / len(out) if out else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    logger.info(
        "C++ evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__ = ["evaluate_single_repo", "main"]
