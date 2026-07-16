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
    collect_js_test_files,
    create_branch,
    get_changed_js_files_from_commits,
    get_js_lint_cmd,
    get_message_js,
    get_target_edit_files_js,
    load_agent_config,
)
from agent.agents_js import AiderJsAgents
from agent.agents import TransientLLMError
from agent._module_retry import INLINE_MODULE_MAX_RETRIES, INLINE_MODULE_WAIT_SEC
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
from agent.agent_utils import agent_test_timeout_sec

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

        # Resume: rebuild the branch from host-persisted per-module patches so a
        # run stopped by a subscription limit/kill continues without redoing
        # finished modules (their .done markers then skip them). No-op unless resuming.
        if os.environ.get("KAIJU_RESUME") == "1":
            from agent.resume_state import restore_prior_progress
            restore_prior_progress(
                local_repo, example["base_commit"], branch,
                Path(log_dir).parent, repo_name, logger)

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
            base_commit=example.get("base_commit"),
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

        # Fail loud on an empty target set: get_target_edit_files_js returns []
        # when the stubbed source diff against reference_commit is empty (stubbing
        # missed src_dir, or reference_commit == stub state), or when strip_non_stubs
        # filtered everything out. Proceeding would emit a degenerate 0-work
        # trajectory that looks like a valid run — refuse it instead.
        if not target_edit_files:
            raise RuntimeError(
                f"No target-edit source files for {repo_name}: the stubbed-source "
                f"diff against reference_commit is empty (src_dir="
                f"{example.get('src_dir', '.')!r}, strip_non_stubs="
                f"{agent_config.strip_non_stubs}). This would produce a degenerate "
                "0-work trajectory; failing loud. Verify stubbing landed in src_dir "
                "and reference_commit is correct."
            )

        # Resolve the test FILES stage 3 will drive. Canonical inventory ids may be
        # file-prefixed (jest/vitest: "src/x.test.js > desc > it") OR bare framework
        # case-names (ava: "counter", "supports Arabic") with NO file prefix. Try to
        # map ids to real files first; if that yields nothing (bare names), DISCOVER
        # the repo's actual test files instead of skipping every id and doing zero
        # work (the old behavior: "Test file not found, skipping: <name>" x N -> 0
        # modules processed).
        test_files_str = [xx for x in get_js_tests(repo_name, verbose=0) for xx in x]
        test_files_raw = sorted(
            {
                i.split(" > ")[0].strip() if " > " in i else i.split(":")[0]
                for i in test_files_str
                if i.strip()
            }
        )
        test_dir = example.get("test", {}).get("test_dir", ".") or "."
        test_files: list[str] = []
        for tf in test_files_raw:
            if (Path(repo_path) / tf).exists():
                test_files.append(tf)
            elif (Path(repo_path) / test_dir / tf).exists():
                resolved = os.path.join(test_dir, tf)
                test_files.append(resolved)
                logger.info("Resolved test file with prefix: %s -> %s", tf, resolved)

        if not test_files:
            # Bare-name ids (ava et al.) don't map to files — discover them directly.
            test_files = _discover_js_test_files(repo_path, test_dir)
            if test_files:
                logger.info(
                    "Discovered %d JS test file(s) for %s: %s",
                    len(test_files), repo_name, test_files,
                )
            else:
                logger.warning(
                    "No JS test files found for %s (test_dir=%r) — stage 3 will run "
                    "the whole suite once via the default test command.",
                    repo_name, test_dir,
                )
        test_files = sorted(set(test_files))

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
                # An empty list falls back to one whole-suite pass (test_file="" ->
                # empty test_ids -> cli_js test runs every test).
                for test_file in (test_files or [""]):
                    test_file_name = (
                        _js_module_slug(test_file) if test_file else "all_tests"
                    )
                    test_log_dir = experiment_log_dir / test_file_name

                    if _is_module_done(test_log_dir):
                        logger.info(
                            "Skipping already-completed test module: %s",
                            test_file_name,
                        )
                        continue

                    # Live-flush this module's turns to <module>/turns.jsonl and
                    # touch .heartbeat (crash-resilience + watchdog liveness) —
                    # parity with go/rust/python. Without it a mid-module kill loses
                    # the partial trajectory and .heartbeat is never written.
                    if thinking_capture is not None:
                        thinking_capture.set_live_path(test_log_dir / "turns.jsonl")

                    test_cmd = (
                        f"{sys.executable} -m commit0.cli_js test"
                        f" {shlex.quote(repo_path)}"
                        f" {shlex.quote(test_file)}"
                        f" --branch {shlex.quote(branch)}"
                        # Pass the pipeline's backend (local_inplace) — parity with
                        # go/rust. Omitting it defaulted to 'local' (Docker), which
                        # inside the pipeline container has no docker.sock, so the
                        # test crashed with a Docker connection error and stage 3
                        # did zero work.
                        f" --backend {shlex.quote(backend)}"
                        f" --commit0-config-file {shlex.quote(commit0_config_file)}"
                        f" --timeout {agent_test_timeout_sec()}"
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
                        agent_config, repo_path,
                        test_files=[test_file] if test_file else [],
                    )
                    if thinking_capture is not None:
                        for c in spec_costs:
                            thinking_capture.summarizer_costs.add(c)

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
                                    test_files_readonly=test_files,
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
                                    thinking_capture.set_live_path(test_log_dir / "turns.jsonl")
                                time.sleep(_wait)
                    if not _module_ok:
                        continue
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

                    if thinking_capture is not None:
                        thinking_capture.set_live_path(lint_log_dir / "turns.jsonl")

                    lint_cmd = get_js_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)

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
                                    test_files_readonly=test_files,
                                    inject_test_files_readonly=agent_config.inject_test_files_readonly,
                                    current_module=lint_file_name,
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
                                    thinking_capture.set_live_path(lint_log_dir / "turns.jsonl")
                                time.sleep(_wait)
                    if not _module_ok:
                        continue
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

                    if thinking_capture is not None:
                        thinking_capture.set_live_path(file_log_dir / "turns.jsonl")

                    iter_message = message

                    lint_cmd = get_js_lint_cmd(
                        repo_name, agent_config.use_lint_info, commit0_config_file
                    )
                    if agent_config.blind_lint and lint_cmd:
                        lint_cmd = _make_blind_lint_cmd(lint_cmd)
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
                                    test_files_readonly=test_files,
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
                                    thinking_capture.set_live_path(file_log_dir / "turns.jsonl")
                                time.sleep(_wait)
                    if not _module_ok:
                        continue
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
                # Backstop (parity with go/rust): a module marked `.done` by a PRIOR
                # run is skipped by `_is_module_done` before its in-loop output.json
                # write can run on resume, leaving it `.done` but output.json-less.
                # Fill any such gap here. NEVER touches a module that already has
                # output.json, so it cannot double-write or double-count metrics.
                # Stage derives from the module's own first turn.
                for module_name in {
                    t.module for t in thinking_capture.turns if t.module
                }:
                    module_log_dir = experiment_log_dir / module_name
                    if (module_log_dir / "output.json").exists():
                        continue
                    module_turns = thinking_capture.get_module_turns(module_name)
                    if not module_turns:
                        continue
                    write_module_output_json(
                        output_dir=str(module_log_dir),
                        module_turns=module_turns,
                        module=module_name,
                        instance_id=f"{instance_id}__{module_name}"
                        if instance_id
                        else module_name,
                        git_patch=module_file_patch(
                            local_repo,
                            example["base_commit"],
                            "HEAD",
                            target_edit_files,
                            logger=logger,
                        ),
                        instruction="",
                        metadata=metadata,
                        metrics=thinking_capture.get_module_metrics(module_name),
                        stage=module_turns[0].stage or "unknown",
                    )

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


_AVA_ROOT_TEST_NAMES = (
    "test.js", "test.mjs", "test.cjs", "test.ts",
    "test.jsx", "test.tsx", "test.cts", "test.mts",
)


def _discover_js_test_files(repo_path: str, test_dir: str = ".") -> list[str]:
    """Discover the repo's ACTUAL test files (repo-relative), robustly.

    Stage 3 must run real test files. The canonical inventory ids are sometimes
    bare framework case-names (ava: ``"counter"``, ``"supports Arabic"``) with NO
    file prefix, so deriving a filename from an id yields a non-file and every id
    gets skipped → zero-work stage 3. Discover the files directly instead:

    * ``collect_js_test_files`` — jest/vitest/mocha ``*.test.*`` / ``*.spec.*`` and
      files under ``test/`` / ``tests/`` / ``__tests__/``.
    * ava root convention — a bare ``test.js`` (etc.) at the repo root or the
      dataset ``test_dir``, which ``collect_js_test_files`` does NOT match.
    """
    found: set[str] = set()
    for f in collect_js_test_files(repo_path):
        try:
            found.add(os.path.relpath(f, repo_path))
        except ValueError:
            continue
    roots = {".", (test_dir or ".").strip("/") or "."}
    for base in roots:
        for name in _AVA_ROOT_TEST_NAMES:
            candidate = Path(repo_path) / base / name
            if candidate.exists():
                found.add(os.path.relpath(candidate, repo_path))
    return sorted(found)


def _js_module_slug(rel_path: str) -> str:
    """Build a deterministic, collision-free module slug from a JS file path.

    The extension is RETAINED (folded into the slug, not stripped) so dual-package
    files that differ ONLY by extension — e.g. ``src/foo.js`` and ``src/foo.mjs`` —
    map to DISTINCT slugs (``src__foo_js`` vs ``src__foo_mjs``) instead of colliding
    into one log directory and overwriting each other's ``output.json``/``.done``
    (which would also make ``_is_module_done`` skip a genuinely distinct file).
    """
    return rel_path.replace("/", "__").replace(".", "_")


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
