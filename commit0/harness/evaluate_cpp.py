"""C++ evaluation pipeline -- mirrors ``evaluate.py`` for C++ repositories.

Uses ``run_cpp_tests.main`` as the per-repo test runner and parses
C++ test framework output for result aggregation.
"""

import bz2
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator, Union

from tqdm import tqdm

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_cpp import (
    CPP_SPLIT,
    CPP_TEST_IDS_DIR,
    RUN_CPP_TESTS_LOG_DIR,
)


def _expected_test_count(name: str) -> int:
    # Resolve via find_test_ids_file (like the go/c/ts/rust readers) so the
    # inventory is found from the host legacy dir AND from KAIJU_TEST_IDS_DIR —
    # the location the containerized run mounts it to (commit0/data/ is pruned
    # from the agent image). Falls back to the legacy CPP_TEST_IDS_DIR.
    from kaiju.paths import find_test_ids_file
    commit0_path = os.path.dirname(os.path.dirname(__file__))
    cache_path = find_test_ids_file(commit0_path, "cpp_test_ids", f"{name}.bz2")
    if cache_path is None:
        cache_path = CPP_TEST_IDS_DIR / f"{name}.bz2"
        if not cache_path.exists():
            return 0
    try:
        raw = bz2.decompress(Path(cache_path).read_bytes()).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return 0
    return sum(1 for line in raw.splitlines() if line.strip())
from commit0.harness.reward_hack import average_pass_rate
from commit0.harness.run_cpp_tests import main as run_cpp_tests
from commit0.harness.cpp_test_parser import (
    parse_cmake_build_attribution,
    parse_cpp_test_output,
)
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

# Statuses that are NOT a measured model score — excluded from the micro-average
# (mirrors evaluate_go.py / evaluate_c.py / evaluate_rust.py). CHEAT_DETECTED is
# a forged run (never trusted); COMPILE_FAILED / BUILD_CONFIGURE_FAILED /
# OUTPUT_MISSING / *_TIMEOUT / PATCH_APPLY_FAILED are infra failures. NOTE:
# COMPILE_FAILED_MODEL is deliberately NOT excluded — the base compiled (A11) so
# a build failure there is a genuine model 0/N (see the :309 comment).
_EXCLUDED_STATUSES = {
    "CHEAT_DETECTED",
    "COMPILE_FAILED",
    "BUILD_CONFIGURE_FAILED",
    "OUTPUT_MISSING",
    "TEST_SUITE_TIMEOUT",
    "PATCH_APPLY_FAILED",
}

_CPP_FAILURE_EXIT_CODES = {1, 42, 200, 201}


def evaluate_single_repo(
    instance: dict,
    patch_path: str,
    timeout: int = 7200,
    backend: str = "local",
    num_cpus: int = 1,
    rebuild_image: bool = False,
) -> dict:
    """Evaluate a single C++ repo given an instance dict and patch file path.

    Returns a dict mapping test_name -> TestStatus.
    """
    from commit0.harness.constants import TestStatus
    from commit0.harness.spec_cpp import make_cpp_spec
    from commit0.harness.execution_context import Docker, Modal, E2B, ExecutionBackend
    from pathlib import Path
    import tempfile

    absolute = backend != "e2b"
    spec = make_cpp_spec(instance, absolute)

    repo_name = instance["repo"].split("/")[-1]
    test_info = instance.get("test", {})
    test_ids = test_info.get("test_dir", "") if isinstance(test_info, dict) else str(test_info)

    with tempfile.TemporaryDirectory(prefix=f"cpp_eval_{repo_name}_") as _tmp_dir:
        log_dir = Path(_tmp_dir)
        eval_logger = logging.getLogger(f"eval.{repo_name}")

        patch_content = Path(patch_path).read_text()
        eval_script = spec.eval_script.replace("{test_ids}", test_ids)

        patch_file = log_dir / "patch.diff"
        patch_file.write_text(patch_content, encoding="utf-8", errors="ignore")
        eval_file = log_dir / "eval.sh"
        eval_file.write_text(eval_script)

        from commit0.harness.constants import Files
        files_to_copy = Files(
            eval_script={
                "src": eval_file,
                "dest": Path("/eval.sh" if absolute else "eval.sh"),
            },
            patch={
                "src": patch_file,
                "dest": Path("/patch.diff" if absolute else "patch.diff"),
            },
        )
        files_to_collect = ["test_exit_code.txt", "test_output.txt"]

        backend_upper = backend.upper()
        if ExecutionBackend(backend_upper) == ExecutionBackend.MODAL:
            execution_context = Modal
        elif ExecutionBackend(backend_upper) == ExecutionBackend.E2B:
            execution_context = E2B
        else:
            execution_context = Docker

        eval_command = (
            "/bin/bash /eval.sh"
            if ExecutionBackend(backend_upper) != ExecutionBackend.E2B
            else "/bin/bash eval.sh"
        )

        try:
            with execution_context(
                spec,
                eval_logger,
                timeout,
                num_cpus,
                log_dir,
                files_to_copy,
                files_to_collect,
                rebuild_image,
            ) as context:
                output, timed_out, total_runtime = context.exec_run_with_timeout(eval_command)
                if timed_out:
                    logger.warning("Evaluation timed out for %s after %ds", repo_name, timeout)
        except Exception as e:
            logger.error("Evaluation failed for %s: %s", repo_name, e, exc_info=True)
            return {"__error__": str(e)}

        test_output_file = log_dir / "test_output.txt"
        exit_code_file = log_dir / "test_exit_code.txt"

        exit_code = -1
        if exit_code_file.exists():
            try:
                exit_code = int(exit_code_file.read_text().strip())
            except (ValueError, OSError):
                pass

        if not test_output_file.exists():
            return {}

        content = test_output_file.read_text()
        report = parse_cpp_test_output(content, exit_code)
        tests = report.get("tests", [])

        results = {}
        for t in tests:
            name = t.get("name", "unknown")
            status = t.get("outcome", "FAILED")
            if status.upper() == "PASSED":
                results[name] = TestStatus.PASSED
            elif status.upper() == "SKIPPED":
                results[name] = TestStatus.SKIPPED
            else:
                results[name] = TestStatus.FAILED

        return results


def _aggregate_cpp_results(
    log_dir: str, name: str, out: list, base_compiles: bool = False
) -> None:
    """Parse C++ test results from *log_dir* and append a summary dict to *out*.

    Looks for ``test_output.txt`` and ``test_exit_code.txt`` in the log
    directory.  Auto-detects the test framework (GTest, Catch2, doctest,
    Boost.Test, CTest) and delegates to the appropriate parser.

    ``base_compiles`` is the prep-time A11 result for this repo. When it is True,
    a build failure here is the MODEL's fault (the stubbed base compiled and the
    patch applied) — a genuine 0/N failure that must count in the denominator,
    NOT an infra 0/0 that silently drops out of the micro-averaged pass rate.
    """
    test_output_file = os.path.join(log_dir, "test_output.txt")
    exit_code_file = os.path.join(log_dir, "test_exit_code.txt")

    if not os.path.exists(test_output_file):
        logger.warning(
            "%s: missing test_output.txt -- check %s", name, log_dir
        )
        out.append(
            {
                "name": name,
                "sum": 0,
                "passed": 0,
                "num_passed": 0,
                "num_tests": 0,
                "status": "OUTPUT_MISSING",
            }
        )
        return

    exit_code = -1
    if os.path.exists(exit_code_file):
        try:
            exit_code = int(Path(exit_code_file).read_text().strip())
        except (ValueError, OSError):
            pass

    # Timeout detection: exit codes 124/137/143 come from the `timeout` wrapper
    # in spec_cpp's eval.sh. Emit TEST_SUITE_TIMEOUT before parsing tests so the
    # kill-signaled run is scored as infra, not a legitimate 0%.
    from commit0.harness._eval_common import (
        detect_patch_apply_failed as _dpaf,
        detect_timeout as _dto,
    )
    if _dto(exit_code):
        logger.warning("%s: test suite timed out (exit %s)", name, exit_code)
        out.append(
            {
                "name": name,
                "sum": 0,
                "passed": 0,
                "num_passed": 0,
                "num_tests": 0,
                "status": "TEST_SUITE_TIMEOUT",
                "timed_out": True,
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
                "status": "OUTPUT_MISSING",
            }
        )
        return

    # PATCH_APPLY_FAILED detection via HEAD+TAIL shared helper (was brittle
    # `content.strip() == "PATCH_APPLY_FAILED"` which broke on any surrounding
    # noise). The sentinel is written by eval.sh when `git apply` of the model
    # patch fails — the 0/N is infra, not a real score.
    if _dpaf([Path(test_output_file), Path(exit_code_file)]):
        logger.warning("%s: patch failed to apply (PATCH_APPLY_FAILED)", name)
        out.append(
            {
                "name": name,
                "sum": 0,
                "passed": 0,
                "num_passed": 0,
                "num_tests": 0,
                "status": "PATCH_APPLY_FAILED",
                "patch_apply_failed": True,
            }
        )
        return

    # Fix for silent-zero-when-configure-fails (regression fixture at
    # outputs/.../stage2_eval_artifacts/fmt/aider-cpp-gpt-5.5-dataset/
    # cdb4ee2aea69cc6a83331b/test_output.txt): if the eval script's configure
    # step failed, `cmake --build build` errored with 'is not a directory'
    # and 0 tests were parsed — downstream scored as 0/N legit. spec_cpp now
    # emits BUILD_CONFIGURE_FAILED as the first line of test_output on that
    # branch so we can distinguish and exclude from the score.
    _content_head = content.lstrip()
    if _content_head.startswith("BUILD_CONFIGURE_FAILED"):
        logger.warning("%s: configure step failed (BUILD_CONFIGURE_FAILED)", name)
        out.append(
            {
                "name": name,
                "sum": 0,
                "passed": 0,
                "num_passed": 0,
                "num_tests": 0,
                "status": "BUILD_CONFIGURE_FAILED",
            }
        )
        return

    # spec_cpp now writes a COMPILE_FAILED sentinel when the build step exits
    # non-zero (matches C's posture). Detect it explicitly at the start of the
    # file before falling through to the heuristic parser that infers it from
    # per-binary build attribution.
    if _content_head.startswith("COMPILE_FAILED"):
        logger.warning("%s: build step failed (COMPILE_FAILED sentinel)", name)
        _expected = _expected_test_count(name)
        if base_compiles and _expected > 0:
            # The stubbed base compiled (A11) and the patch already applied (we
            # reached the build), so this build failure is the MODEL's broken
            # code, NOT infra. Score it 0/N (N = canonical count) so it counts as
            # a real 0% in the micro-averaged pass rate instead of a 0/0 that
            # drops out of the denominator and silently inflates the benchmark.
            logger.warning(
                "%s: base_compiles=True -> model-caused compile failure; scoring "
                "0/%d (COMPILE_FAILED_MODEL), not excluded", name, _expected,
            )
            out.append(
                {
                    "name": name, "sum": 0, "passed": 0,
                    "num_passed": 0, "num_tests": _expected,
                    "status": "COMPILE_FAILED_MODEL",
                }
            )
        else:
            # base_compiles is False/unknown (or no canonical inventory): the base
            # itself may be broken -> infra failure, keep 0/0 (excluded).
            out.append(
                {
                    "name": name, "sum": 0, "passed": 0,
                    "num_passed": 0, "num_tests": 0,
                    "status": "COMPILE_FAILED",
                }
            )
        return

    report = parse_cpp_test_output(content, exit_code)
    tests = report.get("tests", [])
    summary = report.get("summary", {})
    build_attr = parse_cmake_build_attribution(content)
    tests_built = build_attr.get("tests_built", [])
    tests_failed_build = build_attr.get("tests_failed", [])
    total_test_binaries = len(tests_built) + len(tests_failed_build)

    num_passed = summary.get("passed", 0)
    num_tests = summary.get("total", 0)
    framework = summary.get("framework", "unknown")
    # Reward-hacking guard: C++ counts from RAW STDOUT (GTest `[ OK ]`, Catch2,
    # doctest, ...), so a model whose code prints fake pass lines could inflate
    # num_passed. Anchor to the canonical test inventory + the process exit code
    # (both unforgeable by stdout) and flag forged output as CHEAT_DETECTED.
    expected = _expected_test_count(name)
    if framework == "unknown" and num_tests <= 1 and expected > 0:
        num_tests = expected
    # Anchor the denominator to the canonical inventory when available (like the
    # other languages) and clamp passes to it. NOTE: observed > canonical is
    # BENIGN (a stale bz2, doctests, ...), so it is NOT treated as a cheat — that
    # would false-flag a legit run. The only unforgeable signal is the exit code.
    if expected > 0:
        num_tests = max(num_tests, expected)
        num_passed = min(num_passed, num_tests)
    total_runtime = sum(t.get("duration", 0) for t in tests)
    status = "TESTS_RAN"
    # CMake tried to build test binaries and EVERY one failed to compile -> the
    # tests never ran; a 0/N here is a build failure, not a 0% model score. Mirror
    # go/rust/c's COMPILE_FAILED so the pipeline can exclude it from the score.
    if total_test_binaries > 0 and len(tests_built) == 0:
        if base_compiles and expected > 0:
            # Base compiled + patch applied but the model's code fails to build the
            # test binaries -> a genuine MODEL 0/N failure (scored), not infra.
            status = "COMPILE_FAILED_MODEL"
            num_tests = max(num_tests, expected)
            num_passed = 0
            logger.warning(
                "%s: COMPILE_FAILED_MODEL — base compiles but all %d test "
                "binary/binaries failed to build; scoring 0/%d",
                name, total_test_binaries, num_tests,
            )
        else:
            status = "COMPILE_FAILED"
            num_tests = 0  # infra: drop from the denominator
            logger.warning(
                "%s: COMPILE_FAILED — all %d test binary/binaries failed to build",
                name, total_test_binaries,
            )
    # C++ counts from RAW STDOUT (GTest `[ OK ]`, ...), so a model can print fake
    # pass lines. The build+test process exits 0 IFF every test passed, so a claim
    # of all-pass with a non-zero exit is impossible for a genuine run -> forged.
    elif exit_code not in (0, None) and num_tests > 0 and num_passed >= num_tests:
        status = "CHEAT_DETECTED"
        logger.warning(
            "%s: CHEAT_DETECTED — claimed %d/%d passed but the run exited %s "
            "(a genuine all-pass exits 0; forged C++ test output)",
            name, num_passed, num_tests, exit_code,
        )
        num_passed = 0
    passed_rate = num_passed / num_tests if num_tests > 0 else 0.0

    if total_test_binaries > 0:
        logger.info(
            "%s build attribution: %d/%d test binaries built (%d failed: %s)",
            name,
            len(tests_built),
            total_test_binaries,
            len(tests_failed_build),
            ", ".join(tests_failed_build[:5]) + ("..." if len(tests_failed_build) > 5 else ""),
        )

    out.append(
        {
            "name": name,
            "sum": total_runtime,
            "passed": passed_rate,
            "num_passed": num_passed,
            "num_tests": num_tests,
            "status": status,
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
    """Evaluate C++ repositories by running tests and aggregating results."""
    split_dict = CPP_SPLIT
    log_base_dir = RUN_CPP_TESTS_LOG_DIR

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

    cpp_repo_names = set()
    if repo_split == "all":
        for repos in split_dict.values():
            cpp_repo_names.update(r.split("/")[-1] for r in repos)
    elif repo_split in split_dict:
        cpp_repo_names = {r.split("/")[-1] for r in split_dict[repo_split]}

    accept_all_when_split_empty = (repo_split == "all" and not cpp_repo_names)

    repos = []
    if repo_split == "all" or repo_split in split_dict:
        repos = list(cpp_repo_names)
    else:
        repos = [repo_split]

    triples = []
    log_dirs = []
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_split == "all":
            if not accept_all_when_split_empty and repo_name not in cpp_repo_names:
                continue
        elif repo_split in split_dict:
            if repo_name not in cpp_repo_names:
                continue
        else:
            if repo_name.replace("-", "_") != repo_split.replace("-", "_"):
                continue

        test_dir = example["test"]["test_dir"]
        hashed_test_ids = get_hash_string(test_dir)
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
            (example["repo"], test_dir, repo_branch)
        )

    if not triples:
        logger.error(
            "No C++ repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.yaml repo_split matches C++ repo names in CPP_SPLIT.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d C++ repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating C++ repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {}
            for repo, test_dir, repo_branch in triples:
                future = executor.submit(
                    run_cpp_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    repo_branch,
                    test_dir,
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
                    exit_code = future.result()
                    if exit_code not in (0,) and exit_code not in _CPP_FAILURE_EXIT_CODES:
                        logger.warning(
                            "C++ evaluation for %s exited with code %s",
                            repo_name,
                            exit_code,
                        )
                except Exception as e:
                    logger.error(
                        "C++ evaluation failed for %s: %s", repo_name, e, exc_info=True
                    )

    # A11 base-compile result per repo basename — lets a build failure be scored
    # as a MODEL 0/N (base compiled) vs excluded as infra (base broken).
    base_compiles_by_name = {
        ex["repo"].split("/")[-1]: bool(ex.get("base_compiles"))
        for ex in dataset_list
    }

    out = []
    for log_path in tqdm(log_dirs):
        log_name = os.path.basename(os.path.dirname(os.path.dirname(log_path)))
        if not log_name:
            log_name = log_path.split("/")[2] if len(log_path.split("/")) > 2 else "unknown"
        _aggregate_cpp_results(
            log_path, log_name, out,
            base_compiles=base_compiles_by_name.get(log_name, False),
        )

    # 4th column = per-repo outcome so the shell pipeline can tell a genuine 0%
    # model score from a build/patch/infra failure (mirrors go/rust/c). Default
    # TESTS_RAN for older rows without an explicit status.
    print("repo,runtime,num_passed/num_tests,status")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        print(
            f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']},"
            f"{x.get('status', 'TESTS_RAN')}"
        )
    total_runtime = sum(x["sum"] for x in out)
    # An infra-broken / forged (CHEAT_DETECTED) / compile-failed run is NOT a
    # measured model score — its 0.0 must not drag the average down like a
    # genuine 0%. Average over SCORED repos only (mirrors evaluate_go.py).
    averaged_passed, _excluded, _scored = average_pass_rate(out, _EXCLUDED_STATUSES)
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    if _excluded:
        print(
            f"NOTE: {_excluded}/{len(out)} repo(s) EXCLUDED from the average "
            f"(infra-broken / compile-failed / cheat — not a measured model score)."
        )
    logger.info(
        "C++ evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__ = ["evaluate_single_repo", "main"]
