"""CLI for commit0 C integration.

Mirrors commit0/cli_go.py but routes to C-specific harness modules. Entry
point: ``python -m commit0.cli_c [command]``.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Union

import typer
import yaml
from typing_extensions import Annotated

import commit0.harness.build_c
import commit0.harness.evaluate_c
import commit0.harness.get_c_test_ids
import commit0.harness.lint_c
import commit0.harness.run_c_tests
import commit0.harness.save
import commit0.harness.setup_c
from commit0.harness.constants_c import (
    C_SPLIT,
    C_SPLIT_ALL,
    resolve_c_split,
    resolve_c_split_all,
)
from commit0.harness.utils import get_active_branch

logger = logging.getLogger(__name__)

commit0_c_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="""
    Commit-0 C integration. Evaluates LLM-generated C libraries.

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


_COMMIT0_C_REQUIRED_KEYS = {
    "dataset_name": str,
    "dataset_split": str,
    "repo_split": str,
    "base_dir": str,
}


def validate_commit0_c_config(config: dict, config_path: str) -> None:
    missing = [k for k in _COMMIT0_C_REQUIRED_KEYS if k not in config]
    if missing:
        raise ValueError(
            f"Config file '{config_path}' is missing required keys: {missing}. "
            f"Required: {list(_COMMIT0_C_REQUIRED_KEYS.keys())}"
        )
    for key, expected_type in _COMMIT0_C_REQUIRED_KEYS.items():
        if not isinstance(config[key], expected_type):
            raise TypeError(
                f"Config key '{key}' in '{config_path}' must be {expected_type.__name__}, "
                f"got {type(config[key]).__name__}: {config[key]!r}"
            )
    base_dir = config["base_dir"]
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(
            f"base_dir '{base_dir}' from '{config_path}' does not exist. "
            f"Run 'commit0-c setup' first."
        )


def write_commit0_c_config(dot_file_path: str, config: dict) -> None:
    try:
        with open(dot_file_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
    except OSError as e:
        logger.error("Failed to write config to %s: %s", dot_file_path, e)
        raise


def read_commit0_c_config(dot_file_path: str) -> dict:
    if not os.path.exists(dot_file_path):
        raise FileNotFoundError(
            f"Config file '{dot_file_path}' does not exist. Run 'commit0-c setup' first."
        )
    with open(dot_file_path, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file '{dot_file_path}' is empty or invalid. "
            f"Expected a YAML mapping, got {type(data).__name__}."
        )
    validate_commit0_c_config(data, dot_file_path)
    return data


@commit0_c_app.command()
def setup(
    repo_split: str = typer.Argument(
        ...,
        help=f"Split of C repositories, one of: {', '.join([highlight(key, Colors.ORANGE) for key in C_SPLIT.keys()])}",
    ),
    dataset_name: str = typer.Option(
        "wentingzhao/commit0_c", help="Name of the C dataset"
    ),
    dataset_split: str = typer.Option("test", help="Split of the dataset"),
    base_dir: str = typer.Option("repos/", help="Base directory to clone repos to"),
    commit0_config_file: str = typer.Option(
        ".commit0.c.yaml", help="Path for stateful commit0-c configs"
    ),
) -> None:
    """Clone C repositories for a given split."""
    if repo_split != "all":
        merged = resolve_c_split(dataset_name, dataset_split)
        check_valid(
            repo_split,
            list(merged.keys()) + resolve_c_split_all(dataset_name, dataset_split),
        )

    base_dir = str(Path(base_dir).resolve())
    if dataset_name.endswith(".json"):
        dataset_name = str(Path(dataset_name).resolve())
    elif os.path.exists(dataset_name):
        dataset_name = str(Path(dataset_name).resolve())

    typer.echo(f"Cloning C repos for split: {highlight(repo_split, Colors.ORANGE)}")
    typer.echo(f"Dataset: {highlight(dataset_name, Colors.ORANGE)}")
    typer.echo(f"Base directory: {highlight(base_dir, Colors.ORANGE)}")

    commit0.harness.setup_c.main(
        dataset_name,
        dataset_split,
        repo_split,
        base_dir,
    )

    write_commit0_c_config(
        commit0_config_file,
        {
            "dataset_name": dataset_name,
            "dataset_split": dataset_split,
            "repo_split": repo_split,
            "base_dir": base_dir,
        },
    )


@commit0_c_app.command()
def build(
    num_workers: int = typer.Option(8, help="Number of workers"),
    commit0_config_file: str = typer.Option(
        ".commit0.c.yaml", help="Path to commit0-c config file"
    ),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
    single_arch: bool = typer.Option(
        False, "--single-arch", help="Build for native architecture only"
    ),
) -> None:
    """Build Docker images for C repositories."""
    if single_arch:
        import platform as _plat

        machine = _plat.machine()
        native = "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"
        os.environ["COMMIT0_BUILD_PLATFORMS"] = native

    config = read_commit0_c_config(commit0_config_file)
    if (
        config["repo_split"] != "all"
        and "commit0" in config["dataset_name"].split("/")[-1].lower()
    ):
        merged = resolve_c_split(config["dataset_name"], config["dataset_split"])
        check_valid(
            config["repo_split"],
            list(merged.keys())
            + resolve_c_split_all(config["dataset_name"], config["dataset_split"]),
        )

    typer.echo(
        f"Building C images for split: {highlight(config['repo_split'], Colors.ORANGE)}"
    )

    commit0.harness.build_c.main(
        config["dataset_name"],
        config["dataset_split"],
        config["repo_split"],
        num_workers,
        verbose,
    )


@commit0_c_app.command()
def get_tests(
    repo_name: str = typer.Argument(
        ...,
        help=f"C repo name, one of: {', '.join(highlight(r, Colors.ORANGE) for r in C_SPLIT_ALL)}",
    ),
) -> None:
    """Get test IDs for a C repository."""
    commit0.harness.get_c_test_ids.main(repo_name, verbose=1)


@commit0_c_app.command()
def test(
    repo_or_repo_path: str = typer.Argument(
        ..., help="Directory of the C repository to test"
    ),
    test_ids: str = typer.Argument(
        None,
        help="C test IDs to run. Example: 'parse_object' or CTest -R regex.",
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
        ".commit0.c.yaml", help="Path to commit0-c config file"
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
    """Run tests on a C repository."""
    config = read_commit0_c_config(commit0_config_file)
    if repo_or_repo_path.endswith("/"):
        repo_or_repo_path = repo_or_repo_path[:-1]
    merged = resolve_c_split(config["dataset_name"], config["dataset_split"])
    check_valid(
        repo_or_repo_path.split("/")[-1],
        list(merged.keys())
        + resolve_c_split_all(config["dataset_name"], config["dataset_split"]),
    )

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
        typer.echo(f"Running C tests for: {repo_or_repo_path}")
        typer.echo(f"Branch: {branch}")
        typer.echo(f"Test IDs: {test_ids}")

    exit_code = commit0.harness.run_c_tests.main(
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


@commit0_c_app.command()
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
        ".commit0.c.yaml", help="Path to commit0-c config file"
    ),
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild images"),
) -> None:
    """Evaluate C repositories for a split."""
    if reference:
        branch = "reference"

    config = read_commit0_c_config(commit0_config_file)
    if config["repo_split"] != "all":
        merged = resolve_c_split(config["dataset_name"], config["dataset_split"])
        check_valid(
            config["repo_split"],
            list(merged.keys())
            + resolve_c_split_all(config["dataset_name"], config["dataset_split"]),
        )

    typer.echo(f"Evaluating C split: {highlight(config['repo_split'], Colors.ORANGE)}")
    typer.echo(f"Branch: {branch}")

    commit0.harness.evaluate_c.main(
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


@commit0_c_app.command()
def lint(
    repo_or_repo_dir: str = typer.Argument(..., help="C repository to lint"),
    commit0_config_file: str = typer.Option(
        ".commit0.c.yaml", help="Path to commit0-c config file"
    ),
    verbose: int = typer.Option(
        1, "--verbose", "-v", help="Verbosity level", count=True
    ),
) -> None:
    """Lint C files using clang-tidy and cppcheck."""
    config = read_commit0_c_config(commit0_config_file)

    if verbose == 2:
        typer.echo(
            f"Linting C repo: {highlight(str(repo_or_repo_dir), Colors.ORANGE)}"
        )

    commit0.harness.lint_c.main(
        config["dataset_name"],
        config["dataset_split"],
        repo_or_repo_dir,
        config["base_dir"],
    )


@commit0_c_app.command()
def save(
    owner: str = typer.Argument(..., help="Repository owner"),
    branch: str = typer.Argument(..., help="Branch to save"),
    github_token: str = typer.Option(None, help="GitHub token"),
    commit0_config_file: str = typer.Option(
        ".commit0.c.yaml", help="Path to commit0-c config file"
    ),
) -> None:
    """Save C repositories to GitHub."""
    config = read_commit0_c_config(commit0_config_file)

    repo_split = config["repo_split"]
    typer.echo(f"Saving C split: {highlight(repo_split, Colors.ORANGE)}")

    merged = resolve_c_split(config["dataset_name"], config["dataset_split"])
    resolved_repos = merged.get(repo_split, [repo_split])
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


def main() -> None:
    commit0_c_app()


__all__: list[str] = []


if __name__ == "__main__":
    commit0_c_app()
