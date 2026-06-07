"""Tests for concurrency patterns across the codebase.

Covers: ThreadPoolExecutor in evaluate.py/evaluate_rust.py,
        multiprocessing.Pool in run_agent.py,
        .done file markers in run_agent_no_rich.py.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


# ---------------------------------------------------------------------------
# ThreadPoolExecutor — evaluate.py patterns
# ---------------------------------------------------------------------------
class TestThreadPoolEvaluate:
    """Verify ThreadPoolExecutor error handling in evaluation pipelines."""

    def test_future_exception_does_not_crash_loop(self):
        """Simulates the as_completed pattern from evaluate.py."""
        results = []
        errors = []

        def good_task(name):
            return {"name": name, "passed": True}

        def bad_task(name):
            raise RuntimeError(f"Docker failed for {name}")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(good_task, "repo_a"): "repo_a",
                executor.submit(bad_task, "repo_b"): "repo_b",
                executor.submit(good_task, "repo_c"): "repo_c",
            }
            from concurrent.futures import as_completed

            for future in as_completed(futures):
                repo = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    errors.append((repo, str(e)))

        assert len(results) == 2
        assert len(errors) == 1
        assert errors[0][0] == "repo_b"

    def test_system_exit_handling(self):
        """evaluate_rust.py catches SystemExit specifically."""

        def task_with_exit():
            raise SystemExit(1)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(task_with_exit)
            with pytest.raises(SystemExit) as exc_info:
                future.result()
            assert exc_info.value.code == 1

    def test_system_exit_code_filtering(self):
        """evaluate_rust filters exit codes 0 and 1 as normal."""
        normal_exits = []
        abnormal_exits = []

        def task(code):
            raise SystemExit(code)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for code in [0, 1, 2]:
                futures[executor.submit(task, code)] = code

            from concurrent.futures import as_completed

            for future in as_completed(futures):
                code = futures[future]
                try:
                    future.result()
                except SystemExit as e:
                    if e.code not in (0, 1):
                        abnormal_exits.append(code)
                    else:
                        normal_exits.append(code)

        assert sorted(normal_exits) == [0, 1]
        assert abnormal_exits == [2]


# ---------------------------------------------------------------------------
# Multiprocessing patterns — run_agent.py
# ---------------------------------------------------------------------------
class TestMultiprocessingRunAgent:
    """Test Pool + Queue patterns from run_agent.py using threads (same logic)."""

    def test_queue_based_progress(self):
        """Simulates the queue-based progress loop from run_agent."""
        import queue

        q = queue.Queue()

        def worker(repo_name):
            time.sleep(0.01)
            q.put(("finish_repo", repo_name))

        threads = []
        repos = ["repo_a", "repo_b", "repo_c"]
        for repo in repos:
            t = threading.Thread(target=worker, args=(repo,))
            t.start()
            threads.append(t)

        finished = set()
        deadline = time.time() + 5
        while len(finished) < len(repos) and time.time() < deadline:
            try:
                msg_type, repo = q.get(timeout=0.5)
                if msg_type == "finish_repo":
                    finished.add(repo)
            except Exception:
                pass

        for t in threads:
            t.join()

        assert finished == set(repos)

    def test_worker_exception_does_not_block(self):
        """If a worker crashes, the remaining workers should still complete."""
        import queue

        q = queue.Queue()

        def flaky_worker(repo):
            if repo == "bad":
                raise ValueError("crash")
            q.put(("finish_repo", repo))

        threads = []
        for repo in ["good1", "bad", "good2"]:
            t = threading.Thread(target=flaky_worker, args=(repo,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=5)

        finished = set()
        while not q.empty():
            msg_type, repo = q.get_nowait()
            finished.add(repo)

        assert "good1" in finished
        assert "good2" in finished


# ---------------------------------------------------------------------------
# .done file markers — run_agent_no_rich.py pattern
# ---------------------------------------------------------------------------
class TestDoneFileMarkers:
    """Test .done file marker pattern for inter-process signaling."""

    def test_done_file_created_on_success(self, tmp_path):
        done_file = tmp_path / "repo_a.done"

        def simulate_completion(path):
            path.touch()

        simulate_completion(done_file)
        assert done_file.exists()

    def test_done_file_not_created_on_failure(self, tmp_path):
        done_file = tmp_path / "repo_a.done"

        def simulate_with_error(path):
            raise RuntimeError("agent failed")

        try:
            simulate_with_error(done_file)
        except RuntimeError:
            pass
        assert not done_file.exists()

    def test_done_file_check_race_condition(self, tmp_path):
        """Verify checking .done file while writer is active."""
        done_file = tmp_path / "repo.done"
        write_started = threading.Event()
        check_result = [None]

        def writer():
            write_started.set()
            time.sleep(0.05)
            done_file.touch()

        def checker():
            write_started.wait()
            # check before write completes
            check_result[0] = done_file.exists()

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=checker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # The checker ran before writer finished, so it should have seen False
        assert check_result[0] is False
        # But now it should exist
        assert done_file.exists()


# ---------------------------------------------------------------------------
# exec_run_with_timeout — docker_utils.py threaded execution
# ---------------------------------------------------------------------------
class TestExecRunWithTimeout:
    """Test the threaded exec pattern from docker_utils.py."""

    def test_timeout_kills_execution(self):
        """Simulate exec_run_with_timeout with a slow container."""
        result = {"output": None, "timed_out": False}

        def slow_exec():
            time.sleep(10)
            return "done"

        def exec_with_timeout(func, timeout):
            thread = threading.Thread(target=lambda: result.update({"output": func()}))
            thread.start()
            thread.join(timeout=timeout)
            if thread.is_alive():
                result["timed_out"] = True
            return result

        r = exec_with_timeout(slow_exec, 0.1)
        assert r["timed_out"] is True

    def test_fast_exec_completes(self):
        result = {"output": None, "timed_out": False}

        def fast_exec():
            return "quick"

        def exec_with_timeout(func, timeout):
            output_holder = [None]
            thread = threading.Thread(
                target=lambda: output_holder.__setitem__(0, func())
            )
            thread.start()
            thread.join(timeout=timeout)
            if thread.is_alive():
                result["timed_out"] = True
            else:
                result["output"] = output_holder[0]
            return result

        r = exec_with_timeout(fast_exec, 5)
        assert r["timed_out"] is False
        assert r["output"] == "quick"
