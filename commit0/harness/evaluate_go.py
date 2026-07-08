"""Evaluate Go repos — parallel pipeline counterpart to evaluate.py.

Uses GO_SPLIT, run_go_tests, and Go test JSON parsing.
Does NOT modify the original evaluate.py.
"""

import logging
import os
from collections import Counter
from typing import Iterator, Union

import docker
import docker.errors
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from commit0.harness.constants_go import (
    GoRepoInstance,
    GO_SPLIT,
    RUN_GO_TEST_LOG_DIR,
)
from commit0.harness.go_test_parser import parse_go_test_json_with_durations
from commit0.harness.get_go_test_ids import main as get_go_tests
from commit0.harness.run_go_tests import main as run_go_tests
from commit0.harness.spec_go import make_go_spec
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

# Failure-attribution constants — mirror evaluate_rust.py. Surfaced via the
# `status` field on each `out` entry so downstream summarisers can distinguish a
# REAL test failure (TESTS_RAN) from a pipeline failure the harness must NOT
# silently report as a clean 0/N model score.
OUTCOME_TESTS_RAN = "TESTS_RAN"                     # go test reached the test phase
OUTCOME_COMPILE_FAILED = "COMPILE_FAILED"          # go build error; tests never ran
OUTCOME_PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"  # eval.sh couldn't apply patch.diff
OUTCOME_TEST_SUITE_TIMEOUT = "TEST_SUITE_TIMEOUT"  # killed by `timeout` before finish
OUTCOME_NO_TESTS_DEFINED = "NO_TESTS_DEFINED"      # ran but produced 0 test results
OUTCOME_OUTPUT_MISSING = "OUTPUT_MISSING"          # test_output.json absent / infra
OUTCOME_CRASH = "GO_TEST_CRASH"                    # test_output.json absent, stderr present

# Statuses that are NOT a measured model score — excluded from the average and
# reported at 0.0 (never trusted), matching evaluate_rust.py.
_EXCLUDED_STATUSES = {
    OUTCOME_PATCH_APPLY_FAILED,
    OUTCOME_TEST_SUITE_TIMEOUT,
    OUTCOME_OUTPUT_MISSING,
    OUTCOME_CRASH,
}


def _read_go_exit_code(log_dir: str) -> Union[int, None]:
    """Return the go test exit code if ``go_test_exit_code.txt`` is readable, else None."""
    p = os.path.join(log_dir, "go_test_exit_code.txt")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _aggregate_go_results(
    log_dir: str,
    repo_label: str,
    test_ids_flat: list,
    out: list,
) -> None:
    """Parse Go test results from *log_dir* and append a summary dict to *out*.

    Classifies the run (compile / patch-apply / timeout / empty / crash / infra)
    via a ``status`` field so a pipeline failure is never silently reported as a
    clean 0/N model score. Mirrors ``evaluate_rust._aggregate_rust_results``.
    """
    test_output_file = os.path.join(log_dir, "test_output.json")

    if not os.path.exists(test_output_file):
        # test_output.json is written unconditionally by eval.sh (even on
        # patch-apply failure). Its absence means the container/eval never got
        # that far — an infra failure, NOT a 0% model score. Flag it so the
        # average excludes it instead of counting it as a genuine 0/N.
        test_stderr_file = os.path.join(log_dir, "test_stderr.txt")
        if os.path.exists(test_stderr_file):
            status = OUTCOME_CRASH
            detail = "test_output.json missing but test_stderr.txt present (go test crashed)"
        else:
            status = OUTCOME_OUTPUT_MISSING
            detail = "test_output.json missing (container/infra failure)"
        logger.warning("%s: %s -- check %s", repo_label, detail, log_dir)
        out.append(
            {
                "name": repo_label,
                "sum": 0,
                "passed": 0.0,
                "num_passed": 0,
                "num_tests": len(test_ids_flat),
                "status": status,
                "status_detail": detail,
            }
        )
        return

    with open(test_output_file, "r") as f:
        raw_output = f.read()

    results, durations, pkg_durations = parse_go_test_json_with_durations(raw_output)

    exit_code = _read_go_exit_code(log_dir)
    # eval.sh writes a synthetic PATCH_APPLY_FAILED package line to
    # test_output.json when `git apply` fails; the parser produces no per-test
    # results for it. Detect it explicitly so it is NOT scored as a clean 0/N
    # (which would look like a model that failed every test).
    patch_apply_failed = "PATCH_APPLY_FAILED" in raw_output[:4096]

    num_passed = 0
    total_duration = (
        sum(pkg_durations.values()) if pkg_durations else sum(durations.values())
    )
    status_counter: Counter[str] = Counter()
    per_test_results: list[tuple[str, str, float]] = []
    for tid in test_ids_flat:
        if tid in results:
            raw_status = results[tid].value
            if raw_status in ("PASSED", "SKIPPED"):
                tstatus = raw_status
            else:
                tstatus = "FAILED"
            status_counter[tstatus] += 1
            if tstatus == "PASSED":
                num_passed += 1
            dur = durations.get(tid, 0.0)
            per_test_results.append((tid, tstatus, dur))
        else:
            status_counter["FAILED"] += 1
            per_test_results.append((tid, "FAILED", 0.0))

    print(f"\n--- {repo_label}: Individual Test Results ---")
    for tid, tstatus, dur in sorted(per_test_results, key=lambda x: x[1]):
        dur_str = f" ({dur:.3f}s)" if dur > 0 else ""
        print(f"  {tstatus:>7s}  {tid}{dur_str}")
    if status_counter:
        parts = [f"{v} {k.lower()}" for k, v in sorted(status_counter.items())]
        print(f"  Summary: {', '.join(parts)}")
    print()

    # DENOMINATOR: prefer the canonical .bz2 inventory (test_ids_flat). Only when
    # it is missing/empty do we fall back to the observed count — but a model
    # that ADDS passing tests could inflate `len(results)`, so the fallback
    # denominator must be the observed TOTAL, and num_passed is capped to it.
    # When the inventory is present, num_passed counts only canonical IDs that
    # passed, so it self-caps at len(test_ids_flat) and pass_rate <= 1.0.
    if test_ids_flat:
        num_tests = len(test_ids_flat)
    else:
        num_tests = len(results)
    num_passed = min(num_passed, num_tests)

    # Classify the run so a compile/patch/timeout failure is not reported as a
    # legitimate 0/N (matches evaluate_rust.py's status flag).
    status = OUTCOME_TESTS_RAN
    detail = ""
    if patch_apply_failed:
        status = OUTCOME_PATCH_APPLY_FAILED
        detail = "git apply failed — check patch.diff / git_apply_stderr"
    elif exit_code in (124, 137, 143):
        # `timeout` kills with 124 (SIGTERM) / 137 (SIGKILL) / 143. The suite was
        # cut short: a partial pass count is NOT a valid score.
        status = OUTCOME_TEST_SUITE_TIMEOUT
        detail = (
            f"test suite killed by timeout (exit {exit_code}) after "
            f"{num_passed}/{num_tests}; raise the eval timeout if legitimate"
        )
        logger.warning("%s: %s", repo_label, detail)
    elif not results:
        # go test produced NO per-test results. With a non-zero exit that is a
        # compile/build failure (build errors → package FAIL, no test events);
        # with a zero exit it is a genuinely empty suite. Either way it is not a
        # scored 0/N of real test failures.
        if exit_code not in (0, None):
            status = OUTCOME_COMPILE_FAILED
            detail = (
                f"go test produced no results (exit {exit_code}); "
                "compile/build failure — tests never ran"
            )
        else:
            status = OUTCOME_NO_TESTS_DEFINED
            detail = "go test ran but produced 0 test results (empty suite)"
        logger.warning("%s: %s", repo_label, detail)
    elif num_passed < num_tests:
        detail = f"{num_passed}/{num_tests} passed, {num_tests - num_passed} failed_or_missing"
    else:
        detail = f"{num_passed}/{num_tests} passed"

    pass_rate = num_passed / num_tests if num_tests > 0 else 0.0
    # A killed / patch-broken / infra-broken run must not contribute a "valid"
    # pass rate to the average.
    reported_rate = 0.0 if status in _EXCLUDED_STATUSES else pass_rate

    out.append(
        {
            "name": repo_label,
            "sum": total_duration,
            "passed": reported_rate,
            "num_passed": num_passed,
            "num_tests": num_tests,
            "status": status,
            "status_detail": detail,
        }
    )


def _preflight_check_images(
    specs: list,
    backend: str,
) -> list[str]:
    """Check that Docker images exist for all Go specs."""
    if backend.upper() != "LOCAL":
        return []
    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        logger.error("Pre-flight: cannot connect to Docker daemon: %s", e)
        return ["<docker-daemon-unreachable>"]

    missing = []
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
    """Evaluate Go repos using go test -json.

    Parameters
    ----------
    dataset_name : str
        Name or path of Go dataset.
    dataset_split : str
        HuggingFace split or "test".
    repo_split : str
        Key from GO_SPLIT, repo name, or "all".
    base_dir : str
        Local directory containing cloned repos.
    branch : str or None
        Git branch to evaluate (None = auto-detect).
    backend : str
        Execution backend ("local", "modal", "e2b").
    timeout : int
        Per-repo timeout in seconds.
    num_cpus : int
        CPU count for Docker containers.
    num_workers : int
        Number of parallel evaluation threads.
    rebuild_image : bool
        Whether to rebuild Docker images before evaluation.

    """
    dataset: Iterator[GoRepoInstance] = load_dataset_from_config(
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

    allowed_repos = set(resolve_split(repo_split, dataset_list, curated=GO_SPLIT))
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in allowed_repos:
            continue

        test_info = example["test"]
        test_dir = test_info.get("test_dir", test_info.get("test_cmd", "./..."))
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

        log_dir = RUN_GO_TEST_LOG_DIR / repo_name / repo_branch / hashed_test_ids
        log_dirs.append(str(log_dir))
        triples.append((os.path.join(base_dir, repo_name), test_dir, repo_branch))
        specs.append(make_go_spec(example, absolute=True))

    if not triples:
        logger.error(
            "No repos matched repo_split=%r in dataset with %d entries.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d Go repo(s) out of %d dataset entries",
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
                "Run Go build first.",
                len(missing_images),
                missing_images,
            )
            raise RuntimeError(
                f"Missing Docker images: {missing_images}. Run Go build first."
            )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating Go repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(
                    run_go_tests,
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
        repo_label = os.path.basename(os.path.dirname(os.path.dirname(name)))

        test_ids = get_go_tests(repo_label, verbose=0)
        test_ids_flat = [tid for group in test_ids for tid in group if tid]

        _aggregate_go_results(name, repo_label, test_ids_flat, out)

    print("repo,runtime,num_passed/num_tests,status,detail")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        status = x.get("status", "")
        detail = x.get("status_detail", "")
        print(
            f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']},{status},{detail}"
        )
    total_runtime = sum(x["sum"] for x in out)
    # An infra-broken / timed-out / patch-failed run is NOT a measured model
    # score — its 0.0 must not drag the average down like a genuine 0%. Average
    # over SCORED repos only and report how many were excluded (mirrors
    # evaluate_rust.py).
    scored = [x for x in out if x.get("status") not in _EXCLUDED_STATUSES]
    excluded = len(out) - len(scored)
    averaged_passed = sum(x["passed"] for x in scored) / len(scored) if scored else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    if excluded:
        print(
            f"NOTE: {excluded}/{len(out)} repo(s) EXCLUDED from the average "
            f"(infra-broken / timeout / patch-failed — not a measured model score)."
        )

    # Status breakdown so a 0/N is legible as compile-failed / patch-failed /
    # timeout / genuinely-zero rather than a bare number.
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
        "Go evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__: list[str] = []
