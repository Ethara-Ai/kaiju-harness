"""Contract tests for commit0.harness.build_c.

``build_c`` is Docker-bound; only its importability and public callable
surface are asserted here.
"""

from __future__ import annotations

import pytest

import commit0.harness.build_c as build_c


class TestBuildCContract:
    def test_main_is_callable(self) -> None:
        assert callable(build_c.main)

    def test_spec_helper_is_callable(self) -> None:
        assert callable(build_c._get_c_specs)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
