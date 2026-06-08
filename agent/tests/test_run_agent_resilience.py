"""Tests for batch error-isolation in agent.run_agent (R-001).

A single repo/worker failure must NOT abort the whole parallel batch:
- `run_agent_for_repo` must catch any worker error, emit ('finish_repo', repo),
  and return a (repo_name, ok) status instead of raising.
- `_collect_worker_results` must isolate each result.get(): one failure is logged
  and counted, the rest are still collected.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch


class _FakeResult:
    """Minimal stand-in for multiprocessing.pool.AsyncResult."""

    def __init__(self, value: Any = None, exc: Exception | None = None) -> None:
        self._value = value
        self._exc = exc

    def get(self) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._value


class _RecordingQueue:
    """Captures everything put on it, like the multiprocessing update_queue."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def put(self, item: Any) -> None:
        self.items.append(item)


# ---------------------------------------------------------------------------
# _collect_worker_results
# ---------------------------------------------------------------------------
class TestCollectWorkerResults:
    def test_all_success(self) -> None:
        from agent.run_agent import _collect_worker_results

        results = [_FakeResult(("r1", True)), _FakeResult(("r2", True))]
        summary = _collect_worker_results(results)
        assert summary["succeeded"] == 2
        assert summary["failed"] == 0
        assert summary["failed_repos"] == []

    def test_one_get_raises_others_still_collected(self) -> None:
        from agent.run_agent import _collect_worker_results

        results = [
            _FakeResult(("r1", True)),
            _FakeResult(exc=RuntimeError("worker died before returning")),
            _FakeResult(("r3", True)),
        ]
        # Must not raise even though one .get() raises.
        summary = _collect_worker_results(results)
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1

    def test_worker_returns_failed_status(self) -> None:
        from agent.run_agent import _collect_worker_results

        results = [_FakeResult(("good", True)), _FakeResult(("bad", False))]
        summary = _collect_worker_results(results)
        assert summary["succeeded"] == 1
        assert summary["failed"] == 1
        assert summary["failed_repos"] == ["bad"]


# ---------------------------------------------------------------------------
# run_agent_for_repo isolation wrapper
# ---------------------------------------------------------------------------
class TestRunAgentForRepoIsolation:
    def _call(self, queue: _RecordingQueue) -> Any:
        from agent.run_agent import run_agent_for_repo

        return run_agent_for_repo(
            repo_base_dir="/tmp/base",
            agent_config=object(),
            example={"repo": "org/myrepo"},
            branch="agent-branch",
            update_queue=queue,
        )

    def test_worker_failure_does_not_raise_and_reports_failed(self) -> None:
        queue = _RecordingQueue()
        with patch(
            "agent.run_agent._run_agent_for_repo_impl",
            side_effect=RuntimeError("LLM 5xx escaped aider"),
        ):
            result = self._call(queue)
        assert result == ("myrepo", False)

    def test_worker_failure_still_emits_finish_repo(self) -> None:
        queue = _RecordingQueue()
        with patch(
            "agent.run_agent._run_agent_for_repo_impl",
            side_effect=RuntimeError("boom"),
        ):
            self._call(queue)
        assert ("finish_repo", "myrepo") in queue.items

    def test_worker_success_returns_ok(self) -> None:
        queue = _RecordingQueue()
        with patch("agent.run_agent._run_agent_for_repo_impl", return_value=None):
            result = self._call(queue)
        assert result == ("myrepo", True)


class TestRunAgentNoRichIsolation:
    def _call(self) -> Any:
        from agent.run_agent_no_rich import run_agent_for_repo

        return run_agent_for_repo(
            repo_base_dir="/tmp/base",
            agent_config=object(),
            example={"repo": "org/myrepo"},
            branch="agent-branch",
        )

    def test_worker_failure_does_not_raise_and_reports_failed(self) -> None:
        with patch(
            "agent.run_agent_no_rich._run_agent_for_repo_impl",
            side_effect=RuntimeError("boom"),
        ):
            result = self._call()
        assert result == ("myrepo", False)

    def test_worker_success_returns_ok(self) -> None:
        with patch(
            "agent.run_agent_no_rich._run_agent_for_repo_impl", return_value=None
        ):
            result = self._call()
        assert result == ("myrepo", True)
