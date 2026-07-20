"""C stage-3 refine fix: a compile failure must surface as a non-zero exit.

RCA (real run, cJSON): the model implemented `ensure()` using SIZE_MAX without
`#include <stdint.h>`. The build failed, but the C test wrapper wrote
test_exit_code.txt=0 (tests never ran) and run_c_tests.main returned that 0.
aider's cmd_test only hands output to the model on a NON-ZERO exit, so stage-3
test-refine saw "all good", never called the model, and a one-line fix scored a
permanent 0/19.

Fix: run_c_tests surfaces a masked compile failure as exit 1 and prints the
extracted compiler diagnostics to stdout. Eval is unaffected (it scores from
test_report.xml + the compile-error count, and only treats exit codes outside
{0,1} specially).
"""
from commit0.harness.run_c_tests import (
    _extract_build_errors,
    _compile_failure_masked_as_zero,
)

# the exact gcc output that scored the real run 0/19
_CJSON_BUILD_OUTPUT = (
    "[1/46] Building C object CMakeFiles/cJSON_test.dir/test.c.o\n"
    "[2/46] Building C object CMakeFiles/cjson.dir/cJSON.c.o\n"
    "FAILED: CMakeFiles/cjson.dir/cJSON.c.o\n"
    "/tmp/tree/cJSON.c: In function 'ensure':\n"
    "/tmp/tree/cJSON.c:438:19: error: 'SIZE_MAX' undeclared (first use in this function)\n"
    "  438 |     if (needed > (SIZE_MAX - p->offset))\n"
    "ninja: build stopped: subcommand failed.\n"
    # a long tail so the error is NOT in the last 80 lines
    + "\n".join(f"note: line {i}" for i in range(200))
)


def test_extract_build_errors_finds_the_real_diagnostic():
    errs = _extract_build_errors(_CJSON_BUILD_OUTPUT)
    assert "SIZE_MAX' undeclared" in errs
    assert "438:19: error:" in errs
    # extraction is position-independent — the error is near the TOP of a long log
    assert "note: line 199" not in errs  # noise is not pulled in


def test_masked_compile_failure_detected_via_compile_errors_sentinel(tmp_path):
    (tmp_path / "compile_errors.txt").write_text("COMPILE_FAILED")
    # no test_report.xml present
    assert _compile_failure_masked_as_zero(tmp_path, "") is True


def test_masked_compile_failure_detected_via_build_errors_in_output(tmp_path):
    # compile_errors.txt absent, but the build output clearly failed and no report
    assert _compile_failure_masked_as_zero(tmp_path, _CJSON_BUILD_OUTPUT) is True


def test_clean_run_with_report_is_not_a_compile_failure(tmp_path):
    (tmp_path / "test_report.xml").write_text("<testsuite/>")
    (tmp_path / "compile_errors.txt").write_text("")  # empty
    assert _compile_failure_masked_as_zero(tmp_path, "19 passed") is False


def test_report_present_suppresses_output_based_detection(tmp_path):
    # tests DID run (report exists); stray "error:" text in output must not be
    # misread as a build failure that never produced a binary
    (tmp_path / "test_report.xml").write_text("<testsuite/>")
    assert _compile_failure_masked_as_zero(
        tmp_path, "test_foo: expected error: got 5"
    ) is False
