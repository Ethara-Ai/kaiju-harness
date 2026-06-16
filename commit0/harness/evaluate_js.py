"""Batch evaluation for JavaScript repos.

Runs tests across multiple JS repos, parses Jest / Vitest / Mocha JSON or
Node ``--test`` TAP via :mod:`commit0.harness.js_test_parser`, and writes
per-repo summaries to a results JSON file.

Captures ``compile_failed`` (install or ``node --check`` failure) separately
from ``tests_failed`` so a repo that never reached the test runner is not
reported as a normal 0%% pass rate — JS has no static compile step, so this
distinction is the closest available signal.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator

from tqdm import tqdm

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_js import JS_SPLIT, RUN_JS_TEST_LOG_DIR
from commit0.harness.get_ts_test_ids import main as get_ts_test_ids
from commit0.harness.js_test_parser import (
    JsTestResult,
    JsTestStatus,
    parse_js_test_output,
)
from commit0.harness.run_js_tests import main as run_js_tests
from commit0.harness.split_utils import resolve_split
from commit0.harness.utils import (
    get_active_branch,
    get_hash_string,
    load_dataset_from_config,
    relativize,
)


logger = logging.getLogger(__name__)


_RESULTS_FILENAME = "pipeline_js_results.json"


def _detect_framework(example: RepoInstance | dict) -> str:
    setup = {}
    if isinstance(example, dict):
        setup = example.get("setup") or {}
    elif isinstance(example, RepoInstance):
        setup = example.setup or {}
    if isinstance(setup, dict) and setup.get("test_framework"):
        return str(setup["test_framework"])
    if isinstance(example, dict):
        framework = example.get("test_framework")
        if framework:
            return str(framework)
    return "jest"


def _read_exit_code(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _summarize_log_dir(
    log_dir: Path,
    framework: str,
    test_ids: list[str] | None = None,
) -> dict[str, object]:
    install_code = _read_exit_code(log_dir / "install_exit_code.txt")
    syntax_code = _read_exit_code(log_dir / "syntax_exit_code.txt")
    test_code = _read_exit_code(log_dir / "test_exit_code.txt")

    report_file = log_dir / "test_results.json"
    parsed: JsTestResult
    if not report_file.exists():
        parsed = JsTestResult(
            framework=framework, parse_error=f"missing {report_file.name}"
        )
    else:
        parsed = parse_js_test_output(report_file, framework)

    report_missing_or_empty = (
        (not report_file.exists())
        or report_file.stat().st_size == 0
        or parsed.raw_empty
    )
    infra_failed = test_code is None and report_missing_or_empty

    canonical_count = len(test_ids) if test_ids else 0

    compile_failed: bool | None
    tests_failed: bool | None
    if infra_failed:
        compile_failed = None
        tests_failed = None
        num_total = canonical_count
        passed_rate = 0.0
    else:
        compile_failed = (install_code is not None and install_code != 0) or (
            syntax_code is not None and syntax_code != 0
        )
        tests_failed = parsed.num_failed > 0 or (
            test_code is not None and test_code != 0 and not compile_failed
        )
        num_total = parsed.num_total if parsed.num_total > 0 else canonical_count
        passed_rate = (
            parsed.num_passed / num_total if num_total > 0 else 0.0
        )

    return {
        "framework": framework,
        "install_exit_code": install_code,
        "syntax_exit_code": syntax_code,
        "test_exit_code": test_code,
        "infra_failed": infra_failed,
        "compile_failed": compile_failed,
        "tests_failed": tests_failed,
        "num_passed": parsed.num_passed,
        "num_failed": parsed.num_failed,
        "num_skipped": parsed.num_skipped,
        "num_total": num_total,
        "duration_seconds": parsed.duration_seconds,
        "passed_rate": passed_rate,
        "parse_error": parsed.parse_error,
        "truncated": parsed.truncated,
        "raw_empty": parsed.raw_empty,
        "failed_tests": [
            name
            for name, status in parsed.statuses.items()
            if status in (JsTestStatus.FAILED, JsTestStatus.ERROR)
        ],
    }


def _resolve_branch(branch: str | None, base_dir: str, instance_id: str) -> str:
    if branch is not None and branch != "":
        return branch
    git_path = os.path.join(base_dir, instance_id.split("/")[-1])
    return get_active_branch(git_path)


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
    branch: str | None,
    backend: str,
    timeout: int,
    num_cpus: int,
    num_workers: int,
    rebuild_image: bool,
) -> None:
    dataset: Iterator[RepoInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )
    dataset_list = list(dataset) if not isinstance(dataset, list) else dataset
    logger.info(
        "Loaded %d entries from dataset=%s, split=%s, repo_split=%s",
        len(dataset_list),
        relativize(dataset_name),
        dataset_split,
        repo_split,
    )

    allowed_repos = set(resolve_split(repo_split, dataset_list, curated=JS_SPLIT))

    triples: list[tuple[str, str, str, str]] = []
    log_dirs: list[tuple[str, Path, str]] = []

    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in allowed_repos:
            continue

        framework = _detect_framework(example)

        test_info = example["test"]
        test_target = (
            test_info["test_dir"] if isinstance(test_info, dict) else str(test_info)
        )
        hashed_test_ids = get_hash_string(test_target)
        repo_branch = _resolve_branch(branch, base_dir, example["instance_id"])

        log_dir = (
            RUN_JS_TEST_LOG_DIR
            / example["instance_id"].split("/")[-1]
            / repo_branch
            / hashed_test_ids
        ).resolve()
        log_dirs.append((repo_name, log_dir, framework))
        triples.append((example["instance_id"], test_target, repo_branch, framework))

    if not triples:
        logger.error(
            "No repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.js.yaml repo_split matches repo names in the dataset.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating JS repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(
                    run_js_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    eval_branch,
                    test_target,
                    backend,
                    timeout,
                    num_cpus,
                    rebuild_image,
                    0,
                ): repo
                for repo, test_target, eval_branch, _framework in triples
            }
            compile_failed_by_exit: set[str] = set()
            infra_failed_by_exit: set[str] = set()
            for future in as_completed(futures):
                pbar.update(1)
                future_repo_name = futures[future]
                try:
                    future.result()
                except SystemExit as e:
                    if e.code == 2:
                        compile_failed_by_exit.add(future_repo_name)
                    elif e.code == 3:
                        infra_failed_by_exit.add(future_repo_name)
                        logger.warning(
                            "Evaluation for %s exited with code 3 "
                            "(no test_exit_code.txt — infra failure, NOT a "
                            "test failure)",
                            future_repo_name,
                        )
                    elif e.code not in (0, 1):
                        logger.warning(
                            "Evaluation for %s exited with code %s "
                            "(possible OOM or infra failure)",
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
    for display_name, log_dir, framework in tqdm(log_dirs, desc="Parsing JS results"):
        test_ids_raw = get_ts_test_ids(display_name, verbose=0)
        test_ids = [xx for x in test_ids_raw for xx in x if xx]
        summary = _summarize_log_dir(log_dir, framework, test_ids=test_ids)
        summary["name"] = display_name
        summary["log_dir"] = str(log_dir)
        if display_name in compile_failed_by_exit:
            summary["compile_failed"] = True
        if display_name in infra_failed_by_exit:
            summary["infra_failed"] = True
            summary["compile_failed"] = None
        out.append(summary)

    print(
        "repo,framework,runtime,num_passed/num_tests,"
        "compile_failed,tests_failed,status"
    )
    out_sorted = sorted(
        out, key=lambda x: float(x.get("duration_seconds", 0.0) or 0.0), reverse=True
    )
    for x in out_sorted:
        status = "INFRA_FAILED" if x.get("infra_failed") else "OK"
        print(
            f"{x['name']},{x['framework']},{x['duration_seconds']},"
            f"{x['num_passed']}/{x['num_total']},"
            f"{x['compile_failed']},{x['tests_failed']},{status}"
        )

    total_runtime = sum(
        float(x.get("duration_seconds", 0.0) or 0.0) for x in out_sorted
    )
    averaged_passed = (
        sum(float(x.get("passed_rate", 0.0) or 0.0) for x in out_sorted)
        / len(out_sorted)
        if out_sorted
        else 0.0
    )
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")

    results_path = (RUN_JS_TEST_LOG_DIR / _RESULTS_FILENAME).resolve()
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "dataset_name": dataset_name,
                "dataset_split": dataset_split,
                "repo_split": repo_split,
                "total_runtime_seconds": total_runtime,
                "average_pass_rate": averaged_passed,
                "repos": out_sorted,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Wrote JS evaluation summary: %s "
        "(%d repos, avg pass rate %.2f%%, total runtime %.1fs)",
        relativize(results_path),
        len(out_sorted),
        averaged_passed * 100,
        total_runtime,
    )
