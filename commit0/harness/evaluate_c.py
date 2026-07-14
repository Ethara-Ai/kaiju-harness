from pathlib import Path
"""Evaluate C repos — parallel pipeline counterpart to evaluate.py.

Uses C_SPLIT, run_c_tests, and CTest JUnit XML parsing. Does NOT modify the
original evaluate.py. Per-instance report adds a ``compile_errors`` int
metric, which surfaces translation-unit failures as a first-class signal.
"""

import logging
import os
from typing import Iterator, Union

import docker
import docker.errors
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from commit0.harness.constants_c import (
    CRepoInstance,
    C_SPLIT,
    RUN_C_TEST_LOG_DIR,
)
from commit0.harness.c_test_parser import (
    parse_ctest_junit_with_summary,
    parse_ctest_stdout,
    summarize_ctest_results,
    failed_test_names,
)
from commit0.harness.get_c_test_ids import main as get_c_tests
from commit0.harness.run_c_tests import main as run_c_tests
from commit0.harness.spec_c import make_c_spec
from commit0.harness.utils import (
    get_hash_string,
    get_active_branch,
    load_dataset_from_config,
    relativize,
)
from commit0.harness.split_utils import resolve_split

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Failure-attribution constants — mirror evaluate_go.py / evaluate_rust.py.
# Surfaced via the `status` field on each `out` entry so downstream summarisers
# can distinguish a REAL test outcome from a pipeline failure the harness must
# NOT silently report as a clean 0/N model score.
from commit0.harness._eval_common import detect_patch_apply_failed, detect_timeout

OUTCOME_TESTS_RAN = "TESTS_RAN"                    # CTest reached the test phase
OUTCOME_COMPILE_FAILED = "COMPILE_FAILED"          # build error; tests never ran
OUTCOME_OUTPUT_MISSING = "OUTPUT_MISSING"          # test_report.xml absent / infra
OUTCOME_PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"  # eval.sh couldn't apply patch.diff
OUTCOME_TEST_SUITE_TIMEOUT = "TEST_SUITE_TIMEOUT"  # killed by `timeout` (or watchdog)

# Statuses that are NOT a measured model score — excluded from the average and
# reported at 0.0 (never trusted), matching evaluate_go.py.
_EXCLUDED_STATUSES = {
    OUTCOME_COMPILE_FAILED,
    OUTCOME_OUTPUT_MISSING,
}


def _preflight_check_images(specs: list, backend: str) -> list[str]:
    if backend.upper() != "LOCAL":
        return []
    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        logger.error("Pre-flight: cannot connect to Docker daemon: %s", e)
        return ["<docker-daemon-unreachable>"]

    missing: list[str] = []
    checked: set[str] = set()
    for spec in specs:
        for image_key in (spec.base_image_key, spec.repo_image_key):
            if image_key in checked:
                continue
            checked.add(image_key)
            try:
                client.images.get(image_key)
            except docker.errors.ImageNotFound:
                missing.append(image_key)
            except docker.errors.APIError as e:
                logger.warning("Pre-flight: API error checking %s: %s", image_key, e)
    return missing


def _read_compile_errors_count(log_dir_path: str) -> int:
    """Number of error-like lines in compile_errors.txt (0 if file empty / missing)."""
    path = os.path.join(log_dir_path, "compile_errors.txt")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0
    if not text.strip():
        return 0
    count = 0
    for line in text.splitlines():
        lower = line.lower()
        if (
            " error:" in lower
            or lower.startswith("error:")
            or "compile_failed" in lower
            or "patch_apply_failed" in lower
            or "undefined reference" in lower
            or "ld returned" in lower
        ):
            count += 1
    return count


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
    """Evaluate C repos using CTest JUnit XML output."""
    dataset: Iterator[CRepoInstance] = load_dataset_from_config(
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

    triples: list[tuple[str, str, str]] = []
    log_dirs: list[str] = []
    specs: list = []

    allowed_repos = set(resolve_split(repo_split, dataset_list, curated=C_SPLIT))
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in allowed_repos:
            continue

        test_info = example["test"]
        test_dir = test_info.get("test_dir", test_info.get("test_cmd", "."))
        hashed_test_ids = get_hash_string(test_dir)
        repo_branch = branch
        if repo_branch is None:
            git_path = os.path.join(base_dir, repo_name)
            repo_branch = get_active_branch(git_path)
            logger.debug(
                "Branch not specified for %s, resolved to: %s",
                repo_name,
                repo_branch,
            )

        log_dir = RUN_C_TEST_LOG_DIR / repo_name / repo_branch / hashed_test_ids
        log_dirs.append(str(log_dir))
        triples.append((os.path.join(base_dir, repo_name), test_dir, repo_branch))
        specs.append(make_c_spec(example, absolute=True))

    if not triples:
        logger.error(
            "No repos matched repo_split=%r in dataset with %d entries.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d C repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    # local_inplace eval runs in a git worktree (no docker image needed),
    # so the image preflight is skipped — this is what lets the pipeline run
    # entirely inside a container.
    if not rebuild_image and str(backend).lower() != "local_inplace":
        missing_images = _preflight_check_images(specs, backend)
        if missing_images:
            logger.error(
                "Pre-flight failed: %d Docker image(s) not found: %s. "
                "Run C build first.",
                len(missing_images),
                missing_images,
            )
            raise RuntimeError(
                f"Missing Docker images: {missing_images}. Run C build first."
            )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating C repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(
                    run_c_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    branch,
                    test_dir,
                    backend,
                    timeout,
                    num_cpus,
                    rebuild_image=rebuild_image,
                    verbose=0,
                ): repo
                for repo, test_dir, branch in triples
            }
            for future in as_completed(futures):
                pbar.update(1)
                repo_name = futures[future]
                try:
                    exit_code = future.result()
                    if exit_code not in (0, 1):
                        logger.warning(
                            "Evaluation for %s exited with code %s",
                            repo_name,
                            exit_code,
                        )
                except Exception as e:
                    logger.error(
                        "Evaluation failed for %s: %s", repo_name, e, exc_info=True
                    )

    out = []
    for name in tqdm(log_dirs):
        xml_path = os.path.join(name, "test_report.xml")
        repo_label = os.path.basename(os.path.dirname(os.path.dirname(name)))

        test_ids = get_c_tests(repo_label, verbose=0)
        test_ids_flat = [tid for group in test_ids for tid in group if tid]

        compile_errors = _read_compile_errors_count(name)

        # Get per-test results from the junit report if present, else FALL BACK to
        # ctest's console output (test_output.txt). ctest always prints per-test
        # Passed/Failed lines even when --output-junit did NOT write a file (wrong
        # output path under a worktree eval, an older ctest, a crash before flush).
        # Recovering from stdout prevents a FALSE OUTPUT_MISSING on a run that
        # actually executed the tests (observed on C/cJSON: 18/19 passing was being
        # scored 0/19 because the junit landed outside the local_inplace worktree).
        results = None
        summary = None
        if os.path.exists(xml_path):
            with open(xml_path, "r", errors="replace") as f:
                xml_text = f.read()
            results, summary = parse_ctest_junit_with_summary(xml_text)
        elif compile_errors == 0:
            to_path = os.path.join(name, "test_output.txt")
            if os.path.exists(to_path):
                try:
                    with open(to_path, "r", errors="replace") as f:
                        recovered = parse_ctest_stdout(f.read())
                except OSError:
                    recovered = {}
                if recovered:
                    results = recovered
                    summary = summarize_ctest_results(recovered)
                    logger.info(
                        "%s: no test_report.xml; recovered %d test result(s) from "
                        "ctest stdout (junit not written).",
                        repo_label, len(recovered),
                    )

        if results is None:
            # Neither a junit report nor a parseable ctest stdout. Distinguish:
            # PATCH_APPLY_FAILED (eval.sh sentinel) > TEST_SUITE_TIMEOUT (exit 124/137/143)
            # > COMPILE_FAILED (compile_errors>0) > OUTPUT_MISSING (container/infra).
            # All are EXCLUDED from the average below (mirrors evaluate_go.py) so
            # they never masquerade as a real 0/N.
            _output_paths = [Path(name) / "test_output.txt", Path(name) / "test_exit_code.txt"]
            _patch_failed = detect_patch_apply_failed(_output_paths)
            _exit_code = None
            _exit_file = Path(name) / "test_exit_code.txt"
            try:
                _exit_code = int(_exit_file.read_text().strip())
            except (FileNotFoundError, ValueError, OSError):
                pass
            _timed_out = detect_timeout(_exit_code)
            if _patch_failed:
                status = OUTCOME_PATCH_APPLY_FAILED
                reason = "patch_apply_failed"
            elif _timed_out:
                status = OUTCOME_TEST_SUITE_TIMEOUT
                reason = "test_suite_timeout"
            elif compile_errors > 0:
                status = OUTCOME_COMPILE_FAILED
                reason = "compile_failed"
            else:
                status = OUTCOME_OUTPUT_MISSING
                reason = "container_or_infra_failure"
            logger.warning(
                "%s: no test_report.xml and no recoverable ctest output (%s) -- check %s",
                repo_label,
                reason,
                name,
            )
            out.append(
                {
                    "name": repo_label,
                    "sum": 0,
                    "passed": 0.0,
                    "num_passed": 0,
                    "num_tests": len(test_ids_flat),
                    "compile_errors": compile_errors,
                    "status": status,
                }
            )
            continue

        fails = failed_test_names(results)

        if test_ids_flat:
            num_passed = sum(
                1 for tid in test_ids_flat if tid in results and results[tid].value == "PASSED"
            )
            total_tests = len(test_ids_flat)
            per_test_results: list[tuple[str, str]] = []
            for tid in test_ids_flat:
                if tid in results:
                    raw = results[tid].value
                    status = raw if raw in ("PASSED", "SKIPPED") else "FAILED"
                else:
                    status = "FAILED"
                per_test_results.append((tid, status))
        else:
            num_passed = summary["passed"]
            total_tests = summary["total"]
            per_test_results = [
                (name, status.value) for name, status in results.items()
            ]

        print(f"\n--- {repo_label}: Individual Test Results ---")
        for tid, status in sorted(per_test_results, key=lambda x: x[1]):
            print(f"  {status:>7s}  {tid}")
        print(
            f"  Summary: {num_passed} passed, "
            f"{summary['failed']} failed, "
            f"{summary['skipped']} skipped, "
            f"{summary['errored']} errored "
            f"(compile_errors={compile_errors})"
        )
        if fails:
            print(f"  Failing: {fails[:5]}{'...' if len(fails) > 5 else ''}")
        print()

        pass_rate = num_passed / total_tests if total_tests > 0 else 0.0

        out.append(
            {
                "name": repo_label,
                "sum": 0,
                "passed": pass_rate,
                "num_passed": num_passed,
                "num_tests": total_tests,
                "compile_errors": compile_errors,
                # test_report.xml present + parsed ⇒ tests actually ran.
                "status": OUTCOME_TESTS_RAN,
            }
        )

    print("repo,compile_errors,num_passed/num_tests,status")
    out = sorted(out, key=lambda x: x["passed"], reverse=True)
    for x in out:
        status = x.get("status", "")
        print(
            f"{x['name']},{x['compile_errors']},"
            f"{x['num_passed']}/{x['num_tests']},{status}"
        )
    mean_compile_errors = (
        sum(x["compile_errors"] for x in out) / len(out) if out else 0.0
    )
    # An infra-broken / compile-failed run is NOT a measured model score — its
    # 0.0 must not drag the average down like a genuine 0%. Average over SCORED
    # repos only and report how many were excluded (mirrors evaluate_go.py).
    scored = [x for x in out if x.get("status") not in _EXCLUDED_STATUSES]
    excluded = len(out) - len(scored)
    averaged_passed = (
        sum(x["passed"] for x in scored) / len(scored) if scored else 0.0
    )
    print(f"average pass rate: {averaged_passed}")
    print(f"mean compile_errors: {mean_compile_errors}")
    if excluded:
        print(
            f"NOTE: {excluded}/{len(out)} repo(s) EXCLUDED from the average "
            f"(compile-failed / infra-missing — not a measured model score)."
        )

    # Status breakdown so a 0/N is legible as compile-failed / infra-missing /
    # genuinely-zero rather than a bare number.
    status_counts: dict = {}
    for x in out:
        s = x.get("status", "UNCLASSIFIED")
        status_counts[s] = status_counts.get(s, 0) + 1
    if status_counts:
        print(
            "status breakdown: "
            + ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items()))
        )
    logger.info(
        "C evaluation complete: %d repos, avg pass rate %.2f%%, mean compile_errors %.2f",
        len(out),
        averaged_passed * 100,
        mean_compile_errors,
    )


__all__: list[str] = []
