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
)
import logging

logger = logging.getLogger(__name__)


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
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
        )

    with DirContext(repo_path):
        if agent_config is None:
            raise ValueError("Invalid input")

        if agent_config.run_tests:
            for test_file in test_files:
                test_file_name = test_file.replace(".py", "").replace("/", "__")
                test_log_dir = experiment_log_dir / test_file_name

                if _is_module_done(test_log_dir):
                    logger.info(
                        f"Skipping already-completed test module: {test_file_name}"
                    )
                    continue

                test_cmd = f"{sys.executable} -m commit0 test {repo_path} {test_file} --branch {branch} --backend {backend} --commit0-config-file {commit0_config_file} --timeout 100"
                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )
                message, spec_costs = get_message(
                    agent_config, repo_path, test_files=[test_file]
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                with capture_module_calls(
                                    thinking_capture,
                                    module=test_file_name,
                                    log_dir=test_log_dir,
                                    model_short=agent_config.model_short,
                                ):
                    _ = agent.run(
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
                    )
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
                            stage_runtime_seconds=module_elapsed,
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

                if _is_module_done(lint_log_dir):
                    logger.info(f"Skipping already-linted file: {lint_file_name}")
                    continue

                lint_cmd = get_lint_cmd(
                    repo_name, agent_config.use_lint_info, commit0_config_file
                )

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                with capture_module_calls(
                                    thinking_capture,
                                    module=lint_file_name,
                                    log_dir=lint_log_dir,
                                    model_short=agent_config.model_short,
                                ):
                    _ = agent.run(
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
                    )
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
                            stage_runtime_seconds=module_elapsed,
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
                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                with capture_module_calls(
                                    thinking_capture,
                                    module=file_name,
                                    log_dir=file_log_dir,
                                    model_short=agent_config.model_short,
                                ):
                    _ = agent.run(
                        iter_message,
                        "",
                        lint_cmd,
                        [f],
                        file_log_dir,
                        thinking_capture=thinking_capture,
                        current_stage="draft",
                        current_module=file_name,
                        test_files_readonly=test_files_readonly,
                    )
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
                            stage_runtime_seconds=module_elapsed,
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
