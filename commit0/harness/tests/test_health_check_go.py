"""Unit tests for commit0.harness.health_check_go."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


MODULE = "commit0.harness.health_check_go"


from commit0.harness.health_check_go import (
    check_go_tools,
    check_go_version,
    run_go_health_checks,
)


class TestCheckGoVersion:
    def test_matching_version(self) -> None:
        """Test that a matching go version returns (True, detail)."""
        client = MagicMock()
        client.containers.run.return_value = b"go1.21.5\n"
        ok, detail = check_go_version(client, "image:tag", "1.21")
        assert ok is True
        assert "go1.21.5" in detail

    def test_mismatched_version(self) -> None:
        """Test that a non-matching go version returns (False, expected-vs-actual)."""
        client = MagicMock()
        client.containers.run.return_value = b"go1.20.0\n"
        ok, detail = check_go_version(client, "image:tag", "1.21")
        assert ok is False
        assert "1.21" in detail
        assert "go1.20.0" in detail

    def test_container_exception_returns_false(self) -> None:
        """Test that a container failure returns (False, error) — non-crashing."""
        client = MagicMock()
        client.containers.run.side_effect = RuntimeError("container gone")
        ok, detail = check_go_version(client, "image:tag", "1.21")
        assert ok is False
        assert "container gone" in detail

    def test_returns_tuple_of_two(self) -> None:
        """Test that the return is always a 2-tuple (bool, str)."""
        client = MagicMock()
        client.containers.run.return_value = b"go1.21.0\n"
        result = check_go_version(client, "image:tag", "1.21")
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


class TestCheckGoTools:
    def test_tools_available_returns_true(self) -> None:
        """Test that presence of goimports+staticcheck returns (True, detail)."""
        client = MagicMock()
        client.containers.run.return_value = (
            b"/usr/local/bin/goimports\n/usr/local/bin/staticcheck\nOK\n"
        )
        ok, detail = check_go_tools(client, "image:tag")
        assert ok is True
        assert "available" in detail

    def test_tools_missing_returns_false(self) -> None:
        """Test that absence of the OK marker returns (False, detail)."""
        client = MagicMock()
        client.containers.run.return_value = b"error: not found\n"
        ok, detail = check_go_tools(client, "image:tag")
        assert ok is False
        assert "unexpected output" in detail

    def test_container_exception_returns_false(self) -> None:
        """Test that a container-run exception returns (False, error)."""
        client = MagicMock()
        client.containers.run.side_effect = RuntimeError("docker down")
        ok, detail = check_go_tools(client, "image:tag")
        assert ok is False
        assert "docker down" in detail


class TestRunGoHealthChecks:
    def test_returns_list_of_triples(self) -> None:
        """Test that run_go_health_checks returns a list of (bool, name, detail)."""
        client = MagicMock()
        client.containers.run.return_value = b"OK\n"
        results = run_go_health_checks(client, "image:tag")
        assert isinstance(results, list)
        for entry in results:
            assert isinstance(entry, tuple)
            assert len(entry) == 3
            assert isinstance(entry[0], bool)
            assert isinstance(entry[1], str)
            assert isinstance(entry[2], str)

    def test_go_version_check_included_when_requested(self) -> None:
        """Test that a go_version argument adds a 'go_version' probe."""
        client = MagicMock()
        client.containers.run.return_value = b"go1.21.0\nOK\n"
        results = run_go_health_checks(client, "image:tag", go_version="1.21")
        names = [name for _, name, _ in results]
        assert "go_version" in names

    def test_go_version_check_skipped_when_none(self) -> None:
        """Test that omitting go_version skips the go_version probe."""
        client = MagicMock()
        client.containers.run.return_value = b"OK\n"
        results = run_go_health_checks(client, "image:tag")
        names = [name for _, name, _ in results]
        assert "go_version" not in names

    def test_go_tools_always_included(self) -> None:
        """Test that the go_tools probe is always in the result list."""
        client = MagicMock()
        client.containers.run.return_value = b"OK\n"
        results = run_go_health_checks(client, "image:tag")
        names = [name for _, name, _ in results]
        assert "go_tools" in names


class TestModuleExports:
    def test_all_exports(self) -> None:
        """Test that check_go_version, check_go_tools, run_go_health_checks are exported."""
        import commit0.harness.health_check_go as mod
        assert "check_go_version" in mod.__all__
        assert "check_go_tools" in mod.__all__
        assert "run_go_health_checks" in mod.__all__
