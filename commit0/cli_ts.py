import logging
import os

import typer
import yaml
from pathlib import Path
from typing import Union

import commit0.harness.setup_ts

logger = logging.getLogger(__name__)

commit0_ts_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Commit-0 TypeScript pipeline. Setup, build, test, and evaluate TypeScript repos.",
)


class Colors:
    RESET = "\033[0m"
    RED = "\033[91m"
    ORANGE = "\033[38;5;208m"


def highlight(text: str, color: str) -> str:
    return f"{color}{text}{Colors.RESET}"


def check_valid_ts(
    one: str, total: dict[str, list[str]], dataset: "list | None" = None
) -> None:
    """Validate a repo_split (mirror of check_valid_js).

    Accepts ``"all"``, a curated key, OR — when *dataset* is provided — any split
    resolve_split maps to a repo in the dataset, so a CUSTOM single-repo dataset
    (split = a repo name) is accepted. Currently unused in the TS eval/build path
    (which defers to resolve_split), but kept dataset-aware so wiring it later can't
    reintroduce the curated-only rejection bug that hit the JS eval.
    """
    if one == "all" or one in total:
        return
    if dataset is not None:
        from commit0.harness.split_utils import resolve_split

        if resolve_split(one, dataset, curated=total):
            return
    keys = list(total.keys())
    valid = ", ".join(highlight(key, Colors.ORANGE) for key in keys) or "(none)"
    raise typer.BadParameter(
        f"Invalid repo_split {one!r}. Must be 'all', a curated split ({valid}), "
        "or a repo/split present in the dataset.",
        param_hint="REPO_SPLIT",
    )


def write_commit0_ts_config_file(dot_file_path: str, config: dict) -> None:
    try:
        with open(dot_file_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
    except OSError as e:
        logger.error("Failed to write TS config to %s: %s", dot_file_path, e)
        raise


_TS_REQUIRED_KEYS = {
    "dataset_name": str,
    "dataset_split": str,
    "repo_split": str,
    "base_dir": str,
}


def read_commit0_ts_config_file(dot_file_path: str) -> dict:
    if not os.path.exists(dot_file_path):
        raise FileNotFoundError(f"TS config file not found: {dot_file_path}")

    with open(dot_file_path, "r") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"TS config file '{dot_file_path}' is empty or invalid. "
            f"Expected a YAML mapping, got {type(data).__name__}."
        )

    missing = [k for k in _TS_REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(
            f"TS config '{dot_file_path}' missing required keys: {missing}"
        )

    for key, expected_type in _TS_REQUIRED_KEYS.items():
        if not isinstance(data[key], expected_type):
            raise TypeError(
                f"Config key '{key}' in '{dot_file_path}' must be "
                f"{expected_type.__name__}, got {type(data[key]).__name__}"
            )

    return data


@commit0_ts_app.command()
def setup(
    repo_split: str = typer.Argument(
        ...,
        help="Split of TS repos — 'all', a curated subset, or any repo name in the dataset.",
    ),
    dataset_name: str = typer.Option(
        "ts_custom_dataset.json",
        help="Path to TS dataset JSON file",
    ),
    dataset_split: str = typer.Option("test", help="Split of the dataset"),
    base_dir: str = typer.Option(
        "repos_ts/", help="Base directory for cloned TS repos"
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", help="Path for TS commit0 config file"
    ),
) -> None:
    # Split validation deferred to runtime in setup_ts.main.

    base_dir = str(Path(base_dir).resolve())
    if dataset_name.endswith(".json"):
        dataset_name = str(Path(dataset_name).resolve())
    elif os.path.exists(dataset_name):
        dataset_name = str(Path(dataset_name).resolve())

    typer.echo(f"Cloning TS repos for split: {highlight(repo_split, Colors.ORANGE)}")
    typer.echo(f"Dataset: {highlight(dataset_name, Colors.ORANGE)}")
    typer.echo(f"Dataset split: {highlight(dataset_split, Colors.ORANGE)}")
    typer.echo(f"Base directory: {highlight(base_dir, Colors.ORANGE)}")
    typer.echo(f"Config file: {highlight(commit0_config_file, Colors.ORANGE)}")

    commit0.harness.setup_ts.main(dataset_name, dataset_split, repo_split, base_dir)

    write_commit0_ts_config_file(
        commit0_config_file,
        {
            "dataset_name": dataset_name,
            "dataset_split": dataset_split,
            "repo_split": repo_split,
            "base_dir": base_dir,
        },
    )


@commit0_ts_app.command()
def build(
    num_workers: int = typer.Option(8, help="Number of workers"),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", help="Path to TS commit0 config"
    ),
    verbose: int = typer.Option(1, help="Verbosity level (1 or 2)"),
    single_arch: bool = typer.Option(
        False, "--single-arch", help="Build only for native architecture"
    ),
) -> None:
    """Build Docker images for TS repos."""
    import platform as _platform

    if single_arch:
        machine = _platform.machine()
        arch = "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"
        os.environ["COMMIT0_BUILD_PLATFORMS"] = arch
        typer.echo(f"Single-arch build: {highlight(arch, Colors.ORANGE)}")

    config = read_commit0_ts_config_file(commit0_config_file)

    import commit0.harness.build_ts

    commit0.harness.build_ts.main(
        dataset_name=config["dataset_name"],
        dataset_split=config["dataset_split"],
        split=config["repo_split"],
        num_workers=num_workers,
        verbose=verbose,
    )


@commit0_ts_app.command()
def test(
    repo_or_repo_path: str = typer.Argument(..., help="TS repo name or path"),
    test_ids: str = typer.Argument("", help="Test IDs to run"),
    branch: str = typer.Option("", help="Branch to test"),
    backend: str = typer.Option("local", help="Backend (local or modal)"),
    timeout: int = typer.Option(1800, help="Timeout in seconds"),
    num_cpus: int = typer.Option(1, help="Number of CPUs"),
    rebuild: bool = typer.Option(False, help="Rebuild image"),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", help="Path to TS commit0 config"
    ),
    verbose: int = typer.Option(1, help="Verbosity level (1 or 2)"),
) -> None:
    """Run tests on a TypeScript repo."""
    config = read_commit0_ts_config_file(commit0_config_file)
    from commit0.harness.run_ts_tests import main as run_ts_tests_main

    run_ts_tests_main(
        dataset_name=config["dataset_name"],
        dataset_split=config["dataset_split"],
        base_dir=config["base_dir"],
        repo_or_repo_dir=repo_or_repo_path,
        branch=branch,
        test_ids=test_ids,
        backend=backend,
        timeout=timeout,
        num_cpus=num_cpus,
        rebuild_image=rebuild,
        verbose=verbose,
    )


@commit0_ts_app.command()
def evaluate(
    branch: str = typer.Option("", help="Branch to evaluate"),
    backend: str = typer.Option("local", help="Backend (local or modal)"),
    timeout: int = typer.Option(1800, help="Timeout in seconds"),
    num_workers: int = typer.Option(8, help="Number of workers"),
    num_cpus: int = typer.Option(1, help="Number of CPUs"),
    rebuild: bool = typer.Option(False, help="Rebuild images"),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", help="Path to TS commit0 config"
    ),
) -> None:
    """Evaluate TS repos."""
    config = read_commit0_ts_config_file(commit0_config_file)
    from commit0.harness.evaluate_ts import main as evaluate_ts_main

    evaluate_ts_main(
        dataset_name=config["dataset_name"],
        dataset_split=config["dataset_split"],
        repo_split=config["repo_split"],
        base_dir=config["base_dir"],
        branch=branch or None,
        backend=backend,
        timeout=timeout,
        num_cpus=num_cpus,
        num_workers=num_workers,
        rebuild_image=rebuild,
    )


@commit0_ts_app.command()
def lint(
    repo_or_repo_dir: str = typer.Argument(..., help="TS repo to lint"),
    files: Union[list[str], None] = typer.Argument(
        None,
        help=(
            "Files to lint (positional, variadic). aider appends "
            "edit-target file paths here automatically."
        ),
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", help="Path to TS commit0 config"
    ),
    verbose: int = typer.Option(1, help="Verbosity level (1 or 2)"),
) -> None:
    """Lint a TypeScript repo (eslint + tsc --noEmit)."""
    config = read_commit0_ts_config_file(commit0_config_file)
    from commit0.harness.lint_ts import main as lint_ts_main

    lint_ts_main(
        repo_or_repo_dir=repo_or_repo_dir,
        dataset_name=config["dataset_name"],
        dataset_split=config["dataset_split"],
        base_dir=config["base_dir"],
        files=files,
        verbose=verbose,
    )


@commit0_ts_app.command()
def save(
    owner: str = typer.Argument(..., help="Owner of the repository"),
    branch: str = typer.Argument(..., help="Branch to save"),
    github_token: Union[str, None] = typer.Option(None, help="GitHub token"),
    commit0_config_file: str = typer.Option(
        ".commit0.ts.yaml", help="Path to TS commit0 config"
    ),
) -> None:
    """Save TS repo changes to GitHub."""
    raise NotImplementedError("TS save command not yet implemented. See save_ts.py.")


@commit0_ts_app.command()
def get_tests(
    repo_name: str = typer.Argument(..., help="Name of the TS repo"),
    verbose: int = typer.Option(1, help="Verbosity level"),
) -> None:
    """Get test IDs for a TypeScript repo."""
    from commit0.harness.get_ts_test_ids import main as get_ts_test_ids_main

    test_id_groups = get_ts_test_ids_main(repo_name, verbose=verbose)
    for group in test_id_groups:
        for test_id in group:
            typer.echo(test_id)


if __name__ == "__main__":
    commit0_ts_app()
