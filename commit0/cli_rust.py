"""CLI for commit0 Rust integration.

Mirrors commit0/cli_go.py but routes to Rust-specific harness modules.
Entry point: python commit0/cli_rust.py [command]
"""

import logging
import os
import sys
from pathlib import Path
from typing import Union

import typer
import yaml
from typing_extensions import Annotated

import commit0.harness.build_rust
import commit0.harness.evaluate_rust
import commit0.harness.health_check_rust
import commit0.harness.lint_rust
import commit0.harness.run_rust_tests
import commit0.harness.save
import commit0.harness.setup_rust
from commit0.harness.constants_rust import RUST_SPLIT
from commit0.harness.utils import get_active_branch

logger = logging.getLogger(__name__)

commit0_rust_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="""
    Commit-0 Rust integration. Evaluates LLM-generated Rust libraries.

    See https://commit-0.github.io/ for documentation.
    """,
)


class Colors:
    RESET = "\033[0m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    ORANGE = "\033[95m"


def highlight(text: str, color: str) -> str:
    return f"{color}{text}{Colors.RESET}"


def check_valid(one: str, total: Union[list[str], dict[str, list[str]]]) -> None:
    if isinstance(total, dict):
        total = list(total.keys())
    if one not in total:
        valid = ", ".join([highlight(key, Colors.ORANGE) for key in total])
        raise typer.BadParameter(
            f"Invalid {highlight('REPO_OR_REPO_SPLIT', Colors.RED)}. Must be one of: {valid}",
            param_hint="REPO or REPO_SPLIT",
        )


_COMMIT0_RUST_REQUIRED_KEYS = {
    "dataset_name": str,
    "dataset_split": str,
    "repo_split": str,
    "base_dir": str,
}


def validate_commit0_rust_config(config: dict, config_path: str) -> None:
    missing = [k for k in _COMMIT0_RUST_REQUIRED_KEYS if k not in config]
    if missing:
        raise ValueError(
            f"Config file '{config_path}' is missing required keys: {missing}. "
            f"Required: {list(_COMMIT0_RUST_REQUIRED_KEYS.keys())}"
        )
    for key, expected_type in _COMMIT0_RUST_REQUIRED_KEYS.items():
        if not isinstance(config[key], expected_type):
            raise TypeError(
                f"Config key '{key}' in '{config_path}' must be {expected_type.__name__}, "
                f"got {type(config[key]).__name__}: {config[key]!r}"
            )
    base_dir = config["base_dir"]
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(
            f"base_dir '{base_dir}' from '{config_path}' does not exist. "
            f"Run 'setup' first."
        )


def write_commit0_rust_config(dot_file_path: str, config: dict) -> None:
    try:
        with open(dot_file_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
    except OSError as e:
        logger.error("Failed to write config to %s: %s", dot_file_path, e)
        raise


def read_commit0_rust_config(dot_file_path: str) -> dict:
    if not os.path.exists(dot_file_path):
        raise FileNotFoundError(
            f"Config file '{dot_file_path}' does not exist. Run 'setup' first."
        )
    with open(dot_file_path, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file '{dot_file_path}' is empty or invalid. "
            f"Expected a YAML mapping, got {type(data).__name__}."
        )
    validate_commit0_rust_config(data, dot_file_path)
    return data


@commit0_rust_app.command()
def setup(
    repo_split: str = typer.Argument(
        ...,
        help="Split of Rust repositories — 'all', a curated subset, or any repo name in the dataset.",
    ),
    dataset_name: str = typer.Option(
        "wentingzhao/commit0_rust", help="Name of the Rust dataset"
    ),
    dataset_split: str = typer.Option("test", help="Split of the dataset"),
    base_dir: str = typer.Option("repos/", help="Base directory to clone repos to"),
    commit0_config_file: str = typer.Option(
        ".commit0.rust.yaml", help="Path for stateful commit0-rust configs"
    ),
) -> None:
    """Clone Rust repositories for a given split."""
    base_dir = str(Path(base_dir).resolve())
    if dataset_name.endswith(".json"):
        dataset_name = str(Path(dataset_name).resolve())
    elif os.path.exists(dataset_name):
        dataset_name = str(Path(dataset_name).resolve())

    typer.echo(f"Cloning Rust repos for split: {highlight(repo_split, Colors.ORANGE)}")
    typer.echo(f"Dataset: {highlight(dataset_name, Colors.ORANGE)}")
    typer.echo(f"Base directory: {highlight(base_dir, Colors.ORANGE)}")

    commit0.harness.setup_rust.main(
        dataset_name,
        dataset_split,
        repo_split,
        base_dir,
    )

    write_commit0_rust_config(
        commit0_config_file,
        {
            "dataset_name": dataset_name,
            "dataset_split": dataset_split,
            "repo_split": repo_split,
            "base_dir": base_dir,
        },
    )


@commit0_rust_app.command()
def build(
    num_workers: int = typer.Option(4, help="Number of workers"),
    commit0_config_file: str = typer.Option(
        ".commit0.rust.yaml", help="Path to commit0-rust config file"
    ),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
) -> None:
    """Build Docker images for Rust repositories."""
    config = read_commit0_rust_config(commit0_config_file)

    typer.echo(
        f"Building Rust images for split: {highlight(config['repo_split'], Colors.ORANGE)}"
    )

    # build_rust.main takes a dataset_path (file or dir containing
    # *_rust_dataset.json), not the standard (dataset_name, dataset_split,
    # repo_split) tuple.  When dataset_name is a local JSON path, pass it
    # directly.  Otherwise fall back to base_dir (which setup may have
    # populated with JSON files).
    dataset_name = config["dataset_name"]
    if dataset_name.endswith(".json") or os.path.isfile(dataset_name):
        dataset_path = dataset_name
    else:
        dataset_path = config["base_dir"]

    commit0.harness.build_rust.main(
        dataset_path,
        num_workers,
        verbose,
    )


@commit0_rust_app.command()
def get_tests(
    repo_name: str = typer.Argument(
        ...,
        help="Rust repo name",
    ),
    base_dir: str = typer.Option(
        "repos",
        help="Base directory where Rust repos are cloned (default: repos)",
    ),
) -> None:
    """Get test IDs for a Rust repository via ``cargo test --list``.

    Calls the retry-with-backoff function in ``agent.agent_utils_rust`` and
    persists the result to ``commit0/data/rust_test_ids/<repo>.json`` so
    subsequent pipeline runs (and the agent) can read from cache instead of
    re-running cargo.
    """
    import json
    from agent.agent_utils_rust import get_rust_test_ids
    from commit0.harness.constants_rust import RUST_TEST_IDS_DIR

    repo_path = os.path.join(base_dir, repo_name)
    if not os.path.isdir(repo_path):
        raise typer.BadParameter(
            f"Repo path not found: {repo_path!r}. Run 'commit0 setup' first."
        )

    ids = get_rust_test_ids(repo_path)
    if not ids:
        typer.echo(
            f"WARNING: no test IDs collected for {repo_name!r}. "
            "Check that the Rust toolchain can build the repo (cargo --list "
            "may have failed). Run 'commit0 health-check' to diagnose.",
            err=True,
        )
        raise typer.Exit(code=1)

    RUST_TEST_IDS_DIR.mkdir(parents=True, exist_ok=True)
    cache_path_json = RUST_TEST_IDS_DIR / f"{repo_name}.json"
    cache_path_bz2 = RUST_TEST_IDS_DIR / f"{repo_name}.bz2"

    # Write both .json (human-readable / debug) AND .bz2 (canonical cache
    # format used by the other harness languages). The agent's loader at
    # agent_utils_rust.get_rust_test_ids tries .json first then falls back to
    # .bz2 — keeping both keeps the cache resilient to either being lost.
    import bz2 as _bz2
    cache_path_json.write_text(json.dumps(ids, indent=2))
    cache_path_bz2.write_bytes(_bz2.compress("\n".join(ids).encode("utf-8")))
    typer.echo(
        f"Collected {len(ids)} test IDs → "
        f"{cache_path_json.name} + {cache_path_bz2.name}"
    )


@commit0_rust_app.command()
def test(
    repo_or_repo_path: str = typer.Argument(
        ..., help="Directory of the Rust repository to test"
    ),
    test_ids: Union[str, None] = typer.Argument(
        None,
        help="Rust test IDs to run. Example: 'test_name' or '' for all.",
    ),
    branch: Union[str, None] = typer.Option(
        None, help="Branch to test (or use --reference)"
    ),
    backend: str = typer.Option("modal", help="Backend to use"),
    timeout: int = typer.Option(1800, help="Timeout in seconds"),
    num_cpus: int = typer.Option(1, help="Number of CPUs"),
    reference: Annotated[
        bool, typer.Option("--reference", help="Test the reference commit")
    ] = False,
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild image"),
    commit0_config_file: str = typer.Option(
        ".commit0.rust.yaml", help="Path to commit0-rust config file"
    ),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
    stdin: bool = typer.Option(
        False,
        "--stdin",
        help="Read test IDs from stdin",
    ),
) -> None:
    """Run tests on a Rust repository."""
    config = read_commit0_rust_config(commit0_config_file)
    if repo_or_repo_path.endswith("/"):
        repo_or_repo_path = repo_or_repo_path[:-1]

    all_repos = [r.split("/")[-1] for r in RUST_SPLIT.get("all", [])]
    check_valid(repo_or_repo_path.split("/")[-1], list(RUST_SPLIT.keys()) + all_repos)

    if reference:
        branch = "reference"
    elif branch is None:
        git_path = os.path.join(config["base_dir"], repo_or_repo_path.split("/")[-1])
        branch = get_active_branch(git_path)

    if stdin:
        test_ids = sys.stdin.read()
    elif test_ids is None:
        typer.echo("Error: test_ids must be provided or use --stdin", err=True)
        raise typer.Exit(code=1)

    assert test_ids is not None

    if verbose == 2:
        typer.echo(f"Running Rust tests for: {repo_or_repo_path}")
        typer.echo(f"Branch: {branch}")
        typer.echo(f"Test IDs: {test_ids}")

    commit0.harness.run_rust_tests.main(
        config["dataset_name"],
        config["dataset_split"],
        config["base_dir"],
        repo_or_repo_path,
        branch,
        test_ids,
        backend,
        timeout,
        num_cpus,
        rebuild,
        verbose,
    )


@commit0_rust_app.command()
def evaluate(
    branch: Union[str, None] = typer.Option(None, help="Branch to evaluate"),
    backend: str = typer.Option("modal", help="Backend to use"),
    timeout: int = typer.Option(1800, help="Timeout in seconds"),
    num_cpus: int = typer.Option(1, help="Number of CPUs"),
    num_workers: int = typer.Option(8, help="Number of workers"),
    reference: Annotated[
        bool, typer.Option("--reference", help="Evaluate the reference commit")
    ] = False,
    commit0_config_file: str = typer.Option(
        ".commit0.rust.yaml", help="Path to commit0-rust config file"
    ),
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild images"),
) -> None:
    """Evaluate Rust repositories for a split."""
    if reference:
        branch = "reference"

    config = read_commit0_rust_config(commit0_config_file)

    typer.echo(f"Evaluating Rust split: {highlight(config['repo_split'], Colors.ORANGE)}")
    typer.echo(f"Branch: {branch}")

    commit0.harness.evaluate_rust.main(
        config["dataset_name"],
        config["dataset_split"],
        config["repo_split"],
        config["base_dir"],
        branch,
        backend,
        timeout,
        num_cpus,
        num_workers,
        rebuild,
    )


@commit0_rust_app.command()
def lint(
    repo_or_repo_dir: str = typer.Argument(..., help="Rust repository to lint"),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
) -> None:
    """Lint Rust files using cargo clippy and cargo fmt."""
    if verbose == 2:
        typer.echo(
            f"Linting Rust repo: {highlight(str(repo_or_repo_dir), Colors.ORANGE)}"
        )

    commit0.harness.lint_rust.main(repo_or_repo_dir)


@commit0_rust_app.command()
def save(
    owner: str = typer.Argument(..., help="Repository owner"),
    branch: str = typer.Argument(..., help="Branch to save"),
    github_token: str = typer.Option(None, help="GitHub token"),
    commit0_config_file: str = typer.Option(
        ".commit0.rust.yaml", help="Path to commit0-rust config file"
    ),
) -> None:
    """Save Rust repositories to GitHub."""
    config = read_commit0_rust_config(commit0_config_file)

    repo_split = config["repo_split"]
    typer.echo(f"Saving Rust split: {highlight(repo_split, Colors.ORANGE)}")

    resolved_repos = RUST_SPLIT.get(repo_split, [repo_split])
    for repo_name in resolved_repos:
        commit0.harness.save.main(
            config["dataset_name"],
            config["dataset_split"],
            repo_name,
            config["base_dir"],
            owner,
            branch,
            github_token,
        )


@commit0_rust_app.command()
def health_check(
    commit0_config_file: str = typer.Option(
        ".commit0.rust.yaml", help="Path to commit0-rust config file"
    ),
) -> None:
    """Check that the Rust toolchain is installed and working."""
    try:
        config = read_commit0_rust_config(commit0_config_file)
        base_dir = config["base_dir"]
    except (FileNotFoundError, ValueError):
        base_dir = "."

    passed = commit0.harness.health_check_rust.main(base_dir)
    if not passed:
        raise typer.Exit(code=1)


__all__: list[str] = []

if __name__ == "__main__":
    commit0_rust_app()
