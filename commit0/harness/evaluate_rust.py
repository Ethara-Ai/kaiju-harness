"""Rust evaluation pipeline — mirrors ``evaluate.py`` for Rust repositories.

Uses ``run_rust_tests.main`` as the per-repo test runner and parses
cargo/nextest output for result aggregation.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator, Union

from tqdm import tqdm

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_rust import (
    RUST_SPLIT,
    RUN_RUST_TESTS_LOG_DIR,
)
from commit0.harness.run_rust_tests import main as run_rust_tests
from commit0.harness.rust_test_parser import parse_nextest_report
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


def _aggregate_rust_results(log_dir: str, name: str, out: list) -> None:
    """Parse Rust test results from *log_dir* and append a summary dict to *out*.

    Looks for ``test_output.txt`` (cargo/nextest output) in the log directory.
    Attempts JSON-line nextest parsing first, then falls back to counting
    pass/fail lines from plain cargo test output.
    """
    test_output_file = os.path.join(log_dir, "test_output.txt")
    if not os.path.exists(test_output_file):
        logger.warning(
            "%s: missing test_output.txt — check %s", name, log_dir
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

    report = parse_nextest_report(test_output_file)
    tests = report.get("tests", [])
    summary = report.get("summary", {})

    if tests:
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
        return

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

    num_passed = 0
    num_failed = 0
    num_ignored = 0
    found_summary = False
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("test result:"):
            found_summary = True
            parts = line.split()
            for i, part in enumerate(parts):
                if part == "passed;":
                    try:
                        num_passed += int(parts[i - 1])
                    except (ValueError, IndexError):
                        pass
                elif part == "failed;":
                    try:
                        num_failed += int(parts[i - 1])
                    except (ValueError, IndexError):
                        pass
                elif part == "ignored;":
                    try:
                        num_ignored += int(parts[i - 1])
                    except (ValueError, IndexError):
                        pass

    num_tests = num_passed + num_failed + num_ignored
    passed_rate = num_passed / num_tests if num_tests > 0 else 0.0

    if not found_summary:
        logger.warning(
            "%s: no 'test result:' summary line found in %s", name, test_output_file
        )

    out.append(
        {
            "name": name,
            "sum": 0,
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
    """Evaluate Rust repositories by running tests and aggregating results."""
    split_dict = RUST_SPLIT
    log_base_dir = RUN_RUST_TESTS_LOG_DIR

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

    rust_repo_names = set()
    if repo_split == "all":
        for repos in split_dict.values():
            rust_repo_names.update(r.split("/")[-1] for r in repos)
    elif repo_split in split_dict:
        rust_repo_names = {r.split("/")[-1] for r in split_dict[repo_split]}

    repos = []
    if repo_split == "all" or repo_split in split_dict:
        repos = list(rust_repo_names)
    else:
        repos = [repo_split]

    triples = []
    log_dirs = []
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_split == "all":
            if repo_name not in rust_repo_names:
                continue
        elif repo_split in split_dict:
            if repo_name not in rust_repo_names:
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
            (example["repo"], "", repo_branch)
        )

    if not triples:
        logger.error(
            "No Rust repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.yaml repo_split matches Rust repo names in RUST_SPLIT.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d Rust repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating Rust repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {}
            for repo, test_ids, repo_branch in triples:
                future = executor.submit(
                    run_rust_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    repo_branch,
                    test_ids,
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
                    future.result()
                except SystemExit as e:
                    if e.code not in (0, 1):
                        logger.warning(
                            "Rust evaluation for %s exited with code %s",
                            repo_name,
                            e.code,
                        )
                except Exception as e:
                    logger.error(
                        "Rust evaluation failed for %s: %s", repo_name, e, exc_info=True
                    )

    out = []
    for log_path in tqdm(log_dirs):
        log_name = os.path.basename(os.path.dirname(os.path.dirname(log_path)))
        if not log_name:
            log_name = log_path.split("/")[2] if len(log_path.split("/")) > 2 else "unknown"
        _aggregate_rust_results(log_path, log_name, out)

    print("repo,runtime,num_passed/num_tests")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        print(f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']}")
    total_runtime = sum(x["sum"] for x in out)
    averaged_passed = sum(x["passed"] for x in out) / len(out) if out else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    logger.info(
        "Rust evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__ = []
