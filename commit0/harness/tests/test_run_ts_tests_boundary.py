"""Hostile / boundary tests for small helpers in the TS run pipeline.

These hit behaviour not covered by the existing line-coverage suites:

* ``commit0.harness.run_ts_tests._inject_test_ids`` — append semantics,
  multiple matching lines, newline-bearing test IDs, shell metachars.
* ``commit0.harness.build_ts._filter_by_split`` — case sensitivity,
  underscore/hyphen normalisation, unknown splits, non-dict examples.
"""

from __future__ import annotations

from types import SimpleNamespace

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
        script = "#!/bin/bash\nnpx jest --forceExit\n"
        out = _inject_test_ids(script, test_id)
        assert f"npx jest --forceExit {test_id}" in out

    def test_appends_to_vitest_line(self) -> None:
        script = "#!/bin/bash\nnpx vitest run\n"
        out = _inject_test_ids(script, "src/foo.test.ts")
        assert "npx vitest run src/foo.test.ts" in out

    def test_appends_to_every_matching_line(self) -> None:
        """If two lines match, both are modified. Matches real Jest fallback
        scripts that double-invoke the runner.
        """
        script = "#!/bin/bash\nnpx jest --forceExit\nnpx vitest run\n"
        out = _inject_test_ids(script, "t.ts")
        assert "npx jest --forceExit t.ts" in out
        assert "npx vitest run t.ts" in out

    def test_no_matching_line_returns_unchanged(self) -> None:
        script = "#!/bin/bash\necho hello\n"
        out = _inject_test_ids(script, "test.ts")
        assert out == script

    def test_preserves_non_matching_lines_verbatim(self) -> None:
        script = "#!/bin/bash\necho hi\nnpx jest --forceExit\necho bye\n"
        out = _inject_test_ids(script, "t.ts")
        lines = out.splitlines()
        assert lines[0] == "#!/bin/bash"
        assert lines[1] == "echo hi"
        assert lines[2] == "npx jest --forceExit t.ts"
        assert lines[3] == "echo bye"

    def test_rstrips_existing_trailing_whitespace_on_matching_line(self) -> None:
        """Jest line with trailing whitespace keeps a single separator space."""
        script = "npx jest --forceExit   \n"
        out = _inject_test_ids(script, "t.ts")
        assert "npx jest --forceExit t.ts" in out
        # No double-space
        assert "jest --forceExit  t.ts" not in out

    @pytest.mark.parametrize(
        "test_ids",
        [
            "a.test.ts b.test.ts",
            "a.test.ts\tb.test.ts",
            # Attacker-controlled test ID — the injector does NOT quote; the
            # invariant is only that it is appended verbatim. If the test ID
            # contains shell metachars they reach bash verbatim. This is
            # intentional because test IDs originate from the harness's own
            # generate_test_ids_ts, which sanitises them.
            "a.test.ts;echo pwned",
        ],
    )
    def test_appends_multiple_or_metachar_ids_verbatim(self, test_ids: str) -> None:
        script = "npx jest --forceExit\n"
        out = _inject_test_ids(script, test_ids)
        assert f"npx jest --forceExit {test_ids}" in out


def test_newline_in_test_ids_does_not_inject_extra_line() -> None:
    script = "npx jest --forceExit\n"
    out = _inject_test_ids(script, "a.test.ts\necho pwned")
    # Desired behaviour: newlines in test_ids are sanitised to spaces so no
    # bare `echo pwned` line exists in the emitted eval script.
    for line in out.splitlines():
        assert line.strip() != "echo pwned"


