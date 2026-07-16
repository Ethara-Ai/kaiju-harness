"""CLI for commit0 Go integration.

Mirrors commit0/cli.py but routes to Go-specific harness modules.
Entry point: python commit0/cli_go.py [command]
"""

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Union

import typer
import yaml
from typing_extensions import Annotated

import commit0.harness.build_go
import commit0.harness.evaluate_go
import commit0.harness.get_go_test_ids
import commit0.harness.lint_go
import commit0.harness.run_go_tests
import commit0.harness.save
import commit0.harness.setup_go
from commit0.harness.constants_go import GO_SPLIT
from commit0.harness.utils import get_active_branch
from commit0.harness.split_utils import resolve_split
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)

commit0_go_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="""
    Commit-0 Go integration. Evaluates LLM-generated Go libraries.

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


_COMMIT0_GO_REQUIRED_KEYS = {
    "dataset_name": str,
    "dataset_split": str,
    "repo_split": str,
    "base_dir": str,
}


def validate_commit0_go_config(config: dict, config_path: str) -> None:
    missing = [k for k in _COMMIT0_GO_REQUIRED_KEYS if k not in config]
    if missing:
        raise ValueError(
            f"Config file '{config_path}' is missing required keys: {missing}. "
            f"Required: {list(_COMMIT0_GO_REQUIRED_KEYS.keys())}"
        )
    for key, expected_type in _COMMIT0_GO_REQUIRED_KEYS.items():
        if not isinstance(config[key], expected_type):
            raise TypeError(
                f"Config key '{key}' in '{config_path}' must be {expected_type.__name__}, "
                f"got {type(config[key]).__name__}: {config[key]!r}"
            )
    base_dir = config["base_dir"]
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(
            f"base_dir '{base_dir}' from '{config_path}' does not exist. "
            f"Run 'commit0-go setup' first."
        )


def write_commit0_go_config(dot_file_path: str, config: dict) -> None:
    try:
        with open(dot_file_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
    except OSError as e:
        logger.error("Failed to write config to %s: %s", dot_file_path, e)
        raise


def read_commit0_go_config(dot_file_path: str) -> dict:
    if not os.path.exists(dot_file_path):
        raise FileNotFoundError(
            f"Config file '{dot_file_path}' does not exist. Run 'commit0-go setup' first."
        )
    with open(dot_file_path, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file '{dot_file_path}' is empty or invalid. "
            f"Expected a YAML mapping, got {type(data).__name__}."
        )
    validate_commit0_go_config(data, dot_file_path)
    return data


@commit0_go_app.command()
def setup(
    repo_split: str = typer.Argument(
        ...,
        help=f"Split of Go repositories, one of: {', '.join([highlight(key, Colors.ORANGE) for key in GO_SPLIT.keys()])}",
    ),
    dataset_name: str = typer.Option(
        "wentingzhao/commit0_go", help="Name of the Go dataset"
    ),
    dataset_split: str = typer.Option("test", help="Split of the dataset"),
    base_dir: str = typer.Option("repos/", help="Base directory to clone repos to"),
    commit0_config_file: str = typer.Option(
        ".commit0.go.yaml", help="Path for stateful commit0-go configs"
    ),
) -> None:
    """Clone Go repositories for a given split."""
    # Split validation deferred to runtime in setup_go.main.

    base_dir = str(Path(base_dir).resolve())
    if dataset_name.endswith(".json"):
        dataset_name = str(Path(dataset_name).resolve())
    elif os.path.exists(dataset_name):
        dataset_name = str(Path(dataset_name).resolve())

    typer.echo(f"Cloning Go repos for split: {highlight(repo_split, Colors.ORANGE)}")
    typer.echo(f"Dataset: {highlight(dataset_name, Colors.ORANGE)}")
    typer.echo(f"Base directory: {highlight(base_dir, Colors.ORANGE)}")

    commit0.harness.setup_go.main(
        dataset_name,
        dataset_split,
        repo_split,
        base_dir,
    )

    write_commit0_go_config(
        commit0_config_file,
        {
            "dataset_name": dataset_name,
            "dataset_split": dataset_split,
            "repo_split": repo_split,
            "base_dir": base_dir,
        },
    )


@commit0_go_app.command()
def build(
    num_workers: int = typer.Option(8, help="Number of workers"),
    commit0_config_file: str = typer.Option(
        ".commit0.go.yaml", help="Path to commit0-go config file"
    ),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
    single_arch: bool = typer.Option(
        False, "--single-arch", help="Build for native architecture only"
    ),
) -> None:
    """Build Docker images for Go repositories."""
    if single_arch:
        import platform as _plat

        machine = _plat.machine()
        native = "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"
        os.environ["COMMIT0_BUILD_PLATFORMS"] = native

    config = read_commit0_go_config(commit0_config_file)
    # Split validation deferred to runtime in build_go.main.

    typer.echo(
        f"Building Go images for split: {highlight(config['repo_split'], Colors.ORANGE)}"
    )

    commit0.harness.build_go.main(
        config["dataset_name"],
        config["dataset_split"],
        config["repo_split"],
        num_workers,
        verbose,
    )


@commit0_go_app.command()
def get_tests(
    repo_name: str = typer.Argument(
        ...,
        help="Go repo name (must match a repo in the dataset).",
    ),
) -> None:
    """Get test IDs for a Go repository."""
    commit0.harness.get_go_test_ids.main(repo_name, verbose=1)


@commit0_go_app.command()
def test(
    repo_or_repo_path: str = typer.Argument(
        ..., help="Directory of the Go repository to test"
    ),
    test_ids: str = typer.Argument(
        None,
        help="Go test IDs to run. Example: 'package/TestName' or './...' for all.",
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
        ".commit0.go.yaml", help="Path to commit0-go config file"
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
    """Run tests on a Go repository."""
    config = read_commit0_go_config(commit0_config_file)
    if repo_or_repo_path.endswith("/"):
        repo_or_repo_path = repo_or_repo_path[:-1]
    # Split validation deferred to runtime in run_go_tests.main.

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

    if verbose == 2:
        typer.echo(f"Running Go tests for: {repo_or_repo_path}")
        typer.echo(f"Branch: {branch}")
        typer.echo(f"Test IDs: {test_ids}")

    exit_code = commit0.harness.run_go_tests.main(
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
    if exit_code:
        raise typer.Exit(code=exit_code)


@commit0_go_app.command()
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
        ".commit0.go.yaml", help="Path to commit0-go config file"
    ),
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild images"),
) -> None:
    """Evaluate Go repositories for a split."""
    if reference:
        branch = "reference"

    config = read_commit0_go_config(commit0_config_file)
    # Split validation deferred to runtime in evaluate_go.main.

    typer.echo(f"Evaluating Go split: {highlight(config['repo_split'], Colors.ORANGE)}")
    typer.echo(f"Branch: {branch}")

    commit0.harness.evaluate_go.main(
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


@commit0_go_app.command()
def lint(
    repo_or_repo_dir: str = typer.Argument(..., help="Go repository to lint"),
    files: Optional[List[str]] = typer.Argument(
        None,
        help="Absorbs the edited file aider auto-appends to the lint command; "
        "without it the call fails with a CLI usage error and the agent gets that "
        "instead of real lint output.",
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.go.yaml", help="Path to commit0-go config file"
    ),
    backend: str = typer.Option(
        "local",
        "--backend",
        help="Execution backend: 'local' spawns a Docker container (default; "
        "needs docker.sock); 'local_inplace' runs linters directly in the "
        "current environment (matches agent-container pipelines).",
    ),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
) -> None:
    """Lint Go files using goimports, staticcheck, and go vet."""
    config = read_commit0_go_config(commit0_config_file)

    if verbose == 2:
        typer.echo(
            f"Linting Go repo: {highlight(str(repo_or_repo_dir), Colors.ORANGE)}"
        )

    commit0.harness.lint_go.main(
        config["dataset_name"],
        config["dataset_split"],
        repo_or_repo_dir,
        config["base_dir"],
        backend=backend,
    )


@commit0_go_app.command()
def save(
    owner: str = typer.Argument(..., help="Repository owner"),
    branch: str = typer.Argument(..., help="Branch to save"),
    github_token: str = typer.Option(None, help="GitHub token"),
    commit0_config_file: str = typer.Option(
        ".commit0.go.yaml", help="Path to commit0-go config file"
    ),
) -> None:
    """Save Go repositories to GitHub."""
    config = read_commit0_go_config(commit0_config_file)

    repo_split = config["repo_split"]
    typer.echo(f"Saving Go split: {highlight(repo_split, Colors.ORANGE)}")

    save_dataset = load_dataset_from_config(
        config["dataset_name"], split=config["dataset_split"]
    )
    resolved_repos = resolve_split(repo_split, save_dataset, curated=GO_SPLIT)
    if not resolved_repos:
        resolved_repos = [repo_split]
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


__all__: list[str] = []

if __name__ == "__main__":
    commit0_go_app()
