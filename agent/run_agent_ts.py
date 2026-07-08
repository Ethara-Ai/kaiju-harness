"""Per-repo agent orchestration for TypeScript — analogue of run_agent_no_rich.py."""

import os
import time
import yaml
import multiprocessing
from tqdm import tqdm
from git import Repo
from agent.agent_utils import (
    create_branch,
    load_agent_config,
)
from agent.agent_utils_ts import (
    get_target_edit_files_ts,
    get_ts_lint_cmd,
    get_changed_ts_files_from_commits,
    get_message_ts,
)
import shlex
import sys
from agent.agents_ts import TsAiderAgents
from typing import cast
from agent.class_types import AgentConfig
from agent.thinking_capture import ThinkingCapture
from agent.llm_cost_capture import capture_module_calls
from agent.module_patch import module_file_patch
from agent import agent_utils_ts_compile_gate as _ts_compile_gate
from commit0.harness.constants_ts import TS_SPLIT, TS_STUB_MARKER
from commit0.harness.split_utils import resolve_split
from commit0.harness.get_ts_test_ids import main as get_ts_tests
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.utils import load_dataset_from_config
from commit0.cli_ts import read_commit0_ts_config_file
from pathlib import Path
from agent.run_agent import DirContext
from agent.run_agent_no_rich import (
    _is_module_done,
    _mark_module_done,
    _get_stable_log_dir,
    _make_blind_test_cmd,
    _make_blind_lint_cmd,
    _make_names_only_test_cmd,
)
import logging

import typer
from agent.claude_code.recovery import run_with_recovery

logger = logging.getLogger(__name__)

app = typer.Typer()


def run_agent_for_repo_ts(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run TsAiderAgents for a given TypeScript repository."""
    commit0_config = read_commit0_ts_config_file(commit0_config_file)

    ds_name = commit0_config["dataset_name"]
    if "commit0" not in ds_name and not ds_name.endswith(".json"):
        raise ValueError(
            f"dataset_name must contain 'commit0' or end with '.json', got {ds_name!r}"
        )
    _, repo_name = example["repo"].split("/")

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

    try:
        if agent_config.agent_name == "aider":
            agent = TsAiderAgents(
                agent_config.max_iteration,
                agent_config.model_name,
                agent_config.cache_prompts,
            )
        else:
            raise NotImplementedError(
                f"{agent_config.agent_name} is not implemented for TS pipeline."
            )

        thinking_capture = (
            ThinkingCapture()
            if getattr(agent_config, "capture_thinking", False)
            else None
        )

        if local_repo.is_dirty():
            logger.warning("Auto-committing uncommitted changes in %s", repo_path)
            local_repo.git.add(A=True)
            local_repo.index.commit("left from last change")

        create_branch(local_repo, branch, example["base_commit"])

        latest_commit = local_repo.commit(branch)
        if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
            logger.warning(
                "Resetting %s to base commit %s (override_previous_changes=True)",
                repo_name,
                example["base_commit"],
            )
            local_repo.git.reset("--hard", example["base_commit"])

        # No topological sort for TS — flat list of target files, empty dep dict
        target_edit_files, _unused_deps = get_target_edit_files_ts(
            local_repo,
            example["src_dir"],
            example["test"]["test_dir"],
            branch,
            example["reference_commit"],
        )
        if agent_config.strip_non_stubs:
            orig_count = len(target_edit_files)
            target_edit_files = [
                f for f in target_edit_files
                if (Path(repo_path) / f).exists() and TS_STUB_MARKER in (Path(repo_path) / f).read_text(errors="replace")
            ]
            logger.info("strip_non_stubs: kept %d/%d target files", len(target_edit_files), orig_count)

        test_files_str = [xx for x in get_ts_tests(repo_name, verbose=0) for xx in x]
        # TS test IDs use ' > ' separator (e.g., 'test/foo.test.ts > describe > test')
        # Python uses ':' (e.g., 'tests/test_foo.py::TestClass::test_method')
        # Extract file path: everything before first ' > ', or before first ':'
        test_files_raw = sorted(
            list(
                set(
                    [
                        i.split(" > ")[0].strip() if " > " in i else i.split(":")[0]
                        for i in test_files_str
                        if i.strip()
                    ]
                )
            )
        )
        test_dir = example.get("test", {}).get("test_dir", "tests")
        test_files: list[str] = []
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
        test_files_readonly = [str(Path(repo_path) / tf) for tf in test_files]

        experiment_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)

        agent_config_log_file = experiment_log_dir / ".agent.yaml"
        try:
            with open(agent_config_log_file, "w") as agent_config_file:
                yaml.dump(agent_config, agent_config_file)
        except OSError as e:
            logger.error(
                "Failed to write agent config to %s: %s", agent_config_log_file, e
            )
            raise

        message = ""

        from agent.openhands_formatter import write_module_output_json

        instance_id = ""
        metadata: dict[str, object] = {}
        if thinking_capture is not None:
            from agent.output_writer import build_metadata

            commit0_config_for_meta = read_commit0_ts_config_file(commit0_config_file)
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
            if agent_config.run_tests:
                for test_file in test_files:
                    test_file_name = test_file.replace(".ts", "").replace("/", "__")
                    test_log_dir = experiment_log_dir / test_file_name

                    if _is_module_done(test_log_dir):
                        logger.info(
                            f"Skipping already-completed test module: {test_file_name}"
                        )
                        continue

                    test_cmd = (
                        f"{sys.executable} -m commit0.cli_ts test"
                        f" {shlex.quote(repo_path)}"
                        f" {shlex.quote(test_file)}"
                        f" --branch {shlex.quote(branch)}"
                        f" --commit0-config-file {shlex.quote(commit0_config_file)}"
                        f" --timeout 100"
                    )
                    if agent_config.blind_tests:
                        test_cmd = _make_blind_test_cmd(test_cmd)
                    elif agent_config.names_only_tests:
                        test_cmd = _make_names_only_test_cmd(test_cmd)
                    lint_cmd = get_ts_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)
                    message, spec_costs = get_message_ts(
                        agent_config, repo_path, test_files=[test_file]
                    )
                    if thinking_capture is not None:
                        for c in spec_costs:
                            thinking_capture.summarizer_costs.add(c)

                    pre_sha = local_repo.head.commit.hexsha
                    module_start = time.time()
                    _cg_enabled = _ts_compile_gate.is_enabled()
                    with capture_module_calls(
                        thinking_capture,
                        module=test_file_name,
                        log_dir=test_log_dir,
                    ):
                        def _invoke_agent_test():
                            return run_with_recovery(agent.run,
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
                                _kaiju_log_dir=test_log_dir,
                            )

                        def _reprompt_test(err_text):
                            return run_with_recovery(agent.run,
                                f"tsc --noEmit failed after your edits. Fix the regressions below WITHOUT changing public signatures.\n\n{err_text}",
                                test_cmd,
                                lint_cmd,
                                target_edit_files,
                                test_log_dir,
                                test_first=False,
                                thinking_capture=thinking_capture,
                                current_stage="test",
                                current_module=test_file_name,
                                max_test_output_length=agent_config.max_test_output_length,
                                spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                                test_files_readonly=test_files_readonly,
                                inject_test_files_readonly=agent_config.inject_test_files_readonly,
                                _kaiju_log_dir=test_log_dir,
                            )

                        if _cg_enabled:
                            _cg_result = _ts_compile_gate.run_with_compile_gate(
                                _invoke_agent_test,
                                repo_dir=repo_path,
                                local_repo=local_repo,
                                pre_sha=pre_sha,
                                max_retries=int(os.environ.get(
                                    "KAIJU_TS_COMPILE_GATE_MAX_RETRIES", "2"
                                )),
                                re_prompt_callback=_reprompt_test,
                                persist_dir=str(test_log_dir),
                            )
                            if _cg_result.get("status") == "reverted":
                                logger.warning(
                                    "compile_gate REVERTED %s to %s after %d retries (regressions: %s)",
                                    test_file_name,
                                    pre_sha[:8],
                                    _cg_result.get("retries_used", 0),
                                    _cg_result.get("regressions"),
                                )
                        else:
                            _ = _invoke_agent_test()
                    module_elapsed = time.time() - module_start
                    _mark_module_done(test_log_dir)

                    if thinking_capture is not None:
                        # test_file_name is a read-only test module; scope the
                        # patch to the source files this stage actually edits.
                        module_patch = module_file_patch(
                            local_repo,
                            example["base_commit"],
                            "HEAD",
                            target_edit_files,
                            logger=logger,
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
                                metrics=thinking_capture.get_module_metrics(
                                    test_file_name
                                ),
                                stage="test",
                                module_runtime_seconds=module_elapsed,
                            )

            elif agent_config.run_entire_dir_lint:
                message, spec_costs = get_message_ts(
                    agent_config, repo_path, test_files=test_files
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                lint_files = get_changed_ts_files_from_commits(
                    local_repo, "HEAD", example["base_commit"]
                )
                for lint_file in lint_files:
                    lint_file_name = lint_file.replace(".ts", "").replace("/", "__")
                    lint_log_dir = experiment_log_dir / lint_file_name

                    if _is_module_done(lint_log_dir):
                        logger.info(f"Skipping already-linted file: {lint_file_name}")
                        continue

                    lint_cmd = get_ts_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)

                    pre_sha = local_repo.head.commit.hexsha
                    module_start = time.time()
                    with capture_module_calls(
                        thinking_capture,
                        module=lint_file_name,
                        log_dir=lint_log_dir,
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
                    module_elapsed = time.time() - module_start
                    _mark_module_done(lint_log_dir)

                    if thinking_capture is not None:
                        # This module owns exactly lint_file (the file linted).
                        module_patch = module_file_patch(
                            local_repo,
                            example["base_commit"],
                            "HEAD",
                            lint_file,
                            logger=logger,
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
                                metrics=thinking_capture.get_module_metrics(
                                    lint_file_name
                                ),
                                stage="lint",
                                module_runtime_seconds=module_elapsed,
                            )
            else:
                message, spec_costs = get_message_ts(
                    agent_config, repo_path, test_files=test_files
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                for f in target_edit_files:
                    file_name = f.replace(".ts", "").replace("/", "__")
                    file_log_dir = experiment_log_dir / file_name

                    if _is_module_done(file_log_dir):
                        logger.info(f"Skipping already-drafted file: {file_name}")
                        continue

                    # No dependency resolution for TS — use message as-is
                    iter_message = message

                    lint_cmd = get_ts_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)
                    pre_sha = local_repo.head.commit.hexsha
                    module_start = time.time()
                    with capture_module_calls(
                        thinking_capture,
                        module=file_name,
                        log_dir=file_log_dir,
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
                    module_elapsed = time.time() - module_start
                    _mark_module_done(file_log_dir)

                    if thinking_capture is not None:
                        # This module owns exactly f (the drafted edit-target file).
                        module_patch = module_file_patch(
                            local_repo,
                            example["base_commit"],
                            "HEAD",
                            f,
                            logger=logger,
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
                logger.error(
                    "Failed to write thinking capture output: %s", e, exc_info=True
                )
        # Stage-wise cumulative patch alongside the per-module output.json.
        from agent.stage_patch import write_stage_patch
        write_stage_patch(local_repo, example["base_commit"], experiment_log_dir, logger)
    finally:
        local_repo.close()


def _run_agent_for_repo_ts_safe(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool,
    backend: str,
    log_dir: str,
    commit0_config_file: str,
) -> tuple[str, bool]:
    try:
        _, repo_name = example["repo"].split("/")
    except (KeyError, ValueError, AttributeError):
        repo_name = "<unknown>"
    try:
        run_agent_for_repo_ts(
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
            "TS agent worker for %s failed; isolating so the batch continues",
            repo_name,
            exc_info=True,
        )
        return repo_name, False


def _collect_worker_results_ts(
    results: list, per_worker_timeout: float | None
) -> dict:
    succeeded = 0
    failed = 0
    failed_repos: list = []
    for result in results:
        try:
            value = result.get(timeout=per_worker_timeout)
        except multiprocessing.TimeoutError:
            failed += 1
            failed_repos.append("<timeout>")
            logger.error(
                "TS worker exceeded per-worker timeout of %ss; isolating",
                per_worker_timeout,
            )
            continue
        except Exception:
            failed += 1
            logger.error(
                "A TS worker raised before returning a status; isolating",
                exc_info=True,
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
            succeeded += 1
    return {"succeeded": succeeded, "failed": failed, "failed_repos": failed_repos}


def run_agent_ts_impl(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
    display_repo_progress_num: int = 0,
) -> None:
    """Main function to run TsAiderAgents for TS repositories."""
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    commit0_config = read_commit0_ts_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )
    repo_split = commit0_config["repo_split"]
    dataset = list(dataset)
    allowed_repos = set(resolve_split(repo_split, dataset, curated=TS_SPLIT))
    filtered_dataset = [
        example
        for example in dataset
        if isinstance(example, dict)
        and isinstance(example.get("repo"), str)
        and example["repo"].split("/")[-1] in allowed_repos
    ]
    if not filtered_dataset:
        raise ValueError(
            f"No examples matched repo_split={repo_split!r}. "
            f"Available splits: {list(TS_SPLIT.keys())}. "
            f"If using a repo name, check spelling."
        )

    with tqdm(
        total=len(filtered_dataset), smoothing=0, desc="Running TS Aider for repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            results = []

            per_worker_timeout_env = os.environ.get(
                "KAIJU_TS_WORKER_TIMEOUT_SEC", "21600"
            )
            try:
                per_worker_timeout: float | None = float(per_worker_timeout_env)
                if per_worker_timeout <= 0:
                    per_worker_timeout = None
            except ValueError:
                per_worker_timeout = 21600.0

            for example in filtered_dataset:
                result = pool.apply_async(
                    _run_agent_for_repo_ts_safe,
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
                    callback=lambda _: pbar.update(1),
                )
                results.append(result)

            summary = _collect_worker_results_ts(results, per_worker_timeout)
            logger.info(
                "TS agent batch completed: %d succeeded, %d failed (%s)",
                summary["succeeded"],
                summary["failed"],
                ", ".join(summary["failed_repos"])
                if summary["failed_repos"]
                else "none",
            )


@app.command()
def run_agent_ts(
    branch: str = typer.Argument(..., help="Branch to run the agent on"),
    override_previous_changes: bool = typer.Option(
        False, "--override-previous-changes", help="Override previous changes"
    ),
    backend: str = typer.Option("local", help="Test backend"),
    agent_config_file: str = typer.Option(
        ".agent.yaml", "--agent-config-file", help="Path to agent config"
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", "--commit0-config-file", help="Path to TS commit0 config"
    ),
    log_dir: str = typer.Option("logs/aider", "--log-dir", help="Log directory"),
    max_parallel_repos: int = typer.Option(
        1, "--max-parallel-repos", help="Max parallel repos"
    ),
    display_repo_progress_num: int = typer.Option(
        0, "--display-repo-progress-num", help="Number of repos to show file-level progress for"
    ),
) -> None:
    run_agent_ts_impl(
        branch=branch,
        override_previous_changes=override_previous_changes,
        backend=backend,
        agent_config_file=agent_config_file,
        commit0_config_file=commit0_config_file,
        log_dir=log_dir,
        max_parallel_repos=max_parallel_repos,
        display_repo_progress_num=display_repo_progress_num,
    )


if __name__ == "__main__":
    app()
