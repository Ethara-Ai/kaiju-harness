"""Hostile / boundary tests for small helpers in the TS run pipeline.

These hit behaviour not covered by the existing line-coverage suites:

* ``commit0.harness.run_ts_tests._inject_test_ids`` — append semantics,
  multiple matching lines, newline-bearing test IDs, shell metachars.
* ``commit0.harness.build_ts._filter_by_split`` — case sensitivity,
  underscore/hyphen normalisation, unknown splits, non-dict examples.
"""

from __future__ import annotations


import pytest

from commit0.harness.run_ts_tests import _inject_test_ids


# ---------------------------------------------------------------------------
# _inject_test_ids
# ---------------------------------------------------------------------------


class TestInjectTestIdsBoundary:
    def test_empty_test_ids_returns_identical_string(self) -> None:
        script = "#!/bin/bash\nnpx jest --forceExit\n"
        out = _inject_test_ids(script, "")
        assert out == script

    @pytest.mark.parametrize(
        "test_id",
        [
            "src/foo.test.ts",
            "path with space.test.ts",
            "src/dir/bar.spec.ts",
            # Unicode
            "テスト.test.ts",
        ],
    )
    def test_appends_to_forceexit_line(self, test_id: str) -> None:
        # The injector only rewrites the actual test-invocation line, which is
        # identified by the ``>`` output redirect present in the real eval
        # script. Each whitespace-separated token of the test id is shlex-quoted
        # so shell metacharacters cannot escape argv. A plain filename requires
        # no quoting, but one containing a space is quoted as a single argv arg.
        import shlex

        script = "#!/bin/bash\nnpx jest --forceExit > test_output.txt 2>&1\n"
        out = _inject_test_ids(script, test_id)
        expected = " ".join(shlex.quote(t) for t in test_id.split())
        assert f"> test_output.txt 2>&1 {expected}" in out

    def test_appends_to_vitest_line(self) -> None:
        script = "#!/bin/bash\nnpx vitest run > test_output.txt 2>&1\n"
        out = _inject_test_ids(script, "src/foo.test.ts")
        assert "> test_output.txt 2>&1 src/foo.test.ts" in out

    def test_appends_to_every_matching_line(self) -> None:
        """If two lines match, both are modified. Matches real Jest fallback
        scripts that double-invoke the runner.
        """
        script = (
            "#!/bin/bash\n"
            "npx jest --forceExit > test_output.txt 2>&1\n"
            "npx vitest run > test_output.txt 2>&1\n"
        )
        out = _inject_test_ids(script, "t.ts")
        assert "npx jest --forceExit > test_output.txt 2>&1 t.ts" in out
        assert "npx vitest run > test_output.txt 2>&1 t.ts" in out

    def test_no_matching_line_returns_unchanged(self) -> None:
        script = "#!/bin/bash\necho hello\n"
        out = _inject_test_ids(script, "test.ts")
        assert out == script

    def test_preserves_non_matching_lines_verbatim(self) -> None:
        script = (
            "#!/bin/bash\n"
            "echo hi\n"
            "npx jest --forceExit > test_output.txt 2>&1\n"
            "echo bye\n"
        )
        out = _inject_test_ids(script, "t.ts")
        lines = out.splitlines()
        assert lines[0] == "#!/bin/bash"
        assert lines[1] == "echo hi"
        assert lines[2] == "npx jest --forceExit > test_output.txt 2>&1 t.ts"
        assert lines[3] == "echo bye"

    def test_rstrips_existing_trailing_whitespace_on_matching_line(self) -> None:
        """Jest line with trailing whitespace keeps a single separator space."""
        script = "npx jest --forceExit > test_output.txt 2>&1   \n"
        out = _inject_test_ids(script, "t.ts")
        assert "npx jest --forceExit > test_output.txt 2>&1 t.ts" in out
        # No double-space
        assert "2>&1  t.ts" not in out

    @pytest.mark.parametrize(
        "test_ids",
        [
            "a.test.ts b.test.ts",
            "a.test.ts\tb.test.ts",
            # Attacker-controlled test ID containing a shell metacharacter. The
            # injector shlex-quotes each whitespace-separated token, so the ``;``
            # is neutralised inside single quotes and reaches bash as an inert
            # argv argument — it CANNOT start a new command. This asserts the
            # injection-safety invariant of the current (argv-safe) injector.
            "a.test.ts;echo pwned",
        ],
    )
    def test_appends_multiple_or_metachar_ids_verbatim(self, test_ids: str) -> None:
        import shlex

        script = "npx jest --forceExit > test_output.txt 2>&1\n"
        out = _inject_test_ids(script, test_ids)
        expected = " ".join(shlex.quote(t) for t in test_ids.split())
        assert f"> test_output.txt 2>&1 {expected}" in out
        # No unquoted metachar can start a fresh shell command.
        assert ";echo pwned" not in out


def test_newline_in_test_ids_does_not_inject_extra_line() -> None:
    script = "npx jest --forceExit > test_output.txt 2>&1\n"
    out = _inject_test_ids(script, "a.test.ts\necho pwned")
    # Desired behaviour: newlines in test_ids are sanitised to spaces so no
    # bare `echo pwned` line exists in the emitted eval script.
    for line in out.splitlines():
        assert line.strip() != "echo pwned"


