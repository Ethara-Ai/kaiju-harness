"""Rust evaluation pipeline — mirrors ``evaluate.py`` for Rust repositories.

Uses ``run_rust_tests.main`` as the per-repo test runner and parses
cargo/nextest output for result aggregation.
"""

import bz2
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator, Union

from tqdm import tqdm

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_rust import (
    RUST_SPLIT,
    RUN_RUST_TESTS_LOG_DIR,
)
from commit0.harness.run_rust_tests import main as run_rust_tests
from commit0.harness.rust_test_parser import parse_nextest_report
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

# Cargo test summary line: ``test result: ok. 5 passed; 0 failed; 1 ignored; 0 measured...``
# Robust to format drift via lookahead-style counts; only requires the three counters appear in order.
_TEST_SUMMARY_RE = re.compile(
    r"test\s+result:.*?(?P<passed>\d+)\s+passed.*?(?P<failed>\d+)\s+failed.*?(?P<ignored>\d+)\s+ignored",
    re.IGNORECASE,
)

# Failure-attribution constants. Surfaced via the `status` field on each entry
# in the aggregated `out` list so downstream summarisers can distinguish:
#   - REAL test failures (TESTS_RAN with num_tests>0)
#   - Pipeline failures the harness must NOT report as 0/0 silently
OUTCOME_TESTS_RAN = "TESTS_RAN"              # `cargo test` reached the test phase (any pass/fail)
OUTCOME_COMPILE_FAILED = "COMPILE_FAILED"    # rustc/cargo build error; tests never ran
OUTCOME_PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"  # eval.sh couldn't apply patch.diff
OUTCOME_TEST_SUITE_TIMEOUT = "TEST_SUITE_TIMEOUT"  # killed by `timeout` (or watchdog) before finish
OUTCOME_NO_TESTS_DEFINED = "NO_TESTS_DEFINED"      # `cargo test` ran but `running 0 tests`
OUTCOME_OUTPUT_MISSING = "OUTPUT_MISSING"          # test_output.txt absent / unreadable
OUTCOME_PARSER_NO_MATCH = "PARSER_NO_MATCH"        # content present but doesn't match known formats

# Sniffing patterns for outcome classification.
_COMPILE_ERR_RE = re.compile(r"^error(?:\[E\d+\])?:", re.MULTILINE)
_RUNNING_ZERO_RE = re.compile(r"^running\s+0\s+tests\s*$", re.MULTILINE)
_PATCH_FAIL_SENTINEL = "PATCH APPLY FAILED"


def _read_exit_code(log_dir: str) -> int | None:
    """Return cargo exit code if `cargo_test_exit_code.txt` is readable, else None."""
    p = os.path.join(log_dir, "cargo_test_exit_code.txt")
    try:
        with open(p, "r") as f:
            raw = f.read().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _count_compile_errors(content: str) -> int:
    """Count rustc `error[E....]:` lines (compile-error count)."""
    return len(_COMPILE_ERR_RE.findall(content))

def _load_rust_test_ids(repo_name: str) -> list[str] | None:
    """Load the authoritative test inventory for *repo_name* from .bz2.

    Returns the test IDs collected at dataset-prep time via `cargo test --list`,
    or None if the file is missing/unreadable. This is the source of truth for
    total test count — independent of how many tests cargo actually ran (which
    can be cut short by a `timeout` kill). Mirrors `evaluate_go.py`'s use of
    `test_ids_flat` as the canonical total.
    """
    p = os.path.join(
        os.path.dirname(__file__), "..", "data", "rust_test_ids", f"{repo_name}.bz2"
    )
    try:
        with bz2.open(p, "rt") as f:
            return [line.strip() for line in f if line.strip()]
    except (OSError, EOFError) as e:
        logger.debug("rust_test_ids missing for %s (%s): %s", repo_name, p, e)
        return None



def _classify_eval_outcome(log_dir: str, content: str) -> tuple[str, str]:
    """Classify the eval outcome from cargo exit code and test_output.txt content.

    Returns ``(outcome_constant, human_readable_detail)``. The classification is
    ordered by specificity: patch-apply failures are detected first (they short-
    circuit eval.sh before `cargo` even runs), then compile failures (exit 101 +
    rustc error lines), then timeout signals (exit 124/137 from `timeout`'s
    SIGTERM/SIGKILL escalation), then the `running 0 tests` empty-suite case,
    then a final unknown fallback."""
    if _PATCH_FAIL_SENTINEL in content[:4096]:
        return (OUTCOME_PATCH_APPLY_FAILED,
                "patch failed to apply — check git_apply_stderr.log")
    exit_code = _read_exit_code(log_dir)
    n_compile_errors = _count_compile_errors(content)
    # Compile failure: rustc exit 101 with error lines, OR error lines visible even
    # if exit code is missing (we still want the attribution).
    if n_compile_errors > 0 and (exit_code in (101, None) or exit_code != 0):
        return (OUTCOME_COMPILE_FAILED,
                f"cargo build failed with {n_compile_errors} compile error(s)")
    # `timeout` exits 124 on SIGTERM, 137 on SIGKILL (128 + 9). Wrapper may also
    # surface 143 (128 + 15 = SIGTERM-from-shell) on some configurations.
    if exit_code in (124, 137, 143):
        return (OUTCOME_TEST_SUITE_TIMEOUT,
                f"test suite killed by timeout (exit {exit_code}); "
                f"raise EVAL_TEST_TIMEOUT if this is a legitimate slow suite")
    if _RUNNING_ZERO_RE.search(content):
        return (OUTCOME_NO_TESTS_DEFINED,
                "cargo ran 0 tests — suite is empty for this configuration")
    return (OUTCOME_PARSER_NO_MATCH,
            f"test_output.txt present (exit={exit_code}) but no recognised format")


def _aggregate_rust_results(
    log_dir: str,
    name: str,
    out: list,
    expected_tests: list[str] | None = None,
) -> None:
    """Parse Rust test results from *log_dir* and append a summary dict to *out*.

    Looks for ``test_output.txt`` (cargo/nextest output) in the log directory.
    Tries the modern parser first (JSON nextest OR libtest text); on no-match,
    classifies the underlying failure (compile, patch-apply, timeout, etc.) via
    `_classify_eval_outcome` so the pipeline reports WHY tests didn't run
    instead of a bare \"no summary\" warning.

    When ``expected_tests`` is provided (the curated `.bz2` inventory captured
    at dataset-prep time via `cargo test --list`), its length is used as the
    authoritative ``num_tests``. Otherwise we fall back to the parser-observed
    total. Treating the bz2 count as canonical means a timeout-killed suite
    correctly reports e.g. \"51/63 passed\" not the misleading \"51/53 passed\"
    where 53 was just how many tests cargo finished before the kill."""
    canonical_total = len(expected_tests) if expected_tests is not None else None
    test_output_file = os.path.join(log_dir, "test_output.txt")
    if not os.path.exists(test_output_file):
        logger.warning(
            "%s: %s — test_output.txt missing at %s",
            name, OUTCOME_OUTPUT_MISSING, log_dir,
        )
        out.append({
            "name": name,
            "sum": 0,
            "passed": 0,
            "num_passed": 0,
            "num_tests": canonical_total or 0,
            "status": OUTCOME_OUTPUT_MISSING,
            "status_detail": "test_output.txt missing",
        })
        return

    report = parse_nextest_report(test_output_file)
    tests = report.get("tests", [])
    summary = report.get("summary", {})

    if tests:
        num_passed = summary.get("passed", 0)
        observed_total = summary.get("total", 0)
        num_tests = canonical_total if canonical_total is not None else observed_total
        num_failed = summary.get("failed", 0)
        total_runtime = sum(t.get("duration", 0) for t in tests)
        passed_rate = num_passed / num_tests if num_tests > 0 else 0.0
        # Even when tests ran, check exit code to flag if a timeout cut the suite
        # short — observed_total is a lower bound; canonical_total (when present)
        # is the authoritative ceiling.
        exit_code = _read_exit_code(log_dir)
        if exit_code in (124, 137, 143):
            logger.warning(
                "%s: %d/%d tests recovered but suite was killed by timeout "
                "(exit %s) — actual total is likely higher",
                name, num_passed, num_tests, exit_code,
            )
        elif num_failed > 0:
            logger.info(
                "%s: TESTS_RAN — %d passed, %d failed of %d total",
                name, num_passed, num_failed, num_tests,
            )
        else:
            logger.info(
                "%s: TESTS_RAN — %d/%d passed", name, num_passed, num_tests,
            )
        if canonical_total is not None:
            status_detail = f"{num_passed}/{num_tests} passed, {num_tests - num_passed} failed_or_missing"
        else:
            status_detail = f"{num_passed}/{num_tests} passed, {num_failed} failed"
        out.append({
            "name": name,
            "sum": total_runtime,
            "passed": passed_rate,
            "num_passed": num_passed,
            "num_tests": num_tests,
            "status": OUTCOME_TESTS_RAN,
            "status_detail": status_detail,
        })
        return

    try:
        with open(test_output_file, "r") as f:
            content = f.read()
    except OSError as exc:
        logger.warning("%s: failed to read %s: %s", name, test_output_file, exc)
        out.append({
            "name": name,
            "sum": 0,
            "passed": 0,
            "num_passed": 0,
            "num_tests": canonical_total or 0,
            "status": OUTCOME_OUTPUT_MISSING,
            "status_detail": f"read error: {exc}",
        })
        return

    # No test results extracted by the modern parser. Classify the underlying
    # cause from exit code + content sentinels so the user sees what actually
    # happened (compile failure, patch failure, timeout, etc.).
    outcome, detail = _classify_eval_outcome(log_dir, content)
    if outcome == OUTCOME_TESTS_RAN:  # defensive — shouldn't happen with empty parser results
        outcome = OUTCOME_PARSER_NO_MATCH
    logger.warning("%s: %s — %s (see %s)", name, outcome, detail, test_output_file)
    out.append({
        "name": name,
        "sum": 0,
        "passed": 0,
        "num_passed": 0,
        "num_tests": canonical_total or 0,
        "status": outcome,
        "status_detail": detail,
    })


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
    """Evaluate Rust repositories by running tests and aggregating results."""
    split_dict = RUST_SPLIT
    log_base_dir = RUN_RUST_TESTS_LOG_DIR

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

    # Resolve repo_split to a set of dataset repo names to evaluate.
    #
    # Semantics:
    #   - 'all' + RUST_SPLIT populated  → union of curated splits, intersected with dataset
    #   - 'all' + RUST_SPLIT empty      → every repo in the dataset (default for ad-hoc
    #                                     local datasets where RUST_SPLIT isn't seeded)
    #   - <curated-split-name>          → that curated subset, intersected with dataset
    #   - <repo-name>                   → single repo, matched with hyphen/underscore
    #                                     normalisation ('foo-bar' equivalent to 'foo_bar')
    def _normalize(name: str) -> str:
        return name.replace("-", "_")

    dataset_repo_names = {ex["repo"].split("/")[-1] for ex in dataset_list}

    if repo_split == "all":
        if split_dict:
            curated = {r.split("/")[-1] for rs in split_dict.values() for r in rs}
            rust_repo_names = dataset_repo_names & curated
            if not rust_repo_names:
                # Curated splits don't overlap this dataset (common for custom
                # datasets registered with the local pipeline). Fall back to
                # evaluating every entry.
                rust_repo_names = dataset_repo_names
        else:
            # RUST_SPLIT is populated at runtime by dataset loaders; when empty
            # (the default for ad-hoc datasets) every dataset entry is in-scope.
            rust_repo_names = dataset_repo_names
    elif repo_split in split_dict:
        curated = {r.split("/")[-1] for r in split_dict[repo_split]}
        rust_repo_names = dataset_repo_names & curated
    else:
        # Treat repo_split as a single repo name; allow hyphen/underscore equivalence.
        target = _normalize(repo_split)
        rust_repo_names = {n for n in dataset_repo_names if _normalize(n) == target}

    triples = []
    log_dirs = []
    for example in dataset_list:
        repo_name = example["repo"].split("/")[-1]
        if repo_name not in rust_repo_names:
            continue

        # Hash the SAME string we pass to run_rust_tests as test_ids — otherwise
        # the writer (run_rust_tests) and the reader (this aggregator) compute
        # different log-dir paths and we silently report '0/0 passed' for runs
        # that actually wrote results. Previously evaluate_rust hashed
        # example['test']['test_dir'] while run_rust_tests hashed the test_ids
        # argument (always empty string), so 17/17 passing runs were reported as
        # 0/0 because the aggregator read from the wrong directory.
        test_ids = ""  # full suite; no per-test filter applied for Rust runs
        hashed_test_ids = get_hash_string(test_ids)
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
            (example["repo"], "", repo_branch)
        )

    if not triples:
        logger.error(
            "No Rust repos matched repo_split=%r in dataset with %d entries. "
            "Check .commit0.yaml repo_split matches Rust repo names in RUST_SPLIT.",
            repo_split,
            len(dataset_list),
        )
        return

    logger.info(
        "Evaluating %d Rust repo(s) out of %d dataset entries",
        len(triples),
        len(dataset_list),
    )

    with tqdm(total=len(triples), smoothing=0, desc="Evaluating Rust repos") as pbar:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {}
            for repo, test_ids, repo_branch in triples:
                future = executor.submit(
                    run_rust_tests,
                    dataset_name,
                    dataset_split,
                    base_dir,
                    repo,
                    repo_branch,
                    test_ids,
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
                    future.result()
                except SystemExit as e:
                    if e.code not in (0, 1):
                        logger.warning(
                            "Rust evaluation for %s exited with code %s",
                            repo_name,
                            e.code,
                        )
                except Exception as e:
                    logger.error(
                        "Rust evaluation failed for %s: %s", repo_name, e, exc_info=True
                    )

    out = []
    for log_path in tqdm(log_dirs):
        log_name = os.path.basename(os.path.dirname(os.path.dirname(log_path)))
        if not log_name:
            log_name = log_path.split("/")[2] if len(log_path.split("/")) > 2 else "unknown"
        expected_tests = _load_rust_test_ids(log_name)
        _aggregate_rust_results(log_path, log_name, out, expected_tests=expected_tests)

    print("repo,runtime,num_passed/num_tests,status,detail")
    out = sorted(out, key=lambda x: x["sum"], reverse=True)
    for x in out:
        status = x.get("status", "")
        detail = x.get("status_detail", "")
        print(
            f"{x['name']},{x['sum']},{x['num_passed']}/{x['num_tests']},{status},{detail}"
        )
    total_runtime = sum(x["sum"] for x in out)
    averaged_passed = sum(x["passed"] for x in out) / len(out) if out else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")

    # Status breakdown — lets the reader see at a glance whether 0/0 means
    # "compile failed", "patch failed", "timeout", or "genuinely zero tests".
    status_counts: dict = {}
    for x in out:
        s = x.get("status", "UNCLASSIFIED")
        status_counts[s] = status_counts.get(s, 0) + 1
    if status_counts:
        print("status breakdown: " + ", ".join(
            f"{k}={v}" for k, v in sorted(status_counts.items())
        ))

    logger.info(
        "Rust evaluation complete: %d repos, avg pass rate %.2f%%, total runtime %.1fs",
        len(out),
        averaged_passed * 100,
        total_runtime,
    )


__all__ = []
