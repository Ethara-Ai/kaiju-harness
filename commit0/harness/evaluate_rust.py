"""Rust evaluation pipeline — mirrors ``evaluate.py`` for Rust repositories.

Uses ``run_rust_tests.main`` as the per-repo test runner and parses
cargo/nextest output for result aggregation.
"""

import logging
import os
import re
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

# Cargo test summary line: ``test result: ok. 5 passed; 0 failed; 1 ignored; 0 measured...``
# Robust to format drift via lookahead-style counts; only requires the three counters appear in order.
_TEST_SUMMARY_RE = re.compile(
    r"test\s+result:.*?(?P<passed>\d+)\s+passed.*?(?P<failed>\d+)\s+failed.*?(?P<ignored>\d+)\s+ignored",
    re.IGNORECASE,
)


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
        match = _TEST_SUMMARY_RE.search(line)
        if not match:
            continue
        found_summary = True
        try:
            num_passed += int(match.group("passed"))
            num_failed += int(match.group("failed"))
            num_ignored += int(match.group("ignored"))
        except (ValueError, IndexError) as exc:
            logger.warning(
                "Malformed test result line in %s: %r (%s)",
                test_output_file,
                line[:200],
                exc,
            )

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

    # Resolve repo_split to a set of dataset repo names to evaluate.
    #
    # Semantics:
    #   - 'all' + RUST_SPLIT populated  → union of curated splits, intersected with dataset
    #   - 'all' + RUST_SPLIT empty      → every repo in the dataset (default for ad-hoc
    #                                     local datasets where RUST_SPLIT isn't seeded)
    #   - <curated-split-name>          → that curated subset, intersected with dataset
    #   - <repo-name>                   → single repo, matched with hyphen/underscore
    #                                     normalisation ('foo-bar' equivalent to 'foo_bar')
    def _normalize(name: str) -> str:
        return name.replace("-", "_")

    dataset_repo_names = {ex["repo"].split("/")[-1] for ex in dataset_list}

    if repo_split == "all":
        if split_dict:
            curated = {r.split("/")[-1] for rs in split_dict.values() for r in rs}
            rust_repo_names = dataset_repo_names & curated
            if not rust_repo_names:
                # Curated splits don't overlap this dataset (common for custom
                # datasets registered with the local pipeline). Fall back to
                # evaluating every entry.
                rust_repo_names = dataset_repo_names
        else:
            # RUST_SPLIT is populated at runtime by dataset loaders; when empty
            # (the default for ad-hoc datasets) every dataset entry is in-scope.
            rust_repo_names = dataset_repo_names
    elif repo_split in split_dict:
        curated = {r.split("/")[-1] for r in split_dict[repo_split]}
        rust_repo_names = dataset_repo_names & curated
    else:
        # Treat repo_split as a single repo name; allow hyphen/underscore equivalence.
        target = _normalize(repo_split)
        rust_repo_names = {n for n in dataset_repo_names if _normalize(n) == target}

    triples = []
    log_dirs = []
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in rust_repo_names:
            continue

        # Hash the SAME string we pass to run_rust_tests as test_ids — otherwise
        # the writer (run_rust_tests) and the reader (this aggregator) compute
        # different log-dir paths and we silently report '0/0 passed' for runs
        # that actually wrote results. Previously evaluate_rust hashed
        # example['test']['test_dir'] while run_rust_tests hashed the test_ids
        # argument (always empty string), so 17/17 passing runs were reported as
        # 0/0 because the aggregator read from the wrong directory.
        test_ids = ""  # full suite; no per-test filter applied for Rust runs
        hashed_test_ids = get_hash_string(test_ids)
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
