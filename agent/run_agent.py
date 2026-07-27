import logging
import os
import yaml
import multiprocessing
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
import json
import subprocess
import sys
from agent.agents import AiderAgents
from agent.agents import TransientLLMError
from agent._module_retry import INLINE_MODULE_MAX_RETRIES, INLINE_MODULE_WAIT_SEC, mark_module_started
from agent.claude_code.recovery import run_with_recovery
from typing import Optional, Tuple, Type, cast
from types import TracebackType
from agent.class_types import AgentConfig
from commit0.harness.constants import SPLIT
from commit0.harness.split_utils import resolve_split
from commit0.harness.get_pytest_ids import main as get_tests
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.utils import load_dataset_from_config
from commit0.cli import read_commit0_config_file
from pathlib import Path
from agent.display import TerminalDisplay
from agent.thinking_capture import ThinkingCapture
from agent.output_writer import build_metadata
from agent.module_patch import module_file_patch
from agent.openhands_formatter import write_module_output_json
from agent.llm_cost_capture import capture_module_calls
import queue
import time

logger = logging.getLogger(__name__)


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Clear any stale .needs_retry so a now-successful module is not
    # ambiguously marked both done AND needs-retry (auto-resume / --resume
    # and the "any .needs_retry left?" incomplete-check rely on this).
    (log_dir / ".needs_retry").unlink(missing_ok=True)
    (log_dir / ".done").touch()


def _skip_failed_module(log_dir: Path, module_name: str, err: Exception) -> None:
    """One module whose LLM calls kept failing (e.g. a persistent mid-stream /
    timeout error) after run_with_recovery exhausted its retries. Leave it WITHOUT
    a .done marker (so --resume re-runs it) + drop a .needs_retry breadcrumb, and
    let the loop continue. A single stuck module must NOT abort the whole repo —
    for a single-repo run that would trip the "all workers failed -> systemic
    fault" abort and discard every module that already succeeded.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / ".needs_retry").write_text(str(err)[:500], encoding="utf-8")
        # F3 audit fix: also emit a full error.log with the currently-being-handled
        # exception's traceback so post-mortem tooling can machine-parse the failure
        # (was missing in 8/9 runners; only cpp draft had partial coverage).
        import traceback as _tb
        try:
            (log_dir / "error.log").write_text(f"{err}\n\n{_tb.format_exc()}", encoding="utf-8")
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass
    logger.error("Module %s failed after retries (%s) — skipping so the repo "
                 "continues; left .needs_retry for --resume.", module_name, err)


def _write_module_output(
    *,
    thinking_capture: "Optional[ThinkingCapture]",
    module_log_dir: Path,
    module_name: str,
    stage: str,
    local_repo,
    base_commit: str,
    module_rel_files: dict,
    instance_id: str,
    metadata: dict,
) -> None:
    """Write ONE module's output.json right after it finishes (crash-resilient).

    Mirrors run_agent_go.py: a worker killed mid-run keeps output.json (with the
    module's ``git diff base_commit..HEAD`` patch, scoped to the files it owns)
    for every already-completed module — the state resume replays. Best-effort.
    """
    if thinking_capture is None:
        return
    module_turns = thinking_capture.get_module_turns(module_name)
    if not module_turns:
        return
    module_metrics = thinking_capture.get_module_metrics(module_name)
    module_patch = module_file_patch(
        local_repo, base_commit, "HEAD",
        module_rel_files.get(module_name, []), logger=logger,
    )
    try:
        write_module_output_json(
            output_dir=str(module_log_dir),
            module_turns=module_turns,
            module=module_name,
            instance_id=instance_id,
            git_patch=module_patch,
            instruction="",
            metadata=metadata,
            metrics=module_metrics,
            stage=stage,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to write module output JSON for %s: %s", module_name, e)


class DirContext:
    def __init__(self, d: str):
        self.dir = d
        self.cwd = os.getcwd()

    def __enter__(self):
        os.chdir(self.dir)

    def __exit__(
        self,
        exctype: Optional[Type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        os.chdir(self.cwd)


def run_eval_after_each_commit(
    branch: str, backend: str, commit0_config_file: str
) -> str:
    """Run the eval command after each commit."""
    eval_cmd = f"{sys.executable} -m commit0 evaluate --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout {agent_test_timeout_sec()}"
    try:
        result = subprocess.run(
            eval_cmd.split(), capture_output=True, text=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error("Error running eval command: %s", e, exc_info=True)
        return e.stdout if e.stdout else str(e)


def _run_agent_for_repo_impl(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    update_queue: multiprocessing.Queue,
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

    # before starting, display all information to terminal
    update_queue.put(("start_repo", (repo_name, 0)))

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

    # Resume: rebuild the branch from host-persisted per-module patches so a run
    # stopped by a subscription limit/kill continues without redoing finished
    # modules (their .done markers then skip them). No-op unless resuming.
    if os.environ.get("KAIJU_RESUME") == "1":
        from agent.resume_state import restore_prior_progress
        restore_prior_progress(
            local_repo, example["base_commit"], branch,
            Path(log_dir).parent, repo_name, logger)

    # get target files to edit and test files to run
    target_edit_files, import_dependencies = get_target_edit_files(
        local_repo,
        example["src_dir"],
        example["test"]["test_dir"],
        branch,
        example["reference_commit"],
        agent_config.use_topo_sort_dependencies,
    )
    logger.info("Found %d target edit files for %s", len(target_edit_files), repo_name)

    # Stage 2/3 safety net: `get_target_edit_files` scans the CURRENT working tree
    # for `    pass` bodies. Stage 1 fills those stubs, so on stages 2/3 the scan
    # returns 0 files and the entire refine stage silently does nothing (0/N score).
    # Fall back to a base_commit stub scan (`raise NotImplementedError`) which is
    # deterministic across all 3 stages — mirrors JS/C/Go/CPP/Rust behavior.
    if not target_edit_files:
        try:
            from agent.agent_utils import files_stubbed_at_commit, _find_files_to_edit
            from commit0.harness.constants import PYTHON_STUB_MARKER
            _all_files, _ = _find_files_to_edit(
                str(local_repo.working_dir),
                example["src_dir"],
                example["test"]["test_dir"],
            )
            _base_stubbed = files_stubbed_at_commit(
                local_repo, _all_files, example["base_commit"], PYTHON_STUB_MARKER,
            )
            if _base_stubbed:
                target_edit_files = sorted(_base_stubbed)
                logger.info(
                    "stage 2/3 recovery: target_edit_files rebuilt from base_commit %s: %d files",
                    example["base_commit"][:8], len(target_edit_files),
                )
        except Exception as _e:  # noqa: BLE001
            logger.warning("base_commit stub scan for target_edit_files failed: %s", _e)

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

    # prepare the log dir — STABLE "current" (not a per-run timestamp) so a
    # re-run finds the prior stage's .done/output.json for crash-resilience and
    # resume, exactly like go/rust. ATIF globs by content, not dir name.
    experiment_log_dir = Path(log_dir) / repo_name / branch / "current"
    experiment_log_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Experiment log directory: %s", experiment_log_dir)

    # Per-module crash-resilience machinery (mirrors run_agent_go.py): capture
    # thinking/turns/metrics, and write each module's output.json + .done the
    # moment it finishes so a killed worker keeps every completed module.
    thinking_capture: Optional[ThinkingCapture] = (
        ThinkingCapture() if getattr(agent_config, "capture_thinking", False) else None
    )
    metadata = build_metadata(
        dataset_path=commit0_config_file,
        max_iterations=agent_config.max_iteration,
        model_short=getattr(agent_config, "model_short", agent_config.model_name),
        dataset_id=example.get("id"),
    )
    module_instance_id = example.get("instance_id", repo_name)
    # Maps each module -> the repo-relative file(s) it owns, so its output.json
    # patch records only its own changes.
    module_rel_files: dict = {}

    eval_results = {}
    # write agent_config to .agent.yaml in the log_dir for record
    agent_config_log_file = experiment_log_dir / ".agent.yaml"
    try:
        with open(agent_config_log_file, "w") as agent_config_file:
            yaml.dump(agent_config, agent_config_file)
    except OSError as e:
        logger.error("Failed to write agent config to %s: %s", agent_config_log_file, e)
        raise

    with DirContext(repo_path):
        if agent_config is None:
            raise ValueError("Invalid input")

        if agent_config.run_tests:
            update_queue.put(("start_repo", (repo_name, len(test_files))))
            # when unit test feedback is available, iterate over test files
            for test_file in test_files:
                update_queue.put(("set_current_file", (repo_name, test_file)))
                test_cmd = f"{sys.executable} -m commit0 test {repo_path} {test_file} --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout {agent_test_timeout_sec()}"
                test_file_name = test_file.replace(".py", "").replace("/", "__")
                test_log_dir = experiment_log_dir / test_file_name
                module_rel_files[test_file_name] = list(target_edit_files)
                if _is_module_done(test_log_dir):
                    logger.info("Skipping %s (already done)", test_file_name)
                    continue
                mark_module_started(test_log_dir)  # no-limbo: kill at any instant leaves .needs_retry (or .done)
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(test_log_dir) / "turns.jsonl")
                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                message, spec_costs = get_message(
                    agent_config, repo_path, test_files=[test_file]
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                # display the test file to terminal
                agent_return = None
                agent_return = None
                _module_ok = False
                for _mret in range(INLINE_MODULE_MAX_RETRIES):
                    try:
                        with capture_module_calls(
                            thinking_capture=thinking_capture,
                            module=test_file_name,
                            log_dir=test_log_dir,
                        ):
                            agent_return = run_with_recovery(
                                agent.run,
                                "",
                                test_cmd,
                                lint_cmd,
                                target_edit_files,
                                test_log_dir,
                                _kaiju_log_dir=test_log_dir,
                                test_first=True,
                                thinking_capture=thinking_capture,
                                current_stage="test",
                                current_module=test_file_name,
                                max_test_output_length=agent_config.max_test_output_length,
                                spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            )
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
                if not _module_ok or agent_return is None:
                    continue
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                summarizer_cost = sum(c.cost for c in spec_costs) + getattr(
                    agent_return, "test_summarizer_cost", 0.0
                )
                # after running the agent, update the money display
                update_queue.put(
                    (
                        "update_money_display",
                        (
                            repo_name,
                            test_file,
                            agent_return.last_cost + summarizer_cost,
                        ),
                    )
                )
                _mark_module_done(test_log_dir)
                # Write THIS module's output.json now (crash-resilient): a worker
                # killed after this point keeps output.json for the module. Mark
                # .done FIRST so resume never re-runs (and double-counts) a module
                # whose write already happened — matches all sibling runners.
                _write_module_output(
                    thinking_capture=thinking_capture,
                    module_log_dir=test_log_dir,
                    module_name=test_file_name,
                    stage="test",
                    local_repo=local_repo,
                    base_commit=example["base_commit"],
                    module_rel_files=module_rel_files,
                    instance_id=module_instance_id,
                    metadata=metadata,
                )
        elif agent_config.run_entire_dir_lint:
            update_queue.put(("start_repo", (repo_name, len(lint_files))))
            # when unit test feedback is available, iterate over test files
            for lint_file in lint_files:
                update_queue.put(("set_current_file", (repo_name, lint_file)))
                lint_file_name = lint_file.replace(".py", "").replace("/", "__")
                lint_log_dir = experiment_log_dir / lint_file_name
                module_rel_files[lint_file_name] = [lint_file]
                if _is_module_done(lint_log_dir):
                    logger.info("Skipping %s (already done)", lint_file_name)
                    continue
                mark_module_started(lint_log_dir)  # no-limbo: kill at any instant leaves .needs_retry (or .done)
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(lint_log_dir) / "turns.jsonl")
                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )

                # display the test file to terminal
                agent_return = None
                _module_ok = False
                for _mret in range(INLINE_MODULE_MAX_RETRIES):
                    try:
                        with capture_module_calls(
                            thinking_capture=thinking_capture,
                            module=lint_file_name,
                            log_dir=lint_log_dir,
                        ):
                            agent_return = run_with_recovery(
                                agent.run,
                                "",
                                "",
                                lint_cmd,
                                [lint_file],
                                lint_log_dir,
                                _kaiju_log_dir=lint_log_dir,
                                lint_first=True,
                                thinking_capture=thinking_capture,
                                current_stage="lint",
                                current_module=lint_file_name,
                            )
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
                if not _module_ok or agent_return is None:
                    continue
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                # after running the agent, update the money display
                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, lint_file, agent_return.last_cost),
                    )
                )
                _mark_module_done(lint_log_dir)
                # Write THIS module's output.json now (crash-resilient): mark
                # .done FIRST so resume never re-runs (and double-counts) a module
                # whose write already happened — matches all sibling runners.
                _write_module_output(
                    thinking_capture=thinking_capture,
                    module_log_dir=lint_log_dir,
                    module_name=lint_file_name,
                    stage="lint",
                    local_repo=local_repo,
                    base_commit=example["base_commit"],
                    module_rel_files=module_rel_files,
                    instance_id=module_instance_id,
                    metadata=metadata,
                )
        else:
            # when unit test feedback is not available, iterate over target files to edit
            message, spec_costs = get_message(
                agent_config, repo_path, test_files=test_files
            )
            spec_summarizer_cost = sum(c.cost for c in spec_costs)

            update_queue.put(("start_repo", (repo_name, len(target_edit_files))))
            spec_cost_reported = False
            for f in target_edit_files:
                update_queue.put(("set_current_file", (repo_name, f)))
                if agent_config.add_import_module_to_context:
                    dependencies = import_dependencies.get(f, [])
                    message = update_message_with_dependencies(message, dependencies)
                file_name = f.replace(".py", "").replace("/", "__")
                file_log_dir = experiment_log_dir / file_name
                module_rel_files[file_name] = [f]
                if _is_module_done(file_log_dir):
                    logger.info("Skipping %s (already done)", file_name)
                    continue
                mark_module_started(file_log_dir)  # no-limbo: kill at any instant leaves .needs_retry (or .done)
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(file_log_dir) / "turns.jsonl")
                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                agent_return = None
                _module_ok = False
                for _mret in range(INLINE_MODULE_MAX_RETRIES):
                    try:
                        with capture_module_calls(
                            thinking_capture=thinking_capture,
                            module=file_name,
                            log_dir=file_log_dir,
                        ):
                            agent_return = run_with_recovery(
                                agent.run, message, "", lint_cmd, [f], file_log_dir,
                                _kaiju_log_dir=file_log_dir,
                                thinking_capture=thinking_capture,
                                current_stage="draft",
                                current_module=file_name,
                            )
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
                if not _module_ok or agent_return is None:
                    continue
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                # Add spec summarizer cost only once (first file), not per-file
                file_cost = agent_return.last_cost
                if not spec_cost_reported:
                    file_cost += spec_summarizer_cost
                    spec_cost_reported = True
                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, file_name, file_cost),
                    )
                )
                _mark_module_done(file_log_dir)
                # Write THIS module's output.json now (crash-resilient): mark
                # .done FIRST so resume never re-runs (and double-counts) a module
                # whose write already happened — matches all sibling runners.
                _write_module_output(
                    thinking_capture=thinking_capture,
                    module_log_dir=file_log_dir,
                    module_name=file_name,
                    stage="draft",
                    local_repo=local_repo,
                    base_commit=example["base_commit"],
                    module_rel_files=module_rel_files,
                    instance_id=module_instance_id,
                    metadata=metadata,
                )
    if agent_config.record_test_for_each_commit:
        try:
            with open(experiment_log_dir / "eval_results.json", "w") as f:
                json.dump(eval_results, f)
        except OSError as e:
            logger.error(
                "Failed to write eval results to %s: %s",
                experiment_log_dir / "eval_results.json",
                e,
            )
            raise

    # Stage-wise cumulative patch alongside the per-module output.json.
    from agent.stage_patch import write_stage_patch
    write_stage_patch(local_repo, example["base_commit"], experiment_log_dir, logger)

    # IDEMPOTENT BACKSTOP (not the primary write). output.json is now written
    # per-module inside each stage loop the moment the module finishes, so a
    # worker killed mid-run keeps output.json for every completed module. This
    # backstop only fills output.json for a turn-bearing module that STILL
    # lacks one (e.g. a module marked `.done` by a PRIOR run that predates the
    # in-loop write and is skipped by `_is_module_done` on resume, or whose
    # in-loop write raised and was swallowed). It NEVER touches a module that
    # already has output.json, so it cannot double-write or double-count.
    if thinking_capture is not None:
        modules_seen: set[str] = set()
        for turn in thinking_capture.turns:
            if turn.module and turn.module not in modules_seen:
                modules_seen.add(turn.module)
        for module_name in modules_seen:
            module_log_dir = experiment_log_dir / module_name
            if (module_log_dir / "output.json").exists():
                continue  # already written in-loop; don't rewrite / double-count
            module_turns = thinking_capture.get_module_turns(module_name)
            if not module_turns:
                continue
            stage = module_turns[0].stage or "unknown"
            _write_module_output(
                thinking_capture=thinking_capture,
                module_log_dir=module_log_dir,
                module_name=module_name,
                stage=stage,
                local_repo=local_repo,
                base_commit=example["base_commit"],
                module_rel_files=module_rel_files,
                instance_id=module_instance_id,
                metadata=metadata,
            )

    update_queue.put(("finish_repo", repo_name))


def run_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    update_queue: multiprocessing.Queue,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> Tuple[str, bool]:
    """Run Aider for one repo with per-repo error isolation (R-001).

    Any failure inside the worker is caught and logged so that one bad repo
    cannot tear down the whole parallel batch. Always emits a ``finish_repo``
    update for the display and returns ``(repo_name, ok)`` instead of raising.
    """
    _, repo_name = example["repo"].split("/")
    try:
        _run_agent_for_repo_impl(
            repo_base_dir,
            agent_config,
            example,
            branch,
            update_queue,
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
        # Ensure the live display does not leave this repo stuck "in progress".
        try:
            update_queue.put(("finish_repo", repo_name))
        except Exception:
            logger.debug("Could not emit finish_repo for %s", repo_name)
        return repo_name, False


def _collect_worker_results(results: list) -> dict:
    """Collect AsyncResults resiliently: one failure never aborts the rest.

    Mirrors the isolation already used in run_agent_java.py. Returns a summary
    with success/failure counts and the names of repos that reported failure.
    """
    succeeded = 0
    failed = 0
    failed_repos: list = []
    for result in results:
        try:
            value = result.get()
        except Exception:
            failed += 1
            logger.error(
                "A worker raised before returning a status; isolating", exc_info=True
            )
            continue
        if isinstance(value, tuple) and len(value) == 2:
            repo_name, ok = value
            if ok:
                succeeded += 1
            else:
                failed += 1
                failed_repos.append(repo_name)
        else:
            # Backwards-compatible: a bare return counts as success.
            succeeded += 1
    return {"succeeded": succeeded, "failed": failed, "failed_repos": failed_repos}


def run_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
    display_repo_progress_num: int,
) -> None:
    """Main function to run Aider for a given repository."""
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

    with TerminalDisplay(len(filtered_dataset)) as display:
        not_started_repos = [
            cast(RepoInstance, example)["repo"].split("/")[-1]
            for example in filtered_dataset
        ]
        display.set_not_started_repos(not_started_repos)

        start_time = time.time()

        display.update_repo_progress_num(
            min(display_repo_progress_num, max_parallel_repos)
        )
        display.update_backend_display(backend)
        display.update_log_dir_display(log_dir)
        display.update_agent_display(
            agent_config.agent_name,
            agent_config.model_name,
            agent_config.run_tests,
            agent_config.use_topo_sort_dependencies,
            agent_config.use_repo_info,
            agent_config.use_unit_tests_info,
            agent_config.use_spec_info,
            agent_config.use_lint_info,
        )
        display.update_branch_display(branch)
        with multiprocessing.Manager() as manager:
            update_queue = manager.Queue()
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
                            update_queue,
                            override_previous_changes,
                            backend,
                            log_dir,
                            commit0_config_file,
                        ),
                    )
                    results.append(result)

                last_time_update = 0
                while any(not r.ready() for r in results):
                    try:
                        while not update_queue.empty():
                            action, data = update_queue.get_nowait()
                            if action == "start_repo":
                                repo_name, total_files = data
                                display.start_repo(repo_name, total_files)
                            elif action == "finish_repo":
                                repo_name = data
                                display.finish_repo(repo_name)
                            elif action == "set_current_file":
                                repo_name, file_name = data
                                display.set_current_file(repo_name, file_name)
                            elif action == "update_money_display":
                                repo_name, file_name, money_spent = data
                                display.update_money_display(
                                    repo_name, file_name, money_spent
                                )
                    except queue.Empty:
                        logger.debug("Queue empty, waiting for worker updates")

                    # Update time display every second
                    current_time = time.time()
                    if current_time - last_time_update >= 1:
                        elapsed_time = int(current_time - start_time)
                        display.update_time_display(elapsed_time)
                        last_time_update = current_time

                    time.sleep(0.1)  # Small delay to prevent busy-waiting

                # Final update after all repos are processed
                while not update_queue.empty():
                    action, data = update_queue.get()
                    if action == "start_repo":
                        repo_name, total_files = data
                        display.start_repo(repo_name, total_files)
                    elif action == "finish_repo":
                        repo_name = data
                        display.finish_repo(repo_name)
                    elif action == "set_current_file":
                        repo_name, file_name = data
                        display.set_current_file(repo_name, file_name)
                    elif action == "update_money_display":
                        repo_name, file_name, money_spent = data
                        display.update_money_display(repo_name, file_name, money_spent)

                # Final time update
                elapsed_time = int(time.time() - start_time)
                display.update_time_display(elapsed_time)

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
