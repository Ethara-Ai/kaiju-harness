import copy
import os
import time
import yaml
import multiprocessing
from tqdm import tqdm
from git import Repo
from agent.agent_utils import (
    create_branch,
    get_message,
    get_target_edit_files,
    get_changed_files_from_commits,
    update_message_with_dependencies,
    get_lint_cmd,
    load_agent_config,
    agent_test_timeout_sec,
)
import subprocess
import sys
import json
from agent.agents import AiderAgents
from typing import Tuple, cast
from agent.class_types import AgentConfig
from agent.thinking_capture import ThinkingCapture
from agent.llm_cost_capture import capture_module_calls
from commit0.harness.constants import SPLIT
from commit0.harness.split_utils import resolve_split
from commit0.harness.get_pytest_ids import main as get_tests
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.utils import load_dataset_from_config, _PROTECTED_TEST_PATHSPECS
from commit0.cli import read_commit0_config_file
from pathlib import Path
from agent.run_agent import (
    DirContext,
    _collect_worker_results,
    run_eval_after_each_commit,
    _skip_failed_module,
)
from agent.agents import TransientLLMError
from agent._module_retry import INLINE_MODULE_MAX_RETRIES, INLINE_MODULE_WAIT_SEC
import logging
from agent.claude_code.recovery import run_with_recovery

logger = logging.getLogger(__name__)


def _make_blind_lint_cmd(base_cmd: str) -> str:
    """Wrap lint so agent sees only 'lint clean' or 'lint failed: N issues'."""
    return (
        f"bash -c 'set +e; out=$({base_cmd} 2>&1); rc=$?; "
        "if [ $rc -eq 0 ]; then echo lint clean; "
        "else n=$(printf \"%s\" \"$out\" | grep -cE \":[0-9]+:\" 2>/dev/null); n=${n:-0}; "
        "printf \"lint failed: %s issues\\n\" \"$n\"; fi; exit $rc'"
    )


def _make_blind_test_cmd(base_cmd: str) -> str:
    """Wrap pytest so agent sees only the summary line, not per-test failures."""
    return (
        f"bash -c 'set +e; out=$({base_cmd} 2>&1); rc=$?; "
        "summary=$(printf \"%s\" \"$out\" | grep -E \"passed|failed|error\" | tail -1); "
        "if [ -n \"$summary\" ]; then printf \"%s\\n\" \"$summary\"; "
        "else printf \"tests done\\n\"; fi; exit $rc'"
    )


def _make_names_only_test_cmd(base_cmd: str) -> str:
    """Wrap test cmd so agent sees only failed test node IDs + counts, no tracebacks."""
    return (
        f"bash -c 'set +e; out=$({base_cmd} 2>&1); rc=$?; "
        "if [ $rc -eq 0 ]; then printf \"tests pass\\n\"; "
        "else "
        "failed=$(printf \"%s\" \"$out\" | sed -nE \"s/^FAILED ([^ ]+).*/- \\1/p\"); "
        "n_failed=$(printf \"%s\" \"$failed\" | grep -cE \"^- \" 2>/dev/null); n_failed=${n_failed:-0}; "
        "n_passed=$(printf \"%s\" \"$out\" | grep -oE \"[0-9]+ passed\" | head -1 | cut -d\" \" -f1); n_passed=${n_passed:-0}; "
        "total=$((n_failed + n_passed)); "
        "if [ -n \"$failed\" ]; then printf \"%s/%s tests failed:\\n%s\\n\" \"$n_failed\" \"$total\" \"$failed\"; "
        "elif [ \"$n_passed\" -gt 0 ]; then printf \"tests pass (non-zero rc, likely coverage/lint gate): %s passed, rc=%s\\n\" \"$n_passed\" \"$rc\"; "
        "else printf \"tests failed (no per-test names parsed): rc=%s\\n\" \"$rc\"; fi; "
        "fi; exit $rc'"
    )

def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Clear any stale .needs_retry so a now-successful module is not
    # ambiguously marked both done AND needs-retry (auto-resume / --resume
    # and the "any .needs_retry left?" incomplete-check rely on this).
    (log_dir / ".needs_retry").unlink(missing_ok=True)
    (log_dir / ".done").touch()


def _get_stable_log_dir(log_dir: str, repo_name: str, branch: str) -> Path:
    """Return a stable experiment log directory that persists across retries."""
    stable_dir = Path(log_dir) / repo_name / branch / "current"
    stable_dir.mkdir(parents=True, exist_ok=True)
    return stable_dir


def _run_agent_for_repo_impl(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run Aider for a given repository (raises on failure; see wrapper)."""
    # get repo info
    commit0_config = read_commit0_config_file(commit0_config_file)

    assert "commit0" in commit0_config["dataset_name"] or commit0_config[
        "dataset_name"
    ].endswith(".json")
    _, repo_name = example["repo"].split("/")

    # repo_name = repo_name.lower()
    # repo_name = repo_name.replace(".", "-")

    repo_path = os.path.join(repo_base_dir, repo_name)
    repo_path = os.path.abspath(repo_path)

    try:
        local_repo = Repo(repo_path)
    except Exception:
        logger.error(
            "Failed to open repo at %s: not a git repo", repo_path, exc_info=True
        )
        raise Exception(
            f"{repo_path} is not a git repo. Check if base_dir is correctly specified."
        ) from None

    if agent_config.agent_name == "aider":
        agent = AiderAgents(
            agent_config.max_iteration,
            agent_config.model_name,
            agent_config.cache_prompts,
        )
    else:
        raise NotImplementedError(
            f"{agent_config.agent_name} is not implemented; please add your implementations in baselines/agents.py."
        )

    thinking_capture = (
        ThinkingCapture() if getattr(agent_config, "capture_thinking", False) else None
    )

    # Check if there are changes in the current branch
    if local_repo.is_dirty():
        logger.warning("Auto-committing uncommitted changes in %s", repo_path)
        # Stage all changes
        local_repo.git.add(A=True)
        # Commit changes with the message "left from last change"
        local_repo.index.commit("left from last change")

    # # if branch_name is not provided, create a new branch name based on agent_config
    # if branch is None:
    #     branch = args2string(agent_config)
    create_branch(local_repo, branch, example["base_commit"])

    # in cases where the latest commit of branch is not commit 0
    # set it back to commit 0
    latest_commit = local_repo.commit(branch)
    if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
        logger.warning(
            "Resetting %s to base commit %s (override_previous_changes=True)",
            repo_name,
            example["base_commit"],
        )
        local_repo.git.reset("--hard", example["base_commit"])

    # get target files to edit and test files to run
    target_edit_files, import_dependencies, test_files_readonly = get_target_edit_files(
        local_repo,
        example["src_dir"],
        example["test"]["test_dir"],
        branch,
        example["reference_commit"],
        agent_config.use_topo_sort_dependencies,
    )

    # Compute base_commit stub set (files that had `raise NotImplementedError` at base).
    # This is used both for the `strip_non_stubs` intersection (stage 1) AND as a
    # stage 2/3 safety net when `get_target_edit_files` returns 0 files because
    # stage 1 already filled the stubs and the working-tree scan finds nothing.
    _base = example["base_commit"]
    _stubbed_at_base: set[str] = set()
    _base_scan_ok = False
    try:
        _ls = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", _base],
            cwd=repo_path, capture_output=True, text=True, check=True,
        )
        for _rel in _ls.stdout.splitlines():
            if not _rel.endswith(".py"):
                continue
            _show = subprocess.run(
                ["git", "show", f"{_base}:{_rel}"],
                cwd=repo_path, capture_output=True, text=True,
            )
            if _show.returncode == 0 and "raise NotImplementedError" in _show.stdout:
                _stubbed_at_base.add(_rel)
        _base_scan_ok = True
    except (subprocess.CalledProcessError, OSError) as _e:
        logger.warning(
            "base_commit stub scan failed (%s). Falling back to working-tree target_edit_files.",
            _e,
        )

    # Stage 2/3 safety net: if working-tree scan came up empty (stage 1 filled all
    # stubs) but base_commit has stubs, use the base-derived list as authoritative.
    if not target_edit_files and _stubbed_at_base:
        target_edit_files = sorted(_stubbed_at_base)
        logger.info(
            "stage 2/3 recovery: target_edit_files rebuilt from base_commit %s: %d files",
            _base[:8], len(target_edit_files),
        )
    elif agent_config.strip_non_stubs and _base_scan_ok:
        # Stage 1 case: intersect working-tree target list with base_commit stubs.
        target_edit_files = [f for f in target_edit_files if f in _stubbed_at_base]
        logger.info(
            "strip_non_stubs: kept %d/%d target files (filtered against base_commit %s)",
            len(target_edit_files), len(_stubbed_at_base), _base[:8],
        )

    lint_files = get_changed_files_from_commits(
        local_repo, "HEAD", example["base_commit"]
    )
    # Call the commit0 get-tests command to retrieve test files
    test_files_str = [xx for x in get_tests(repo_name, verbose=0) for xx in x]
    test_files_raw = sorted(
        list(set([i.split(":")[0] for i in test_files_str if i.strip()]))
    )
    test_dir = example.get("test", {}).get("test_dir", "tests")
    test_files = []
    for tf in test_files_raw:
        full_path = Path(repo_path) / tf
        if full_path.exists():
            test_files.append(tf)
        elif (Path(repo_path) / test_dir / tf).exists():
            resolved = os.path.join(test_dir, tf)
            test_files.append(resolved)
            logger.info("Resolved test file with prefix: %s -> %s", tf, resolved)
        else:
            logger.warning("Test file not found, skipping: %s", tf)
    test_files.sort()

    # prepare the log dir — stable across retries (no timestamp)
    experiment_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)
    eval_results = {}

    # write agent_config to .agent.yaml in the log_dir for record
    agent_config_log_file = experiment_log_dir / ".agent.yaml"
    try:
        with open(agent_config_log_file, "w") as agent_config_file:
            yaml.dump(agent_config, agent_config_file)
    except OSError as e:
        logger.error("Failed to write agent config to %s: %s", agent_config_log_file, e)
        raise

    message = ""

    time.monotonic()

    from agent.openhands_formatter import write_module_output_json

    instance_id = ""
    metadata: dict = {}
    if thinking_capture is not None:
        from agent.output_writer import build_metadata

        commit0_config_for_meta = read_commit0_config_file(commit0_config_file)
        instance_id = (
            example["instance_id"]
            if "instance_id" in example.keys()
            else f"commit-0/{repo_name}"
        )
        metadata = build_metadata(
            model_name=agent_config.model_name,
            dataset_path=commit0_config_for_meta.get("dataset_name", ""),
            max_iterations=agent_config.max_iteration,
            model_short=agent_config.model_short,
            dataset_id=example.get("id"),
        )

    with DirContext(repo_path):
        if agent_config is None:
            raise ValueError("Invalid input")

        if agent_config.run_tests:
            for test_file in test_files:
                test_file_name = test_file.replace(".py", "").replace("/", "__")
                test_log_dir = experiment_log_dir / test_file_name
                # Flush each turn live to turns.jsonl (+ .heartbeat) so this
                # variant produces the same trajectory artifacts as go/rust.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(test_log_dir / "turns.jsonl")

                if _is_module_done(test_log_dir):
                    logger.info(
                        f"Skipping already-completed test module: {test_file_name}"
                    )
                    continue

                if os.environ.get("KAIJU_DIRECT_PYTEST"):
                    test_cmd = f"{sys.executable} -m pytest {test_file} --tb=short --continue-on-collection-errors --no-header -q"
                else:
                    test_cmd = f"{sys.executable} -m commit0 test {repo_path} {test_file} --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout {agent_test_timeout_sec()}"
                if agent_config.blind_tests:
                    test_cmd = _make_blind_test_cmd(test_cmd)
                elif agent_config.names_only_tests:
                    test_cmd = _make_names_only_test_cmd(test_cmd)
                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd(lint_cmd)
                message, spec_costs = get_message(
                    agent_config, repo_path, test_files=[test_file]
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                _module_ok = False
                for _mret in range(INLINE_MODULE_MAX_RETRIES):
                    try:
                        with capture_module_calls(
                                            thinking_capture,
                                            module=test_file_name,
                                            log_dir=test_log_dir,
                                            model_short=agent_config.model_short,
                                        ):
                            _ = run_with_recovery(agent.run, 
                            "",
                            test_cmd,
                            lint_cmd,
                            target_edit_files,
                            test_log_dir,
                            test_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="test",
                            current_module=test_file_name,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        _kaiju_log_dir=test_log_dir,)
                        _module_ok = True
                        break
                    except TransientLLMError as _tle:
                        if _mret >= INLINE_MODULE_MAX_RETRIES - 1:
                            _skip_failed_module(test_log_dir, test_file_name, _tle)
                            break
                        _wait = INLINE_MODULE_WAIT_SEC * (_mret + 1)
                        logger.warning(
                            "Module %s (test) TransientLLMError attempt %d/%d — inline-retrying after %ds",
                            test_file_name, _mret + 1, INLINE_MODULE_MAX_RETRIES, _wait,
                        )
                        if thinking_capture is not None:
                            thinking_capture.set_live_path(Path(test_log_dir) / "turns.jsonl")
                        time.sleep(_wait)
                if not _module_ok:
                    continue
                module_elapsed = time.time() - module_start
                _mark_module_done(test_log_dir)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    module_patch = (
                        local_repo.git.diff("--no-renames", pre_sha, post_sha, "--", ".", *_PROTECTED_TEST_PATHSPECS)
                        if pre_sha != post_sha
                        else ""
                    )
                    module_turns = thinking_capture.get_module_turns(test_file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(test_log_dir),
                            module_turns=module_turns,
                            module=test_file_name,
                            instance_id=f"{instance_id}__{test_file_name}"
                            if instance_id
                            else test_file_name,
                            git_patch=module_patch,
                            instruction=message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(test_file_name),
                            stage="test",
                            module_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
        elif agent_config.run_entire_dir_lint:
            message, spec_costs = get_message(
                agent_config, repo_path, test_files=test_files
            )
            if thinking_capture is not None:
                for c in spec_costs:
                    thinking_capture.summarizer_costs.add(c)
            for lint_file in lint_files:
                lint_file_name = lint_file.replace(".py", "").replace("/", "__")
                lint_log_dir = experiment_log_dir / lint_file_name
                if thinking_capture is not None:
                    thinking_capture.set_live_path(lint_log_dir / "turns.jsonl")

                if _is_module_done(lint_log_dir):
                    logger.info(f"Skipping already-linted file: {lint_file_name}")
                    continue

                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd(lint_cmd)

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                _module_ok = False
                for _mret in range(INLINE_MODULE_MAX_RETRIES):
                    try:
                        with capture_module_calls(
                                            thinking_capture,
                                            module=lint_file_name,
                                            log_dir=lint_log_dir,
                                            model_short=agent_config.model_short,
                                        ):
                            _ = run_with_recovery(agent.run, 
                            "",
                            "",
                            lint_cmd,
                            [lint_file],
                            lint_log_dir,
                            lint_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="lint",
                            current_module=lint_file_name,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        _kaiju_log_dir=lint_log_dir,)
                        _module_ok = True
                        break
                    except TransientLLMError as _tle:
                        if _mret >= INLINE_MODULE_MAX_RETRIES - 1:
                            _skip_failed_module(lint_log_dir, lint_file_name, _tle)
                            break
                        _wait = INLINE_MODULE_WAIT_SEC * (_mret + 1)
                        logger.warning(
                            "Module %s (lint) TransientLLMError attempt %d/%d — inline-retrying after %ds",
                            lint_file_name, _mret + 1, INLINE_MODULE_MAX_RETRIES, _wait,
                        )
                        if thinking_capture is not None:
                            thinking_capture.set_live_path(Path(lint_log_dir) / "turns.jsonl")
                        time.sleep(_wait)
                if not _module_ok:
                    continue
                module_elapsed = time.time() - module_start
                _mark_module_done(lint_log_dir)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    module_patch = (
                        local_repo.git.diff("--no-renames", pre_sha, post_sha, "--", ".", *_PROTECTED_TEST_PATHSPECS)
                        if pre_sha != post_sha
                        else ""
                    )
                    module_turns = thinking_capture.get_module_turns(lint_file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(lint_log_dir),
                            module_turns=module_turns,
                            module=lint_file_name,
                            instance_id=f"{instance_id}__{lint_file_name}"
                            if instance_id
                            else lint_file_name,
                            git_patch=module_patch,
                            instruction=message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(lint_file_name),
                            stage="lint",
                            module_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
        else:
            message, spec_costs = get_message(
                agent_config, repo_path, test_files=test_files
            )
            if thinking_capture is not None:
                for c in spec_costs:
                    thinking_capture.summarizer_costs.add(c)

            for f in target_edit_files:
                file_name = f.replace(".py", "").replace("/", "__")
                file_log_dir = experiment_log_dir / file_name
                if thinking_capture is not None:
                    thinking_capture.set_live_path(file_log_dir / "turns.jsonl")

                if _is_module_done(file_log_dir):
                    logger.info(f"Skipping already-drafted file: {file_name}")
                    continue

                if agent_config.add_import_module_to_context:
                    dependencies = import_dependencies.get(f, [])
                    iter_message = update_message_with_dependencies(
                        copy.deepcopy(message), dependencies
                    )
                else:
                    iter_message = message

                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd(lint_cmd)
                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                _module_ok = False
                for _mret in range(INLINE_MODULE_MAX_RETRIES):
                    try:
                        with capture_module_calls(
                                            thinking_capture,
                                            module=file_name,
                                            log_dir=file_log_dir,
                                            model_short=agent_config.model_short,
                                        ):
                            _ = run_with_recovery(agent.run, 
                            iter_message,
                            "",
                            lint_cmd,
                            [f],
                            file_log_dir,
                            thinking_capture=thinking_capture,
                            current_stage="draft",
                            current_module=file_name,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        _kaiju_log_dir=file_log_dir,)
                        _module_ok = True
                        break
                    except TransientLLMError as _tle:
                        if _mret >= INLINE_MODULE_MAX_RETRIES - 1:
                            _skip_failed_module(file_log_dir, file_name, _tle)
                            break
                        _wait = INLINE_MODULE_WAIT_SEC * (_mret + 1)
                        logger.warning(
                            "Module %s (draft) TransientLLMError attempt %d/%d — inline-retrying after %ds",
                            file_name, _mret + 1, INLINE_MODULE_MAX_RETRIES, _wait,
                        )
                        if thinking_capture is not None:
                            thinking_capture.set_live_path(Path(file_log_dir) / "turns.jsonl")
                        time.sleep(_wait)
                if not _module_ok:
                    continue
                module_elapsed = time.time() - module_start
                _mark_module_done(file_log_dir)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    module_patch = (
                        local_repo.git.diff("--no-renames", pre_sha, post_sha, "--", ".", *_PROTECTED_TEST_PATHSPECS)
                        if pre_sha != post_sha
                        else ""
                    )
                    module_turns = thinking_capture.get_module_turns(file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(file_log_dir),
                            module_turns=module_turns,
                            module=file_name,
                            instance_id=f"{instance_id}__{file_name}"
                            if instance_id
                            else file_name,
                            git_patch=module_patch,
                            instruction=iter_message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(file_name),
                            stage="draft",
                            module_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
    if agent_config.record_test_for_each_commit:
        try:
            with open(experiment_log_dir / "eval_results.json", "w") as f:
                json.dump(eval_results, f)
        except OSError as e:
            logger.error("Failed to write eval results: %s", e)
            raise

    if thinking_capture is not None:
        try:
            from agent.trajectory_writer import write_trajectory_md

            # IDEMPOTENT BACKSTOP (parity with go/c/js/ts): output.json is written
            # per-module inside each stage loop the moment the module finishes, so a
            # worker killed mid-run keeps output.json for every completed module. This
            # backstop only fills output.json for a turn-bearing module that STILL
            # lacks one — the one real gap the in-loop write can't cover:
            #   * a module marked `.done` by a PRIOR run (which predates the in-loop
            #     write, or was killed between `_mark_module_done` and the in-loop
            #     `write_module_output_json`) is skipped by `_is_module_done` before
            #     its in-loop write can run on resume, leaving it `.done` but
            #     output.json-less on resume.
            # It NEVER touches a module that already has output.json, so it cannot
            # double-write or double-count metrics.
            modules_seen: set[str] = set()
            for _turn in thinking_capture.turns:
                if _turn.module and _turn.module not in modules_seen:
                    modules_seen.add(_turn.module)
            for _module_name in modules_seen:
                _module_log_dir = experiment_log_dir / _module_name
                if (_module_log_dir / "output.json").exists():
                    continue  # already written in-loop; don't rewrite / double-count
                _module_turns = thinking_capture.get_module_turns(_module_name)
                if not _module_turns:
                    continue
                _module_log_dir.mkdir(parents=True, exist_ok=True)
                _stage = _module_turns[0].stage or "unknown"
                # Best-effort cumulative diff (base_commit → HEAD) scoped to the
                # working tree with tests protected — matches the in-loop write's
                # pathspec set. Overcounts co-modified files vs a per-module pre/post
                # window, but preserves scoring data for the orphaned module.
                try:
                    _bp = example["base_commit"] or "HEAD"
                    _module_patch = local_repo.git.diff(
                        "--no-renames", _bp, "HEAD", "--", ".", *_PROTECTED_TEST_PATHSPECS
                    )
                except Exception:
                    _module_patch = ""
                write_module_output_json(
                    output_dir=str(_module_log_dir),
                    module_turns=_module_turns,
                    module=_module_name,
                    instance_id=f"{instance_id}__{_module_name}" if instance_id else _module_name,
                    git_patch=_module_patch,
                    instruction="",
                    metadata=metadata,
                    metrics=thinking_capture.get_module_metrics(_module_name),
                    stage=_stage,
                )


            logger.info(
                "Per-module output written: %d turns across %d modules",
                len(thinking_capture.turns),
                len(set(t.module for t in thinking_capture.turns)),
            )

            if getattr(agent_config, "trajectory_md", True):
                write_trajectory_md(
                    output_path=experiment_log_dir / "trajectory.md",
                    repo_name=repo_name,
                    turns=thinking_capture.turns,
                )

            logger.info(
                f"Wrote thinking capture: {len(thinking_capture.turns)} turns, "
                f"{thinking_capture.get_metrics()['total_thinking_tokens']} thinking tokens"
            )
        except Exception as e:
            logger.warning(f"Failed to write thinking capture output: {e}")

    # Full model-changes record (base_commit..HEAD), matching go/rust — keeps
    # tests/manifests, strips binary/cache noise; distinct from the eval's scored
    # patch.diff. Best-effort; never fatal.
    try:
        from agent.stage_patch import write_stage_patch
        write_stage_patch(local_repo, example.get("base_commit", "HEAD"),
                          experiment_log_dir, logger)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to write model_changes.diff: %s", e)


def run_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> Tuple[str, bool]:
    """Run Aider for one repo with per-repo error isolation (R-001).

    Catches any worker failure so one bad repo cannot abort the whole batch;
    returns ``(repo_name, ok)`` instead of raising.
    """
    _, repo_name = example["repo"].split("/")
    try:
        _run_agent_for_repo_impl(
            repo_base_dir,
            agent_config,
            example,
            branch,
            override_previous_changes,
            backend,
            log_dir,
            commit0_config_file,
        )
        return repo_name, True
    except Exception:
        logger.error(
            "Agent worker for %s failed; isolating so the batch continues",
            repo_name,
            exc_info=True,
        )
        return repo_name, False


def run_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Main function to run Aider for a given repository.

    Will run in parallel for each repo.
    """
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    commit0_config = read_commit0_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )
    repo_split = commit0_config["repo_split"]
    dataset = list(dataset)
    allowed_repos = set(resolve_split(repo_split, dataset, curated=SPLIT))
    filtered_dataset = [
        example
        for example in dataset
        if isinstance(example, dict)
        and isinstance(example.get("repo"), str)
        and example["repo"].split("/")[-1] in allowed_repos
    ]
    assert len(filtered_dataset) > 0, (
        f"No examples available for repo_split={repo_split!r}. "
        f"If using a custom dataset, ensure the JSON file is non-empty."
    )

    # if len(filtered_dataset) > 1:
    #     sys.stdout = open(os.devnull, "w")
    if agent_config.add_import_module_to_context:
        # Install Chrome for Playwright for browser-based agents
        try:
            subprocess.run(["playwright", "install", "chromium"], check=True)
            logger.info("Chrome installed successfully for Playwright")
        except subprocess.CalledProcessError as e:
            logger.error("Error installing Chrome for Playwright: %s", e)
        except FileNotFoundError:
            logger.warning(
                "Playwright not found. Make sure it's installed and in your PATH."
            )

    with tqdm(
        total=len(filtered_dataset), smoothing=0, desc="Running Aider for repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            results = []

            # Use apply_async to submit jobs and add progress bar updates
            for example in filtered_dataset:
                result = pool.apply_async(
                    run_agent_for_repo,
                    args=(
                        commit0_config["base_dir"],
                        agent_config,
                        cast(RepoInstance, example),
                        branch,
                        override_previous_changes,
                        backend,
                        log_dir,
                        commit0_config_file,
                    ),
                    callback=lambda _: pbar.update(
                        1
                    ),  # Update progress bar on task completion
                )
                results.append(result)

            summary = _collect_worker_results(results)
            logger.info(
                "All %d agent workers completed: %d succeeded, %d failed",
                len(results),
                summary["succeeded"],
                summary["failed"],
            )
            if summary["failed_repos"]:
                logger.warning(
                    "Repos that failed (isolated, batch continued): %s",
                    ", ".join(summary["failed_repos"]),
                )
