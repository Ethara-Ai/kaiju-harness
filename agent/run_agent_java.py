import bz2
import json
import logging
import os
import subprocess
import sys
import multiprocessing
import time
import yaml
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from git import Repo
from tqdm import tqdm

from agent.agent_utils import create_branch, MODULE_SCOPE_NOTE
from agent.agent_utils_java import (
    collect_java_files,
    is_java_stubbed,
    count_java_stubs,
    get_specification,
    summarize_specification_java,
    SPEC_INFO_HEADER,
)
from agent.agents_java import JavaAgents
from agent.config_java import JavaAgentConfig
from agent.thinking_capture import ThinkingCapture, SummarizerCost
from agent.llm_cost_capture import capture_module_calls
from commit0.harness.constants_java import (
    JAVA_STUB_MARKER,
    JAVA_BASE_BRANCH,
    detect_build_system,
)
from agent.claude_code.recovery import run_with_recovery
from agent.module_patch import module_file_patch

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
class DirContext:
    def __init__(self, d: str):
        self.dir = d
        self.cwd = os.getcwd()

    def __enter__(self):
        os.chdir(self.dir)

    def __exit__(self, exctype, excinst, exctb) -> None:
        os.chdir(self.cwd)


def _get_stable_log_dir(log_dir: str, repo_name: str, branch: str) -> Path:
    """Return a stable experiment log directory that persists across retries."""
    stable_dir = Path(log_dir) / repo_name / branch / "current"
    stable_dir.mkdir(parents=True, exist_ok=True)
    return stable_dir


def _is_module_done(log_dir: Path) -> bool:
    return (log_dir / ".done").exists()


def _mark_module_done(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / ".done").touch()


def run_eval_after_each_commit(
    repo: str,
    branch: str,
    timeout: int = 100,
) -> str:
    """Run commit0-java evaluate after each commit and return stdout."""
    eval_cmd = [
        sys.executable, "-m", "commit0", "cli_java", "evaluate",
        "--repo", repo,
        "--branch", branch,
        "--timeout", str(timeout),
    ]
    # Fallback: try the commit0-java entry-point directly
    commit0_java = os.path.join(os.path.dirname(sys.executable), "commit0-java")
    if os.path.isfile(commit0_java):
        eval_cmd = [
            commit0_java, "evaluate",
            "--repo", repo,
            "--branch", branch,
            "--timeout", str(timeout),
        ]
    try:
        result = subprocess.run(
            eval_cmd, capture_output=True, text=True, check=True, timeout=timeout + 60
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error("Eval command failed: %s", e)
        return e.stdout if e.stdout else str(e)
    except subprocess.TimeoutExpired as e:
        logger.error("Eval command timed out: %s", e)
        return str(e)


def _get_java_message(
    config: JavaAgentConfig,
    repo_path: str,
    stubbed_file: str,
) -> Tuple[str, List[SummarizerCost]]:
    spec_costs: List[SummarizerCost] = []
    prompt_parts = [config.user_prompt]

    rel_path = str(Path(stubbed_file).relative_to(repo_path))
    prompt_parts.append(f"\nTarget file: {rel_path}")

    stub_count = Path(stubbed_file).read_text().count(JAVA_STUB_MARKER)
    if stub_count > 0:
        prompt_parts.append(f"This file has {stub_count} stub(s) to implement.")

    if config.use_unit_tests_info:
        test_dirs = _find_related_tests(repo_path, stubbed_file)
        if test_dirs:
            test_info = "\n\nRelated test files:\n"
            for test_file in test_dirs[:5]:
                try:
                    content = Path(test_file).read_text()
                    test_info += f"\n--- {Path(test_file).relative_to(repo_path)} ---\n"
                    test_info += content[:config.max_unit_tests_info_length // max(1, len(test_dirs))]
                except (OSError, ValueError):
                    continue
            prompt_parts.append(test_info[:config.max_unit_tests_info_length])

    # Spec info processing — mirrors Python's get_message() logic
    if config.use_spec_info:
        spec_pdf_path = Path(repo_path) / "spec.pdf"
        spec_bz2_path = Path(repo_path) / "spec.pdf.bz2"
        decompress_failed = False
        if spec_bz2_path.exists() and not spec_pdf_path.exists():
            try:
                _MAX_SPEC_DECOMPRESSED = 100 * 1024 * 1024
                with bz2.open(str(spec_bz2_path), "rb") as in_file:
                    with open(str(spec_pdf_path), "wb") as out_file:
                        _written = 0
                        while True:
                            _chunk = in_file.read(1 << 16)
                            if not _chunk:
                                break
                            _written += len(_chunk)
                            if _written > _MAX_SPEC_DECOMPRESSED:
                                raise ValueError(
                                    f"Decompressed spec exceeds "
                                    f"{_MAX_SPEC_DECOMPRESSED // (1024*1024)}MB limit"
                                )
                            out_file.write(_chunk)
            except Exception as e:
                logger.warning(
                    "Failed to decompress spec file %s: %s", spec_bz2_path, e
                )
                if spec_pdf_path.exists():
                    spec_pdf_path.unlink()
                decompress_failed = True
        if not decompress_failed and spec_pdf_path.exists():
            raw_spec = get_specification(specification_pdf_path=spec_pdf_path)
            if len(raw_spec) > int(config.max_spec_info_length * 1.5):
                processed_spec, spec_costs = summarize_specification_java(
                    spec_text=raw_spec,
                    model=config.model,
                    max_tokens=config.spec_summary_max_tokens,
                    max_char_length=config.max_spec_info_length,
                    cache_path=spec_pdf_path.parent / ".spec_summary_cache.json",
                )
            else:
                processed_spec = raw_spec
            prompt_parts.append(f"\n{SPEC_INFO_HEADER} " + processed_spec)
        else:
            for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
                readme_path = Path(repo_path) / readme_name
                if readme_path.exists():
                    try:
                        readme_text = readme_path.read_text(errors="replace")
                        readme_text = readme_text[:config.max_spec_info_length]
                        prompt_parts.append(f"\n{SPEC_INFO_HEADER} " + readme_text)
                        logger.info(
                            "Using %s as spec fallback for %s", readme_name, repo_path
                        )
                        break
                    except Exception as e:
                        logger.warning("Failed to read %s: %s", readme_path, e)

    return "\n".join(prompt_parts) + MODULE_SCOPE_NOTE, spec_costs


def _find_related_tests(repo_path: str, source_file: str) -> List[str]:
    p = Path(repo_path)
    source_name = Path(source_file).stem

    test_patterns = [
        f"{source_name}Test.java",
        f"Test{source_name}.java",
        f"{source_name}Tests.java",
        f"{source_name}IT.java",
    ]

    found = []
    for test_file in p.rglob("*.java"):
        rel = str(test_file.relative_to(p))
        if "/src/test/" not in rel:
            continue
        if test_file.name in test_patterns:
            found.append(str(test_file))

    return sorted(found)


def run_java_agent(
    instance: dict,
    agent_config: JavaAgentConfig,
    branch: str = "java-agent",
    override_previous_changes: bool = True,
    log_dir: str = "logs/agent",
    timeout: int = 1800,
) -> Optional[Dict]:
    repo_path = os.path.abspath(
        instance.get("repo_path")
        or instance.get("repo", "").split("/")[-1]
        or "."
    )
    repo_name = instance.get("repo", "").split("/")[-1]

    try:
        local_repo = Repo(repo_path)
    except Exception:
        logger.error("Not a git repo: %s", repo_path)
        raise

    raw_build = instance.get("build_system") or detect_build_system(repo_path)
    build_system = raw_build if raw_build not in ("auto",) else detect_build_system(repo_path)
    java_agent = JavaAgents(agent_config)

    if local_repo.is_dirty():
        logger.warning("Stashing uncommitted changes in %s", repo_path)
        local_repo.git.stash("push", "-m", "commit0-java: auto-stash before agent run")

    # Java repos have stubs on the 'base' branch created by setup_java.py.
    # base_commit is the original library tag (pre-stub), so we must branch
    # from 'base' where the stubbed source files actually live.
    stub_branch = JAVA_BASE_BRANCH
    if stub_branch in local_repo.heads:
        stub_base = local_repo.commit(stub_branch).hexsha
    else:
        # Fallback: resolve base_commit (may be a tag name or SHA)
        logger.warning(
            "Branch '%s' not found in %s — falling back to base_commit",
            stub_branch, repo_name,
        )
        stub_base = local_repo.commit(
            instance.get("base_commit", "HEAD")
        ).hexsha

    create_branch(local_repo, branch, stub_base)

    latest_commit = local_repo.commit(branch)
    if latest_commit.hexsha != stub_base and override_previous_changes:
        logger.warning("Resetting %s to stub base %s", repo_name, stub_base)
        local_repo.git.reset("--hard", stub_base)

    # Resume: rebuild the branch from host-persisted per-module patches so a run
    # stopped by a subscription limit/kill continues without redoing finished
    # modules (their .done markers then skip them). No-op unless resuming.
    if os.environ.get("KAIJU_RESUME") == "1":
        from agent.resume_state import restore_prior_progress
        restore_prior_progress(
            local_repo, stub_base, branch,
            Path(log_dir).parent, repo_name, logger)

    java_files = collect_java_files(repo_path)
    stubbed_files = [f for f in java_files if is_java_stubbed(f)]
    if agent_config.strip_non_stubs:
        logger.info(
            "strip_non_stubs: Java already operates on stub-only files; %d/%d files are stubs",
            len(stubbed_files), len(java_files),
        )

    if not stubbed_files:
        logger.info("No stubbed files found in %s, skipping", repo_name)
        return None

    logger.info(
        "Java agent starting: %s, %d/%d files stubbed",
        repo_name, len(stubbed_files), len(java_files),
    )

    experiment_log_dir = _get_stable_log_dir(log_dir, repo_name, branch)

    # Clear .done sentinels on override so modules are re-processed
    if override_previous_changes:
        for done_file in experiment_log_dir.rglob(".done"):
            done_file.unlink()
            logger.debug("Cleared stale .done: %s", done_file)

    config_log = experiment_log_dir / ".agent.yaml"
    with open(config_log, "w") as f:
        yaml.dump(asdict(agent_config), f)

    compile_cmd = java_agent.get_compile_command(build_system, repo_path) if agent_config.compile_check else ""
    test_cmd = java_agent.get_test_command(build_system, repo_path)

    test_files_readonly = _find_all_test_files(repo_path)

    if agent_config.blind_tests:
        test_cmd = _make_blind_test_cmd(test_cmd)
    elif agent_config.names_only_tests:
        test_cmd = _make_names_only_test_cmd(test_cmd)
    if agent_config.blind_lint and compile_cmd:
        compile_cmd = _make_blind_lint_cmd(compile_cmd)

    thinking_capture = (
        ThinkingCapture() if agent_config.capture_thinking else None
    )

    instance_id = f"commit-0/{repo_name}"
    metadata: dict = {}
    if thinking_capture is not None:
        from agent.output_writer import build_metadata

        metadata = build_metadata(
            model_name=agent_config.model,
            dataset_path="",
            max_iterations=agent_config.max_iteration,
            model_short=agent_config.model_short,
            dataset_id=instance.get("id"),
        )

    from agent.openhands_formatter import write_module_output_json
    from agent.trajectory_writer import write_trajectory_md

    def _flush_trajectory():
        if thinking_capture is not None and agent_config.trajectory_md:
            try:
                write_trajectory_md(
                    output_path=experiment_log_dir / "trajectory.md",
                    repo_name=repo_name,
                    turns=thinking_capture.turns,
                )
            except Exception as exc:
                logger.warning("Failed to flush trajectory.md: %s", exc)

    results = {}
    eval_results: Dict[str, str] = {}
    repo_full_name = instance.get("repo", repo_name)
    total_summarizer_cost = 0.0
    try:
      with DirContext(repo_path):
        if agent_config.run_tests:
            test_files = _find_all_test_files(repo_path)
            logger.info("Found %d test files for %s", len(test_files), repo_name)

            for test_file in test_files:
                rel_test = str(Path(test_file).relative_to(repo_path))
                test_log_name = rel_test.replace("/", "__").replace(".java", "")
                test_log_dir = experiment_log_dir / test_log_name

                if _is_module_done(test_log_dir):
                    logger.info("Skipping already-completed test module: %s", test_log_name)
                    continue

                try:
                    # In test-first mode, the message is empty (aider runs tests
                    # directly). Skip the expensive _get_java_message() which does
                    # spec summarization — it's never sent to the model.
                    _message = ""
                    spec_costs = []
                    if thinking_capture is not None:
                        for c in spec_costs:
                            thinking_capture.summarizer_costs.add(c)

                    module_start = time.time()
                    with capture_module_calls(
                        thinking_capture=thinking_capture,
                        module=test_log_name,
                        log_dir=test_log_dir,
                    ):
                        agent_return = run_with_recovery(java_agent.run, 
                            message="",
                            test_cmd=test_cmd,
                            lint_cmd=compile_cmd,
                            fnames=stubbed_files,
                            log_dir=test_log_dir,
                            test_first=True,
                            thinking_capture=thinking_capture,
                            current_stage="test",
                            current_module=test_log_name,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                    _kaiju_log_dir=test_log_dir,)
                    module_elapsed = time.time() - module_start
                    _mark_module_done(test_log_dir)

                    if thinking_capture is not None:
                        # Test modules are read-only on the test file; their
                        # edit targets are the stubbed source files aider may
                        # implement. Scope to those source files (repo-relative)
                        # from stub_base so other modules' bundled edits are
                        # not attributed to this test module.
                        _edit_targets = [
                            os.path.relpath(f, repo_path) for f in stubbed_files
                        ]
                        module_patch = module_file_patch(
                            local_repo, stub_base, "HEAD", _edit_targets, logger=logger
                        )
                        module_turns = thinking_capture.get_module_turns(test_log_name)
                        if module_turns:
                            write_module_output_json(
                                output_dir=str(test_log_dir),
                                module_turns=module_turns,
                                module=test_log_name,
                                instance_id=f"{instance_id}__{test_log_name}",
                                git_patch=module_patch,
                                instruction=_message,
                                metadata=metadata,
                                metrics=thinking_capture.get_module_metrics(test_log_name),
                                stage="test",
                                module_runtime_seconds=module_elapsed,
                            )
                        _flush_trajectory()

                    summarizer_cost = sum(c.cost for c in spec_costs) + getattr(
                        agent_return, "test_summarizer_cost", 0.0
                    )
                    total_summarizer_cost += summarizer_cost

                    results[rel_test] = {
                        "status": "completed",
                        "cost": agent_return.last_cost + summarizer_cost,
                    }

                    if agent_config.record_test_for_each_commit:
                        current_commit = local_repo.head.commit.hexsha
                        eval_results[current_commit] = run_eval_after_each_commit(
                            repo_full_name, branch
                        )
                except Exception:
                    logger.exception("Failed processing test %s, skipping", rel_test)
                    results[rel_test] = {"status": "error", "cost": 0.0}
        else:
            for stubbed_file in stubbed_files:
                rel_path = str(Path(stubbed_file).relative_to(repo_path))
                file_log_name = rel_path.replace("/", "__").replace(".java", "")
                file_log_dir = experiment_log_dir / file_log_name

                if _is_module_done(file_log_dir):
                    logger.info("Skipping already-completed module: %s", file_log_name)
                    continue

                logger.info("Processing: %s", rel_path)

                try:
                    message, spec_costs = _get_java_message(agent_config, repo_path, stubbed_file)
                    if thinking_capture is not None:
                        for c in spec_costs:
                            thinking_capture.summarizer_costs.add(c)

                    module_start = time.time()
                    with capture_module_calls(
                        thinking_capture=thinking_capture,
                        module=file_log_name,
                        log_dir=file_log_dir,
                    ):
                        agent_return = run_with_recovery(java_agent.run, 
                            message=message,
                            test_cmd="",
                            lint_cmd=compile_cmd,
                            fnames=[stubbed_file],
                            log_dir=file_log_dir,
                            thinking_capture=thinking_capture,
                            current_stage="draft",
                            current_module=file_log_name,
                            max_test_output_length=agent_config.max_test_output_length,
                            spec_summary_max_tokens=agent_config.spec_summary_max_tokens,
                            test_files_readonly=test_files_readonly,
                            inject_test_files_readonly=agent_config.inject_test_files_readonly,
                    _kaiju_log_dir=file_log_dir,)
                    module_elapsed = time.time() - module_start
                    _mark_module_done(file_log_dir)

                    if thinking_capture is not None:
                        # This module owns exactly one stubbed source file
                        # (rel_path, repo-relative). Scope its patch to that
                        # file from stub_base so co-committed edits to other
                        # modules' files are not attributed here.
                        module_patch = module_file_patch(
                            local_repo, stub_base, "HEAD", rel_path, logger=logger
                        )
                        module_turns = thinking_capture.get_module_turns(file_log_name)
                        if module_turns:
                            write_module_output_json(
                                output_dir=str(file_log_dir),
                                module_turns=module_turns,
                                module=file_log_name,
                                instance_id=f"{instance_id}__{file_log_name}",
                                git_patch=module_patch,
                                instruction=message,
                                metadata=metadata,
                                metrics=thinking_capture.get_module_metrics(file_log_name),
                                stage="draft",
                                module_runtime_seconds=module_elapsed,
                            )
                        _flush_trajectory()

                    summarizer_cost = sum(c.cost for c in spec_costs) + getattr(
                        agent_return, "test_summarizer_cost", 0.0
                    )
                    total_summarizer_cost += summarizer_cost

                    results[rel_path] = {
                        "status": "completed",
                        "cost": agent_return.last_cost + summarizer_cost,
                    }

                    if agent_config.record_test_for_each_commit:
                        current_commit = local_repo.head.commit.hexsha
                        eval_results[current_commit] = run_eval_after_each_commit(
                            repo_full_name, branch
                        )
                except Exception:
                    logger.exception("Failed processing %s, skipping", rel_path)
                    results[rel_path] = {"status": "error", "cost": 0.0}

    finally:
        _flush_trajectory()

    if agent_config.record_test_for_each_commit and eval_results:
        try:
            with open(experiment_log_dir / "eval_results.json", "w") as f:
                json.dump(eval_results, f)
        except OSError as e:
            logger.error("Failed to write eval results: %s", e)

    if thinking_capture is not None:
        try:
            logger.info(
                "Thinking capture: %d turns, %d thinking tokens",
                len(thinking_capture.turns),
                thinking_capture.get_metrics()["total_thinking_tokens"],
            )
        except Exception as e:
            logger.warning("Failed to log thinking capture metrics: %s", e)

    total_cost = sum(r.get("cost", 0) for r in results.values())
    remaining = count_java_stubs(repo_path)
    logger.info(
        "Java agent finished: %s — cost=$%.4f, stubs_remaining=%d",
        repo_name, total_cost, remaining.get("total_stubs", 0),
    )

    # Stage-wise cumulative patch alongside the per-module output.json.
    from agent.stage_patch import write_stage_patch
    write_stage_patch(local_repo, stub_base, experiment_log_dir, logger)

    return results


def _find_all_test_files(repo_path: str) -> List[str]:
    p = Path(repo_path)
    test_files = []
    for f in p.rglob("*.java"):
        rel = str(f.relative_to(p))
        if "/src/test/" in rel and (
            f.name.endswith("Test.java")
            or f.name.endswith("Tests.java")
            or f.name.startswith("Test")
        ):
            test_files.append(str(f))
    return sorted(test_files)


def _run_java_agent_worker(
    instance: dict,
    agent_config: JavaAgentConfig,
    branch: str,
    override_previous_changes: bool,
    log_dir: str,
) -> Tuple[str, Optional[Dict]]:
    repo_name = instance.get("repo", "unknown").split("/")[-1]
    try:
        result = run_java_agent(
            instance=instance,
            agent_config=agent_config,
            branch=branch,
            override_previous_changes=override_previous_changes,
            log_dir=log_dir,
            timeout=agent_config.timeout,
        )
        return repo_name, result
    except Exception:
        logger.error("Agent failed for %s", repo_name, exc_info=True)
        return repo_name, {"status": "error"}


def run_java_agent_for_repos(
    instances: List[dict],
    agent_config: JavaAgentConfig,
    branch: str = "java-agent",
    override_previous_changes: bool = False,
    log_dir: str = "logs/agent",
    max_parallel_repos: int = 1,
) -> Dict[str, Optional[Dict]]:
    all_results: Dict[str, Optional[Dict]] = {}

    with tqdm(
        total=len(instances), smoothing=0, desc="Running Java agent for repos"
    ) as pbar:
        with multiprocessing.Pool(processes=max_parallel_repos) as pool:
            async_results = []
            for instance in instances:
                ar = pool.apply_async(
                    _run_java_agent_worker,
                    args=(
                        instance,
                        agent_config,
                        branch,
                        override_previous_changes,
                        log_dir,
                    ),
                    callback=lambda _: pbar.update(1),
                )
                async_results.append(ar)

            for ar in async_results:
                try:
                    repo_name, result = ar.get()
                    all_results[repo_name] = result
                except Exception:
                    logger.error("Worker failed to return result", exc_info=True)

    return all_results
