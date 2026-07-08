"""Per-repo agent orchestration for JavaScript — analogue of run_agent_ts.py.

This is the entry point that the pipeline shell (``run_pipeline_js.sh``) and
``agent/config_js.py`` invoke. It wraps the per-instance ``AiderJsAgents`` run
loop with multiprocessing across repositories.

Per JS-PLAN §11 item 6, ``get_tests`` for JS currently delegates to the TS
test-id provider (``commit0.harness.get_ts_test_ids``) — a JS-native helper
is Phase C scope. Test-id grouping uses the ``' > '`` separator (matching the
TS placeholder).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shlex
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import cast

import typer
import yaml
from git import Repo
from tqdm import tqdm

from agent.agent_utils_js import (
    create_branch,
    get_changed_js_files_from_commits,
    get_js_lint_cmd,
    get_message_js,
    get_target_edit_files_js,
    load_agent_config,
)
from agent.agents_js import AiderJsAgents
from agent.class_types import AgentConfig
from agent.llm_cost_capture import capture_module_calls
from agent.module_patch import module_file_patch
from agent.thinking_capture import ThinkingCapture
from agent.run_agent_no_rich import (
    _make_blind_lint_cmd,
    _make_blind_test_cmd,
    _make_names_only_test_cmd,
)
from commit0.cli_js import read_commit0_js_config_file
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.constants_js import JS_SPLIT, JS_STUB_MARKER
from commit0.harness.get_ts_test_ids import main as get_js_tests
from commit0.harness.split_utils import resolve_split
from commit0.harness.utils import load_dataset_from_config
from agent.claude_code.recovery import run_with_recovery

logger = logging.getLogger(__name__)

app = typer.Typer()

_JS_PROTECTED_TEST_PATHSPECS: tuple[str, ...] = (
    ":!**/*.test.js",
    ":!**/*.test.mjs",
    ":!**/*.test.cjs",
    ":!**/*.test.jsx",
    ":!**/*.spec.js",
    ":!**/*.spec.mjs",
    ":!**/*.spec.cjs",
    ":!**/*.spec.jsx",
    ":!**/__tests__/**",
    ":!test/",
    ":!tests/",
    ":!jest.config.*",
    ":!vitest.config.*",
    ":!.mocharc.*",
)


class DirContext:
    """Inlined here to avoid pulling in ``agent.run_agent`` whose transitive
    import chain reaches ``agent.agent_utils`` and ``fitz`` (PyMuPDF). JS does
    not use PDF spec extraction (Phase C ships empty-string specification).
    """

    def __init__(self, d: str) -> None:
        self.dir = d
        self.cwd = os.getcwd()

    def __enter__(self) -> None:
        os.chdir(self.dir)

    def __exit__(
        self,
        exctype: type[BaseException] | None,
        excinst: BaseException | None,
        exctb: TracebackType | None,
    ) -> None:
        os.chdir(self.cwd)


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


def _run_agent_for_repo_js_impl(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "local",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run AiderJsAgents for a given JavaScript repository."""
    commit0_config = read_commit0_js_config_file(commit0_config_file)

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
            agent = AiderJsAgents(
                agent_config.max_iteration,
                agent_config.model_name,
                agent_config.cache_prompts,
            )
        else:
            raise NotImplementedError(
                f"{agent_config.agent_name} is not implemented for JS pipeline."
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

        prospective_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)
        latest_commit = local_repo.commit(branch)
        if latest_commit.hexsha != example["base_commit"] and override_previous_changes:
            done_markers = sorted(prospective_log_dir.rglob(".done"))
            if done_markers:
                raise RuntimeError(
                    f"Refusing to reset {repo_name} to base commit "
                    f"{example['base_commit']}: found {len(done_markers)} "
                    f".done marker(s) under {prospective_log_dir} indicating prior "
                    "completed stage work that the hard-reset would silently erase. "
                    "Pass override_previous_changes=False to resume, or remove the "
                    ".done markers (and re-run from scratch) to opt into the reset. "
                    f"First marker: {done_markers[0]}"
                )
            logger.warning(
                "Resetting %s to base commit %s (override_previous_changes=True)",
                repo_name,
                example["base_commit"],
            )
            local_repo.git.reset("--hard", example["base_commit"])

        if "reference_commit" not in example or not example["reference_commit"]:
            raise ValueError(
                f"Dataset row for {repo_name} is missing 'reference_commit'. "
                "JS pipeline requires an explicit reference_commit SHA so that "
                "get_target_edit_files_js can diff source files against a known "
                "post-stub baseline. Refusing to fall back to HEAD because that "
                "produces an empty diff and a silent 0-work agent run."
            )

        target_edit_files, _unused_deps = get_target_edit_files_js(
            local_repo,
            example.get("src_dir", "."),
            example.get("test", {}).get("test_dir", "tests"),
            branch,
            example["reference_commit"],
        )
        if agent_config.strip_non_stubs:
            orig_count = len(target_edit_files)
            target_edit_files = [
                f for f in target_edit_files
                if (Path(repo_path) / f).exists()
                and JS_STUB_MARKER in (Path(repo_path) / f).read_text(errors="replace")
            ]
            logger.info(
                "strip_non_stubs: kept %d/%d target files",
                len(target_edit_files), orig_count,
            )

        test_files_str = [xx for x in get_js_tests(repo_name, verbose=0) for xx in x]
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

        experiment_log_dir = prospective_log_dir

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

            commit0_config_for_meta = read_commit0_js_config_file(commit0_config_file)
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
                    test_file_name = _js_module_slug(test_file)
                    test_log_dir = experiment_log_dir / test_file_name

                    if _is_module_done(test_log_dir):
                        logger.info(
                            "Skipping already-completed test module: %s",
                            test_file_name,
                        )
                        continue

                    test_cmd = (
                        f"{sys.executable} -m commit0.cli_js test"
                        f" {shlex.quote(repo_path)}"
                        f" {shlex.quote(test_file)}"
                        f" --branch {shlex.quote(branch)}"
                        f" --commit0-config-file {shlex.quote(commit0_config_file)}"
                        f" --timeout 100"
                    )
                    lint_cmd = get_js_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_tests and test_cmd:
                        test_cmd = _make_blind_test_cmd(test_cmd)
                    elif agent_config.names_only_tests and test_cmd:
                        test_cmd = _make_names_only_test_cmd(test_cmd)
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)
                    message, spec_costs = get_message_js(
                        agent_config, repo_path, test_files=[test_file]
                    )
                    if thinking_capture is not None:
                        for c in spec_costs:
                            thinking_capture.summarizer_costs.add(c)

                    module_start = time.time()
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
                            test_files_readonly=test_files,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                    _kaiju_log_dir=test_log_dir,)
                    module_elapsed = time.time() - module_start
                    _mark_module_done(test_log_dir)

                    if thinking_capture is not None:
                        # Test stage: the test file is read-only; the agent edits
                        # the source target-edit files. Scope this module's patch
                        # to those source files, not the whole commit window.
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
                message, spec_costs = get_message_js(
                    agent_config, repo_path, test_files=test_files
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                lint_files = get_changed_js_files_from_commits(
                    local_repo, "HEAD", example["base_commit"]
                )
                for lint_file in lint_files:
                    lint_file_name = _js_module_slug(lint_file)
                    lint_log_dir = experiment_log_dir / lint_file_name

                    if _is_module_done(lint_log_dir):
                        logger.info(
                            "Skipping already-linted file: %s", lint_file_name
                        )
                        continue

                    lint_cmd = get_js_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)

                    module_start = time.time()
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
                            test_files_readonly=test_files,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                            current_module=lint_file_name,
                    _kaiju_log_dir=lint_log_dir,)
                    module_elapsed = time.time() - module_start
                    _mark_module_done(lint_log_dir)

                    if thinking_capture is not None:
                        # Lint stage: scope this module's patch to the single
                        # lint-target file it owns, not the whole commit window.
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
                message, spec_costs = get_message_js(
                    agent_config, repo_path, test_files=test_files
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                for f in target_edit_files:
                    file_name = _js_module_slug(f)
                    file_log_dir = experiment_log_dir / file_name

                    if _is_module_done(file_log_dir):
                        logger.info("Skipping already-drafted file: %s", file_name)
                        continue

                    iter_message = message

                    lint_cmd = get_js_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)
                    module_start = time.time()
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
                            test_files_readonly=test_files,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                    _kaiju_log_dir=file_log_dir,)
                    module_elapsed = time.time() - module_start
                    _mark_module_done(file_log_dir)

                    if thinking_capture is not None:
                        # Draft stage: scope this module's patch to the single
                        # target-edit file it owns, not the whole commit window.
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
                    len({t.module for t in thinking_capture.turns}),
                )

                if getattr(agent_config, "trajectory_md", True):
                    write_trajectory_md(
                        output_path=experiment_log_dir / "trajectory.md",
                        repo_name=repo_name,
                        turns=thinking_capture.turns,
                    )

                logger.info(
                    "Wrote thinking capture: %d turns, %d thinking tokens",
                    len(thinking_capture.turns),
                    thinking_capture.get_metrics()["total_thinking_tokens"],
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


def run_agent_for_repo_js(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "local",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> tuple[str, bool]:
    """Worker wrapper that never raises; returns ``(repo_name, ok)``."""
    try:
        _, repo_name = example["repo"].split("/")
    except (KeyError, ValueError, AttributeError):
        repo_name = "<unknown>"
    try:
        _run_agent_for_repo_js_impl(
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
            "JS agent worker for %s failed; isolating so the batch continues",
            repo_name,
            exc_info=True,
        )
        return repo_name, False


def _collect_worker_results_js(results: list) -> dict:
    succeeded = 0
    failed = 0
    failed_repos: list = []
    for result in results:
        try:
            value = result.get()
        except Exception:
            failed += 1
            logger.error(
                "A JS worker raised before returning a status; isolating",
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


def _js_module_slug(rel_path: str) -> str:
    """Build a deterministic module slug from a JS file path.

    Strips JS source extensions (.js, .mjs, .cjs, .jsx) and replaces path
    separators with '__'. The result is used as a log-directory name.
    """
    stripped = rel_path
    for ext in (".jsx", ".mjs", ".cjs", ".js"):
        if stripped.endswith(ext):
            stripped = stripped[: -len(ext)]
            break
    return stripped.replace("/", "__").replace(".", "_")


def run_agent_js_impl(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Main function to run AiderJsAgents for JS repositories."""
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    commit0_config = read_commit0_js_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )
    repo_split = commit0_config["repo_split"]
    dataset = list(dataset)
    allowed_repos = set(resolve_split(repo_split, dataset, curated=JS_SPLIT))
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
            f"Available splits: {list(JS_SPLIT.keys())}. "
            f"If using a repo name, check spelling."
        )

    with tqdm(
        total=len(filtered_dataset), smoothing=0, desc="Running JS Aider for repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            results = []

            for example in filtered_dataset:
                result = pool.apply_async(
                    run_agent_for_repo_js,
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

            summary = _collect_worker_results_js(results)
            logger.info(
                "JS agent workers: %d succeeded, %d failed (failed_repos=%s)",
                summary["succeeded"],
                summary["failed"],
                summary["failed_repos"],
            )


def run_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Public entry called by ``agent/config_js.py`` ``run`` command."""
    run_agent_js_impl(
        branch=branch,
        override_previous_changes=override_previous_changes,
        backend=backend,
        agent_config_file=agent_config_file,
        commit0_config_file=commit0_config_file,
        log_dir=log_dir,
        max_parallel_repos=max_parallel_repos,
    )


@app.command()
def run_agent_js(
    branch: str = typer.Argument(..., help="Branch to run the agent on"),
    override_previous_changes: bool = typer.Option(
        False, "--override-previous-changes", help="Override previous changes"
    ),
    backend: str = typer.Option("local", help="Test backend"),
    agent_config_file: str = typer.Option(
        ".agent.yaml", "--agent-config-file", help="Path to agent config"
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", "--commit0-config-file", help="Path to JS commit0 config"
    ),
    log_dir: str = typer.Option("logs/aider", "--log-dir", help="Log directory"),
    max_parallel_repos: int = typer.Option(
        1, "--max-parallel-repos", help="Max parallel repos"
    ),
) -> None:
    run_agent_js_impl(
        branch=branch,
        override_previous_changes=override_previous_changes,
        backend=backend,
        agent_config_file=agent_config_file,
        commit0_config_file=commit0_config_file,
        log_dir=log_dir,
        max_parallel_repos=max_parallel_repos,
    )


main = app


if __name__ == "__main__":
    app()


__all__ = [
    "RUN_AGENT_LOG_DIR",
    "app",
    "main",
    "run_agent",
    "run_agent_for_repo_js",
    "run_agent_js_impl",
]
