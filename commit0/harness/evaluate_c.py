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
    failed_test_names,
)
from commit0.harness.get_c_test_ids import main as get_c_tests
from commit0.harness.run_c_tests import main as run_c_tests
from commit0.harness.spec_c import make_c_spec
from commit0.harness.utils import (
    get_hash_string,
    get_active_branch,
    load_dataset_from_config,
)
from commit0.harness.split_utils import resolve_split

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
        dataset_name,
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

    if not rebuild_image:
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

        if not os.path.exists(xml_path):
            reason = "compile_failed" if compile_errors > 0 else "container_or_infra_failure"
            logger.warning(
                "%s: missing test_report.xml (%s) -- check %s",
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
                }
            )
            continue

        with open(xml_path, "r", errors="replace") as f:
            xml_text = f.read()

        results, summary = parse_ctest_junit_with_summary(xml_text)
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
            }
        )

    print("repo,compile_errors,num_passed/num_tests")
    out = sorted(out, key=lambda x: x["passed"], reverse=True)
    for x in out:
        print(f"{x['name']},{x['compile_errors']},{x['num_passed']}/{x['num_tests']}")
    averaged_passed = sum(x["passed"] for x in out) / len(out) if out else 0.0
    mean_compile_errors = (
        sum(x["compile_errors"] for x in out) / len(out) if out else 0.0
    )
    print(f"average pass rate: {averaged_passed}")
    print(f"mean compile_errors: {mean_compile_errors}")
    logger.info(
        "C evaluation complete: %d repos, avg pass rate %.2f%%, mean compile_errors %.2f",
        len(out),
        averaged_passed * 100,
        mean_compile_errors,
    )


__all__: list[str] = []
