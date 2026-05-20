"""Contract tests for commit0.harness.health_check_c.

Every check shells out to gcc/cmake/clang inside a Docker container, so
behaviour is not unit-testable. We assert the public callable surface
only.
"""

from __future__ import annotations

import pytest

import commit0.harness.health_check_c as hc


class TestHealthCheckContract:
    @pytest.mark.parametrize(
        "name",
        [
            "check_c_compilers",
            "check_c_build_tools",
            "check_c_lint_tools",
            "run_c_health_checks",
        ],
    )
    def test_public_helpers_callable(self, name: str) -> None:
        assert callable(getattr(hc, name))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
