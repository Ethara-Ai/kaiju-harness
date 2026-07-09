from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_TSC_ERROR_LINE = re.compile(
    r"^(?P<file>[^\(\r\n]+?)\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*error\s+TS\d+"
)


def is_enabled() -> bool:
    return os.environ.get("KAIJU_TS_COMPILE_GATE", "0").lower() in ("1", "true", "yes")


def _extract_ts_error_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines():
        match = _TSC_ERROR_LINE.match(line.strip())
        if not match:
            continue
        file = match.group("file")
        counts[file] = counts.get(file, 0) + 1
    return counts


def _run_tsc_check(
    repo_dir: str,
    tsconfig: str = ".",
    timeout: int = 120,
) -> dict[str, int]:
    if not Path(repo_dir).exists():
        return {}
    tsc_cmd = ["npx", "-y", "--", "tsc", "--noEmit", "--pretty", "false"]
    if tsconfig and tsconfig != ".":
        tsc_cmd.extend(["-p", tsconfig])
    try:
        proc = subprocess.run(
            tsc_cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("compile_gate: tsc invocation skipped (%s)", exc)
        return {}
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return _extract_ts_error_counts(combined)


def _files_edited_since(repo_dir: str, base_sha: str) -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", base_sha, "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return set()
    return {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}


def _errors_regressed_for_touched_files(
    baseline: dict[str, int],
    current: dict[str, int],
    touched: set[str],
) -> bool:
    for file in touched:
        base_count = baseline.get(file, 0)
        curr_count = current.get(file, 0)
        if curr_count > base_count:
            return True
    return False


def _format_ts_errors_for_prompt(
    errors: dict[str, int], max_files: int = 10
) -> str:
    if not errors:
        return "No TypeScript compilation errors detected."
    top = sorted(errors.items(), key=lambda x: -x[1])[:max_files]
    total = sum(errors.values())
    lines = [
        f"TypeScript reports {total} error(s) across {len(errors)} file(s):"
    ]
    for file, count in top:
        lines.append(f"  {file}: {count} error(s)")
    if len(errors) > max_files:
        lines.append(f"  ... and {len(errors) - max_files} more file(s)")
    return "\n".join(lines)


def gate_post_agent_run(
    repo_dir: str,
    pre_sha: str,
    post_sha: str,
    baseline_errors: dict[str, int],
    tsconfig: str = ".",
) -> tuple[bool, str]:
    if pre_sha == post_sha:
        return False, "compile_gate: no changes since baseline"
    touched = _files_edited_since(repo_dir, pre_sha)
    ts_touched = {
        f for f in touched if f.endswith((".ts", ".tsx", ".mts", ".cts"))
    }
    if not ts_touched:
        return False, "compile_gate: no TS files touched"
    current_errors = _run_tsc_check(repo_dir, tsconfig)
    if _errors_regressed_for_touched_files(
        baseline_errors, current_errors, ts_touched
    ):
        return True, _format_ts_errors_for_prompt(current_errors)
    return False, "compile_gate: no compile regression on touched files"


_BASELINE_CACHE: dict[tuple[str, str], dict[str, int]] = {}


def _cached_baseline(
    repo_dir: str, tsconfig: str = "."
) -> dict[str, int]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
        clean = porcelain.strip() == ""
    except (subprocess.SubprocessError, OSError):
        head, clean = "", False
    key = (os.path.realpath(repo_dir), head) if (head and clean) else None
    if key is not None and key in _BASELINE_CACHE:
        return _BASELINE_CACHE[key]
    result = _run_tsc_check(repo_dir, tsconfig)
    if key is not None:
        _BASELINE_CACHE[key] = result
    return result


def _regressions_on_touched(
    baseline: dict[str, int],
    current: dict[str, int],
    touched: set[str],
) -> list[str]:
    return sorted(
        f
        for f in touched
        if current.get(f, 0) > baseline.get(f, 0)
    )


def run_with_compile_gate(
    run_aider_call: Callable[[], Any],
    *,
    repo_dir: str,
    local_repo: Any,
    pre_sha: str,
    tsconfig: str = ".",
    max_retries: int = 2,
    re_prompt_callback: Optional[Callable[[str], Any]] = None,
    persist_dir: Optional[str] = None,
) -> dict:
    baseline = _cached_baseline(repo_dir, tsconfig)
    baseline_broken = sorted(baseline.keys())

    run_aider_call()

    result: dict = {
        "status": "kept",
        "retries_used": 0,
        "baseline_broken_files": baseline_broken,
        "final_broken_files": [],
        "regressions": [],
    }

    for attempt in range(max_retries + 1):
        post_sha = _current_head(repo_dir)
        if post_sha == pre_sha:
            result["status"] = "clean_no_op"
            break
        touched = _files_edited_since(repo_dir, pre_sha)
        ts_touched = {
            f for f in touched if f.endswith((".ts", ".tsx", ".mts", ".cts"))
        }
        if not ts_touched:
            result["status"] = "kept"
            break
        current = _run_tsc_check(repo_dir, tsconfig)
        regressions = _regressions_on_touched(baseline, current, ts_touched)
        result["final_broken_files"] = sorted(current.keys())
        result["regressions"] = regressions

        if not regressions:
            result["status"] = "kept"
            break

        if attempt < max_retries and re_prompt_callback is not None:
            err_text = _format_ts_errors_for_prompt(current)
            result["retries_used"] = attempt + 1
            try:
                re_prompt_callback(err_text)
            except Exception as _e:
                from agent.agents import TransientLLMError as _TLE
                if isinstance(_e, _TLE):
                    raise  # don't swallow: let per-module isolation skip this module
                logger.exception(
                    "compile_gate: re_prompt_callback raised on attempt %d",
                    attempt + 1,
                )
                break
            continue

        try:
            local_repo.git.reset("--hard", pre_sha)
            result["status"] = "reverted"
        except Exception:
            logger.exception(
                "compile_gate: reset --hard %s failed; leaving working tree as-is",
                pre_sha[:8],
            )
            result["status"] = "revert_failed"
        break

    if persist_dir:
        try:
            Path(persist_dir).mkdir(parents=True, exist_ok=True)
            (Path(persist_dir) / ".compile_gate.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(
                "compile_gate: could not persist gate result to %s: %s",
                persist_dir,
                exc,
            )
    return result


def _current_head(repo_dir: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return proc.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""
