"""Tests for opt-in eval-container sandbox hardening (S-001).

Hardening is OFF by default so existing behavior is byte-for-byte unchanged
(see test_docker_utils.TestCreateContainer.test_creates_with_correct_args,
which asserts the exact default containers.run kwargs). When the operator opts
in via COMMIT0_SANDBOX_HARDEN, create_container forwards the extra flags.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from commit0.harness.docker_utils import (
    create_container,
    sandbox_hardening_kwargs,
)

MODULE = "commit0.harness.docker_utils"


def _logger() -> logging.Logger:
    return logging.getLogger("test_sandbox_hardening")


class TestSandboxHardeningKwargs:
    def test_off_by_default_returns_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("COMMIT0_SANDBOX_HARDEN", raising=False)
        assert sandbox_hardening_kwargs() == {}

    def test_enabled_returns_hardening_flags(self, monkeypatch) -> None:
        monkeypatch.setenv("COMMIT0_SANDBOX_HARDEN", "1")
        monkeypatch.delenv("COMMIT0_SANDBOX_NETWORK", raising=False)
        kw = sandbox_hardening_kwargs()
        assert kw["cap_drop"] == ["ALL"]
        assert "no-new-privileges" in kw["security_opt"][0]
        assert "pids_limit" in kw
        assert "mem_limit" in kw
        # Network is NOT restricted unless separately requested (avoids breaking eval).
        assert "network_mode" not in kw

    def test_network_opt_in_within_hardening(self, monkeypatch) -> None:
        monkeypatch.setenv("COMMIT0_SANDBOX_HARDEN", "1")
        monkeypatch.setenv("COMMIT0_SANDBOX_NETWORK", "none")
        kw = sandbox_hardening_kwargs()
        assert kw["network_mode"] == "none"


class TestCreateContainerHardening:
    @patch(f"{MODULE}.image_exists_locally", return_value=True)
    def test_default_call_has_no_hardening_flags(self, _mock_exists) -> None:
        client = MagicMock()
        client.containers.run.return_value = MagicMock(id="c1")
        create_container(client, "img:tag", "cname", _logger())
        _, kwargs = client.containers.run.call_args
        for flag in ("cap_drop", "pids_limit", "mem_limit", "security_opt", "network_mode"):
            assert flag not in kwargs

    @patch(f"{MODULE}.image_exists_locally", return_value=True)
    def test_hardening_flags_forwarded_when_provided(self, _mock_exists) -> None:
        client = MagicMock()
        client.containers.run.return_value = MagicMock(id="c2")
        hardening = {"cap_drop": ["ALL"], "pids_limit": 2048, "mem_limit": "4g"}
        create_container(
            client, "img:tag", "cname", _logger(), sandbox_hardening=hardening
        )
        _, kwargs = client.containers.run.call_args
        assert kwargs["cap_drop"] == ["ALL"]
        assert kwargs["pids_limit"] == 2048
        assert kwargs["mem_limit"] == "4g"
