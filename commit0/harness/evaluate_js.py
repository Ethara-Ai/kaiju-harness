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

    # PATCH_APPLY_FAILED sentinel detection. eval.sh writes this string to
    # test output when `git apply patch.diff` fails; without this check the
    # patch-failure would score as a legitimate 0/N. Search head+tail of
    # test_stdout.txt + test_results.json so large intervening output can't
    # push the sentinel out of a fixed-size window.
    _patch_failed = False
    for _sentinel_file in (log_dir / "test_stdout.txt", log_dir / "test_results.json"):
        if _sentinel_file.exists():
            try:
                _sz = _sentinel_file.stat().st_size
                with _sentinel_file.open("rb") as _fh:
                    _head = _fh.read(8192).decode("utf-8", errors="replace")
                    if "PATCH_APPLY_FAILED" in _head:
                        _patch_failed = True
                        break
                    if _sz > 16384:
                        _fh.seek(max(0, _sz - 8192))
                        _tail = _fh.read(8192).decode("utf-8", errors="replace")
                        if "PATCH_APPLY_FAILED" in _tail:
                            _patch_failed = True
                            break
            except OSError:
                pass

    # Timeout detection: exit codes 124 (GNU timeout), 137 (SIGKILL), 143 (SIGTERM)
    # indicate the test suite was killed by the eval script's `timeout` wrapper.
    # Without this check a timeout-killed run scores as legitimate 0/N.
    _timed_out = test_code in (124, 137, 143)

    report_file = log_dir / "test_results.json"
    parsed: JsTestResult
    if not report_file.exists():
        parsed = JsTestResult(
            framework=framework, parse_error=f"missing {report_file.name}"
        )
    else:
        parsed = parse_js_test_output(report_file, framework)

    # Fallback: older jest/vitest/mocha ignore --outputFile and print the report to
    # STDOUT, leaving test_results.json empty. If the reporter file yielded nothing
    # usable, try the captured stdout before concluding infra/0.
    if parsed.raw_empty or (parsed.parse_error and parsed.num_total == 0):
        stdout_file = log_dir / "test_stdout.txt"
        if stdout_file.exists() and stdout_file.stat().st_size > 0:
            alt = parse_js_test_output(stdout_file, framework)
            if alt.num_total > 0:
                parsed = alt

    report_missing_or_empty = (
        (not report_file.exists())
        or report_file.stat().st_size == 0
        or parsed.raw_empty
    )
    # Patch-apply failure and test-suite timeout are ALWAYS infra failures
    # (never scored as legitimate 0%): the module never got a chance to run
    # its tests, so num_passed/num_total is meaningless. Flag them so the
    # aggregator excludes the module from denominators rather than counting
    # it toward the failure column.
    infra_failed = (test_code is None and report_missing_or_empty) or _patch_failed or _timed_out

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
        observed_total = parsed.num_total
        # The frozen (canonical) inventory is the AUTHORITATIVE denominator when
        # present: a run that collected fewer tests than the repo actually has (a
        # describe block threw at load, an ESM import silently failed, the suite
        # bailed after N tests) must NOT be scored over only the tests that
        # registered — that inflates the pass rate toward a false 100%. Score over
        # the larger of observed vs canonical.
        num_total = max(observed_total, canonical_count)

        # "0 collected while the frozen inventory says there ARE tests" is an
        # infra/compile failure masquerading as a clean 0%: `node --check` gates
        # syntax but NOT ESM import-resolution / load-time throws. A truncated or
        # unparseable report is likewise untrustworthy (its counts are
        # regex-fabricated). In either case refuse to emit a confident verdict and
        # flag it as infra so it is excluded, not scored as a legitimate 0%.
        # J2 fix: previously zero_but_expected required canonical_count > 0.
        # If test-ID capture ALSO failed (canonical_count == 0) and the framework
        # then silently collected 0 tests, the both-zero case was scored as a
        # legitimate 0% failure instead of being flagged as infra. Widen to also
        # trigger when the framework can enumerate tests (jest/mocha/vitest all
        # produce a non-empty inventory for a healthy repo) but observed_total
        # is 0 — that's an infra signal regardless of canonical availability.
        zero_but_expected = observed_total == 0 and canonical_count > 0
        zero_both = observed_total == 0 and canonical_count == 0
        untrustworthy = parsed.truncated or (
            bool(parsed.parse_error) and observed_total == 0
        )
        if not compile_failed and (zero_but_expected or untrustworthy or zero_both):
            infra_failed = True
            compile_failed = None
            tests_failed = None
            passed_rate = 0.0
        else:
            tests_failed = parsed.num_failed > 0 or (
                test_code is not None and test_code != 0 and not compile_failed
            )
            passed_rate = (
                parsed.num_passed / num_total if num_total > 0 else 0.0
            )

    return {
        "framework": framework,
        "install_exit_code": install_code,
        "syntax_exit_code": syntax_code,
        "test_exit_code": test_code,
        "infra_failed": infra_failed,
        "patch_apply_failed": _patch_failed,
        "timed_out": _timed_out,
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
        # The frozen test-id inventory is best-effort in prepare (capture "never
        # raises"), so a repo whose capture failed has no <repo>.bz2 and
        # get_ts_test_ids RAISES FileNotFoundError. Without this guard a SINGLE such
        # repo aborts the whole parse loop -> zero results written for the entire
        # batch. Degrade to an empty inventory (summarizer tolerates test_ids=[]).
        try:
            test_ids_raw = get_ts_test_ids(display_name, verbose=0)
            test_ids = [xx for x in test_ids_raw for xx in x if xx]
        except FileNotFoundError:
            logger.warning(
                "No frozen test-id inventory for %s; scoring without a canonical "
                "denominator (observed counts only).", display_name,
            )
            test_ids = []
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
