import logging
import os
import subprocess

import typer
import yaml
from pathlib import Path

import commit0.harness.setup_js
from commit0.harness.constants_js import JS_SPLIT

logger = logging.getLogger(__name__)


def check_commit0_js_path() -> None:
    url = "https://commit-0.github.io/setup/"
    try:
        subprocess.run(["commit0", "--help"], capture_output=True)
        return
    except FileNotFoundError:
        logger.warning("commit0 command not found on PATH")
        typer.echo(
            typer.style(
                "The `commit0` command was not found on your path!", fg=typer.colors.RED
            )
            + "\n"
            + typer.style(
                "You may need to add it to your path or use `python -m commit0.cli_js` as a workaround.",
                fg=typer.colors.RED,
            )
        )
    except PermissionError:
        logger.warning("commit0 command is not executable")
        typer.echo(
            typer.style(
                "The `commit0` command is not executable!", fg=typer.colors.RED
            )
            + "\n"
            + typer.style(
                "You may need to give it permissions or use `python -m commit0.cli_js` as a workaround.",
                fg=typer.colors.RED,
            )
        )
    typer.echo(f"See more information here:\n\n{url}")
    typer.echo("─" * 80)

commit0_js_app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Commit-0 JavaScript pipeline. Setup, build, test, and evaluate JS repos.",
)

app = commit0_js_app


class Colors:
    RESET = "\033[0m"
    RED = "\033[91m"
    ORANGE = "\033[38;5;208m"


def highlight(text: str, color: str) -> str:
    return f"{color}{text}{Colors.RESET}"


def check_valid_js(
    one: str, total: dict[str, list[str]], dataset: "list | None" = None
) -> None:
    """Validate a repo_split.

    Accepts ``"all"``, a curated split (a key of *total*), OR — when *dataset* is
    provided — any split that :func:`resolve_split` maps to a repo IN THE DATASET
    (repo basename / instance_id / fork path / fuzzy ``-``<->``_``). This is what
    lets a CUSTOM single-repo dataset whose split is a repo name (e.g. ``slugify``,
    ``JSON-java``) pass — the previous curated-only check rejected every custom
    split even though the downstream build/eval filter (``resolve_split``) accepts
    it. Raises ``typer.BadParameter`` ONLY for a genuine typo (not ``all``, not
    curated, and not resolvable against the dataset).
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


def _dataset_for_split_check(config: dict) -> "list | None":
    """Best-effort load of the dataset so check_valid_js can validate a custom split
    against real repos. Returns None on any failure — a load problem must NOT block
    the command (the downstream eval/build resolves + reports clearly)."""
    try:
        from commit0.harness.utils import load_dataset_from_config

        return list(
            load_dataset_from_config(
                config["dataset_name"], split=config.get("dataset_split", "test")
            )
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("repo_split validation: could not load dataset (%s)", e)
        return None


def write_commit0_js_config_file(dot_file_path: str, config: dict) -> None:
    try:
        with open(dot_file_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
    except OSError as e:
        logger.error("Failed to write JS config to %s: %s", dot_file_path, e)
        raise


_JS_REQUIRED_KEYS = {
    "dataset_name": str,
    "dataset_split": str,
    "repo_split": str,
    "base_dir": str,
}

_JS_OPTIONAL_KEYS: frozenset[str] = frozenset()


def read_commit0_js_config_file(dot_file_path: str) -> dict:
    if not os.path.exists(dot_file_path):
        raise FileNotFoundError(f"JS config file not found: {dot_file_path}")

    with open(dot_file_path, "r") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"JS config file '{dot_file_path}' is empty or invalid. "
            f"Expected a YAML mapping, got {type(data).__name__}."
        )

    missing = [k for k in _JS_REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(
            f"JS config '{dot_file_path}' missing required keys: {missing}"
        )

    for key, expected_type in _JS_REQUIRED_KEYS.items():
        if not isinstance(data[key], expected_type):
            raise TypeError(
                f"Config key '{key}' in '{dot_file_path}' must be "
                f"{expected_type.__name__}, got {type(data[key]).__name__}"
            )

    unknown = set(data.keys()) - (set(_JS_REQUIRED_KEYS) | _JS_OPTIONAL_KEYS)
    if unknown:
        raise ValueError(
            f"unknown commit0.js.yaml keys: {sorted(unknown)}"
        )

    return data


@commit0_js_app.command(name="setup")
def setup(
    repo_split: str = typer.Argument(
        ...,
        help="Split of JS repos — 'all', a curated subset, or any repo name in the dataset.",
    ),
    dataset_name: str = typer.Option(
        "js_custom_dataset.json",
        help="Path to JS dataset JSON file",
    ),
    dataset_split: str = typer.Option("test", help="Split of the dataset"),
    base_dir: str = typer.Option(
        "repos_js/", help="Base directory for cloned JS repos"
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", help="Path for JS commit0 config file"
    ),
) -> None:
    check_commit0_js_path()
    base_dir = str(Path(base_dir).resolve())
    if dataset_name.endswith(".json"):
        dataset_name = str(Path(dataset_name).resolve())
    elif os.path.exists(dataset_name):
        dataset_name = str(Path(dataset_name).resolve())

    typer.echo(f"Cloning JS repos for split: {highlight(repo_split, Colors.ORANGE)}")
    typer.echo(f"Dataset: {highlight(dataset_name, Colors.ORANGE)}")
    typer.echo(f"Dataset split: {highlight(dataset_split, Colors.ORANGE)}")
    typer.echo(f"Base directory: {highlight(base_dir, Colors.ORANGE)}")
    typer.echo(f"Config file: {highlight(commit0_config_file, Colors.ORANGE)}")

    commit0.harness.setup_js.main(dataset_name, dataset_split, repo_split, base_dir)

    write_commit0_js_config_file(
        commit0_config_file,
        {
            "dataset_name": dataset_name,
            "dataset_split": dataset_split,
            "repo_split": repo_split,
            "base_dir": base_dir,
        },
    )


@commit0_js_app.command(name="build")
def build(
    num_workers: int = typer.Option(8, help="Number of workers"),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", help="Path to JS commit0 config"
    ),
    verbose: int = typer.Option(
        1,
        "--verbose",
        "-v",
        help="Set this to 2 for more logging information",
        count=True,
    ),
    single_arch: bool = typer.Option(
        False, "--single-arch", help="Build only for native architecture"
    ),
) -> None:
    """Build Docker images for JS repos."""
    check_commit0_js_path()
    import platform as _platform

    if single_arch:
        machine = _platform.machine()
        arch = "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"
        os.environ["COMMIT0_BUILD_PLATFORMS"] = arch
        typer.echo(f"Single-arch build: {highlight(arch, Colors.ORANGE)}")

    config = read_commit0_js_config_file(commit0_config_file)
    check_valid_js(config["repo_split"], JS_SPLIT, _dataset_for_split_check(config))

    import commit0.harness.build_js

    commit0.harness.build_js.main(
        dataset_name=config["dataset_name"],
        dataset_split=config["dataset_split"],
        split=config["repo_split"],
        num_workers=num_workers,
        verbose=verbose,
    )


@commit0_js_app.command(name="test")
def test(
    repo_or_repo_path: str = typer.Argument(..., help="JS repo name or path"),
    test_ids: str = typer.Argument("", help="Test IDs to run"),
    branch: str = typer.Option("", help="Branch to test (branch MUST be provided or use --reference)"),
    reference: bool = typer.Option(False, "--reference", help="Test the reference commit"),
    coverage: bool = typer.Option(False, "--coverage", help="Whether to get coverage information (not yet implemented for JS)"),
    backend: str = typer.Option("local", help="Backend (local or modal)"),
    timeout: int = typer.Option(1800, help="Timeout in seconds"),
    num_cpus: int = typer.Option(1, help="Number of CPUs"),
    rebuild: bool = typer.Option(False, help="Rebuild image"),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", help="Path to JS commit0 config"
    ),
    verbose: int = typer.Option(
        1,
        "--verbose",
        "-v",
        help="Set this to 2 for more logging information",
        count=True,
    ),
) -> None:
    """Run tests on a JavaScript repo."""
    check_commit0_js_path()
    config = read_commit0_js_config_file(commit0_config_file)
    from commit0.harness.run_js_tests import main as run_js_tests_main

    if reference:
        branch = "reference"
    if coverage:
        logger.warning(
            "--coverage is not yet implemented for the JS harness; flag accepted but ignored"
        )
    if not branch:
        raise typer.BadParameter(
            "branch is required (pass --branch <name> or --reference)",
            param_hint="--branch/--reference",
        )

    if any(sep in repo_or_repo_path for sep in ("/", os.sep)) or os.path.isabs(
        repo_or_repo_path
    ):
        resolved = str(Path(repo_or_repo_path).resolve(strict=False))
        if resolved != repo_or_repo_path:
            logger.info(
                "Normalised repo path: %r -> %r", repo_or_repo_path, resolved
            )
        repo_or_repo_path = resolved

    run_js_tests_main(
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


@commit0_js_app.command(name="evaluate")
def evaluate(
    branch: str = typer.Option("", help="Branch to evaluate (branch MUST be provided or use --reference)"),
    reference: bool = typer.Option(False, "--reference", help="Evaluate the reference commit"),
    coverage: bool = typer.Option(False, "--coverage", help="Whether to get coverage information (not yet implemented for JS)"),
    backend: str = typer.Option("local", help="Backend (local or modal)"),
    timeout: int = typer.Option(1800, help="Timeout in seconds"),
    num_workers: int = typer.Option(8, help="Number of workers"),
    num_cpus: int = typer.Option(1, help="Number of CPUs"),
    rebuild: bool = typer.Option(False, help="Rebuild images"),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", help="Path to JS commit0 config"
    ),
) -> None:
    """Evaluate JS repos."""
    check_commit0_js_path()
    config = read_commit0_js_config_file(commit0_config_file)
    check_valid_js(config["repo_split"], JS_SPLIT, _dataset_for_split_check(config))
    from commit0.harness.evaluate_js import main as evaluate_js_main

    if reference:
        branch = "reference"
    if coverage:
        logger.warning(
            "--coverage is not yet implemented for the JS harness; flag accepted but ignored"
        )

    evaluate_js_main(
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


@commit0_js_app.command(name="lint")
def lint(
    repo_or_repo_dir: str = typer.Argument(..., help="JS repo to lint"),
    files: list[str] | None = typer.Argument(
        None,
        help=(
            "Files to lint (positional, variadic). aider appends edit-target file "
            "paths here automatically — must be an Argument, not an Option, or the "
            "lint_cmd fails with 'Got unexpected extra argument'."
        ),
    ),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", help="Path to JS commit0 config"
    ),
    verbose: int = typer.Option(
        1,
        "--verbose",
        "-v",
        help="Set this to 2 for more logging information",
        count=True,
    ),
) -> None:
    """Lint a JavaScript repo (eslint + node --check)."""
    check_commit0_js_path()
    config = read_commit0_js_config_file(commit0_config_file)
    from commit0.harness.lint_js import main as lint_js_main

    lint_js_main(
        repo_or_repo_dir=repo_or_repo_dir,
        dataset_name=config["dataset_name"],
        dataset_split=config["dataset_split"],
        base_dir=config["base_dir"],
        files=files,
        verbose=verbose,
    )


# `save` (dataset publishing flow) is JS-PLAN §13 deferred: no HuggingFace
# dataset name has been decided for JS yet. Subagents must call
# `prepare_repo_js.py` directly meanwhile to materialise per-repo dataset rows.
@commit0_js_app.command(name="save")
def save(
    owner: str = typer.Argument(..., help="Owner of the repository"),
    branch: str = typer.Argument(..., help="Branch to save"),
    github_token: str | None = typer.Option(None, help="GitHub token"),
    commit0_config_file: str = typer.Option(
        ".commit0.js.yaml", help="Path to JS commit0 config"
    ),
) -> None:
    """Save JS repo changes to GitHub."""
    raise NotImplementedError(
        "save not implemented; see JS-PLAN.md §13 — no HF dataset name decided for JS"
    )


@commit0_js_app.command(name="get_tests")
def get_tests(
    repo_name: str = typer.Argument(..., help="Name of the JS repo"),
    verbose: int = typer.Option(
        1,
        "--verbose",
        "-v",
        help="Set this to 2 for more logging information",
        count=True,
    ),
) -> None:
    """Get test IDs for a JavaScript repo."""
    check_commit0_js_path()
    # NOTE: JS and TS both dispatch through generate_test_ids_js via the shared
    # get_ts_test_ids entry point (there is no separate get_js_test_ids module;
    # the discovery logic is framework-based, not language-based). Rename to
    # `get_node_test_ids` is a future refactor.
    from commit0.harness.get_ts_test_ids import main as get_test_ids_main

    test_id_groups = get_test_ids_main(repo_name, verbose=verbose)
    for group in test_id_groups:
        for test_id in group:
            typer.echo(test_id)


if __name__ == "__main__":
    commit0_js_app()
