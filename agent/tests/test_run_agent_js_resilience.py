"""Tests for batch error-isolation in agent.run_agent_js (AG-G2).

A single repo/worker failure must NOT abort the whole parallel batch:
- ``run_agent_for_repo_js`` must catch any worker error and return
  ``(repo_name, ok)`` instead of raising.
- ``_collect_worker_results_js`` must isolate each ``result.get()``: one
  failure is logged and counted, the rest are still collected.

Mirrors agent/tests/test_run_agent_resilience.py for the JS path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch


class _FakeResult:
    def __init__(self, value: Any = None, exc: Exception | None = None) -> None:
        self._value = value
        self._exc = exc

    def get(self) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._value


class TestCollectWorkerResultsJs:
    def test_all_success(self) -> None:
        from agent.run_agent_js import _collect_worker_results_js

        results = [_FakeResult(("r1", True)), _FakeResult(("r2", True))]
        summary = _collect_worker_results_js(results)
        assert summary["succeeded"] == 2
        assert summary["failed"] == 0
        assert summary["failed_repos"] == []

    def test_one_get_raises_others_still_collected(self) -> None:
        from agent.run_agent_js import _collect_worker_results_js

        results = [
            _FakeResult(("r1", True)),
            _FakeResult(exc=RuntimeError("worker died before returning")),
            _FakeResult(("r3", True)),
        ]
        summary = _collect_worker_results_js(results)
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1

    def test_worker_returns_failed_status(self) -> None:
        from agent.run_agent_js import _collect_worker_results_js

        results = [_FakeResult(("good", True)), _FakeResult(("bad", False))]
        summary = _collect_worker_results_js(results)
        assert summary["succeeded"] == 1
        assert summary["failed"] == 1
        assert summary["failed_repos"] == ["bad"]


class TestRunAgentForRepoJsIsolation:
    def _call(self) -> Any:
        from agent.run_agent_js import run_agent_for_repo_js

        return run_agent_for_repo_js(
            repo_base_dir="/tmp/base",
            agent_config=object(),
            example={"repo": "org/myrepo"},
            branch="agent-branch",
        )

    def test_worker_failure_does_not_raise_and_reports_failed(self) -> None:
        with patch(
            "agent.run_agent_js._run_agent_for_repo_js_impl",
            side_effect=RuntimeError("LLM 5xx escaped aider"),
        ):
            result = self._call()
        assert result == ("myrepo", False)

    def test_worker_success_returns_ok(self) -> None:
        with patch(
            "agent.run_agent_js._run_agent_for_repo_js_impl", return_value=None
        ):
            result = self._call()
        assert result == ("myrepo", True)

    def test_malformed_repo_defaults_to_unknown(self) -> None:
        from agent.run_agent_js import run_agent_for_repo_js

        with patch(
            "agent.run_agent_js._run_agent_for_repo_js_impl",
            side_effect=RuntimeError("boom"),
        ):
            result = run_agent_for_repo_js(
                repo_base_dir="/tmp/base",
                agent_config=object(),
                example={},
                branch="agent-branch",
            )
        assert result == ("<unknown>", False)
