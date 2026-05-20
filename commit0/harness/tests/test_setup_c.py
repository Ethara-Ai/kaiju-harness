"""Contract tests for commit0.harness.setup_c.

``setup_c.main`` performs git clone / branch derivation against the
filesystem and network, so only the import + callable contract is
asserted here. The branch-name casing logic is covered indirectly by
the integration suite.
"""

from __future__ import annotations

import pytest

import commit0.harness.setup_c as setup_c


class TestSetupCContract:
    def test_main_is_callable(self) -> None:
        assert callable(setup_c.main)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
