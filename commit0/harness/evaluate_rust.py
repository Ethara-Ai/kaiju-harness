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
OUTCOME_INFRA_FETCH_FAILED = "INFRA_FETCH_FAILED"  # A10: cargo couldn't fetch deps (network) — NOT a model failure

# Sniffing patterns for outcome classification.
_COMPILE_ERR_RE = re.compile(r"^error(?:\[E\d+\])?:", re.MULTILINE)
# cargo emits these AFTER the test phase completes; they are NOT rustc compile
# diagnostics and must not be misattributed as COMPILE_FAILED. `error: could
# not compile` is intentionally NOT excluded — that one IS a build failure.
_RUNTIME_ERR_RE = re.compile(
    r"^error:\s*(?:test failed|bench failed|build failed|\d+\s+tests?\s+failed)",
    re.MULTILINE,
)
_RUNNING_ZERO_RE = re.compile(r"^running\s+0\s+tests\s*$", re.MULTILINE)
_PATCH_FAIL_SENTINEL = "PATCH APPLY FAILED"
# Cheat-guard sentinel written by the eval script when the model edited in-`src`
# test code (see run_rust_tests). Scored runs carrying it are flagged, not passed.
_CHEAT_SENTINEL = "CHEAT_DETECTED"
# A10: written by the eval script when cargo failed to fetch dependencies
# (network/registry). Such a run is infra-broken, NOT a real compile/test 0%.
_FETCH_FAIL_SENTINEL = "INFRA_FETCH_FAILED"
# `cargo test --list` reports doctests as `path/file.rs - some::Item (line N): test`.
# The libtest TEXT parser only counts unit/integration `test <name> ... ok` lines —
# the doctest binary's runner uses a different format the parser cannot read. So a
# doctest-inclusive denominator vs a doctest-blind numerator caps a PERFECT solution
# below 1.0 (e.g. 17/35=0.486). Drop doctests from the canonical inventory so the
# denominator matches what the numerator can actually observe.
# Anchor to the actual `cargo test --list` doctest format:
#   `path/to/file.rs - some::Item (line N): test`
# Requiring a `.rs ` path prefix avoids false-dropping a legit unit/integration
# test whose NAME merely contains ` - ` and `(line N)` (those have no `.rs` path).
_DOCTEST_INVENTORY_RE = re.compile(r"^\S+\.rs\s+-\s+.+\(line\s+\d+\)")


# A model whose code runs during `cargo test` can print to the same stdout the
# parser reads. It could forge `test <name> ... ok` lines or a fake
# `test result: N passed` summary to inflate the score. These are the invariants
# a genuine libtest run always satisfies; a violation means the output was
# tampered with -> the run is scored 0 (CHEAT_DETECTED), never trusted.
_SUMMARY_PASSED_RE = re.compile(r"test\s+result:.*?(\d+)\s+passed", re.IGNORECASE)
_RUNNING_BIN_RE = re.compile(r"^\s*Running\b", re.MULTILINE)
_DOCTESTS_RE = re.compile(r"^\s*Doc-tests\b", re.MULTILINE)


def _detect_result_injection(
    content: str, exit_code: int | None, parsed_passed: int, parsed_total: int
) -> str:
    """Return a reason string if the test output looks forged, else ''.

    Uses only STRUCTURAL libtest invariants that a genuine run never violates —
    so it does not false-flag legitimate runs:
      (1) the number of `test ... ok` lines equals the sum of the per-binary
          `test result: N passed` summaries — injected `ok` lines break this;
      (2) there is exactly one `test result:` summary per test binary / doc-test
          run — an extra summary line is a forged one.

    NB: an exit-code vs claimed-pass reconciliation was deliberately NOT used
    here: `cargo test` can exit non-zero for reasons unrelated to a test failure
    (a bench/doctest that fails to compile, a post-run lint/coverage gate) while
    every unit test genuinely passed, so keying CHEAT on `exit!=0 && all-passed`
    would zero legitimate runs. Exit-code handling stays in the caller's
    timeout/compile classification.
    """
    text = content or ""
    summaries = [int(m) for m in _SUMMARY_PASSED_RE.findall(text)]
    summary_passed = sum(summaries)
    n_bins = len(_RUNNING_BIN_RE.findall(text)) + len(_DOCTESTS_RE.findall(text))
    # (2) more summaries than test binaries + doc-test runs -> forged summary.
    if n_bins and len(summaries) > n_bins:
        return (f"{len(summaries)} 'test result:' summaries but only {n_bins} test "
                f"binaries ran (forged summary line)")
    # (1) more per-line "ok" than libtest actually summarised -> injected `ok`.
    # BUT only when every binary that ran produced a summary. If a binary
    # hard-aborts (SIGSEGV/abort) it prints its `ok` lines with NO summary, so
    # per-line > summary is then benign (a crash, not injection) — don't flag it.
    _binary_missing_summary = n_bins > 0 and len(summaries) < n_bins
    if summary_passed and parsed_passed > summary_passed and not _binary_missing_summary:
        return (f"per-line passed={parsed_passed} exceeds libtest summary "
                f"total={summary_passed} (injected 'test ... ok' lines)")
    return ""


def _read_exit_code(log_dir: str) -> int | None:
    """Return cargo exit code if `cargo_test_exit_code.txt` is readable, else None."""
    p = os.path.join(log_dir, "cargo_test_exit_code.txt")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read().strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _count_compile_errors(content: str) -> int:
    """Count rustc compile-error lines, excluding cargo's post-test runtime
    `error:` summary lines (e.g. `error: test failed`) which would otherwise be
    misattributed as a build failure."""
    total = len(_COMPILE_ERR_RE.findall(content))
    runtime = len(_RUNTIME_ERR_RE.findall(content))
    return max(0, total - runtime)

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
            ids = [line.strip() for line in f if line.strip()]
    except (OSError, EOFError) as e:
        logger.debug("rust_test_ids missing for %s (%s): %s", repo_name, p, e)
        return None
    # Exclude doctests so the denominator matches the doctest-blind numerator.
    unit = [i for i in ids if not _DOCTEST_INVENTORY_RE.search(i)]
    dropped = len(ids) - len(unit)
    if dropped:
        logger.info(
            "%s: dropped %d doctest entries from canonical inventory (%d unit tests remain)",
            repo_name, dropped, len(unit),
        )
    return unit



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
    # A10: a fetch/network failure must be classified BEFORE COMPILE_FAILED — a
    # failed dep download also emits `error:` lines that would otherwise be
    # miscounted as the model's compile errors and scored as a real 0%.
    if _FETCH_FAIL_SENTINEL in content:
        return (OUTCOME_INFRA_FETCH_FAILED,
                "cargo could not fetch dependencies (network/registry) — infra, not a model failure")
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
        # A8: the canonical inventory is collected at the reference_commit while
        # eval runs at the patched base. If `#[cfg]`/feature gating differs between
        # the two (e.g. a feature toggled by an un-reverted Cargo.toml, or a
        # platform cfg), the observed test set diverges from the inventory. The
        # extracted-flags + Cargo.toml-revert guards make this rare, but surface it
        # loudly when observed != canonical so a silent denominator drift is caught.
        # Only warn when we observed FEWER tests than the canonical inventory — that
        # means tests that should exist didn't run (cfg/feature-gated out, a real
        # drift symptom). observed > canonical is EXPECTED and benign (e.g. the
        # inventory drops doctests but the live run may print some), so it must not
        # trigger a spurious "drift" warning.
        if canonical_total is not None and observed_total < canonical_total:
            logger.warning(
                "%s: A8 possible cfg/feature drift — observed only %d tests but the "
                "canonical inventory has %d (reference_commit vs patched base); some "
                "tests may be gated out. num_tests uses the canonical total.",
                name, observed_total, canonical_total,
            )
        # Guard against a stale/short canonical inventory: the total can never be
        # below what we actually observed, and passes can never exceed the total.
        # Without this, a smaller bz2 than the live run yields passed_rate > 1.0
        # and a negative "failed_or_missing" in status_detail.
        num_tests = max(num_tests, observed_total)
        num_passed = min(num_passed, num_tests)
        num_failed = summary.get("failed", 0)
        total_runtime = sum(t.get("duration", 0) for t in tests)
        passed_rate = num_passed / num_tests if num_tests > 0 else 0.0
        # Even when tests ran, check exit code to flag if a timeout cut the suite
        # short — observed_total is a lower bound; canonical_total (when present)
        # is the authoritative ceiling.
        exit_code = _read_exit_code(log_dir)
        # Read raw output once for sentinel checks (timeout / cheat).
        try:
            _raw = open(test_output_file, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            _raw = ""
        status = OUTCOME_TESTS_RAN
        # Only 124 (timeout) / 137 (SIGKILL from --kill-after) mean OUR inner
        # `timeout` cut the suite short. 143 (SIGTERM) is dropped here: a run that
        # already produced parseable test results was NOT mid-run-killed by us, and
        # 143 can come from unrelated causes — zeroing it would discard a real run.
        if exit_code in (124, 137):
            # The suite was KILLED mid-run; a partial pass count is NOT a score.
            status = OUTCOME_TEST_SUITE_TIMEOUT
            logger.warning(
                "%s: suite killed by timeout (exit %s) after %d/%d — NOT scored as TESTS_RAN",
                name, exit_code, num_passed, num_tests,
            )
        elif _CHEAT_SENTINEL in _raw:
            status = "CHEAT_DETECTED"
            logger.warning("%s: CHEAT_DETECTED — model edited in-src test code; flagging", name)
        elif _detect_result_injection(_raw, exit_code, num_passed, observed_total):
            status = "CHEAT_DETECTED"
            logger.warning(
                "%s: CHEAT_DETECTED — forged test output: %s", name,
                _detect_result_injection(_raw, exit_code, num_passed, observed_total),
            )
        elif _FETCH_FAIL_SENTINEL in _raw:
            status = OUTCOME_INFRA_FETCH_FAILED
            logger.warning("%s: INFRA_FETCH_FAILED — cargo couldn't fetch deps; NOT scored as a model failure", name)
        elif num_failed > 0:
            logger.info(
                "%s: TESTS_RAN — %d passed, %d failed of %d total",
                name, num_passed, num_failed, num_tests,
            )
        else:
            logger.info(
                "%s: TESTS_RAN — %d/%d passed", name, num_passed, num_tests,
            )
        if status == OUTCOME_TEST_SUITE_TIMEOUT:
            status_detail = f"killed by timeout after {num_passed}/{num_tests}; pass rate NOT valid"
        elif status == "CHEAT_DETECTED":
            status_detail = f"in-src test code modified; {num_passed}/{num_tests} NOT trusted"
        elif status == OUTCOME_INFRA_FETCH_FAILED:
            status_detail = f"dependency fetch failed (infra); {num_passed}/{num_tests} NOT a valid score"
        elif canonical_total is not None:
            status_detail = f"{num_passed}/{num_tests} passed, {num_tests - num_passed} failed_or_missing"
        else:
            status_detail = f"{num_passed}/{num_tests} passed, {num_failed} failed"
        # A killed/cheating/infra-broken run must not contribute a "valid" pass rate.
        reported_rate = 0.0 if status in (
            OUTCOME_TEST_SUITE_TIMEOUT, "CHEAT_DETECTED", OUTCOME_INFRA_FETCH_FAILED
        ) else passed_rate
        out.append({
            "name": name,
            "sum": total_runtime,
            "passed": reported_rate,
            "num_passed": num_passed,
            "num_tests": num_tests,
            "status": status,
            "status_detail": status_detail,
        })
        return

    try:
        with open(test_output_file, "r", encoding="utf-8", errors="replace") as f:
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
        # Carry repo_name explicitly. Reconstructing it from the path via
        # basename(dirname(dirname(...))) breaks for branches containing '/'
        # (e.g. 'fix/bug' adds a path component and yields 'fix').
        log_dirs.append((str(log_dir), repo_name))
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
    for log_path, log_name in tqdm(log_dirs):
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
    # A10/A11: an infra-broken / timed-out / cheating run is NOT a measured model
    # score — its 0.0 must NOT drag the average down like a genuine 0%. Average
    # over SCORED repos only; report how many were excluded so a run that is
    # mostly infra-broken can't masquerade as a real low score.
    _EXCLUDED_STATUSES = {
        OUTCOME_INFRA_FETCH_FAILED, OUTCOME_TEST_SUITE_TIMEOUT, "CHEAT_DETECTED",
    }
    scored = [x for x in out if x.get("status") not in _EXCLUDED_STATUSES]
    excluded = len(out) - len(scored)
    averaged_passed = sum(x["passed"] for x in scored) / len(scored) if scored else 0.0
    print(f"total runtime: {total_runtime}")
    print(f"average pass rate: {averaged_passed}")
    if excluded:
        print(
            f"NOTE: {excluded}/{len(out)} repo(s) EXCLUDED from the average "
            f"(infra-broken / timeout / cheat — not a measured model score)."
        )

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
