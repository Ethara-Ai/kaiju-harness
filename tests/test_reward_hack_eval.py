"""Reward-hacking defenses in the Rust result aggregator.

Covers the stdout-injection attack: a model whose code runs during `cargo test`
prints forged `test ... ok` lines or a fake `test result:` summary to inflate
the score. _detect_result_injection must catch every variant, and
_aggregate_rust_results must then score the run 0 (CHEAT_DETECTED).
"""

from __future__ import annotations

from commit0.harness.evaluate_rust import (
    _detect_result_injection,
    _aggregate_rust_results,
)


_CLEAN = """\
     Running unittests src/lib.rs (target/debug/deps/foo-abc)

running 2 tests
test tests::a ... ok
test tests::b ... ok

test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s
"""


def test_clean_output_not_flagged():
    assert _detect_result_injection(_CLEAN, 0, parsed_passed=2, parsed_total=2) == ""


def test_injected_ok_lines_detected():
    forged = _CLEAN.replace(
        "test tests::b ... ok\n",
        "test tests::b ... ok\ntest evil1 ... ok\ntest evil2 ... ok\n",
    )
    # Per-line ok count (4) now exceeds the libtest summary (2).
    reason = _detect_result_injection(forged, 0, parsed_passed=4, parsed_total=4)
    assert "exceeds libtest summary" in reason


def test_clean_output_with_nonzero_exit_not_flagged():
    """A genuine run where all parsed tests passed but cargo exited non-zero
    (e.g. a bench/doctest failed to compile, or a post-run coverage gate) must
    NOT be flagged — exit-code reconciliation was dropped to avoid this false
    positive; only structural forgery (injected lines/summaries) is flagged."""
    reason = _detect_result_injection(_CLEAN, exit_code=101,
                                      parsed_passed=2, parsed_total=2)
    assert reason == ""


def test_forged_summary_line_detected():
    forged = _CLEAN + "test result: ok. 99 passed; 0 failed; 0 ignored\n"
    # Two summaries but only one 'Running' binary.
    reason = _detect_result_injection(forged, 0, parsed_passed=2, parsed_total=101)
    assert "summaries but only" in reason


def test_aggregate_scores_injected_run_zero(tmp_path):
    """End-to-end: a forged test_output.txt is scored 0 with CHEAT_DETECTED."""
    forged = _CLEAN.replace(
        "test tests::b ... ok\n",
        "test tests::b ... ok\n" + "".join(
            f"test evil{i} ... ok\n" for i in range(20)),
    )
    log_dir = tmp_path
    (log_dir / "test_output.txt").write_text(forged)
    (log_dir / "cargo_test_exit_code.txt").write_text("101")  # real run failed

    out: list = []
    _aggregate_rust_results(str(log_dir), "evmap", out, expected_tests=None)
    assert out, "aggregator produced no result"
    r = out[0]
    assert r["status"] == "CHEAT_DETECTED"
    assert r["passed"] == 0.0  # reported pass-rate zeroed


def test_aggregate_clean_run_scores_normally(tmp_path):
    """A genuine passing run is unaffected by the injection guard."""
    log_dir = tmp_path
    (log_dir / "test_output.txt").write_text(_CLEAN)
    (log_dir / "cargo_test_exit_code.txt").write_text("0")

    out: list = []
    _aggregate_rust_results(str(log_dir), "foo", out, expected_tests=None)
    assert out and out[0]["status"] == "TESTS_RAN"
    assert out[0]["num_passed"] == 2 and out[0]["passed"] == 1.0


# --------------------------------------------------------------------------
# C++ stdout-injection defense (evaluate_cpp): the count comes from raw stdout
# (GTest [ OK ] lines), so it's anchored to the canonical count + exit code.
# --------------------------------------------------------------------------
import commit0.harness.evaluate_cpp as ecpp


def _gtest(n_ok: int) -> str:
    lines = ["[==========] Running tests from 1 test suite.",
             "[----------] Global test environment set-up.",
             "[----------] %d tests from T" % n_ok]
    for i in range(n_ok):
        lines.append("[ RUN      ] T.t%d" % i)
        lines.append("[       OK ] T.t%d (0 ms)" % i)
    lines.append("[----------] %d tests from T (1 ms total)" % n_ok)
    lines.append("[==========] %d tests from 1 test suite ran. (1 ms total)" % n_ok)
    lines.append("[  PASSED  ] %d tests." % n_ok)
    return "\n".join(lines) + "\n"


def test_cpp_forged_gtest_output_flagged(tmp_path, monkeypatch):
    # Canonical suite has 2 tests; forged output claims 10 passes.
    monkeypatch.setattr(ecpp, "_expected_test_count", lambda name: 2)
    (tmp_path / "test_output.txt").write_text(_gtest(10))
    (tmp_path / "test_exit_code.txt").write_text("1")  # real run failed
    out: list = []
    ecpp._aggregate_cpp_results(str(tmp_path), "widget", out)
    assert out and out[0].get("status") == "CHEAT_DETECTED"
    assert out[0]["passed"] == 0.0 and out[0]["num_passed"] == 0


def test_cpp_allpass_with_failure_exit_flagged(tmp_path, monkeypatch):
    # Reports exactly the canonical count as passed, but the process failed.
    monkeypatch.setattr(ecpp, "_expected_test_count", lambda name: 2)
    (tmp_path / "test_output.txt").write_text(_gtest(2))
    (tmp_path / "test_exit_code.txt").write_text("1")
    out: list = []
    ecpp._aggregate_cpp_results(str(tmp_path), "widget", out)
    assert out and out[0].get("status") == "CHEAT_DETECTED"


def test_cpp_clean_run_scores_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(ecpp, "_expected_test_count", lambda name: 2)
    (tmp_path / "test_output.txt").write_text(_gtest(2))
    (tmp_path / "test_exit_code.txt").write_text("0")  # genuine all-pass
    out: list = []
    ecpp._aggregate_cpp_results(str(tmp_path), "widget", out)
    assert out and out[0].get("status") == "TESTS_RAN"
    assert out[0]["num_passed"] == 2 and out[0]["passed"] == 1.0


def test_cpp_more_tests_than_stale_canonical_not_flagged(tmp_path, monkeypatch):
    """A legit run whose real suite exceeds a stale canonical bz2 (all passing,
    exit 0) must NOT be flagged — observed > canonical is benign."""
    monkeypatch.setattr(ecpp, "_expected_test_count", lambda name: 10)  # stale/small
    (tmp_path / "test_output.txt").write_text(_gtest(15))  # 15 real passes
    (tmp_path / "test_exit_code.txt").write_text("0")      # genuine all-pass
    out: list = []
    ecpp._aggregate_cpp_results(str(tmp_path), "widget", out)
    assert out and out[0].get("status") == "TESTS_RAN"
    assert out[0]["passed"] == 1.0  # not zeroed
