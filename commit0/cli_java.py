import typer
from typing import List, Optional

app = typer.Typer(
    name="commit0-java",
    help="Commit0 Java Pipeline",
    no_args_is_help=True,
)


@app.command()
def setup(
    dataset_name: str = typer.Option("java_dataset.json", help="Dataset name or local JSON path"),
    dataset_split: str = typer.Option("all", help="Dataset split"),
    java_version: str = typer.Option("17", help="Java version: 11, 17, or 21"),
    base_dir: str = typer.Option("repos/java", help="Base directory for Java repos"),
) -> None:
    """Set up Java repositories."""
    from commit0.harness.setup_java import main as setup_main
    setup_main(
        dataset_name=dataset_name,
        dataset_split=dataset_split,
        java_version=java_version,
        base_dir=base_dir,
    )


@app.command()
def build(
    repo: Optional[str] = typer.Option(None, help="Specific repo to build"),
    nocache: bool = typer.Option(False, help="Build without Docker cache"),
) -> None:
    """Build Java Docker images."""
    from commit0.harness.build_java import build_java_base_images, build_java_repo_images
    dataset = _load_dataset_map()
    build_java_base_images(nocache=nocache)
    if repo:
        build_java_repo_images(repo_names=[repo], dataset=dataset, nocache=nocache)
    else:
        build_java_repo_images(dataset=dataset, nocache=nocache)


@app.command(name="get-tests")
def get_tests(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    save: bool = typer.Option(False, help="Save test IDs as .bz2 file"),
    output_dir: str = typer.Option("./test_ids", help="Output directory for .bz2 files"),
) -> None:
    """Discover Java test IDs."""
    from commit0.harness.get_java_test_ids import get_java_test_ids

    instance = _load_instance(repo)
    test_ids = get_java_test_ids(instance)

    if not test_ids:
        typer.echo("No test IDs found.")
        return

    typer.echo(f"Found {len(test_ids)} test IDs:")
    for tid in test_ids:
        typer.echo(f"  {tid}")

    if save and repo:
        from pathlib import Path as _Path

        try:
            from tools.generate_test_ids_java import save_test_ids
        except ImportError:
            raise typer.BadParameter(
                "The 'tools' package is required for --save but is not installed. "
                "Install from a development checkout (tools/ is not shipped in the wheel)."
            )

        out_file = save_test_ids(test_ids, repo, _Path(output_dir))
        typer.echo(f"\nSaved to {out_file}")


@app.command()
def test(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    timeout: int = typer.Option(600, help="Test timeout in seconds"),
    verbose: int = typer.Option(0, help="Verbosity level"),
) -> None:
    """Run Java tests."""
    from commit0.harness.run_java_tests import run_java_tests
    instance = _load_instance(repo)
    results = run_java_tests(instance=instance, timeout=timeout, verbose=verbose)
    # Surface results to STDOUT so the test-refine agent (aider captures the
    # command's stdout) has real signal — the return dict was previously discarded,
    # so the agent saw NOTHING and could not refine (same class as the C bug).
    if not results:
        # run_java_tests already printed the raw build/test output tail.
        typer.echo("Java test summary: no test reports parsed (see output above).")
    elif "__COMPILATION__" in results:
        typer.echo("Java test summary: COMPILATION FAILED (see javac errors above).")
    elif "__TIMEOUT__" in results:
        typer.echo(f"Java test summary: TIMED OUT after {timeout}s.")
    else:
        _ok = {"PASSED", "PASS", "SUCCESS"}
        _skip = {"SKIPPED", "SKIP"}
        passed = sum(1 for v in results.values() if str(v).upper() in _ok)
        total = len(results)
        typer.echo(f"Java test results: {passed} passed / {total} total")
        failures = [
            (k, v) for k, v in results.items()
            if str(v).upper() not in _ok and str(v).upper() not in _skip
        ]
        if failures:
            typer.echo("Failed/errored tests:")
            for k, v in failures[:60]:
                typer.echo(f"  {k}: {v}")


@app.command()
def evaluate(
    repo: Optional[str] = typer.Option(None, help="Specific repo (omit for all repos in dataset)"),
    patch_path: Optional[str] = typer.Option(None, help="Path to patch file (auto-discovered from config if omitted)"),
    branch: Optional[str] = typer.Option(None, help="Evaluate from git branch (generates patch from diff vs base_commit)"),
    timeout: int = typer.Option(600, help="Evaluation timeout in seconds"),
    num_workers: int = typer.Option(4, help="Parallel evaluation workers (multi-repo mode)"),
    backend: str = typer.Option(
        "local",
        help="Execution backend: 'local' (Docker) or 'local_inplace' (git worktree, "
             "no Docker daemon — REQUIRED inside the containerized pipeline).",
    ),
    log_dir: Optional[str] = typer.Option(
        None,
        help="Directory for eval artifacts (evaluate_java.log, eval.sh, compile_output.txt, test_output.txt, test_exit_code.txt, surefire reports). Defaults to logs/pytest/<repo>, which is EPHEMERAL inside pipeline containers — pass an explicit path on a persisted mount to enable post-mortem debugging.",
    ),
) -> None:
    """Evaluate Java patches. Single-repo (--repo) or multi-repo (--branch without --repo)."""
    from pathlib import Path as _Path

    if branch and not repo and not patch_path:
        from commit0.harness.evaluate_java import evaluate_java_repos
        dataset_map = _load_dataset_map()
        dataset_list = list(dataset_map.values())

        results = evaluate_java_repos(
            dataset=dataset_list,
            branch=branch,
            timeout=timeout,
            num_workers=num_workers,
            backend=backend,
        )

        typer.echo("repo,runtime,num_passed/num_tests")
        for name, elapsed, num_passed, num_total in results:
            typer.echo(f"{name},{elapsed:.1f},{num_passed}/{num_total}")

        total_runtime = sum(r[1] for r in results)
        avg_pass = (
            sum(r[2] / r[3] for r in results if r[3] > 0) / len([r for r in results if r[3] > 0])
            if any(r[3] > 0 for r in results)
            else 0.0
        )
        typer.echo(f"total runtime: {total_runtime:.3f}")
        typer.echo(f"average pass rate: {avg_pass}")
        return

    from commit0.harness.evaluate_java import evaluate_java_repo
    instance = _load_instance(repo)

    if branch and patch_path:
        raise typer.BadParameter("Provide --branch or --patch-path, not both.")

    if branch:
        import subprocess
        repo_path = instance.get("repo_path", "")
        base_commit = instance.get("base_commit", "")
        if not repo_path or not _Path(repo_path).exists():
            raise typer.BadParameter(f"Repo path '{repo_path}' not found.")
        if not base_commit:
            raise typer.BadParameter("No base_commit in instance data.")

        diff_result = subprocess.run(
            [
                "git", "diff", f"{base_commit}..{branch}",
                "--", ".",
                ":(exclude)spec.pdf.bz2",
                ":(exclude)*.bz2",
                ":(exclude).aider*",
                ":(exclude)logs/",
            ],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        if diff_result.returncode != 0:
            raise typer.BadParameter(
                f"git diff failed: {diff_result.stderr.strip()}"
            )

        patch_content = diff_result.stdout
        if not patch_content.strip():
            typer.echo(f"Warning: empty diff for {instance.get('repo', '?')} on branch {branch}")
            return

        tmp_patch = _Path(repo_path) / ".commit0_eval_patch.diff"
        tmp_patch.write_text(patch_content)
        patch_path = str(tmp_patch)

    if not patch_path:
        config = _load_config()
        patches_dir = _Path(config.get("patches_dir", "patches/java"))
        if repo:
            candidate = patches_dir / f"{repo.split('/')[-1]}.patch"
        else:
            candidate = patches_dir / "patch.diff"
        if candidate.exists():
            patch_path = str(candidate)
        else:
            raise typer.BadParameter(
                f"No patch file found at {candidate}. Provide --patch-path or --branch."
            )

    import time as _time

    repo_name = instance.get("repo", "unknown")
    short_name = repo_name.split("/")[-1] if "/" in repo_name else repo_name

    typer.echo(f"Evaluating Java repo: {short_name}")
    if branch:
        typer.echo(f"Branch: {branch}")

    _start = _time.monotonic()
    results = evaluate_java_repo(instance=instance, patch_path=patch_path, timeout=timeout, backend=backend, log_dir=log_dir)
    _elapsed = _time.monotonic() - _start

    from commit0.harness.constants import TestStatus as _TS

    num_passed = sum(1 for v in results.values() if v is _TS.PASSED)
    num_failed = sum(1 for v in results.values() if v is not _TS.PASSED)
    num_total = len(results)

    # Individual test results (matches Go eval log format)
    typer.echo(f"\n--- {short_name}: Individual Test Results ---")
    for test_id, status in sorted(results.items()):
        label = "PASSED" if status is _TS.PASSED else "FAILED"
        typer.echo(f"   {label}  {test_id}")
    typer.echo(f"  Summary: {num_failed} failed, {num_passed} passed")

    # CSV output (parsed by run_pipeline_java.sh)
    typer.echo("\nrepo,runtime,num_passed/num_tests")
    typer.echo(f"{repo_name},{_elapsed:.1f},{num_passed}/{num_total}")
    typer.echo(f"total runtime: {_elapsed:.3f}")
    if num_total > 0:
        typer.echo(f"average pass rate: {num_passed / num_total}")


@app.command()
def lint(
    repo: Optional[str] = typer.Option(None, help="Specific repo (resolves path from config)"),
    repo_path: Optional[str] = typer.Option(None, help="Explicit path to Java repo"),
    config: str = typer.Option("google_checks.xml", help="Checkstyle config"),
    files: Optional[List[str]] = typer.Argument(
        None,
        help="Absorbs the edited file aider auto-appends to the lint command; "
        "without it the call fails with a CLI usage error and the agent gets that "
        "instead of real lint output.",
    ),
) -> None:
    """Lint Java source files with Checkstyle."""
    from pathlib import Path as _Path
    from commit0.harness.lint_java import lint_java_checkstyle

    if not repo_path:
        if repo:
            instance = _load_instance(repo)
            repo_path = instance.get("repo_path", "")
        else:
            cfg = _load_config()
            repo_path = cfg.get("repos_dir", "repos/java")

    if not repo_path or not _Path(repo_path).exists():
        raise typer.BadParameter(
            f"Repo path '{repo_path}' not found. Provide --repo or --repo-path."
        )

    lint_java_checkstyle(repo_path=repo_path, config=config)


@app.command()
def stub(
    src_dir: Optional[str] = typer.Option(None, help="Java source directory to stub"),
    repo: Optional[str] = typer.Option(None, help="Repo name (resolves src dir from config)"),
    write_in_place: bool = typer.Option(True, help="Modify files in place"),
    preserve_javadoc: bool = typer.Option(True, help="Preserve Javadoc comments"),
) -> None:
    """Stub Java method bodies for benchmarking."""
    import json as _json
    from pathlib import Path as _Path

    try:
        from tools.stub_java import stub_java_sources
    except ImportError:
        raise typer.BadParameter(
            "The 'tools' package is required for 'stub' but is not installed. "
            "Install from a development checkout (tools/ is not shipped in the wheel)."
        )

    if not src_dir and not repo:
        raise typer.BadParameter("Provide --src-dir or --repo")

    if not src_dir:
        instance = _load_instance(repo)
        repo_path = instance.get("repo_path", "")
        if not repo_path:
            raise typer.BadParameter(f"Cannot resolve repo path for '{repo}'")
        candidate = _Path(repo_path) / "src" / "main" / "java"
        if candidate.is_dir():
            src_dir = str(candidate)
        else:
            raise typer.BadParameter(
                f"No src/main/java found in {repo_path}. Use --src-dir instead."
            )

    result = stub_java_sources(
        src_dir=src_dir,
        write_in_place=write_in_place,
        preserve_javadoc=preserve_javadoc,
    )
    typer.echo(_json.dumps(result, indent=2))


@app.command()
def agent(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    branch: str = typer.Option("java-agent", help="Git branch for agent work"),
    model: str = typer.Option("gpt-4", help="LLM model name"),
    max_iteration: int = typer.Option(3, help="Max aider reflections"),
    run_tests: bool = typer.Option(True, help="Use test-driven mode"),
    run_entire_dir_lint: bool = typer.Option(
        False,
        help="Stage 2 (lint) mode: run compile/lint first and drive fixes from its "
        "errors (lint_first) instead of re-sending the draft prompt.",
    ),
    log_dir: str = typer.Option("logs/agent", help="Log directory"),
    override_previous: bool = typer.Option(False, help="Reset to base commit"),
    use_unit_tests_info: bool = typer.Option(True, help="Include unit test context in prompts"),
    inject_test_files_readonly: bool = typer.Option(
        False,
        "--inject-test-files-readonly/--no-inject-test-files-readonly",
        help="Inject test SOURCE files as read-only context. Default OFF: reading "
        "the test bodies is answer-leakage and blows the prompt; anti-cheat "
        "protection of test files is unaffected. run_pipeline_java.sh passes "
        "--no-inject-test-files-readonly by default.",
    ),
    use_spec_info: bool = typer.Option(False, help="Include spec/README info in prompts"),
    # Ablation flags (parity with run_pipeline_java.sh, which passes these). They
    # exist on JavaAgentConfig but were not exposed on the CLI, so the pipeline
    # errored on --blind-tests/--names-only-tests/--strip-non-stubs when enabled.
    blind_lint: bool = typer.Option(False, help="Ablation: agent sees only lint summary, not per-error detail"),
    blind_tests: bool = typer.Option(False, help="Ablation: agent sees only the test summary line, not per-test failures"),
    names_only_tests: bool = typer.Option(False, help="Ablation: agent sees only failed test node IDs + counts, no tracebacks"),
    strip_non_stubs: bool = typer.Option(False, help="Ablation: strip non-stub source from context"),
    compile_check: bool = typer.Option(True, help="Run compile check before submitting"),
    cache_prompts: bool = typer.Option(True, help="Enable prompt caching"),
    max_test_output_length: int = typer.Option(15000, help="Max test output length in chars"),
    capture_thinking: bool = typer.Option(False, help="Capture reasoning/thinking tokens from LLM"),
    trajectory_md: bool = typer.Option(True, help="Write trajectory.md summary"),
    output_jsonl: bool = typer.Option(False, help="Write output.jsonl (OpenHands format)"),
    model_short: str = typer.Option("", help="Short model name for output metadata"),
    record_test_for_each_commit: bool = typer.Option(False, help="Run eval after each module commit"),
    repos_file: Optional[str] = typer.Option(None, help="File listing repo names, one per line (batch mode)"),
    max_parallel_repos: int = typer.Option(1, help="Max repos to run in parallel (batch mode)"),
) -> None:
    """Run the Java AI agent to implement stubbed methods."""
    import logging
    from pathlib import Path as _Path
    from agent.run_agent_java import run_java_agent, run_java_agent_for_repos
    from agent.config_java import JavaAgentConfig

    _Path(log_dir).mkdir(parents=True, exist_ok=True)
    agent_log_path = _Path(log_dir) / "agent_run.log"
    file_handler = logging.FileHandler(str(agent_log_path), mode="a")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)

    config = JavaAgentConfig(
        model=model,
        max_iteration=max_iteration,
        run_tests=run_tests,
        run_entire_dir_lint=run_entire_dir_lint,
        use_unit_tests_info=use_unit_tests_info,
        inject_test_files_readonly=inject_test_files_readonly,
        use_spec_info=use_spec_info,
        blind_lint=blind_lint,
        blind_tests=blind_tests,
        names_only_tests=names_only_tests,
        strip_non_stubs=strip_non_stubs,
        compile_check=compile_check,
        cache_prompts=cache_prompts,
        max_test_output_length=max_test_output_length,
        capture_thinking=capture_thinking,
        trajectory_md=trajectory_md,
        output_jsonl=output_jsonl,
        model_short=model_short,
        record_test_for_each_commit=record_test_for_each_commit,
    )
    if repos_file is not None:
        repos = [r.strip() for r in _Path(repos_file).read_text().splitlines() if r.strip()]
        if not repos:
            raise typer.BadParameter(f"repos_file '{repos_file}' contains no repo names")
        instances = [_load_instance(r) for r in repos]
        run_java_agent_for_repos(
            instances=instances,
            agent_config=config,
            branch=branch,
            override_previous_changes=override_previous,
            log_dir=log_dir,
            max_parallel_repos=max_parallel_repos,
        )
        return
    instance = _load_instance(repo)
    run_java_agent(
        instance=instance,
        agent_config=config,
        branch=branch,
        override_previous_changes=override_previous,
        log_dir=log_dir,
    )


@app.command(name="generate-test-ids")
def generate_test_ids(
    dataset_file: str = typer.Argument(..., help="Input dataset_entries.json"),
    output_dir: str = typer.Option("./test_ids", help="Output directory for .bz2 files"),
    docker: bool = typer.Option(False, help="Use Docker containers for discovery"),
    clone_dir: Optional[str] = typer.Option(None, help="Directory where repos are cloned"),
    install: bool = typer.Option(False, help="Install .bz2 files into commit0 data dir"),
    timeout: int = typer.Option(300, help="Timeout per repo in seconds"),
    max_repos: Optional[int] = typer.Option(None, help="Max repos to process"),
    validate_base: bool = typer.Option(False, help="Validate base commit compiles (requires --docker)"),
    build_system: Optional[str] = typer.Option(None, help="Override build system: maven or gradle"),
) -> None:
    """Generate Java test ID .bz2 files for batch processing."""
    from pathlib import Path as _Path

    try:
        from tools.generate_test_ids_java import generate_for_dataset, install_test_ids
    except ImportError:
        raise typer.BadParameter(
            "The 'tools' package is required for 'generate-test-ids' but is not installed. "
            "Install from a development checkout (tools/ is not shipped in the wheel)."
        )

    dataset_path = _Path(dataset_file)
    if not dataset_path.exists():
        raise typer.BadParameter(f"File not found: {dataset_path}")

    out = _Path(output_dir)
    results = generate_for_dataset(
        dataset_path=dataset_path,
        output_dir=out,
        use_docker=docker,
        clone_dir=_Path(clone_dir) if clone_dir else None,
        timeout=timeout,
        max_repos=max_repos,
        validate_base=validate_base,
        build_system_override=build_system,
    )

    total = sum(abs(v) for v in results.values())
    repos_with_tests = sum(1 for v in results.values() if v > 0)
    typer.echo(f"\nGenerated {total} test IDs across {repos_with_tests} repos")

    if install:
        installed = install_test_ids(out)
        typer.echo(f"Installed {installed} test ID files")


@app.command()
def save(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
) -> None:
    """Save Java repo patches."""
    from commit0.harness.setup_java import save_main
    save_main(repo=repo)


@app.command(name="list-repos")
def list_repos(
    split: Optional[str] = typer.Option(None, help="Dataset split (e.g. lite, all). Uses config split if omitted."),
) -> None:
    """List repo names in the dataset, one per line."""
    config = _load_config()
    dataset_name = config.get("dataset_name", "java_dataset.json")
    effective_split = split or config.get("dataset_split", "all")

    from commit0.harness.constants_java import JAVA_SPLIT
    from commit0.harness.utils import load_dataset_from_config

    dataset = load_dataset_from_config(dataset_name, split="test")

    from commit0.harness.split_utils import resolve_split

    allowed = set(resolve_split(effective_split, dataset, curated=JAVA_SPLIT))
    filtered = [
        e for e in dataset
        if e.get("repo", "").split("/")[-1] in allowed
        or e.get("original_repo", "").split("/")[-1] in allowed
    ]

    for entry in filtered:
        typer.echo(entry["repo"])


def _load_dataset_map() -> dict:
    """Load the Java dataset and return a repo_name -> instance dict.

    Each instance has setup fields promoted to top level for use by
    build_java_repo_images / make_java_spec.
    """
    config = _load_config()
    dataset_name = config.get("dataset_name", "java_dataset.json")

    from commit0.harness.utils import load_dataset_from_config

    entries = load_dataset_from_config(dataset_name, split="test")
    mapping = {}
    for entry in entries:
        instance = dict(entry)
        _promote_setup_fields(instance, config)
        mapping[instance["repo"]] = instance
    return mapping


def _load_config() -> dict:
    from pathlib import Path
    import yaml

    config_path = Path(".commit0.java.yaml")
    if not config_path.exists():
        raise typer.BadParameter(
            ".commit0.java.yaml not found. Run 'commit0-java setup' first."
        )
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def _promote_setup_fields(instance: dict, config: dict) -> None:
    setup = instance.get("setup", {})
    if "java_version" not in instance and "java_version" in setup:
        instance["java_version"] = setup["java_version"]
    if "build_system" not in instance and "build_system" in setup:
        instance["build_system"] = setup["build_system"]
    instance.setdefault("java_version", config.get("java_version", "17"))
    instance.setdefault("build_system", config.get("build_system", "maven"))
    instance.setdefault("test_framework", config.get("test_framework", "junit5"))


def _load_instance(repo: Optional[str]) -> dict:
    from pathlib import Path

    config = _load_config()
    dataset_name = config.get("dataset_name", "java_dataset.json")
    repos_dir = config.get("repos_dir", "repos/java")

    from commit0.harness.utils import load_dataset_from_config

    dataset = load_dataset_from_config(dataset_name, split="test")

    if repo:
        for entry in dataset:
            if (entry["repo"] == repo
                or entry["repo"].split("/")[-1] == repo
                or entry.get("original_repo", "") == repo
                or entry.get("original_repo", "").split("/")[-1] == repo):
                instance = dict(entry)
                repo_short = instance["repo"].split("/")[-1]
                instance["repo_path"] = str(Path(repos_dir) / repo_short)
                _promote_setup_fields(instance, config)
                return instance
        raise typer.BadParameter(
            f"Repo '{repo}' not found in dataset '{dataset_name}'."
        )

    if len(dataset) == 1:
        entry = dataset[0]
        instance = dict(entry)
        repo_short = instance["repo"].split("/")[-1]
        instance["repo_path"] = str(Path(repos_dir) / repo_short)
        _promote_setup_fields(instance, config)
        return instance

    raise typer.BadParameter(
        "Multiple repos in dataset. Provide --repo to select one."
    )


def main():
    app()


if __name__ == "__main__":
    main()
