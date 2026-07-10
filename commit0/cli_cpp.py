import typer
from typing import List, Optional

app = typer.Typer(
    name="commit0-cpp",
    help="Commit0 C++ Pipeline",
    no_args_is_help=True,
)


@app.command()
def setup(
    dataset_name: str = typer.Option("fmt_cpp_dataset.json", help="Dataset name or local JSON path"),
    dataset_split: str = typer.Option("all", help="Dataset split"),
    repo_split: str = typer.Option("all", help="Repo split (filter by CPP_SPLIT key or repo name)"),
    base_dir: str = typer.Option("repos/cpp", help="Base directory for C++ repos"),
) -> None:
    """Set up C++ repositories."""
    from commit0.harness.setup_cpp import main as setup_main
    setup_main(
        dataset_name=dataset_name,
        dataset_split=dataset_split,
        repo_split=repo_split,
        base_dir=base_dir,
    )


@app.command()
def build(
    dataset_path: str = typer.Option("fmt_cpp_dataset.json", help="Dataset JSON path or directory"),
    num_workers: int = typer.Option(4, "-j", "--workers", help="Parallel build workers"),
    verbose: int = typer.Option(1, "-v", "--verbose", help="Verbosity level"),
) -> None:
    """Build C++ Docker images."""
    from commit0.harness.build_cpp import main as build_main
    build_main(dataset_path=dataset_path, num_workers=num_workers, verbose=verbose)


@app.command(name="get-tests")
def get_tests(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    save: bool = typer.Option(False, help="Save test IDs as .bz2 file"),
    output_dir: str = typer.Option("./test_ids", help="Output directory for .bz2 files"),
) -> None:
    """Discover C++ test IDs."""
    from pathlib import Path as _Path
    instance = _load_instance(repo)

    repo_name = instance.get("repo", "").split("/")[-1]

    bz2_path = _Path("commit0/data/cpp_test_ids") / f"{repo_name}.bz2"
    if bz2_path.exists():
        import bz2 as _bz2
        data = _bz2.decompress(bz2_path.read_bytes()).decode()
        test_ids = [l for l in data.strip().split("\n") if l]
    else:
        from tools.generate_test_ids_cpp import collect_test_ids_local
        repo_path = instance.get("repo_path", "")
        if not repo_path:
            config = _load_config()
            repo_path = str(_Path(config.get("repos_dir", "repos/cpp")) / repo_name)
        test_ids = collect_test_ids_local(_Path(repo_path))

    if not test_ids:
        typer.echo("No test IDs found.")
        return

    typer.echo(f"Found {len(test_ids)} test IDs:")
    for tid in test_ids[:20]:
        typer.echo(f"  {tid}")
    if len(test_ids) > 20:
        typer.echo(f"  ... and {len(test_ids) - 20} more")

    if save and repo:
        from tools.generate_test_ids_cpp import save_test_ids
        repo_short = repo.split("/")[-1] if "/" in repo else repo
        out_file = save_test_ids(repo_short, test_ids, _Path(output_dir))
        typer.echo(f"\nSaved to {out_file}")


@app.command()
def test(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    timeout: int = typer.Option(7200, help="Test timeout in seconds"),
    verbose: int = typer.Option(0, help="Verbosity level"),
) -> None:
    """Run C++ tests."""
    from commit0.harness.run_cpp_tests import main as run_cpp_tests_main
    instance = _load_instance(repo)

    config = _load_config()
    dataset_name = config.get("dataset_name", "fmt_cpp_dataset.json")
    dataset_split = config.get("dataset_split", "all")
    base_dir = config.get("base_dir", "repos/cpp")
    backend = config.get("backend", "local")
    num_cpus = config.get("num_cpus", 1)
    rebuild_image = config.get("rebuild_image", False)

    repo_name = instance.get("repo", "")
    test_info = instance.get("test", {})
    test_ids = test_info.get("test_dir", "") if isinstance(test_info, dict) else str(test_info)

    exit_code = run_cpp_tests_main(
        dataset_name=dataset_name,
        dataset_split=dataset_split,
        base_dir=base_dir,
        repo_or_repo_dir=instance.get("repo_path", repo_name),
        branch=config.get("branch", "commit0"),
        test_ids=test_ids,
        backend=backend,
        timeout=timeout,
        num_cpus=num_cpus,
        rebuild_image=rebuild_image,
        verbose=verbose,
    )
    raise typer.Exit(code=exit_code)


@app.command()
def evaluate(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    patch_path: Optional[str] = typer.Option(None, help="Path to patch file"),
    branch: Optional[str] = typer.Option(None, help="Evaluate from git branch"),
    timeout: int = typer.Option(7200, help="Evaluation timeout in seconds"),
    num_workers: int = typer.Option(4, help="Parallel evaluation workers"),
) -> None:
    """Evaluate C++ patches."""
    from pathlib import Path as _Path

    if branch and not repo and not patch_path:
        from commit0.harness.evaluate_cpp import main as evaluate_main
        config = _load_config()
        evaluate_main(
            dataset_name=config.get("dataset_name", "fmt_cpp_dataset.json"),
            dataset_split=config.get("dataset_split", "all"),
            repo_split=config.get("repo_split", "all"),
            base_dir=config.get("base_dir", "repos/cpp"),
            branch=branch,
            backend=config.get("backend", "local"),
            timeout=timeout,
            num_cpus=config.get("num_cpus", 1),
            num_workers=num_workers,
            rebuild_image=config.get("rebuild_image", False),
        )
        return

    from commit0.harness.evaluate_cpp import evaluate_single_repo
    instance = _load_instance(repo)

    if branch and patch_path:
        raise typer.BadParameter("Provide --branch or --patch-path, not both.")

    if branch:
        import subprocess
        repo_path = instance.get("repo_path", "")
        base_commit = instance.get("base_commit", "")
        if not repo_path or not _Path(repo_path).exists():
            raise typer.BadParameter(f"Repo path '{repo_path}' not found.")

        diff_result = subprocess.run(
            [
                "git", "diff", f"{base_commit}..{branch}",
                "--", ".",
                ":(exclude)build/",
                ":(exclude).aider*",
                ":(exclude)logs/",
            ],
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=120,
        )
        if diff_result.returncode != 0:
            raise typer.BadParameter(f"git diff failed: {diff_result.stderr.strip()}")

        patch_content = diff_result.stdout
        if not patch_content.strip():
            typer.echo(f"Warning: empty diff for {instance.get('repo', '?')} on branch {branch}")
            return

        tmp_patch = _Path(repo_path) / ".commit0_eval_patch.diff"
        tmp_patch.write_text(patch_content)
        patch_path = str(tmp_patch)

    if not patch_path:
        config = _load_config()
        patches_dir = _Path(config.get("patches_dir", "patches/cpp"))
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

    typer.echo(f"Evaluating C++ repo: {short_name}")
    _start = _time.monotonic()
    results = evaluate_single_repo(instance=instance, patch_path=patch_path, timeout=timeout)
    _elapsed = _time.monotonic() - _start

    if "__error__" in results:
        typer.echo(f"\nEvaluation error: {results['__error__']}", err=True)
        raise typer.Exit(code=1)

    from commit0.harness.constants import TestStatus as _TS
    num_passed = sum(1 for v in results.values() if v is _TS.PASSED)
    num_total = len(results)

    typer.echo("\nrepo,runtime,num_passed/num_tests")
    typer.echo(f"{repo_name},{_elapsed:.1f},{num_passed}/{num_total}")
    typer.echo(f"total runtime: {_elapsed:.3f}")
    if num_total > 0:
        typer.echo(f"average pass rate: {num_passed / num_total}")


@app.command()
def lint(
    repo: Optional[str] = typer.Option(None, help="Specific repo"),
    repo_path: Optional[str] = typer.Option(None, help="Explicit path to C++ repo"),
    files: Optional[List[str]] = typer.Argument(
        None,
        help="Absorbs the edited file aider auto-appends to the lint command; "
        "without it the call fails with a CLI usage error and the agent gets that "
        "instead of real lint output.",
    ),
) -> None:
    """Lint C++ source files with clang-tidy and clang-format."""
    from pathlib import Path as _Path
    from commit0.harness.lint_cpp import lint_cpp_repo

    if not repo_path:
        if repo:
            instance = _load_instance(repo)
            repo_path = instance.get("repo_path", "")
        else:
            cfg = _load_config()
            repo_path = cfg.get("repos_dir", "repos/cpp")

    if not repo_path or not _Path(repo_path).exists():
        raise typer.BadParameter(
            f"Repo path '{repo_path}' not found. Provide --repo or --repo-path."
        )

    lint_cpp_repo(repo_or_dir=repo_path)


@app.command()
def stub(
    src_dir: Optional[str] = typer.Option(None, help="C++ source directory to stub"),
    repo: Optional[str] = typer.Option(None, help="Repo name (resolves src dir from config)"),
    compile_commands: Optional[str] = typer.Option(None, help="Path to compile_commands.json"),
) -> None:
    """Stub C++ function bodies for benchmarking."""
    try:
        from tools.stub_cpp import stub_cpp_directory
    except ImportError:
        raise typer.BadParameter(
            "The 'tools' package is required for 'stub' but is not installed."
        )

    if not src_dir and not repo:
        raise typer.BadParameter("Provide --src-dir or --repo")

    if not src_dir:
        instance = _load_instance(repo)
        repo_path = instance.get("repo_path", "")
        if not repo_path:
            raise typer.BadParameter(f"Cannot resolve repo path for '{repo}'")
        src_dir = repo_path

    count = stub_cpp_directory(
        src_dir=src_dir,
        compile_commands=compile_commands,
    )
    typer.echo(f"Stubbed {count} functions")


@app.command()
def agent(
    repo: Optional[str] = typer.Option(None, help="Specific repo (runs single-repo mode)"),
    branch: str = typer.Option("cpp-agent", help="Git branch for agent work"),
    model: str = typer.Option("gpt-4", help="LLM model name"),
    max_iteration: int = typer.Option(3, help="Max aider reflections"),
    run_tests: bool = typer.Option(True, help="Use test-driven mode"),
    log_dir: str = typer.Option("logs/agent", help="Log directory"),
    override_previous: bool = typer.Option(False, help="Reset to base commit"),
    use_unit_tests_info: bool = typer.Option(True, help="Include unit test context in prompts"),
    use_lint_info: bool = typer.Option(False, help="Include lint info in prompts"),
    backend: str = typer.Option("local", help="Execution backend (local/modal)"),
    agent_config_file: str = typer.Option("", help="Path to agent config YAML (overrides CLI flags)"),
    commit0_config_file: str = typer.Option(".commit0.cpp.yaml", help="Path to commit0 config"),
    max_parallel_repos: int = typer.Option(1, help="Max repos to process in parallel (batch mode)"),
    cache_prompts: bool = typer.Option(True, help="Enable prompt caching"),
    capture_thinking: bool = typer.Option(False, help="Capture reasoning/thinking tokens"),
) -> None:
    """Run the C++ AI agent to implement stubbed functions."""
    import logging
    from pathlib import Path as _Path
    from agent.run_cpp_agent import run_cpp_agent, run_cpp_agent_for_repo
    from agent.agent_utils import load_agent_config
    from agent.class_types import AgentConfig

    _Path(log_dir).mkdir(parents=True, exist_ok=True)
    agent_log_path = _Path(log_dir) / "agent_run.log"
    file_handler = logging.FileHandler(str(agent_log_path), mode="a")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)

    if repo:
        # Single-repo mode: build AgentConfig from CLI flags and call for_repo
        if agent_config_file:
            agent_config = load_agent_config(agent_config_file)
        else:
            agent_config = AgentConfig(
                agent_name="aider",
                model_name=model,
                use_user_prompt=False,
                user_prompt="",
                use_topo_sort_dependencies=False,
                add_import_module_to_context=False,
                use_repo_info=False,
                max_repo_info_length=0,
                use_unit_tests_info=use_unit_tests_info,
                max_unit_tests_info_length=8000,
                use_spec_info=False,
                max_spec_info_length=0,
                use_lint_info=use_lint_info,
                run_entire_dir_lint=False,
                max_lint_info_length=4000,
                pre_commit_config_path="",
                run_tests=run_tests,
                max_iteration=max_iteration,
                record_test_for_each_commit=False,
                cache_prompts=cache_prompts,
                capture_thinking=capture_thinking,
            )

        instance = _load_instance(repo)
        config = _load_config()
        repo_base_dir = config.get("base_dir", "repos/cpp")

        run_cpp_agent_for_repo(
            repo_base_dir=repo_base_dir,
            agent_config=agent_config,
            example=instance,
            branch=branch,
            override_previous_changes=override_previous,
            backend=backend,
            log_dir=log_dir,
            commit0_config_file=commit0_config_file,
        )
    else:
        # Batch mode: use agent config file and run across all repos
        if not agent_config_file:
            agent_config_file = "agent/config/agent_config.yaml"
        run_cpp_agent(
            branch=branch,
            override_previous_changes=override_previous,
            backend=backend,
            agent_config_file=agent_config_file,
            commit0_config_file=commit0_config_file,
            log_dir=log_dir,
            max_parallel_repos=max_parallel_repos,
        )


@app.command(name="list-repos")
def list_repos(
    split: Optional[str] = typer.Option(None, help="Dataset split"),
) -> None:
    """List repo names in the dataset."""
    config = _load_config()
    dataset_name = config.get("dataset_name", "fmt_cpp_dataset.json")
    effective_split = split or config.get("dataset_split", "all")

    from commit0.harness.constants_cpp import CPP_SPLIT
    import json
    from pathlib import Path

    dataset_path = Path(dataset_name)
    if not dataset_path.exists():
        typer.echo(f"Dataset file not found: {dataset_path}")
        raise typer.Exit(1)

    with open(dataset_path) as f:
        dataset = json.load(f)

    if isinstance(dataset, dict):
        dataset = list(dataset.values())

    from commit0.harness.split_utils import resolve_split

    allowed = set(resolve_split(effective_split, dataset, curated=CPP_SPLIT))
    filtered = [e for e in dataset if e.get("repo", "").split("/")[-1] in allowed]

    for entry in filtered:
        typer.echo(entry["repo"])


def _load_config() -> dict:
    from pathlib import Path
    import yaml

    config_path = Path(".commit0.cpp.yaml")
    if not config_path.exists():
        raise typer.BadParameter(
            ".commit0.cpp.yaml not found. Run 'commit0-cpp setup' first."
        )
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def _load_instance(repo: Optional[str]) -> dict:
    from pathlib import Path
    import json

    config = _load_config()
    dataset_name = config.get("dataset_name", "fmt_cpp_dataset.json")
    repos_dir = config.get("repos_dir", "repos/cpp")

    dataset_path = Path(dataset_name)
    if not dataset_path.exists():
        raise typer.BadParameter(f"Dataset file not found: {dataset_path}")

    with open(dataset_path) as f:
        dataset = json.load(f)

    if isinstance(dataset, dict):
        dataset = list(dataset.values())

    if repo:
        for entry in dataset:
            if (entry["repo"] == repo
                or entry["repo"].split("/")[-1] == repo
                or entry.get("instance_id", "") == repo):
                instance = dict(entry)
                repo_short = instance["repo"].split("/")[-1]
                instance["repo_path"] = str(Path(repos_dir) / repo_short)
                return instance
        raise typer.BadParameter(f"Repo '{repo}' not found in dataset '{dataset_name}'.")

    if len(dataset) == 1:
        instance = dict(dataset[0])
        repo_short = instance["repo"].split("/")[-1]
        instance["repo_path"] = str(Path(repos_dir) / repo_short)
        return instance

    raise typer.BadParameter("Multiple repos in dataset. Provide --repo to select one.")


def main() -> None:
    """Commit0 C++ CLI entry point."""
    app()


if __name__ == "__main__":
    main()
