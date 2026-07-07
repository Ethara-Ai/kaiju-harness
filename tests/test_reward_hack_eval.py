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


def test_claimed_allpass_with_failure_exit_detected():
    reason = _detect_result_injection(_CLEAN, exit_code=101,
                                      parsed_passed=2, parsed_total=2)
    assert "exited 101" in reason


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
