"""Rust agent runner — mirrors ``run_agent_no_rich.py`` for Rust repos.

Uses Rust-specific file discovery, test IDs, lint commands, and system prompts
while reusing all language-agnostic infrastructure (progress tracking, git ops,
trajectory capture, output formatting).
"""

import json
import logging
import multiprocessing
import os
import time
from pathlib import Path
from typing import cast

import yaml
from git import Repo
from tqdm import tqdm

from agent.agent_utils import create_branch, load_agent_config
from agent.agent_utils_rust import (
    extract_rust_function_stubs,
    find_rust_files_to_edit,
    get_target_edit_files_rust,
)
from agent.agents_rust import RustAiderAgents
from agent.class_types import AgentConfig
from agent.run_agent import DirContext, run_eval_after_each_commit
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.llm_cost_capture import capture_module_calls
from commit0.cli import read_commit0_config_file
from commit0.harness.constants import RUN_AGENT_LOG_DIR, RepoInstance
from commit0.harness.constants_rust import RUST_SPLIT
from commit0.harness.split_utils import resolve_split
from commit0.harness.patch_utils_rust import generate_rust_patch, InvalidRustPatchError
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)

_RUST_PROMPT_PATH = Path(__file__).parent / "prompts" / "rust_system_prompt.md"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _load_spec_text(repo_path: str) -> str:
    """Read spec PDF from repo dir, decompressing bz2 if needed. Returns raw text or ''."""
    import bz2

    spec_pdf = Path(repo_path) / "spec.pdf"
    spec_bz2 = Path(repo_path) / "spec.pdf.bz2"

    if spec_bz2.exists() and not spec_pdf.exists():
        try:
            with bz2.open(str(spec_bz2), "rb") as fin:
                with open(str(spec_pdf), "wb") as fout:
                    fout.write(fin.read())
        except Exception as exc:
            logger.warning("Failed to decompress %s: %s", spec_bz2, exc)
            if spec_pdf.exists():
                spec_pdf.unlink()
            return ""

    if not spec_pdf.exists():
        return ""

    try:
        import fitz

        raw = ""
        with fitz.open(spec_pdf) as doc:
            for page in doc:
                raw += page.get_text()  # type: ignore[attr-defined]
        return raw
    except Exception as exc:
        logger.warning("Failed to extract spec PDF text: %s", exc)
        return ""


def get_rust_message(
    agent_config: AgentConfig,
    repo_path: str,
    target_files: list[str],
) -> tuple[str, list[SummarizerCost]]:
    """Build the Rust system prompt from the template.

    Fills ``{repo_name}``, ``{function_list}``, and ``{file_context}`` placeholders
    in ``agent/prompts/rust_system_prompt.md``.

    Returns ``(formatted_message, summarizer_costs)`` — costs are always empty for
    now (no spec summarization in V1).
    """
    repo_name = os.path.basename(repo_path)

    function_lines: list[str] = []
    for fpath in target_files:
        stubs = extract_rust_function_stubs(fpath)
        rel = os.path.relpath(fpath, repo_path)
        for stub in stubs:
            function_lines.append(f"- `{rel}` line {stub['line']}: `{stub['signature']}`")

    function_list = "\n".join(function_lines) if function_lines else "(no stubs found)"

    context_parts: list[str] = []
    for fpath in target_files:
        rel = os.path.relpath(fpath, repo_path)
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            context_parts.append(f"### {rel}\n```rust\n{content}\n```")
        except OSError as exc:
            logger.warning("Could not read %s for context: %s", fpath, exc)

    file_context = "\n\n".join(context_parts) if context_parts else "(no files)"

    try:
        template = _RUST_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("Rust system prompt not found at %s", _RUST_PROMPT_PATH)
        template = "Implement the Rust stub functions for {repo_name}."

    message = template.format(
        repo_name=repo_name,
        function_list=function_list,
        file_context=file_context,
    )

    if agent_config.use_user_prompt and agent_config.user_prompt:
        message = agent_config.user_prompt + "\n\n" + message

    spec_costs: list[SummarizerCost] = []
    if agent_config.use_spec_info:
        spec_text = _load_spec_text(repo_path)
        if spec_text and len(spec_text) > 200:
            if len(spec_text) > int(agent_config.max_spec_info_length * 1.5):
                try:
                    from agent.agent_utils import summarize_specification

                    spec_pdf_path = Path(repo_path) / "spec.pdf"
                    processed_spec, spec_costs = summarize_specification(
                        spec_text=spec_text,
                        model=agent_config.model_name,
                        max_tokens=agent_config.spec_summary_max_tokens,
                        max_char_length=agent_config.max_spec_info_length,
                        cache_path=spec_pdf_path.parent / ".spec_summary_cache.json",
                        model_short=getattr(agent_config, "model_short", ""),
                    )
                except Exception as exc:
                    logger.warning("Spec summarization failed: %s", exc)
                    processed_spec = spec_text[: agent_config.max_spec_info_length]
            else:
                processed_spec = spec_text
            message += (
                "\n\n>>> Here is the Specification Information:\n" + processed_spec
            )
        else:
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = Path(repo_path) / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[: agent_config.max_spec_info_length]
                        message += (
                            "\n\n>>> Here is the Specification Information:\n"
                            + readme_text
                        )
                        logger.info(
                            "Using %s as spec fallback for %s", readme_name, repo_path
                        )
                        break
                    except Exception as exc:
                        logger.warning("Failed to read %s: %s", readme_path, exc)

    return message, spec_costs


_BLIND_LINT_SHELL = (
    '_out=$(cargo clippy --all-targets --all-features -- -D warnings 2>&1); '
    '_rc=$?; '
    'if [ $_rc -eq 0 ]; then echo "build clean"; '
    'else _n=$(printf "%s" "$_out" | grep -cE "^error\\[E[0-9]+\\]" 2>/dev/null); '
    '[ -z "$_n" ] && _n=0; '
    'printf "build failed: %s compile errors\\n" "$_n"; fi; '
    'exit $_rc'
)

_BLIND_TEST_SHELL = (
    '_out=$(cargo test --all-features 2>&1); '
    '_rc=$?; '
    '_summary=$(printf "%s" "$_out" | grep -E "^test result:" | tail -1); '
    'if [ -n "$_summary" ]; then printf "%s\\n" "$_summary"; '
    'else _n=$(printf "%s" "$_out" | grep -cE "^error\\[E[0-9]+\\]" 2>/dev/null); '
    '[ -z "$_n" ] && _n=0; '
    'printf "compilation failed: %s errors\\n" "$_n"; fi; '
    'exit $_rc'
)


def _make_blind_lint_cmd() -> str:
    """Wrap clippy so output is just \"build clean\" or \"build failed: N errors\"."""
    return f"bash -c '{_BLIND_LINT_SHELL}' --"


def _make_blind_test_cmd() -> str:
    """Wrap cargo test so output is just the summary line, no per-test failures."""
    return f"bash -c '{_BLIND_TEST_SHELL}'"


def get_rust_lint_cmd(repo_path: str) -> str:
    """Return the cargo clippy lint command for the repo at *repo_path*.

    NOTE: aider's Linter.run_cmd() always appends the filename to the command
    (``cmd += " " + quote(fname)``).  ``cargo clippy`` does not accept source
    file arguments — it lints the whole crate.  We use ``bash -c '...' --`` so
    the appended filename is harmlessly consumed as a positional arg to bash
    (after ``--``) instead of being passed to cargo.
    """
    return "bash -c 'cargo clippy --all-targets --all-features -- -D warnings' --"


# ---------------------------------------------------------------------------
# Progress tracking helpers (imported pattern from run_agent_no_rich)
# ---------------------------------------------------------------------------


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / ".done").touch()


def _get_stable_log_dir(log_dir: str, repo_name: str, branch: str) -> Path:
    stable_dir = Path(log_dir) / repo_name / branch / "current"
    stable_dir.mkdir(parents=True, exist_ok=True)
    return stable_dir


# ---------------------------------------------------------------------------
# Per-repo worker
# ---------------------------------------------------------------------------


def run_rust_agent_for_repo(
    repo_base_dir: str,
    agent_config: AgentConfig,
    example: RepoInstance,
    branch: str,
    override_previous_changes: bool = False,
    backend: str = "modal",
    log_dir: str = str(RUN_AGENT_LOG_DIR.resolve()),
    commit0_config_file: str = "",
) -> None:
    """Run aider for a single Rust repository."""
    _, repo_name = example["repo"].split("/")

    repo_path = os.path.abspath(os.path.join(repo_base_dir, repo_name))

    try:
        local_repo = Repo(repo_path)
    except Exception:
        logger.error("Failed to open repo at %s: not a git repo", repo_path, exc_info=True)
        raise Exception(
            f"{repo_path} is not a git repo. Check if base_dir is correctly specified."
        ) from None

    agent = RustAiderAgents(
        agent_config.max_iteration,
        agent_config.model_name,
        agent_config.cache_prompts,
    )

    thinking_capture = (
        ThinkingCapture() if getattr(agent_config, "capture_thinking", False) else None
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

    target_edit_files = get_target_edit_files_rust(repo_path)
    all_source_files = find_rust_files_to_edit(repo_path)
    if agent_config.strip_non_stubs:
        # Use base_commit's stub list (persistent across stages). The current
        # target_edit_files becomes empty after Stage 1 fills the stubs, which
        # would leave Stage 2/3 with no files to iterate. Reading the base_commit
        # state keeps the strip filter consistent across all 3 stages.
        import subprocess as _sp
        _base = example["base_commit"]
        _stubbed_at_base: set[str] = set()
        try:
            _ls = _sp.run(
                ["git", "ls-tree", "-r", "--name-only", _base],
                cwd=repo_path, capture_output=True, text=True, check=True,
            )
            for _rel in _ls.stdout.splitlines():
                if not _rel.endswith(".rs"):
                    continue
                _show = _sp.run(
                    ["git", "show", f"{_base}:{_rel}"],
                    cwd=repo_path, capture_output=True, text=True,
                )
                if _show.returncode == 0 and 'panic!("STUB: not implemented")' in _show.stdout:
                    _stubbed_at_base.add(os.path.join(repo_path, _rel))
            all_source_files = [f for f in all_source_files if f in _stubbed_at_base]
            logger.info(
                "strip_non_stubs: kept %d/%d source files (filtered against base_commit %s)",
                len(all_source_files), len(_stubbed_at_base), _base[:8],
            )
        except (_sp.CalledProcessError, OSError) as _e:
            logger.warning(
                "strip_non_stubs: failed to compute base-commit stub list (%s). Falling back to current target_edit_files (may break Stage 2/3).",
                _e,
            )
            all_source_files = list(target_edit_files)


    experiment_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)
    eval_results = {}

    agent_config_log_file = experiment_log_dir / ".agent.yaml"
    try:
        with open(agent_config_log_file, "w") as agent_config_file:
            yaml.dump(agent_config, agent_config_file)
    except OSError as e:
        logger.error("Failed to write agent config to %s: %s", agent_config_log_file, e)
        raise

    message = ""


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
            model_short=getattr(agent_config, "model_short", agent_config.model_name),
        )

    with DirContext(repo_path):

        if agent_config.run_tests:
            for src_file in all_source_files:
                src_file_name = os.path.relpath(src_file, repo_path).replace(".rs", "").replace("/", "__")
                test_log_dir = experiment_log_dir / src_file_name

                if _is_module_done(test_log_dir):
                    logger.info("Skipping already-completed test module: %s", src_file_name)
                    continue

                test_cmd = (
                    _make_blind_test_cmd()
                    if agent_config.blind_tests
                    else "cargo test --all-features"
                )
                lint_cmd = get_rust_lint_cmd(repo_path) if agent_config.use_lint_info else ""
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd()
                message, spec_costs = get_rust_message(
                    agent_config, repo_path, target_files=[src_file]
                )
                if thinking_capture is not None:
                    for c in spec_costs:
                        thinking_capture.summarizer_costs.add(c)

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=src_file_name,
                    log_dir=test_log_dir,
                ):
                    _ = agent.run(
                        "",
                        test_cmd,
                        lint_cmd,
                        all_source_files,
                        test_log_dir,
                        test_first=True,
                        thinking_capture=thinking_capture,
                        current_stage="test",
                        current_module=src_file_name,
                        max_test_output_length=agent_config.max_test_output_length,
                        spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                        repo_map_tokens=agent_config.repo_map_tokens,
                    )
                module_elapsed = time.time() - module_start
                _mark_module_done(test_log_dir)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    module_patch = ""
                    if pre_sha != post_sha:
                        try:
                            module_patch = generate_rust_patch(
                                repo_path, pre_sha, post_sha, strict=True
                            )
                        except InvalidRustPatchError as exc:
                            rejected_path = test_log_dir / ".rejected_patch.diff"
                            try:
                                rejected_path.write_text(exc.patch, encoding="utf-8")
                                logger.error(
                                    "InvalidRustPatchError for %s: %s. Rejected patch persisted to %s.",
                                    src_file_name, exc, rejected_path,
                                )
                            except OSError as write_exc:
                                logger.error(
                                    "InvalidRustPatchError for %s: %s. Could not persist rejected patch to %s: %s",
                                    src_file_name, exc, rejected_path, write_exc,
                                )
                    module_turns = thinking_capture.get_module_turns(src_file_name)
                    if module_turns:
                        write_module_output_json(
                            output_dir=str(test_log_dir),
                            module_turns=module_turns,
                            module=src_file_name,
                            instance_id=f"{instance_id}__{src_file_name}"
                            if instance_id
                            else src_file_name,
                            git_patch=module_patch,
                            instruction=message,
                            metadata=metadata,
                            metrics=thinking_capture.get_module_metrics(src_file_name),
                            stage="test",
                            stage_runtime_seconds=module_elapsed,
                        )

                if agent_config.record_test_for_each_commit:
                    current_commit = local_repo.head.commit.hexsha
                    eval_results[current_commit] = run_eval_after_each_commit(
                        branch, backend, commit0_config_file
                    )

        elif agent_config.run_entire_dir_lint:
            message, spec_costs = get_rust_message(
                agent_config, repo_path, target_files=all_source_files
            )
            if thinking_capture is not None:
                for c in spec_costs:
                    thinking_capture.summarizer_costs.add(c)

            lint_files = all_source_files
            for lint_file in lint_files:
                lint_file_name = os.path.relpath(lint_file, repo_path).replace(".rs", "").replace("/", "__")
                lint_log_dir = experiment_log_dir / lint_file_name

                if _is_module_done(lint_log_dir):
                    logger.info("Skipping already-linted file: %s", lint_file_name)
                    continue

                lint_cmd = get_rust_lint_cmd(repo_path) if agent_config.use_lint_info else ""
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd()

                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=lint_file_name,
                    log_dir=lint_log_dir,
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
                        repo_map_tokens=agent_config.repo_map_tokens,
                    )
                module_elapsed = time.time() - module_start
                _mark_module_done(lint_log_dir)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    module_patch = ""
                    if pre_sha != post_sha:
                        try:
                            module_patch = generate_rust_patch(
                                repo_path, pre_sha, post_sha, strict=True
                            )
                        except InvalidRustPatchError as exc:
                            rejected_path = lint_log_dir / ".rejected_patch.diff"
                            try:
                                rejected_path.write_text(exc.patch, encoding="utf-8")
                                logger.error(
                                    "InvalidRustPatchError for %s: %s. Rejected patch persisted to %s.",
                                    lint_file_name, exc, rejected_path,
                                )
                            except OSError as write_exc:
                                logger.error(
                                    "InvalidRustPatchError for %s: %s. Could not persist rejected patch to %s: %s",
                                    lint_file_name, exc, rejected_path, write_exc,
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
            message, spec_costs = get_rust_message(
                agent_config, repo_path, target_files=target_edit_files
            )
            if thinking_capture is not None:
                for c in spec_costs:
                    thinking_capture.summarizer_costs.add(c)

            for f in target_edit_files:
                file_name = os.path.relpath(f, repo_path).replace(".rs", "").replace("/", "__")
                file_log_dir = experiment_log_dir / file_name

                if _is_module_done(file_log_dir):
                    logger.info("Skipping already-drafted file: %s", file_name)
                    continue

                iter_message = message

                lint_cmd = get_rust_lint_cmd(repo_path) if agent_config.use_lint_info else ""
                if agent_config.blind_lint and lint_cmd:
                    lint_cmd = _make_blind_lint_cmd()
                pre_sha = local_repo.head.commit.hexsha
                module_start = time.time()
                with capture_module_calls(
                    thinking_capture=thinking_capture,
                    module=file_name,
                    log_dir=file_log_dir,
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
                        repo_map_tokens=agent_config.repo_map_tokens,
                    )
                module_elapsed = time.time() - module_start
                _mark_module_done(file_log_dir)

                if thinking_capture is not None:
                    post_sha = local_repo.head.commit.hexsha
                    module_patch = ""
                    if pre_sha != post_sha:
                        try:
                            module_patch = generate_rust_patch(
                                repo_path, pre_sha, post_sha, strict=True
                            )
                        except InvalidRustPatchError as exc:
                            rejected_path = file_log_dir / ".rejected_patch.diff"
                            try:
                                rejected_path.write_text(exc.patch, encoding="utf-8")
                                logger.error(
                                    "InvalidRustPatchError for %s: %s. Rejected patch persisted to %s.",
                                    file_name, exc, rejected_path,
                                )
                            except OSError as write_exc:
                                logger.error(
                                    "InvalidRustPatchError for %s: %s. Could not persist rejected patch to %s: %s",
                                    file_name, exc, rejected_path, write_exc,
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_rust_agent(
    branch: str,
    override_previous_changes: bool,
    backend: str,
    agent_config_file: str,
    commit0_config_file: str,
    log_dir: str,
    max_parallel_repos: int,
) -> None:
    """Main function to run aider for Rust repositories.

    Filters dataset by ``RUST_SPLIT`` instead of ``SPLIT``.
    Spawns a multiprocessing pool of ``run_rust_agent_for_repo`` workers.
    """
    agent_config = load_agent_config(agent_config_file)

    commit0_config_file = os.path.abspath(commit0_config_file)
    commit0_config = read_commit0_config_file(commit0_config_file)

    dataset = load_dataset_from_config(
        commit0_config["dataset_name"], split=commit0_config["dataset_split"]
    )
    repo_split = commit0_config.get("repo_split", "all")
    dataset = list(dataset)
    allowed_repos = set(resolve_split(repo_split, dataset, curated=RUST_SPLIT))
    filtered_dataset = [
        example
        for example in dataset
        if isinstance(example, dict)
        and isinstance(example.get("repo"), str)
        and example["repo"].split("/")[-1] in allowed_repos
    ]

    assert len(filtered_dataset) > 0, (
        f"No Rust examples available for repo_split={repo_split!r}. "
        f"Ensure the dataset contains Rust repos from RUST_SPLIT."
    )

    with tqdm(
        total=len(filtered_dataset), smoothing=0, desc="Running aider for Rust repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            results = []
            for example in filtered_dataset:
                result = pool.apply_async(
                    run_rust_agent_for_repo,
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

            for result in results:
                result.get()
            logger.info("All %d Rust agent workers completed", len(results))
