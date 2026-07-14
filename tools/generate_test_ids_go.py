"""Generate Go test ID files (.bz2) for custom commit0 Go repos.

Runs `go test -list .` against each Go repo to discover all test function names,
then saves them as bz2-compressed files compatible with commit0's Go evaluation harness.

Test IDs use the format: package/TestName (e.g., "github.com/foo/bar/pkg/TestSomething").

Usage:
    python -m tools.generate_test_ids_go dataset_entries.json --output-dir ./test_ids
    python -m tools.generate_test_ids_go --repo-dir /path/to/repo --name mylib --output-dir ./test_ids
    python -m tools.generate_test_ids_go dataset_entries.json --docker --output-dir ./test_ids
    python -m tools.generate_test_ids_go dataset_entries.json --install

Requires:
    - Go toolchain installed (for local collection)
    - Docker (for --docker mode)
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import docker
import docker.errors
import requests.exceptions

from commit0.harness.docker_utils import get_docker_platform

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _find_docker_image(repo_name: str) -> str | None:
    """Find a built Docker image for this repo by searching commit0.repo.<name>.* tags."""
    try:
        client = docker.from_env()
        short_name = repo_name.split("__")[-1].split("-")[0].lower()
        needle = f"commit0.repo.{short_name}."
        for image in client.images.list():
            for tag in image.tags:
                if tag.startswith(needle):
                    return tag
        return None
    except (docker.errors.DockerException, OSError):
        logger.debug("Failed to find Docker image for %s", repo_name, exc_info=True)
        return None


def _parse_go_test_list_json(stdout: str) -> list[str]:
    """Parse `go test -list . -json` output into test IDs.

    Each JSON line has Package and Output fields. Output events contain test
    function names (one per line). The Package field is always correct,
    avoiding the sequencing bug in plain-text parsing where test names
    appear before their package summary line.

    Returns test IDs in format: package/TestName
    """
    test_ids: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("Action") != "output":
            continue

        package = event.get("Package", "")
        output = event.get("Output", "").strip()
        if not output or not package:
            continue

        # Match test/example/fuzz names; skip Benchmark (eval doesn't run -bench)
        if re.match(r"^(Test|Example|Fuzz)\w+$", output):
            test_ids.append(f"{package}/{output}")

    return test_ids

def _parse_go_test_list_plain(stdout: str, module_path: str = "") -> list[str]:
    """Fallback parser for plain ``go test -list . ./...`` output (non-JSON).

    Go writes test names FIRST, then a summary line naming the package
    (``ok example.com/pkg 0.123s``, ``FAIL ...``, or ``? example.com/pkg
    [no test files]``). The naive line-by-line reader assigned pending
    names to the *previous* package summary; this implementation buffers
    pending names and flushes them to the correct package once its summary
    line appears. Names that never see a summary (rare — implies truncated
    output or a bare ``go test -list .`` on a single package with no
    trailing status) fall back to ``module_path`` if provided.

    Returns test IDs in format: package/TestName.
    """
    test_ids: list[str] = []
    pending: list[str] = []

    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        pkg_match = re.match(r"^(?:ok|FAIL|\?)\s+(\S+)", line)
        if pkg_match:
            package = pkg_match.group(1)
            for name in pending:
                test_ids.append(f"{package}/{name}")
            pending = []
            continue

        if line.startswith("---") or line.startswith("# ") or line.startswith("FAIL\t"):
            continue

        if re.match(r"^(Test|Example|Fuzz)\w+", line):
            pending.append(line.split()[0])

    if pending and module_path:
        for name in pending:
            test_ids.append(f"{module_path}/{name}")

    return test_ids


def _get_module_path(repo_dir: Path) -> str:
    """Read the module path declared in ``go.mod`` (e.g. ``github.com/foo/bar``)."""
    go_mod = repo_dir / "go.mod"
    if not go_mod.exists():
        return ""
    for line in go_mod.read_text().splitlines():
        line = line.strip()
        if line.startswith("module "):
            return line.split(None, 1)[1]
    return ""


def _ensure_go_modules(repo_dir: Path, timeout: int = 300) -> None:
    """Best-effort `go mod download` so `go test -list ./...` can compile the test
    targets (which it must, to enumerate them).

    Unlike Rust's `cargo test --list` — which resolves+fetches the dependency
    graph from the registry on demand — `go test -list ./...` needs the module
    dependencies already present in the local module cache. At prepare time the
    cache is typically COLD (prep only stubs + commits; the setup dict's
    `go mod download` runs later, in the Docker image build). Without this,
    listing fails to compile every package that imports a not-yet-downloaded
    dependency and silently returns an EMPTY inventory — collapsing the scoring
    denominator to the observed test count.

    A vendored repo (``vendor/`` present) needs no download — `go test` reads
    the checked-in vendor tree — so we skip it there. A non-zero exit is
    raised as a ``RuntimeError`` so callers see the real cause instead of a
    downstream empty-inventory silent failure.
    """
    if (repo_dir / "vendor" / "modules.txt").exists():
        logger.debug("  Vendored module detected; skipping go mod download")
        return
    if not (repo_dir / "go.mod").exists():
        return
    try:
        result = subprocess.run(
            ["go", "mod", "download"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"go mod download timed out after {timeout}s in {repo_dir}: {e}"
        ) from e
    except OSError as e:
        raise RuntimeError(
            f"go mod download could not be executed in {repo_dir} "
            f"(is the 'go' binary installed?): {e}"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"go mod download failed in {repo_dir} (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def collect_test_ids_local(
    repo_dir: Path,
    timeout: int = 300,
) -> list[str]:
    """Run `go test -list .` locally to discover Go test names."""
    # Warm the module cache first: `go test -list ./...` compiles the test
    # targets to enumerate them and cannot do so with missing deps (Go, unlike
    # cargo, does not fetch on demand during listing).
    _ensure_go_modules(repo_dir, timeout=timeout)
    cmd = ["go", "test", "-list", ".", "-json", "-count=1", "./..."]
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("  go test -list timed out after %ds", timeout)
        return []

    combined = result.stdout + "\n" + result.stderr
    test_ids = _parse_go_test_list_json(combined)

    if not test_ids:
        logger.info("  JSON parsing found 0 IDs, trying plain-text fallback")
        module_path = _get_module_path(repo_dir)
        cmd_plain = ["go", "test", "-list", ".", "-count=1", "./..."]
        try:
            result_plain = subprocess.run(
                cmd_plain,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("  go test -list (plain fallback) timed out after %ds", timeout)
            return []
        combined_plain = result_plain.stdout + "\n" + result_plain.stderr
        test_ids = _parse_go_test_list_plain(combined_plain, module_path)


    # A package that fails to COMPILE during listing is dropped from the
    # inventory silently — `go test -list ./...` still lists the packages that
    # DO compile, so a partial failure yields a partial (under-counted)
    # denominator. Surface it: the eval runs the same `./...` sweep, so a
    # capture-time build error is a real signal the stubbed base is broken for
    # that package, not a benign warning.
    build_err_markers = ("[build failed]", "build constraints exclude", "# ")
    combined = result.stdout + "\n" + result.stderr
    if any(m in combined for m in build_err_markers):
        logger.warning(
            "  go test -list reported build errors for some package(s) in %s — "
            "those tests are NOT in the captured inventory (denominator may be "
            "under-counted). Ensure the stubbed base compiles.",
            repo_dir,
        )

    return test_ids


def collect_test_ids_docker(
    repo_name: str,
    image_name: str | None = None,
    reference_commit: str | None = None,
    timeout: int = 300,
) -> list[str]:
    """Run `go test -list .` inside a Docker container."""
    if image_name is None:
        image_name = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    checkout = f"git checkout {reference_commit} -- . && " if reference_commit else ""

    client = docker.from_env()
    bash_cmd = (
        f"cd /testbed && {checkout}go test -list . -json -count=1 ./... 2>&1; true"
    )

    try:
        raw = client.containers.run(
            image_name,
            command=f"bash -c '{bash_cmd}'",
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
    except requests.exceptions.ReadTimeout:
        logger.warning("  Docker go test -list timed out after %ds", timeout)
        return []

    test_ids = _parse_go_test_list_json(stdout)

    if not test_ids:
        logger.info("  JSON parsing found 0 IDs in Docker, trying plain-text fallback")
        bash_cmd_plain = (
            f"cd /testbed && {checkout}go test -list . -count=1 ./... 2>&1; true"
        )
        try:
            raw_plain = client.containers.run(
                image_name,
                command=f"bash -c '{bash_cmd_plain}'",
                remove=True,
                platform=get_docker_platform(),
            )
            stdout_plain = (
                raw_plain.decode("utf-8", errors="replace")
                if isinstance(raw_plain, bytes)
                else raw_plain
            )
        except (docker.errors.ContainerError, requests.exceptions.ReadTimeout):
            logger.warning("  Docker plain-text fallback failed")
            return []
        test_ids = _parse_go_test_list_plain(stdout_plain)


    return test_ids


def save_test_ids(
    test_ids: list[str],
    name: str,
    output_dir: Path,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    name = name.lower().replace(".", "-")
    output_file = output_dir / f"{name}.bz2"

    content = "\n".join(test_ids)
    with bz2.open(output_file, "wt") as f:
        f.write(content)

    return output_file


def install_test_ids(
    source_dir: Path,
    repo_names: list[str] | None = None,
) -> int:
    try:
        import commit0

        data_dir = Path(os.path.dirname(commit0.__file__)) / "data" / "test_ids"
    except ImportError:
        logger.error("commit0 package not found — cannot install test IDs")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    installed = 0

    for bz2_file in sorted(source_dir.glob("*.bz2")):
        name = bz2_file.stem
        if repo_names and name not in [r.lower().replace(".", "-") for r in repo_names]:
            continue

        import shutil

        dest = data_dir / bz2_file.name
        shutil.copy2(bz2_file, dest)
        logger.info("  Installed: %s -> %s", bz2_file.name, dest)
        installed += 1

    return installed


def generate_for_dataset(
    dataset_path: Path,
    output_dir: Path,
    use_docker: bool = False,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
) -> dict[str, int]:
    data = json.loads(dataset_path.read_text())

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

        logger.info(
            "\n[%d/%d] Collecting Go test IDs for %s...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            instance_id,
        )

        if use_docker:
            test_ids = collect_test_ids_docker(
                repo_name=repo_name,
                image_name=_find_docker_image(repo_name),
                reference_commit=entry.get("reference_commit"),
                timeout=timeout,
            )
        else:
            base = clone_dir or Path("./repos_staging")
            repo_dir = base / repo.replace("/", "__")

            if not repo_dir.is_dir():
                original = entry.get("original_repo", "")
                if original:
                    repo_dir = base / original.replace("/", "__")

            if not repo_dir.is_dir():
                logger.warning("  Repo dir not found — skipping")
                results[repo_name] = 0
                continue

            reference_commit = entry.get("reference_commit")
            if reference_commit:
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

            test_ids = collect_test_ids_local(
                repo_dir=repo_dir,
                timeout=timeout,
            )

        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(test_ids), out_file)
            results[repo_name] = len(test_ids)
        else:
            logger.warning("  No test IDs collected for %s", instance_id)
            results[repo_name] = 0

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Go test ID files for commit0 repos"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Input dataset_entries.json",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        help="Single local repo directory to collect from",
    )
    parser.add_argument("--name", type=str, help="Repo name (used with --repo-dir)")
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
        test_ids = collect_test_ids_local(args.repo_dir, timeout=args.timeout)
        if test_ids:
            out_file = save_test_ids(test_ids, args.name, args.output_dir)
            logger.info("Saved %d test IDs to %s", len(test_ids), out_file)
        else:
            logger.warning("No test IDs collected")
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
            installed = install_test_ids(
                args.output_dir, repo_names=list(results.keys()) or None
            )
            logger.info("Installed %d files into commit0 data directory", installed)
    else:
        parser.error("Provide either input_file or --repo-dir")


if __name__ == "__main__":
    main()
