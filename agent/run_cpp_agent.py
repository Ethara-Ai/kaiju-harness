"""C++ agent runner \u2014 mirrors run_agent_no_rich.py for C++ repos."""

import json
import logging
import multiprocessing
import os
import time
from pathlib import Path

import yaml
from git import Repo
from tqdm import tqdm

from agent.agent_utils import create_branch, load_agent_config
from agent.agent_utils_cpp import (
    extract_cpp_function_stubs,
    get_target_edit_files_cpp,
)
from agent.agents_cpp import CppAiderAgents
from agent.agents import TransientLLMError
from agent.class_types import AgentConfig
from agent.module_patch import module_file_patch
from agent.run_agent import DirContext, run_eval_after_each_commit
from agent.thinking_capture import SummarizerCost, ThinkingCapture
from agent.llm_cost_capture import capture_module_calls
from commit0.cli import read_commit0_config_file
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.constants_cpp import CPP_SPLIT, CPP_STUB_MARKER
from commit0.harness.utils import load_dataset_from_config
from agent.claude_code.recovery import run_with_recovery

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
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
    """Wrap test cmd so agent sees only the summary line, not per-test failures."""
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

_CPP_PROMPT_PATH = Path(__file__).parent / "prompts" / "cpp_system_prompt.md"

_MAX_FILE_CONTEXT_CHARS = 50_000
_MAX_PER_FILE_CONTEXT_CHARS = 20_000
_MAX_FUNCTION_LIST_CHARS = 30_000


def get_cpp_message(
    agent_config: AgentConfig,
    repo_path: str,
    target_files: list[str],
    test_files: list[str] | None = None,
) -> tuple[str, list[SummarizerCost]]:
    """Build the C++ system prompt from ``cpp_system_prompt.md``, filling
    ``{repo_name}``, ``{function_list}``, and ``{file_context}`` placeholders.

    Args:
        agent_config: Agent configuration.
        repo_path: Absolute path to the repo's working directory.
        target_files: The stub files this invocation should focus on. Must be
            scoped to ONE file (or a small set) in per-file callers — passing
            every stub in the crate produces megabyte-scale prompts that
            exceed model context windows on large C++ codebases (e.g. LLVM,
            gRPC, Boost).
        test_files: Optional list of test file paths (absolute or repo-relative).
            When provided AND ``agent_config.use_unit_tests_info`` is True, the
            test bodies are concatenated and appended (capped at
            ``agent_config.max_unit_tests_info_length`` chars). Previously the
            caller never passed this, so ``use_unit_tests_info`` was silently
            a no-op.

    """
    repo_name = Path(repo_path).name

    function_list_parts: list[str] = []
    fl_chars = 0
    fl_truncated = 0
    for tf in target_files:
        full_path = Path(repo_path) / tf
        if not full_path.exists():
            continue
        stubs = extract_cpp_function_stubs(str(full_path))
        if not stubs:
            continue
        block = f"// {tf}\n{stubs}"
        if fl_chars + len(block) > _MAX_FUNCTION_LIST_CHARS:
            fl_truncated += 1
            continue
        function_list_parts.append(block)
        fl_chars += len(block)
    if fl_truncated:
        function_list_parts.append(
            f"// ... {fl_truncated} additional file(s)' stubs elided to stay under "
            f"function_list cap of {_MAX_FUNCTION_LIST_CHARS} chars ..."
        )

    function_list = "\n\n".join(function_list_parts)

    file_context_parts: list[str] = []
    running_chars = 0
    truncated_files = 0
    for tf in target_files:
        if running_chars >= _MAX_FILE_CONTEXT_CHARS:
            truncated_files += 1
            continue
        full_path = Path(repo_path) / tf
        if not full_path.exists():
            continue
        try:
            content = full_path.read_text(errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s for context: %s", full_path, exc)
            continue
        if len(content) > _MAX_PER_FILE_CONTEXT_CHARS:
            content = (
                content[:_MAX_PER_FILE_CONTEXT_CHARS]
                + f"\n// ... truncated ({len(content) - _MAX_PER_FILE_CONTEXT_CHARS} chars elided) ...\n"
            )
        block = f"```cpp\n// {tf}\n{content}\n```"
        remaining = _MAX_FILE_CONTEXT_CHARS - running_chars
        if len(block) > remaining:
            block = block[:remaining] + "\n// ... file_context cap reached ...\n```"
        file_context_parts.append(block)
        running_chars += len(block)

    if truncated_files:
        file_context_parts.append(
            f"\n// ... {truncated_files} additional file(s) elided to stay under "
            f"file_context cap of {_MAX_FILE_CONTEXT_CHARS} chars ...\n"
        )

    file_context = "\n\n".join(file_context_parts)

    if _CPP_PROMPT_PATH.exists():
        template = _CPP_PROMPT_PATH.read_text()
    else:
        template = (
            "You are working on the C++ repository '{repo_name}'.\n\n"
            "## Functions to implement\n{function_list}\n\n"
            "## Current file contents\n{file_context}\n"
        )

    message = template.format(
        repo_name=repo_name,
        function_list=function_list,
        file_context=file_context,
    )

    if agent_config.use_unit_tests_info and test_files:
        unit_tests_section = "\n\n>>> Here is the Unit Tests Information:\n"
        for tf in test_files:
            tf_path = Path(tf) if os.path.isabs(tf) else Path(repo_path) / tf
            if tf_path.exists():
                try:
                    unit_tests_section += (
                        f"\n### {tf_path.name}\n```cpp\n"
                        + tf_path.read_text(errors="replace")
                        + "\n```\n"
                    )
                except OSError as exc:
                    logger.warning("Could not read test file %s: %s", tf_path, exc)
        max_unit = max(0, int(getattr(agent_config, "max_unit_tests_info_length", 10000)))
        if len(unit_tests_section) > max_unit:
            unit_tests_section = unit_tests_section[:max_unit] + "\n... (truncated)\n"
        message += unit_tests_section

    return message, []


def get_cpp_lint_cmd(repo_path: str) -> str:
    """Return a ``clang-tidy`` command when ``build/compile_commands.json``
    exists, otherwise fall back to ``clang-format --dry-run --Werror``.
    """
    compile_db = Path(repo_path) / "build" / "compile_commands.json"
    if compile_db.exists():
        return "clang-tidy -p build"
    return "clang-format --dry-run --Werror"


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


def _get_stable_log_dir(log_dir: str, repo_name: str, branch: str) -> Path:
    """Return stable log dir mirroring Java's {log_dir}/{repo}/{branch}/current."""
    safe_branch = branch.replace("/", "__")
    stable_dir = Path(log_dir) / repo_name / safe_branch / "current"
    stable_dir.mkdir(parents=True, exist_ok=True)
    return stable_dir


def _file_stem(tf: str, repo_path: str = "") -> str:
    """Convert a file path to a log-directory stem using relative path from repo root."""
    if repo_path:
        try:
            tf = os.path.relpath(tf, repo_path)
        except ValueError:
            pass
    return (
        tf.replace(".cpp", "")
        .replace(".hpp", "")
        .replace(".cc", "")
        .replace(".h", "")
        .replace("/", "__")
        .replace("\\", "__")
        .lstrip("_")
    )


def run_cpp_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run the C++ agent for a single repository, processing one file at a time."""
    repo_name = example["repo"].split("/")[-1]
    repo_path = os.path.join(repo_base_dir, repo_name)

    stable_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)

    if not override_previous_changes and _is_module_done(stable_log_dir):
        logger.info("Skipping %s - already completed", repo_name)
        return

    target_files = get_target_edit_files_cpp(repo_path)

    if not target_files:
        logger.warning("No target files found for %s", repo_name)
        _mark_module_done(stable_log_dir)
        return

    if agent_config.strip_non_stubs:
        _stub_marker = CPP_STUB_MARKER
        _filtered: list[str] = []
        for _tf in target_files:
            _full = Path(repo_path) / _tf
            try:
                if _full.exists() and _stub_marker in _full.read_text(errors="replace"):
                    _filtered.append(_tf)
            except OSError:
                pass
        logger.info(
            "strip_non_stubs: kept %d/%d target files",
            len(_filtered), len(target_files),
        )
        target_files = _filtered


    try:
        local_repo = Repo(repo_path)
        create_branch(local_repo, branch, example.get("base_commit", ""))
    except Exception as e:
        logger.error("Failed to create branch for %s: %s", repo_name, e)
        return

    # Resume: rebuild the branch from host-persisted per-module patches so a run
    # stopped by a subscription limit/kill continues without redoing finished
    # modules (their .done markers then skip them). No-op unless resuming.
    _cpp_base = example.get("base_commit", "") if isinstance(example, dict) else ""
    if os.environ.get("KAIJU_RESUME") == "1" and _cpp_base:
        from agent.resume_state import restore_prior_progress
        restore_prior_progress(
            local_repo, _cpp_base, branch,
            Path(log_dir).parent, repo_name, logger)

    # Write agent config snapshot — mirrors Java's .agent.yaml
    agent_config_log_file = stable_log_dir / ".agent.yaml"
    try:
        with open(agent_config_log_file, "w") as f:
            yaml.dump(agent_config, f)
    except Exception as e:
        logger.warning("Failed to write .agent.yaml for %s: %s", repo_name, e)

    lint_cmd = get_cpp_lint_cmd(repo_path)
    dataset_test_cmd = example.get("test", {}).get("test_cmd", "") if isinstance(example, dict) else ""
    if dataset_test_cmd:
        test_cmd = dataset_test_cmd.replace("/testbed/", f"{repo_path}/")
    else:
        test_cmd = "cmake -B build && cmake --build build -j$(nproc) && ctest --test-dir build --output-on-failure"

    _test_files_ro = [
        str(p) for p in Path(repo_path).rglob("*.cpp")
        if "/test" in str(p) or "/tests/" in str(p)
    ]
    _test_files_ro += [
        str(p) for p in Path(repo_path).rglob("*.hpp")
        if "/test" in str(p) or "/tests/" in str(p)
    ]

    if agent_config.blind_lint and lint_cmd:
        lint_cmd = _make_blind_lint_cmd(lint_cmd)
    if agent_config.blind_tests:
        test_cmd = _make_blind_test_cmd(test_cmd)
    elif agent_config.names_only_tests:
        test_cmd = _make_names_only_test_cmd(test_cmd)


    agent = CppAiderAgents(
        agent_config.max_iteration,
        agent_config.model_name,
        agent_config.cache_prompts,
    )

    # One ThinkingCapture per repo run (covers all files in this stage)
    thinking_capture: ThinkingCapture | None = (
        ThinkingCapture()
        if getattr(agent_config, "capture_thinking", False)
        else None
    )

    from agent.output_writer import build_metadata
    from agent.openhands_formatter import write_module_output_json

    instance_id = f"commit-0/{repo_name}"
    metadata: dict = {}
    if thinking_capture is not None:
        metadata = build_metadata(
            model_name=agent_config.model_name,
            dataset_path="",
            max_iterations=agent_config.max_iteration,
            model_short=getattr(agent_config, "model_short", agent_config.model_name),
        )

    eval_results: dict = {}

    # Process one file at a time to avoid exceeding model context limits
    for tf in target_files:
        stem = _file_stem(tf, repo_path)
        file_log_dir = stable_log_dir / stem
        file_log_dir.mkdir(parents=True, exist_ok=True)

        message, summarizer_costs = get_cpp_message(
            agent_config,
            repo_path,
            [tf],
            test_files=_test_files_ro,
        )

        if thinking_capture is not None:
            for c in summarizer_costs:
                thinking_capture.summarizer_costs.add(c)

        pre_sha = local_repo.head.commit.hexsha
        module_start = time.time()
        stage = "test" if agent_config.run_tests else ("lint" if agent_config.use_lint_info else "draft")

        if agent_config.run_tests:
            try:
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=stem,
                    log_dir=file_log_dir,
                ):
                    with DirContext(repo_path):
                        _ = run_with_recovery(agent.run, 
                            message,
                            test_cmd,
                            lint_cmd,
                            [tf],
                            file_log_dir,
                            test_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="test",
                            current_module=stem,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                            test_files_readonly=_test_files_ro,
                    _kaiju_log_dir=file_log_dir,)
                if agent_config.record_test_for_each_commit and commit0_config_file:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )
            except TransientLLMError as _tle:
                _skip_failed_module(file_log_dir, stem, _tle)
                continue
            except Exception as e:
                logger.error("Agent failed for %s/%s: %s", repo_name, tf, e)
                (file_log_dir / "error.log").write_text(str(e))

        elif agent_config.use_lint_info:
            try:
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=stem,
                    log_dir=file_log_dir,
                ):
                    with DirContext(repo_path):
                        _ = run_with_recovery(agent.run, 
                            message,
                            "",
                            lint_cmd,
                            [tf],
                            file_log_dir,
                            lint_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="lint",
                            current_module=stem,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                            test_files_readonly=_test_files_ro,
                    _kaiju_log_dir=file_log_dir,)
            except TransientLLMError as _tle:
                _skip_failed_module(file_log_dir, stem, _tle)
                continue
            except Exception as e:
                logger.error("Agent failed for %s/%s (lint mode): %s", repo_name, tf, e)
                (file_log_dir / "error.log").write_text(str(e))

        else:
            try:
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=stem,
                    log_dir=file_log_dir,
                ):
                    with DirContext(repo_path):
                        _ = run_with_recovery(agent.run, 
                            message,
                            "",
                            "",
                            [tf],
                            file_log_dir,
                            thinking_capture=thinking_capture,
                            current_stage="draft",
                            current_module=stem,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                            test_files_readonly=_test_files_ro,
                    _kaiju_log_dir=file_log_dir,)
            except TransientLLMError as _tle:
                _skip_failed_module(file_log_dir, stem, _tle)
                continue
            except Exception as e:
                import traceback as _tb
                tb_str = _tb.format_exc()
                logger.error("Agent failed for %s/%s (draft mode): %s\n%s", repo_name, tf, e, tb_str)
                (file_log_dir / "error.log").write_text(f"{e}\n\n{tb_str}")

        # Per-module .done marker — mirrors Java structure
        _mark_module_done(file_log_dir)

        module_elapsed = time.time() - module_start
        if thinking_capture is not None:
            post_sha = local_repo.head.commit.hexsha
            # Scope this module's patch to ONLY the file it owns (``tf``), so
            # other modules' bundled edits within the same commit window are not
            # attributed to this module. ``tf`` may be absolute or repo-relative.
            _tf_rel = os.path.relpath(tf, repo_path) if os.path.isabs(tf) else tf
            module_patch = (
                module_file_patch(
                    local_repo,
                    example.get("base_commit", ""),
                    "HEAD",
                    _tf_rel,
                    logger=logger,
                )
                if pre_sha != post_sha
                else ""
            )
            module_turns = thinking_capture.get_module_turns(stem)
            if module_turns:
                write_module_output_json(
                    output_dir=str(file_log_dir),
                    module_turns=module_turns,
                    module=stem,
                    instance_id=f"{instance_id}__{stem}",
                    git_patch=module_patch,
                    instruction=message,
                    metadata=metadata,
                    metrics=thinking_capture.get_module_metrics(stem),
                    stage=stage,
                    module_runtime_seconds=module_elapsed,
                )

    # Write eval_results.json — mirrors Java structure
    try:
        with open(stable_log_dir / "eval_results.json", "w") as f:
            json.dump(eval_results, f)
    except Exception as e:
        logger.warning("Failed to write eval_results.json for %s: %s", repo_name, e)

    # Write trajectory.md when thinking capture is enabled
    if thinking_capture is not None:
        try:
            from agent.trajectory_writer import write_trajectory_md

            if getattr(agent_config, "trajectory_md", True):
                write_trajectory_md(
                    output_path=stable_log_dir / "trajectory.md",
                    repo_name=repo_name,
                    turns=thinking_capture.turns,
                )
                logger.info(
                    f"Wrote trajectory.md for {repo_name}: "
                    f"{len(thinking_capture.turns)} turns"
                )
        except Exception as e:
            logger.warning("Failed to write trajectory.md for %s: %s", repo_name, e)

    # Stage-wise cumulative patch alongside the per-module output.json.
    from agent.stage_patch import write_stage_patch
    write_stage_patch(local_repo, example.get("base_commit", ""), stable_log_dir, logger)
    _mark_module_done(stable_log_dir)
    logger.info("Completed %s", repo_name)


def run_cpp_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Run the C++ agent across all C++ repos in the dataset."""
    agent_config = load_agent_config(agent_config_file)
    commit0_config = read_commit0_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )

    cpp_repo_names = {r.split("/")[-1] for r in CPP_SPLIT.get("all", [])}

    cpp_examples: list[RepoInstance] = []
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if not cpp_repo_names or repo_name in cpp_repo_names:
            cpp_examples.append(example)

    assert len(cpp_examples) > 0, (
        "No C++ examples available. Check that CPP_SPLIT is correctly configured "
        "and the dataset contains C++ repositories."
    )

    logger.info("Found %d C++ repositories to process", len(cpp_examples))

    repo_base_dir = commit0_config.get("base_dir", "repos")

    if max_parallel_repos <= 1:
        for example in tqdm(cpp_examples, desc="Running aider for C++ repos"):
            run_cpp_agent_for_repo(
                repo_base_dir=repo_base_dir,
                agent_config=agent_config,
                example=example,
                branch=branch,
                override_previous_changes=override_previous_changes,
                backend=backend,
                log_dir=log_dir,
                commit0_config_file=commit0_config_file,
            )
    else:
        with tqdm(
            total=len(cpp_examples), smoothing=0, desc="Running aider for C++ repos"
        ) as pbar:
            with multiprocessing.Pool(processes=max_parallel_repos) as pool:
                async_results = []
                for example in cpp_examples:
                    ar = pool.apply_async(
                        run_cpp_agent_for_repo,
                        args=(
                            repo_base_dir,
                            agent_config,
                            example,
                            branch,
                            override_previous_changes,
                            backend,
                            log_dir,
                            commit0_config_file,
                        ),
                        callback=lambda _: pbar.update(1),
                    )
                    async_results.append(ar)

                for ar in async_results:
                    ar.get()
                logger.info("All %d C++ agent workers completed", len(async_results))


def main() -> None:
    """CLI entry point for the C++ agent runner."""
    import argparse

    parser = argparse.ArgumentParser(description="Run C++ agent on commit0 repos")
    parser.add_argument(
        "--branch",
        type=str,
        default="ai-cpp-agent",
        help="Branch name to create for agent changes",
    )
    parser.add_argument(
        "--override-previous-changes",
        action="store_true",
        help="Override previous agent changes",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default="modal",
        help="Backend for evaluation (modal or local)",
    )
    parser.add_argument(
        "--agent-config-file",
        type=str,
        default="agent/config/agent_config.yaml",
        help="Path to agent config file",
    )
    parser.add_argument(
        "--commit0-config-file",
        type=str,
        default=".commit0.yaml",
        help="Path to commit0 config file",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=str(RUN_AGENT_LOG_DIR.resolve()),
        help="Directory for agent logs",
    )
    parser.add_argument(
        "--max-parallel-repos",
        type=int,
        default=1,
        help="Maximum number of repos to process in parallel",
    )

    args = parser.parse_args()

    run_cpp_agent(
        branch=args.branch,
        override_previous_changes=args.override_previous_changes,
        backend=args.backend,
        agent_config_file=args.agent_config_file,
        commit0_config_file=args.commit0_config_file,
        log_dir=args.log_dir,
        max_parallel_repos=args.max_parallel_repos,
    )


if __name__ == "__main__":
    main()
