"""Batch evaluation for TypeScript repos.

Analogue of evaluate.py — runs tests across multiple TS repos, parses
Jest/Vitest JSON reports, and prints pass-rate summaries.
"""

import json
import logging
import os
from collections import Counter

from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from typing import Iterator, Union

from commit0.harness.run_ts_tests import main as run_ts_tests
from commit0.harness.get_ts_test_ids import main as get_ts_test_ids
from commit0.harness.constants import RepoInstance
from commit0.harness.constants_ts import TS_SPLIT, RUN_TS_TEST_LOG_DIR
from commit0.harness.utils import (
    get_hash_string,
    get_active_branch,
    load_dataset_from_config,
    relativize,
)
from commit0.harness.split_utils import resolve_split

logger = logging.getLogger(__name__)

# Maps Jest/Vitest assertion status values to normalized status strings.
STATUS_MAP: dict[str, str] = {
    "passed": "passed",
    "failed": "failed",
    "pending": "skipped",
    "skipped": "skipped",
    "todo": "skipped",
    "disabled": "skipped",
    "focused": "passed",
}


def parse_jest_vitest_report(
    report: dict,
    test_ids: list[str],
) -> tuple[Counter[str], float]:
    """Parse a Jest/Vitest JSON report and return (status_counter, total_duration_s).

    ``report`` is the parsed ``report.json`` produced by Jest's ``--json`` or
    Vitest's ``--reporter=json`` flag.

    * ``assertionResults`` (single 's') is the key used by both Jest and Vitest.
    * Durations are in **milliseconds** — they are converted to seconds here.
    * Any ``test_ids`` not present in the report are counted as ``"failed"``.
    """
    canonical: set[str] = set()
    for tid in test_ids:
        if not tid:
            continue
        canonical.add(tid)
        if " > " in tid:
            canonical.add(tid.split(" > ", 1)[1])

    matched: dict[str, str] = {}
    durations_ms: list[float] = []

    for test_result in report.get("testResults", []):
        for assertion in test_result.get("assertionResults", []):
            full_name = assertion.get("fullName", "")
            if not full_name or full_name not in canonical:
                continue
            raw_status: str = assertion.get("status", "failed")
            mapped = STATUS_MAP.get(raw_status, "failed")
            if full_name not in matched or (
                matched[full_name] == "failed" and mapped == "passed"
            ):
                matched[full_name] = mapped
            durations_ms.append(float(assertion.get("duration", 0)))

    status: list[str] = []
    for tid in test_ids:
        if not tid:
            continue
        bare = tid.split(" > ", 1)[1] if " > " in tid else tid
        if bare in matched:
            status.append(matched[bare])
        elif tid in matched:
            status.append(matched[tid])
        else:
            status.append("failed")

    total_seconds = sum(durations_ms) / 1000.0
    return Counter(status), total_seconds


def detect_jest_vitest_suite_crash(report: dict) -> bool:
    test_results = report.get("testResults", [])
    if not test_results:
        return True
    for tr in test_results:
        assertions = tr.get("assertionResults", [])
        failure_message = tr.get("failureMessage")
        if not assertions and failure_message:
            return True
    return False


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

    allowed_repos = set(resolve_split(repo_split, dataset_list, curated=TS_SPLIT))

    triples: list[tuple[str, str, str]] = []
    log_dirs: list[str] = []

    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in allowed_repos:
            continue

        test_info = example["test"]
        test_target = (
            test_info["test_dir"] if isinstance(test_info, dict) else str(test_info)
        )
        hashed_test_ids = get_hash_string(test_target)
        repo_branch = branch
        if repo_branch is None:
            git_path = os.path.join(base_dir, example["instance_id"])
            repo_branch = get_active_branch(git_path)
            logger.debug(
                "Branch not specified for %s, resolved to: %s", repo_name, repo_branch
            )
        log_dir = (
            RUN_TS_TEST_LOG_DIR
            / example["instance_id"].split("/")[-1]
            / repo_branch
            / hashed_test_ids
        )
        log_dirs.append(str(log_dir))
        triples.append((example["instance_id"], test_target, repo_branch))

    if not triples:
        logger.error(
            "No repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.ts.yaml repo_split matches repo names in the dataset.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating TS repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(
                    run_ts_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    eval_branch,
                    test_target,
                    backend,
                    timeout,
                    num_cpus,
                    rebuild_image=rebuild_image,
                    verbose=0,
                ): repo
                for repo, test_target, eval_branch in triples
            }
            for future in as_completed(futures):
                pbar.update(1)
                future_repo_name = futures[future]
                try:
                    future.result()
                except SystemExit as e:
                    if e.code not in (0, 1):
                        logger.warning(
                            "Evaluation for %s exited with code %s (possible OOM or infra failure)",
                            future_repo_name,
                            e.code,
                        )
                except Exception as e:
                    logger.error(
                        "Evaluation failed for %s (infrastructure error, "
                        "results may show 0%% pass rate): %s",
                        future_repo_name,
                        e,
                        exc_info=True,
                    )

    out: list[dict[str, object]] = []
    for name in tqdm(log_dirs):
        tap_file = os.path.join(name, "report.tap")
        json_file = os.path.join(name, "report.json")
        display_name = name.split("/")[2] if len(name.split("/")) > 2 else name
        test_ids_raw = get_ts_test_ids(display_name, verbose=0)
        test_ids = [xx for x in test_ids_raw for xx in x if xx]

        report_file = tap_file if os.path.exists(tap_file) else json_file

        if not os.path.exists(report_file):
            log_parent = os.path.dirname(report_file)
            test_output_file = os.path.join(log_parent, "test_output.txt")
            # Map to the shared status vocabulary so the pipeline never scores an
            # infra/patch failure as a genuine 0%. eval.sh writes the sentinel
            # "PATCH_APPLY_FAILED" (and nothing else) when `git apply` fails.
            if os.path.exists(test_output_file):
                try:
                    with open(test_output_file, "r") as _f:
                        _to = _f.read()
                except OSError:
                    _to = ""
                if _to.strip() == "PATCH_APPLY_FAILED":
                    reason = "patch_apply_failed"
                    status = "PATCH_APPLY_FAILED"
                else:
                    # produced build/test output but no parseable report -> the
                    # runner crashed or tests never compiled/collected.
                    reason = "runner_crash_or_collection_error"
                    status = "COMPILE_FAILED"
            else:
                reason = "container_or_infra_failure"
                status = "OUTPUT_MISSING"
            logger.warning(
                f"{display_name}: missing report.tap/report.json ({reason}) — check {log_parent}"
            )
            out.append(
                {
                    "name": display_name,
                    "sum": 0,
                    "passed": 0,
                    "num_passed": 0,
                    "num_tests": len(test_ids),
                    "status": status,
                }
            )
            continue

        try:
            if report_file.endswith(".tap"):
                from commit0.harness.node_test_tap import tap_to_jest_report_shape
                with open(report_file, "r") as file:
                    report = tap_to_jest_report_shape(file.read())
            else:
                with open(report_file, "r") as file:
                    report = json.load(file)
        except json.JSONDecodeError:
            logger.warning(
                "Corrupt report file for %s (truncated or invalid) "
                "— treating as 0%% pass rate",
                display_name,
            )
            out.append(
                {
                    "name": display_name,
                    "sum": 0.0,
                    "passed": 0.0,
                    "num_passed": 0,
                    "num_tests": len(test_ids),
                    "status": "OUTPUT_MISSING",
                }
            )
            continue

        status_counter, total_duration = parse_jest_vitest_report(report, test_ids)
        suite_crashed = detect_jest_vitest_suite_crash(report)

        num_total = len([t for t in test_ids if t])
        num_passed = status_counter.get("passed", 0)
        passed_rate = num_passed / num_total if num_total > 0 else 0.0

        if suite_crashed:
            logger.warning(
                "%s: test suite CRASHED at import/setup — 0/%d indicates infrastructure "
                "failure, not real test failures. Check the failureMessage in report.json.",
                display_name,
                num_total,
            )

        out.append(
            {
                "name": display_name,
                "sum": total_duration,
                "passed": passed_rate,
                "num_passed": num_passed,
                "num_tests": num_total,
                "suite_crashed": suite_crashed,
                # A suite that crashes at import/setup produces 0/N that is an infra
                # failure, not real test failures -> distinct status so it isn't
                # scored as a genuine 0% (mirrors the other languages).
                "status": "SUITE_CRASHED" if suite_crashed else "TESTS_RAN",
            }
        )

    # 4th column = per-repo outcome so the shell pipeline can tell a genuine 0%
    # model score from a build/patch/infra failure (mirrors go/rust/c/cpp).
    print("repo,runtime,num_passed/num_tests,status")
    out = sorted(out, key=lambda x: float(str(x["sum"])), reverse=True)
    for x in out:
        print(
            f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']},"
            f"{x.get('status', 'TESTS_RAN')}"
        )
    total_runtime = sum(float(str(x["sum"])) for x in out)
    averaged_passed = (
        sum(float(str(x["passed"])) for x in out) / len(out) if out else 0.0
    )
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    logger.info(
        "Evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )
