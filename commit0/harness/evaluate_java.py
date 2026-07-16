"""Java evaluation pipeline.

Uses ExecutionContext for backend-agnostic container lifecycle (Docker/Modal/E2B).
Supports single-repo evaluation and multi-repo parallel orchestration.
"""
import bz2
import logging
import os
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import docker
import docker.errors

from commit0.harness.execution_context import Docker, ExecutionBackend, LocalInplace
from commit0.harness.constants import Files, RUN_PYTEST_LOG_DIR, TestStatus
from commit0.harness.spec_java import make_java_spec
from commit0.harness.java_test_parser import (
    parse_surefire_reports,
    JavaTestResult,
)
from commit0.harness.utils import setup_logger

logger = logging.getLogger(__name__)


def evaluate_java_repo(
    instance: dict,
    patch_path: str,
    test_ids: Optional[List[str]] = None,
    timeout: int = 600,
    num_cpus: int = 1,
    log_dir: Optional[str] = None,
    backend: str = "local",
) -> Dict[str, TestStatus]:
    """Evaluate a Java patch: apply it in a Docker container and parse test results.

    Args:
    ----
        instance: Repo instance dict.
        patch_path: Path to the patch file to apply.
        test_ids: Specific test IDs to run. None = run all tests.
        timeout: Container execution timeout in seconds.
        num_cpus: CPU limit for the container.
        log_dir: Directory for logs and artifacts.

    """
    spec = make_java_spec(instance, test_ids=test_ids)
    repo_name = instance.get("repo", instance.get("instance_id", "unknown"))

    log_path = Path(log_dir) if log_dir else RUN_PYTEST_LOG_DIR / repo_name
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "evaluate_java.log"
    eval_logger = setup_logger(repo_name, log_file)

    eval_script_content = "\n".join(
        ["#!/bin/bash", "set -uxo pipefail"] + spec.make_eval_script_list() + [""]
    )
    eval_file = log_path / "eval.sh"
    eval_file.write_text(eval_script_content)

    patch_file = Path(patch_path)
    if not patch_file.exists():
        raise FileNotFoundError(f"Patch file not found: {patch_path}")

    report_dir = spec._get_report_dir()

    files_to_copy = Files(
        eval_script={
            "src": eval_file,
            "dest": Path("/eval.sh"),
        },
        patch={
            "src": patch_file,
            "dest": Path("/patch.diff"),
        },
    )

    files_to_collect = [
        report_dir,
        "test_output.txt",
        "compile_output.txt",
        "test_exit_code.txt",
    ]

    _ctx = (LocalInplace
            if ExecutionBackend(backend.upper()) == ExecutionBackend.LOCAL_INPLACE
            else Docker)
    try:
        with _ctx(
            spec=spec,
            logger=eval_logger,
            timeout=timeout,
            num_cpus=num_cpus,
            log_dir=log_path,
            files_to_copy=files_to_copy,
            files_to_collect=files_to_collect,
        ) as ctx:
            output, timed_out, elapsed = ctx.exec_run_with_timeout(
                "/bin/bash /eval.sh"
            )
            eval_logger.info(output)

            if timed_out:
                eval_logger.error(
                    f"Evaluation timed out for {repo_name} after {elapsed:.1f}s"
                )
                return {"TIMEOUT": TestStatus.ERROR}

        # Docker.__exit__ closes eval_logger and cleans up the container.

        exit_code_file = log_path / "test_exit_code.txt"
        test_output_file = log_path / "test_output.txt"
        # Detect PATCH_APPLY_FAILED sentinel (eval.sh writes this when git apply
        # fails) BEFORE checking COMPILATION_FAILED so a patch-apply failure is
        # attributed correctly instead of being surfaced as a spurious compile fail.
        # HEAD+TAIL 8KB search handles both small failures and huge noisy outputs.
        from commit0.harness._eval_common import detect_patch_apply_failed
        if detect_patch_apply_failed([test_output_file, exit_code_file]):
            logger.error(f"Patch apply failed for {repo_name}")
            return {"PATCH_APPLY": TestStatus.FAILED}
        if exit_code_file.exists():
            exit_content = exit_code_file.read_text().strip()
            if "COMPILATION_FAILED" in exit_content:
                logger.error(f"Compilation failed for {repo_name}")
                return {"COMPILATION": TestStatus.FAILED}

        xml_dir = log_path / report_dir
        if xml_dir.exists():
            results = parse_surefire_reports(str(xml_dir))
            if results:
                return {
                    test_id: _java_to_test_status(result)
                    for test_id, result in results.items()
                }

        test_output_file = log_path / "test_output.txt"
        tail = ""
        if test_output_file.exists():
            try:
                tail = test_output_file.read_text(errors="replace")[-2000:]
            except OSError:
                tail = "<test_output.txt unreadable>"
        logger.error(
            "No surefire XML reports for %s at %s (xml_dir_exists=%s). "
            "test_output.txt tail:\n%s",
            repo_name, xml_dir, xml_dir.exists(), tail or "<no test_output.txt>",
        )
        return {}

    except Exception as e:
        error_msg = (
            f"Error evaluating Java repo {repo_name}: {e}\n"
            f"{traceback.format_exc()}"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


def _java_to_test_status(result: JavaTestResult) -> TestStatus:
    mapping = {
        JavaTestResult.PASSED: TestStatus.PASSED,
        JavaTestResult.FAILED: TestStatus.FAILED,
        JavaTestResult.ERROR: TestStatus.FAILED,
        JavaTestResult.SKIPPED: TestStatus.SKIPPED,
    }
    return mapping.get(result, TestStatus.FAILED)


def _generate_patch_for_repo(
    instance: dict,
    branch: str,
) -> Tuple[str, Optional[str]]:
    repo_name = instance.get("repo", instance.get("instance_id", "unknown"))
    short_name = repo_name.split("/")[-1] if "/" in repo_name else repo_name
    repo_path = instance.get("repo_path", "")
    # Containerized local_inplace runs don't carry a host repo_path in the dataset
    # (prepare runs on the host; the repo is baked into the image). Default to the
    # canonical in-container repo dir so the eval finds the tree instead of failing
    # with "Repo path '' not found".
    if not repo_path:
        from commit0.harness.constants import ABSOLUTE_REPO_DIR
        if Path(ABSOLUTE_REPO_DIR).exists():
            repo_path = ABSOLUTE_REPO_DIR
    base_commit = instance.get("base_commit", "")

    if not repo_path or not Path(repo_path).exists():
        logger.error("Repo path '%s' not found for %s", repo_path, short_name)
        return short_name, None
    if not base_commit:
        logger.error("No base_commit in instance for %s", short_name)
        return short_name, None

    diff_result = subprocess.run(
        [
            "git", "diff", f"{base_commit}..{branch}",
            "--", ".",
            ":(exclude)spec.pdf.bz2",
            ":(exclude)*.bz2",
            ":(exclude).aider*",
            ":(exclude)logs/",
        ],
        capture_output=True,
        text=True,
        cwd=repo_path,
    )
    if diff_result.returncode != 0:
        logger.error("git diff failed for %s: %s", short_name, diff_result.stderr.strip())
        return short_name, None

    if not diff_result.stdout.strip():
        logger.warning("Empty diff for %s on branch %s", short_name, branch)
        return short_name, None

    tmp_patch = Path(repo_path) / ".commit0_eval_patch.diff"
    tmp_patch.write_text(diff_result.stdout)
    return short_name, str(tmp_patch)


def _load_java_test_ids(repo_name: str) -> Optional[List[str]]:
    """Load the frozen canonical test inventory (test CLASSES) for repo_name, or None.

    NOTE: the Java inventory is CLASS-level (e.g. ``org.json.junit.CDLTest``) while
    surefire reports are METHOD-level — so this is NOT a method denominator. It is
    used only to distinguish a genuine zero-tests result from an INFRA failure
    (compile error / test-phase timeout / no reports) where the canonical suite says
    tests DO exist. Resolved via find_test_ids_file so it works from both the host
    dir (commit0/data/java_test_ids/) and the containerized KAIJU_TEST_IDS_DIR mount.
    """
    from kaiju.paths import find_test_ids_file

    commit0_path = os.path.dirname(os.path.dirname(__file__))
    p = find_test_ids_file(commit0_path, "java_test_ids", f"{repo_name}.bz2")
    if p is None:
        return None
    try:
        with bz2.open(str(p), "rt") as f:
            return [line.strip() for line in f if line.strip()]
    except (OSError, EOFError):
        return None


def _eval_single_repo(
    instance: dict,
    patch_path: str,
    timeout: int,
    num_cpus: int,
    backend: str = "local",
) -> Tuple[str, float, int, int]:
    repo_name = instance.get("repo", instance.get("instance_id", "unknown"))
    short_name = repo_name.split("/")[-1] if "/" in repo_name else repo_name

    start = time.monotonic()
    try:
        results = evaluate_java_repo(
            instance=instance,
            patch_path=patch_path,
            timeout=timeout,
            num_cpus=num_cpus,
            backend=backend,
        )
    except Exception as e:
        logger.error(
            "Evaluation CRASHED for %s: %s\n%s",
            short_name, e, traceback.format_exc(),
        )
        canonical = _load_java_test_ids(short_name)
        num_total = len(canonical) if canonical else 0
        return short_name, time.monotonic() - start, 0, num_total

    elapsed = time.monotonic() - start
    # Infra markers (TIMEOUT/COMPILATION) are not real tests — exclude them.
    _special = {"TIMEOUT", "COMPILATION"}
    real = {k: v for k, v in results.items() if k not in _special}
    num_passed = sum(1 for v in real.values() if v is TestStatus.PASSED)
    observed_total = len(real)
    canonical = _load_java_test_ids(short_name)
    canonical_total = len(canonical) if canonical else 0
    if observed_total == 0:
        num_total = canonical_total
    else:
        num_total = max(canonical_total, observed_total)
    return short_name, elapsed, num_passed, num_total


def _preflight_check_java_images(instances: List[dict]) -> list[str]:
    """Validate that all required Java Docker images exist BEFORE launching evaluation.

    Returns a list of missing image names. An empty list means all images are present.
    """
    try:
        client = docker.from_env()
    except docker.errors.DockerException as e:
        logger.error("Pre-flight: cannot connect to Docker daemon: %s", e)
        return ["<docker-daemon-unreachable>"]

    missing: list[str] = []
    checked: set[str] = set()
    for inst in instances:
        spec = make_java_spec(inst)
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


def evaluate_java_repos(
    dataset: List[dict],
    branch: str,
    timeout: int = 600,
    num_cpus: int = 1,
    num_workers: int = 4,
    repo_filter: Optional[str] = None,
    backend: str = "local",
) -> List[Tuple[str, float, int, int]]:
    instances = dataset if isinstance(dataset, list) else list(dataset)

    if repo_filter and repo_filter != "all":
        instances = [
            inst for inst in instances
            if repo_filter in (
                inst.get("repo", ""),
                inst.get("repo", "").split("/")[-1],
                inst.get("original_repo", ""),
                inst.get("original_repo", "").split("/")[-1],
            )
        ]

    if not instances:
        logger.error("No repos matched filter '%s' in %d dataset entries", repo_filter, len(dataset))
        return []

    logger.info("Evaluating %d repo(s) with %d workers", len(instances), num_workers)

    # local_inplace eval runs in a git worktree (no docker image); skip the
    # image preflight so the pipeline can run entirely inside a container.
    missing_images = (
        [] if str(backend).lower() == "local_inplace"
        else _preflight_check_java_images(instances)
    )
    if missing_images:
        logger.error(
            "Pre-flight failed: %d Docker image(s) not found: %s. "
            "Run 'commit0-java build' first.",
            len(missing_images),
            missing_images,
        )
        raise RuntimeError(
            f"Missing Docker images: {missing_images}. Run 'commit0-java build' first."
        )

    patch_map: Dict[str, Tuple[dict, str]] = {}
    for inst in instances:
        short_name, patch_path = _generate_patch_for_repo(inst, branch)
        if patch_path:
            patch_map[short_name] = (inst, patch_path)
        else:
            logger.warning("Skipping %s — no patch generated", short_name)

    if not patch_map:
        logger.error("No patches generated for any repo")
        return []

    results: List[Tuple[str, float, int, int]] = []
    with ThreadPoolExecutor(max_workers=min(num_workers, len(patch_map))) as executor:
        futures = {
            executor.submit(
                _eval_single_repo,
                inst,
                pp,
                timeout,
                num_cpus,
                backend,
            ): name
            for name, (inst, pp) in patch_map.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error("Evaluation future failed for %s: %s", name, e)
                results.append((name, 0.0, 0, 0))

    results.sort(key=lambda r: r[1], reverse=True)
    return results
