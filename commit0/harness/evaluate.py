import json
import logging
import os
from collections import Counter

import docker
from commit0.harness.docker_utils import docker_client  # context-aware client (B10)
import docker.errors
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from typing import Iterator, Union

from commit0.harness.run_pytest_ids import main as run_tests
from commit0.harness._eval_common import detect_patch_apply_failed, detect_timeout
from commit0.harness.get_pytest_ids import main as get_tests
from commit0.harness.constants import RepoInstance, SPLIT, RUN_PYTEST_LOG_DIR
from commit0.harness.split_utils import resolve_split
from commit0.harness.spec import get_specs_from_dataset
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


def _preflight_check_images(
    dataset_name: str,
    dataset_split: str,
    backend: str,
) -> list[str]:
    """Validate that all required Docker images exist BEFORE launching evaluation.

    Returns a list of missing image names. An empty list means all images are present.
    This catches missing/deleted images early with a clear error message instead of
    N parallel threads each failing independently with confusing docker pull errors.
    """
    if backend.upper() != "LOCAL":
        return []  # only Docker backend needs local images

    try:
        client = docker_client()
    except docker.errors.DockerException as e:
        logger.error(f"Pre-flight: cannot connect to Docker daemon: {e}")
        return ["<docker-daemon-unreachable>"]

    dataset = load_dataset_from_config(dataset_name, split=dataset_split)
    dataset_type = "swebench" if "swe" in dataset_name.lower() else "commit0"
    specs = get_specs_from_dataset(list(dataset), dataset_type, absolute=True)

    missing = []
    checked_images: set[str] = set()
    for spec in specs:
        for image_key in (spec.base_image_key, spec.repo_image_key):
            if image_key in checked_images:
                continue
            checked_images.add(image_key)
            try:
                client.images.get(image_key)
            except docker.errors.ImageNotFound:
                missing.append(image_key)
            except docker.errors.APIError as e:
                logger.warning(f"Pre-flight: API error checking {image_key}: {e}")

    return missing


def main(
    dataset_name: str,
    dataset_split: str,
    repo_split: str,
    base_dir: str,
    branch: Union[str, None],
    coverage: bool,
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
    # P12: previous dead-code block computed all_instance_ids and two filtered
    # views but discarded the results. The real filtering happens inline in the
    # `for example in dataset_list` loop below (via `allowed_repos` for non-swe
    # and instance_id substring check for swe). Removed to avoid the confusion
    # that spawned the original critique.
    triples = []
    log_dirs = []
    allowed_repos: set[str] = set()
    if "swe" not in dataset_name.lower():
        allowed_repos = set(resolve_split(repo_split, dataset_list, curated=SPLIT))
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if "swe" in dataset_name.lower():
            if repo_split != "all" and repo_split not in example["instance_id"]:
                continue
        else:
            if repo_name not in allowed_repos:
                continue
        hashed_test_ids = get_hash_string(example["test"]["test_dir"])
        repo_branch = branch
        if repo_branch is None:
            git_path = os.path.join(base_dir, example["instance_id"])
            repo_branch = get_active_branch(git_path)
            logger.debug(
                "Branch not specified for %s, resolved to: %s", repo_name, repo_branch
            )
        log_dir = (
            RUN_PYTEST_LOG_DIR
            / example["instance_id"].split("/")[-1]
            / repo_branch
            / hashed_test_ids
        )
        log_dirs.append(str(log_dir))
        triples.append(
            (example["instance_id"], example["test"]["test_dir"], repo_branch)
        )

    if not triples:
        logger.error(
            "No repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.yaml repo_split matches repo names in the dataset.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    # Pre-flight: validate all required Docker images exist before launching parallel eval
    # local_inplace eval runs in a git worktree (no docker image needed),
    # so the image preflight is skipped — this is what lets the pipeline run
    # entirely inside a container.
    if not rebuild_image and str(backend).lower() != "local_inplace":
        missing_images = _preflight_check_images(dataset_name, dataset_split, backend)
        if missing_images:
            logger.error(
                f"Pre-flight failed: {len(missing_images)} Docker image(s) not found: "
                f"{missing_images}. Run 'commit0 build' first."
            )
            raise RuntimeError(
                f"Missing Docker images: {missing_images}. Run 'commit0 build' first."
            )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            # Create a future for running each instance
            futures = {
                executor.submit(
                    run_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    branch,
                    test_dir,
                    coverage,
                    backend,
                    timeout,
                    num_cpus,
                    rebuild_image=rebuild_image,
                    verbose=0,
                ): repo
                for repo, test_dir, branch in triples
            }
            # Wait for each future to complete
            for future in as_completed(futures):
                pbar.update(1)
                repo_name = futures[future]
                try:
                    future.result()
                except SystemExit as e:
                    # run_pytest_ids.main() calls sys.exit(pytest_exit_code);
                    # exit code 0-1 is normal (0=all passed, 1=some failed).
                    if e.code not in (0, 1):
                        logger.warning(
                            "Evaluation for %s exited with code %s",
                            repo_name,
                            e.code,
                        )
                except Exception as e:
                    logger.error(
                        f"Evaluation failed for {repo_name}: {e}", exc_info=True
                    )

    # get numbers
    out = []
    for name in tqdm(log_dirs):
        report_file = os.path.join(name, "report.json")
        name = name.split("/")[2]
        # P7: get_tests returns List[List[str]] (fail_to_pass + pass_to_pass)
        # per commit0.harness.get_pytest_ids.main signature — this flattens the
        # two sublists into a single list of test node ids. Do NOT change to a
        # single-level comprehension: if tests_ids were flat strings, iterating
        # 'for xx in x' would expose characters.
        test_ids = get_tests(name, verbose=0)
        test_ids = [xx for x in test_ids for xx in x if xx]
        if not os.path.exists(report_file):
            log_parent = os.path.dirname(report_file)
            test_output_file = os.path.join(log_parent, "test_output.txt")
            pytest_exit_file = os.path.join(log_parent, "pytest_exit_code.txt")
            # Detect patch-apply failure (eval.sh wrote PATCH_APPLY_FAILED sentinel)
            # and timeout kill (exit 124/137/143 from `timeout` wrapper). Both are
            # infra failures that must be flagged so run_pipeline.sh can distinguish
            # them from a legitimate 0% score.
            from pathlib import Path as _Path
            _patch_failed = detect_patch_apply_failed([_Path(test_output_file)])
            _pytest_exit = None
            try:
                _pytest_exit = int(_Path(pytest_exit_file).read_text().strip())
            except (FileNotFoundError, ValueError, OSError):
                pass
            _timed_out = detect_timeout(_pytest_exit)
            if _patch_failed:
                reason = "patch_apply_failed"
            elif _timed_out:
                reason = "test_suite_timeout"
            elif os.path.exists(test_output_file):
                reason = "pytest_crash_or_collection_error"
            else:
                reason = "container_or_infra_failure"
            logger.warning(
                f"{name}: missing report.json ({reason}) — check {log_parent}"
            )
            # Emit a machine-readable marker so downstream (run_pipeline.sh) can
            # distinguish an EVAL INFRA CRASH (pytest never produced a report) from
            # a legitimate 0% score. Without this the crashed eval's 0/N reads as
            # "the model passed nothing", silently corrupting the recorded result.
            print(f"PYTEST_INFRA_ERROR,{name},{reason}")
            out.append(
                {
                    "name": name,
                    "sum": 0,
                    "passed": 0,
                    "num_passed": 0,
                    "num_tests": len(test_ids),
                    "error": reason,
                    "patch_apply_failed": _patch_failed,
                    "timed_out": _timed_out,
                }
            )
            continue
        with open(report_file, "r") as file:
            report = json.load(file)
        # new version of pytest json
        if "created" in report:
            logger.debug("Using new pytest report format for %s", name)
            tests = {x["nodeid"]: x["call"] for x in report["tests"] if "call" in x}
        # old version of pytest json
        else:
            logger.debug("Using old pytest report format for %s", name)
            tests = {
                x["nodeid"]: {"outcome": x["outcome"], "duration": x["duration"]}
                for x in report
                if x["when"] == "call"
            }
        status = []
        runtimes = []
        no_runs = 0
        for test_id in test_ids:
            if test_id in tests and tests[test_id] is not None:
                status.append(tests[test_id]["outcome"])
                runtimes.append(tests[test_id]["duration"])
                no_runs += 1
            else:
                status.append("failed")
                runtimes.append(0)
        # Defensive false-0/N guard (general, layout-agnostic): the frozen
        # inventory ids matched NONE of the report's collected nodeids, yet the
        # report DID collect tests. That is the fingerprint of an inventory<->
        # report nodeid FORMAT/rootdir mismatch (a stray path prefix, or a
        # conftest.py/pytest.ini inside test_dir shifting pytest's rootdir) or a
        # stubbed-base ImportError — every passing test would be silently scored
        # `failed`. Emit a machine-readable marker so run_pipeline.sh flags it
        # LOUDLY (eval_status) instead of recording a bogus 0/N. len(tests)>0
        # distinguishes it from a genuinely empty/degenerate run.
        if no_runs == 0 and len(tests) > 0:
            _report_passed = sum(
                1 for v in tests.values()
                if v is not None and v.get("outcome") == "passed"
            )
            print(f"INVENTORY_MISMATCH,{name},{len(test_ids)},{len(tests)},{_report_passed}")
            logger.error(
                "%s: frozen inventory (%d ids) intersects ZERO of the %d collected "
                "report nodeids (%d passed in report) — inventory is stale/mis-formatted "
                "vs eval; scoring would be a false 0/N. Regenerate it (delete "
                "commit0/data/<subdir>/%s.bz2; run_trajectory step 3b).",
                name, len(test_ids), len(tests), _report_passed, name,
            )
            # P1 fix: DO NOT record a bogus score alongside the marker.
            # Previously we fell through to append(...) with a 0/N score that
            # downstream consumers could mistake for a legitimate result. Emit
            # the marker as an entry with sentinel status so callers see BOTH
            # signals coherently.
            out.append({
                "name": name,
                "sum": 0,
                "passed": None,  # sentinel: not scored
                "num_passed": 0,
                "num_tests": len(test_ids),
                "status": "INVENTORY_MISMATCH",
            })
            continue
        status = Counter(status)
        if no_runs == 0:
            total = 0
        else:
            total = sum(runtimes)
        if "xfail" not in status:
            status["xfail"] = 0
        observed_passed_or_xfail = sum(
            1 for v in tests.values()
            if v is not None and v.get("outcome") in ("passed", "xfail")
        )
        num_tests_effective = max(len(test_ids), len(tests))
        passed = (
            observed_passed_or_xfail / num_tests_effective
            if num_tests_effective > 0
            else 0.0
        )
        out.append(
            {
                "name": name,
                "sum": total,
                "passed": passed,
                "num_passed": observed_passed_or_xfail,
                "num_tests": num_tests_effective,
            }
        )
    print("repo,runtime,num_passed/num_tests")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        print(f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']}")
    total_runtime = sum([x["sum"] for x in out])
    averaged_passed = sum([x["passed"] for x in out]) / len(out) if out else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    logger.info(
        "Evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__ = []
