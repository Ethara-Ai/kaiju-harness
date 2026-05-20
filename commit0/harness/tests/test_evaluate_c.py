"""Pure-logic tests for commit0.harness.evaluate_c.

Only the deterministic compile-error counter is exercised here; the Docker
orchestration in ``main`` is out of scope for unit testing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.evaluate_c import _read_compile_errors_count


class TestReadCompileErrorsCount:
    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        assert _read_compile_errors_count(str(tmp_path)) == 0

    def test_empty_file_returns_zero(self, tmp_path: Path) -> None:
        (tmp_path / "compile_errors.txt").write_text("")
        assert _read_compile_errors_count(str(tmp_path)) == 0

    def test_counts_only_matching_lines(self, tmp_path: Path) -> None:
        content = "\n".join(
            [
                "main.c:10:5: error: expected ';'",
                "this is just informational noise",
                "error: something went wrong",
                "/usr/bin/ld: undefined reference to `foo'",
                "collect2: ld returned 1 exit status",
                "COMPILE_FAILED",
                "PATCH_APPLY_FAILED",
                "all good here",
            ]
        )
        (tmp_path / "compile_errors.txt").write_text(content)
        # 6 of the 8 lines match one of the failure heuristics.
        assert _read_compile_errors_count(str(tmp_path)) == 6

    def test_clean_log_returns_zero(self, tmp_path: Path) -> None:
        (tmp_path / "compile_errors.txt").write_text(
            "Building...\nLinking...\nDone.\n"
        )
        assert _read_compile_errors_count(str(tmp_path)) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
