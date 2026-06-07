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

    if not rebuild_image:
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
        test_output_file = os.path.join(name, "test_output.json")
        repo_label = os.path.basename(os.path.dirname(os.path.dirname(name)))

        test_ids = get_go_tests(repo_label, verbose=0)
        test_ids_flat = [tid for group in test_ids for tid in group if tid]

        if not os.path.exists(test_output_file):
            test_stderr_file = os.path.join(name, "test_stderr.txt")
            if os.path.exists(test_stderr_file):
                reason = "go_test_crash"
            else:
                reason = "container_or_infra_failure"
            logger.warning(
                "%s: missing test_output.json (%s) -- check %s",
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
                }
            )
            continue

        with open(test_output_file, "r") as f:
            raw_output = f.read()

        results, durations, pkg_durations = parse_go_test_json_with_durations(
            raw_output
        )

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
                    status = raw_status
                else:
                    status = "FAILED"
                status_counter[status] += 1
                if status == "PASSED":
                    num_passed += 1
                dur = durations.get(tid, 0.0)
                per_test_results.append((tid, status, dur))
            else:
                status_counter["FAILED"] += 1
                per_test_results.append((tid, "FAILED", 0.0))

        print(f"\n--- {repo_label}: Individual Test Results ---")
        for tid, status, dur in sorted(per_test_results, key=lambda x: x[1]):
            dur_str = f" ({dur:.3f}s)" if dur > 0 else ""
            print(f"  {status:>7s}  {tid}{dur_str}")
        if status_counter:
            parts = [f"{v} {k.lower()}" for k, v in sorted(status_counter.items())]
            print(f"  Summary: {', '.join(parts)}")
        print()

        total_tests = len(test_ids_flat) if test_ids_flat else len(results)
        pass_rate = num_passed / total_tests if total_tests > 0 else 0.0

        out.append(
            {
                "name": repo_label,
                "sum": total_duration,
                "passed": pass_rate,
                "num_passed": num_passed,
                "num_tests": total_tests,
            }
        )

    print("repo,runtime,num_passed/num_tests")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        print(f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']}")
    total_runtime = sum(x["sum"] for x in out)
    averaged_passed = sum(x["passed"] for x in out) / len(out) if out else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    logger.info(
        "Go evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__: list[str] = []
