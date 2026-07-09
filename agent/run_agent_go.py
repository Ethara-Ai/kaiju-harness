"""Go agent runner for commit0.

Mirrors run_agent.py but uses Go-specific splits, test IDs, utilities,
and agent configuration. Orchestrates parallel agent execution across
Go repositories.
"""

import logging
import multiprocessing
import os
import queue
import subprocess
import sys
import time
import json
import yaml
from pathlib import Path
from typing import Optional
from types import TracebackType

import git

from agent.agents_go import AiderGoAgents
from agent.agents import TransientLLMError
from agent.agent_utils_go import (
    collect_go_test_files,
    create_branch,
    get_go_lint_cmd,
    get_go_message,
    get_target_edit_files,
    load_agent_config,
)
from agent.class_types import AgentConfig
from agent.display import TerminalDisplay
from agent.thinking_capture import ThinkingCapture
from agent.llm_cost_capture import capture_module_calls
from agent.trajectory_writer import write_trajectory_md
from agent.output_writer import build_metadata
from agent.module_patch import module_file_patch
from agent.openhands_formatter import write_module_output_json
from agent.run_agent_no_rich import (
    _make_blind_lint_cmd,
    _make_blind_test_cmd,
    _make_names_only_test_cmd,
)
from commit0.harness.constants_go import (
    GO_SPLIT,
    GO_STUB_MARKER,
)
from commit0.harness.split_utils import resolve_split
from commit0.harness.get_go_test_ids import main as get_go_test_ids
from commit0.harness.utils import load_dataset_from_config
from agent.claude_code.recovery import run_with_recovery

logger = logging.getLogger(__name__)

_CLI_GO_PATH = str(Path(__file__).resolve().parent.parent / "commit0" / "cli_go.py")

RUN_AGENT_LOG_DIR = Path("logs/agent")

# Per-test-run timeout (seconds) handed to `cli_go test`/`evaluate` via
# --timeout. This bounds the actual `go test` inside the container
# (execution_context.exec_run_with_timeout does the process-group reaping of a
# hung run). The old hardcoded 100s was far too short: go tests with -race,
# integration suites, or many packages routinely exceed it, so the agent's own
# test runs got falsely killed mid-coding and the model saw a spurious
# timeout/failure. Rationalized to match the Rust baseline (KAIJU_TEST_TIMEOUT,
# default 600s). Kept strictly below the inactivity watchdog (900s in
# run_pipeline_go.sh) so the container-level timeout fires — and the agent gets
# a real test result — before the watchdog would kill the agent for inactivity.
_GO_TEST_TIMEOUT_DEFAULT = 600
_GO_TEST_TIMEOUT_MAX = 840  # < 900s inactivity watchdog, leaves margin for teardown


def _go_test_timeout() -> int:
    """Return the per-test-run timeout in seconds (KAIJU_TEST_TIMEOUT override).

    Falls back to the 600s default on unset/blank/non-numeric/non-positive
    values, and clamps to stay safely under the inactivity watchdog so a slow
    (but progressing) test run cannot be misattributed as an agent hang.
    """
    raw = os.environ.get("KAIJU_TEST_TIMEOUT", "").strip()
    if not raw:
        return _GO_TEST_TIMEOUT_DEFAULT
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "Invalid KAIJU_TEST_TIMEOUT=%r; falling back to %ds",
            raw,
            _GO_TEST_TIMEOUT_DEFAULT,
        )
        return _GO_TEST_TIMEOUT_DEFAULT
    if val <= 0:
        logger.warning(
            "Non-positive KAIJU_TEST_TIMEOUT=%d; falling back to %ds",
            val,
            _GO_TEST_TIMEOUT_DEFAULT,
        )
        return _GO_TEST_TIMEOUT_DEFAULT
    return min(val, _GO_TEST_TIMEOUT_MAX)


def _read_commit0_go_config(config_file: str) -> dict:
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class DirContext:
    def __init__(self, d: str):
        self.dir = d
        self.cwd = os.getcwd()

    def __enter__(self):
        os.chdir(self.dir)

    def __exit__(
        self,
        exctype: Optional[type[BaseException]],
        excinst: Optional[BaseException],
        exctb: Optional[TracebackType],
    ) -> None:
        os.chdir(self.cwd)


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
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
    except Exception:  # noqa: BLE001
        pass
    logger.error("Module %s failed after retries (%s) — skipping so the repo "
                 "continues; left .needs_retry for --resume.", module_name, err)


def _write_module_output(
    *,
    thinking_capture: Optional[ThinkingCapture],
    module_log_dir: Path,
    module_name: str,
    stage: str,
    local_repo,
    base_commit: str,
    module_rel_files: dict[str, list[str]],
    instance_id: str,
    metadata: dict,
) -> None:
    """Write ONE module's output.json right after that module finishes.

    Crash-resilience: mirrors the Rust runner (run_rust_agent.py
    ``_module_file_patch`` + per-module ``write_module_output_json``). Called
    from inside each stage loop so a worker killed mid-run keeps output.json for
    every already-completed module (the old post-loop write lost them all).

    The per-module patch is ``git diff base_commit..HEAD -- <this module's own
    files>``. Because modules own disjoint files, this is byte-identical to what
    the old post-loop computed for the same module — same base, same HEAD, same
    file set. Best-effort: a failure here never breaks the run.
    """
    if thinking_capture is None:
        return
    module_turns = thinking_capture.get_module_turns(module_name)
    # Mirror Rust's ``if module_turns:`` guard — a module that produced no turns
    # (e.g. agent no-op) gets no output.json, exactly as the old post-loop
    # (which iterated only modules that appeared on a turn) behaved.
    if not module_turns:
        return
    module_metrics = thinking_capture.get_module_metrics(module_name)
    # Scope this module's patch to the file(s) IT owns, so its output.json
    # records only its own changes (not the whole branch). Disjoint files ⇒
    # this equals the post-loop result.
    module_patch = module_file_patch(
        local_repo,
        base_commit,
        "HEAD",
        module_rel_files.get(module_name, []),
        logger=logger,
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
    except Exception as e:
        logger.warning(
            "Failed to write module output JSON for %s: %s", module_name, e
        )


def run_eval_after_each_commit(
    branch: str, backend: str, commit0_config_file: str
) -> str:
    eval_cmd = f"{sys.executable} {_CLI_GO_PATH} evaluate --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout {_go_test_timeout()}"
    try:
        result = subprocess.run(
            eval_cmd.split(), capture_output=True, text=True, check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error("Error running eval command: %s", e, exc_info=True)
        return e.stdout if e.stdout else str(e)


def run_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: dict,
    branch: str,
    update_queue: multiprocessing.Queue,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    _, repo_name = example["repo"].split("/")

    update_queue.put(("start_repo", (repo_name, 0)))

    repo_path = os.path.join(repo_base_dir, repo_name)
    repo_path = os.path.abspath(repo_path)

    try:
        local_repo = git.Repo(repo_path)
    except Exception:
        logger.error(
            "Failed to open repo at %s: not a git repo", repo_path, exc_info=True
        )
        raise Exception(
            f"{repo_path} is not a git repo. Check if base_dir is correctly specified."
        ) from None

    agent = AiderGoAgents(
        agent_config.max_iteration,
        agent_config.model_name,
        agent_config.cache_prompts,
    )

    if local_repo.is_dirty():
        logger.warning("Auto-committing uncommitted changes in %s", repo_path)
        local_repo.git.add(A=True)
        local_repo.index.commit("left from last change")

    create_branch(local_repo, branch, override=override_previous_changes)

    latest_commit = local_repo.commit(branch)
    if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
        logger.warning(
            "Resetting %s to base commit %s (override_previous_changes=True)",
            repo_name,
            example["base_commit"],
        )
        local_repo.git.reset("--hard", example["base_commit"])

    # Resume: rebuild the branch from host-persisted per-module patches so a run
    # stopped by a subscription limit/kill continues WITHOUT redoing finished
    # modules (their .done markers below then skip them). No-op unless resuming.
    if os.environ.get("KAIJU_RESUME") == "1":
        from agent.resume_state import restore_prior_progress
        restore_prior_progress(
            local_repo, example["base_commit"], branch,
            Path(log_dir).parent, repo_name, logger)

    src_dir = example.get("src_dir", ".")
    reference_commit = example.get("reference_commit", "HEAD")

    target_edit_files = get_target_edit_files(
        repo_path, src_dir, branch, reference_commit
    )
    # Convert to relative paths for consistent log directory naming
    target_edit_files_rel = [os.path.relpath(f, repo_path) for f in target_edit_files]
    if agent_config.strip_non_stubs:
        orig_count = len(target_edit_files)
        target_edit_files = [
            f for f in target_edit_files
            if Path(f).exists() and GO_STUB_MARKER in Path(f).read_text(errors="replace")
        ]
        target_edit_files_rel = [os.path.relpath(f, repo_path) for f in target_edit_files]
        logger.info("strip_non_stubs: kept %d/%d target files", len(target_edit_files), orig_count)
    test_files = collect_go_test_files(repo_path)
    test_files_readonly = test_files
    logger.info("Found %d target edit files for %s", len(target_edit_files), repo_name)

    test_files_str = [xx for x in get_go_test_ids(repo_name, verbose=0) for xx in x]

    experiment_log_dir = Path(log_dir) / repo_name / branch / "current"
    experiment_log_dir.mkdir(parents=True, exist_ok=True)

    eval_results = {}
    # Maps each module name (as recorded on thinking_capture turns) to the
    # repo-relative source file(s) THAT module owns/edits, so per-module
    # output.json records only that module's own file changes.
    module_rel_files: dict[str, list[str]] = {}
    thinking_capture: Optional[ThinkingCapture] = None
    if agent_config.capture_thinking:
        thinking_capture = ThinkingCapture()

    agent_config_log_file = experiment_log_dir / ".agent.yaml"
    try:
        with open(agent_config_log_file, "w") as acf:
            yaml.dump(agent_config, acf)
    except OSError as e:
        logger.error("Failed to write agent config to %s: %s", agent_config_log_file, e)
        raise

    # Build the output.json metadata ONCE up front so each stage loop can write
    # its module's output.json the moment that module completes (crash-resilient,
    # matching the Rust runner). Cheap and side-effect-free, so computing it even
    # when capture_thinking is off is harmless (the per-module writer no-ops when
    # thinking_capture is None).
    metadata = build_metadata(
        dataset_path=commit0_config_file,
        max_iterations=agent_config.max_iteration,
        model_short=getattr(agent_config, "model_short", agent_config.model_name),
        dataset_id=example.get("id"),
    )
    module_instance_id = example.get("instance_id", repo_name)

    with DirContext(repo_path):
        if agent_config.run_tests:
            update_queue.put(("start_repo", (repo_name, len(test_files_str))))
            for test_id in test_files_str:
                if not test_id.strip():
                    continue
                update_queue.put(("set_current_file", (repo_name, test_id)))
                test_cmd = f"{sys.executable} {_CLI_GO_PATH} test {repo_path} {test_id} --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout {_go_test_timeout()}"
                if agent_config.blind_tests:
                    test_cmd = _make_blind_test_cmd(test_cmd)
                elif agent_config.names_only_tests:
                    test_cmd = _make_names_only_test_cmd(test_cmd)
                short_test_id = (
                    test_id.rsplit("/", 1)[-1] if "/" in test_id else test_id
                )
                test_id_safe = short_test_id.replace("/", "__").replace(".", "_")
                # This module is a (read-only) test module; scope its patch to
                # the source files the model was allowed to edit.
                module_rel_files[test_id_safe] = list(target_edit_files_rel)
                test_log_dir = experiment_log_dir / test_id_safe
                if _is_module_done(test_log_dir):
                    logger.info("Skipping %s (already done)", test_id_safe)
                    continue

                # E6: flush each turn live so a killed worker keeps a partial trajectory.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(test_log_dir) / "turns.jsonl")

                lint_cmd = (
                    get_go_lint_cmd(
                        repo_name,
                        commit0_config_file,
                    )
                    if agent_config.use_lint_info
                    else ""
                )
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd(lint_cmd)
                message, spec_costs = get_go_message(
                    agent_config,
                    repo_path,
                    test_files,
                    commit0_config_file=commit0_config_file,
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=test_id_safe,
                    log_dir=test_log_dir,
                ):
                    try:
                        agent_return = run_with_recovery(agent.run,
                            message,
                            test_cmd,
                            lint_cmd,
                            target_edit_files,
                            test_log_dir,
                            test_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="test",
                            current_module=test_id_safe,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        _kaiju_log_dir=test_log_dir,)
                    except TransientLLMError as _tle:
                        _skip_failed_module(test_log_dir, test_id_safe, _tle)
                        continue
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, test_id, agent_return.last_cost),
                    )
                )
                _mark_module_done(test_log_dir)
                # Write THIS module's output.json now (crash-resilient): a worker
                # killed after this point keeps output.json for the module.
                _write_module_output(
                    thinking_capture=thinking_capture,
                    module_log_dir=test_log_dir,
                    module_name=test_id_safe,
                    stage="test",
                    local_repo=local_repo,
                    base_commit=example["base_commit"],
                    module_rel_files=module_rel_files,
                    instance_id=module_instance_id,
                    metadata=metadata,
                )
        elif agent_config.run_entire_dir_lint:
            lint_cmd = get_go_lint_cmd(
                repo_name,
                commit0_config_file,
            )
            if agent_config.blind_lint and lint_cmd:
                lint_cmd = _make_blind_lint_cmd(lint_cmd)
            update_queue.put(("start_repo", (repo_name, len(target_edit_files_rel))))
            for edit_file, edit_file_rel in zip(
                target_edit_files, target_edit_files_rel
            ):
                update_queue.put(("set_current_file", (repo_name, edit_file_rel)))
                file_name = edit_file_rel.replace(".go", "").replace("/", "__")
                # This module owns exactly its own edit file.
                module_rel_files[file_name] = [edit_file_rel]
                lint_log_dir = experiment_log_dir / file_name
                if _is_module_done(lint_log_dir):
                    logger.info("Skipping %s (already done)", file_name)
                    continue

                # E6: flush each turn live so a killed worker keeps a partial trajectory.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(lint_log_dir) / "turns.jsonl")

                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=file_name,
                    log_dir=lint_log_dir,
                ):
                    try:
                        agent_return = run_with_recovery(agent.run,
                            "",
                            "",
                            lint_cmd,
                            [edit_file],
                            lint_log_dir,
                            lint_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="lint",
                            current_module=file_name,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        _kaiju_log_dir=lint_log_dir,)
                    except TransientLLMError as _tle:
                        _skip_failed_module(lint_log_dir, file_name, _tle)
                        continue
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, edit_file, agent_return.last_cost),
                    )
                )
                _mark_module_done(lint_log_dir)
                # Write THIS module's output.json now (crash-resilient).
                _write_module_output(
                    thinking_capture=thinking_capture,
                    module_log_dir=lint_log_dir,
                    module_name=file_name,
                    stage="lint",
                    local_repo=local_repo,
                    base_commit=example["base_commit"],
                    module_rel_files=module_rel_files,
                    instance_id=module_instance_id,
                    metadata=metadata,
                )
        else:
            message, spec_costs = get_go_message(
                agent_config,
                repo_path,
                test_files,
                commit0_config_file=commit0_config_file,
            )
            if thinking_capture is not None:
                for c in spec_costs:
                    thinking_capture.summarizer_costs.add(c)

            update_queue.put(("start_repo", (repo_name, len(target_edit_files_rel))))
            for f, f_rel in zip(target_edit_files, target_edit_files_rel):
                update_queue.put(("set_current_file", (repo_name, f_rel)))
                file_name = f_rel.replace(".go", "").replace("/", "__")
                # This module owns exactly its own edit file.
                module_rel_files[file_name] = [f_rel]
                file_log_dir = experiment_log_dir / file_name
                if _is_module_done(file_log_dir):
                    logger.info("Skipping %s (already done)", file_name)
                    continue

                # E6: flush each turn live so a killed worker keeps a partial trajectory.
                if thinking_capture is not None:
                    thinking_capture.set_live_path(Path(file_log_dir) / "turns.jsonl")

                lint_cmd = (
                    get_go_lint_cmd(
                        repo_name,
                        commit0_config_file,
                    )
                    if agent_config.use_lint_info
                    else ""
                )
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd(lint_cmd)
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=file_name,
                    log_dir=file_log_dir,
                ):
                    try:
                        agent_return = run_with_recovery(agent.run,
                            message,
                            "",
                            lint_cmd,
                            [f],
                            file_log_dir,
                            thinking_capture=thinking_capture,
                            current_stage="draft",
                            current_module=file_name,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                        _kaiju_log_dir=file_log_dir,)
                    except TransientLLMError as _tle:
                        _skip_failed_module(file_log_dir, file_name, _tle)
                        continue
                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

                update_queue.put(
                    (
                        "update_money_display",
                        (repo_name, f, agent_return.last_cost),
                    )
                )
                _mark_module_done(file_log_dir)
                # Write THIS module's output.json now (crash-resilient).
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

    if thinking_capture is not None:
        if agent_config.trajectory_md:
            traj_path = experiment_log_dir / "trajectory.md"
            try:
                write_trajectory_md(traj_path, repo_name, thinking_capture.turns)
            except Exception as e:
                logger.warning("Failed to write trajectory.md: %s", e)

        # Full model-changes record (keeps tests/benches/manifests, strips
        # binary/cache/build noise) — distinct from the eval's scored patch.diff.
        from agent.stage_patch import write_stage_patch
        write_stage_patch(local_repo, example.get("base_commit", "HEAD"),
                          experiment_log_dir, logger)

        # IDEMPOTENT BACKSTOP (not the primary write). output.json is now written
        # per-module inside each stage loop the moment the module finishes, so a
        # worker killed mid-run keeps output.json for every completed module. This
        # backstop only fills output.json for a turn-bearing module that STILL
        # lacks one — the one real gap the in-loop write can't cover:
        #   * a module marked `.done` by a PRIOR run (which predates the in-loop
        #     write) is skipped by `_is_module_done` before its in-loop write can
        #     run, leaving it `.done` but output.json-less on resume.
        # It NEVER touches a module that already has output.json, so it cannot
        # double-write or double-count metrics. Modules with 0 turns are skipped,
        # exactly as before. `stage` is derived from the module's own turns (same
        # value the in-loop write's per-loop constant would produce).
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
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    config = _read_commit0_go_config(commit0_config_file)

    dataset = load_dataset_from_config(
        config["dataset_name"], split=config["dataset_split"]
    )
    repo_split = config["repo_split"]
    dataset = list(dataset)
    allowed_repos = set(resolve_split(repo_split, dataset, curated=GO_SPLIT))
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

    with TerminalDisplay(len(filtered_dataset)) as display:
        not_started_repos = [
            example["repo"].split("/")[-1] for example in filtered_dataset
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

                for example in filtered_dataset:
                    result = pool.apply_async(
                        run_agent_for_repo,
                        args=(
                            config["base_dir"],
                            agent_config,
                            example,
                            branch,
                            update_queue,
                            override_previous_changes,
                            backend,
                            log_dir,
                            commit0_config_file,
                        ),
                    )
                    results.append(result)

                last_time_update = 0.0
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

                    current_time = time.time()
                    if current_time - last_time_update >= 1:
                        elapsed_time = int(current_time - start_time)
                        display.update_time_display(elapsed_time)
                        last_time_update = current_time

                    time.sleep(0.1)

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

                elapsed_time = int(time.time() - start_time)
                display.update_time_display(elapsed_time)

                # Collect every worker. A bare `result.get()` re-raised the
                # FIRST failing worker and abandoned the rest, discarding their
                # already-written trajectories/output.json. Isolate per-worker
                # failures (mirrors run_rust_agent.py's E8) so one bad repo can't
                # sink the batch; only a TOTAL wipeout is treated as systemic.
                n_failed = 0
                for result in results:
                    try:
                        result.get()
                    except Exception as werr:  # noqa: BLE001
                        n_failed += 1
                        logger.error(
                            "Go agent worker failed: %s", werr, exc_info=True
                        )
                logger.info(
                    "All %d agent workers completed (%d failed)",
                    len(results),
                    n_failed,
                )
                if n_failed and n_failed == len(results):
                    raise RuntimeError(
                        f"All {len(results)} Go agent workers failed — "
                        f"systemic fault, aborting."
                    )
