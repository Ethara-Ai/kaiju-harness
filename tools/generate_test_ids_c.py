"""Generate C test ID files (.bz2) for custom commit0 C repos.

Runs ``ctest --show-only=json-v1`` against each C repo to enumerate test
names, then saves them as bz2-compressed files compatible with commit0's C
evaluation harness.

CTest test names are unique per CMake project, so we use the bare name as the
test ID (no package prefix, unlike Go's ``package/TestName`` or Java's
``classname#method``).

Usage:
    python -m tools.generate_test_ids_c c_dataset.json --output-dir ./test_ids_c
    python -m tools.generate_test_ids_c --repo-dir /path/to/repo --name mylib \\
        --output-dir ./test_ids_c
    python -m tools.generate_test_ids_c c_dataset.json --docker --output-dir ./test_ids_c
    python -m tools.generate_test_ids_c c_dataset.json --docker --install
    python -m tools.generate_test_ids_c c_dataset.json --docker --validate-base
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import docker
import docker.errors

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
    except Exception:
        logger.debug("Failed to find Docker image for %s", repo_name, exc_info=True)
        return None


def _parse_ctest_show_only(stdout: str) -> list[str]:
    """Parse ``ctest --show-only=json-v1`` output into test IDs."""
    test_ids: list[str] = []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("ctest --show-only output was not JSON: %s", exc)
        return _parse_ctest_show_only_plain(stdout)

    for test in data.get("tests", []):
        name = test.get("name", "")
        if name:
            test_ids.append(name)
    return test_ids


def _parse_ctest_show_only_plain(stdout: str) -> list[str]:
    """Fallback parser for ``ctest -N`` (plain text) output.

    Format: ``  Test #1: test_parse_object``
    """
    test_ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Test #") or line.startswith("Test  #"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                name = parts[1].strip()
                if name:
                    test_ids.append(name)
    return test_ids


def _enumerate_local(repo_dir: Path) -> list[str]:
    build_dir = repo_dir / "build"
    if not build_dir.exists():
        logger.info("No build/ in %s — running cmake configure+build first", repo_dir)
        try:
            subprocess.run(
                ["cmake", "-S", str(repo_dir), "-B", str(build_dir),
                 "-DBUILD_TESTING=ON", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["cmake", "--build", str(build_dir), "-j"],
                check=False,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("cmake configure failed for %s: %s", repo_dir, exc.stderr)
            return []

    try:
        result = subprocess.run(
            ["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        logger.error("ctest not found on PATH")
        return []

    if result.returncode != 0 or not result.stdout.strip():
        # Fall back to ctest -N (plain text)
        result = subprocess.run(
            ["ctest", "--test-dir", str(build_dir), "-N"],
            capture_output=True,
            text=True,
            check=False,
        )
        return _parse_ctest_show_only_plain(result.stdout)

    return _parse_ctest_show_only(result.stdout)


def _enumerate_docker(repo_name: str, image_tag: str) -> list[str]:
    client = docker.from_env()
    cmd = (
        "cd /testbed && "
        "(cmake -S . -B build -G Ninja -DBUILD_TESTING=ON 2>/dev/null || true) && "
        "(cmake --build build -j 2>/dev/null || true) && "
        "(ctest --test-dir build --show-only=json-v1 2>&1 || "
        "ctest --test-dir build -N 2>&1; true)"
    )
    try:
        output = client.containers.run(
            image_tag,
            command=["bash", "-c", cmd],
            remove=True,
            stderr=False,
            stdout=True,
        )
    except docker.errors.ContainerError as exc:
        logger.error("Docker enumeration failed for %s: %s", repo_name, exc)
        return []

    text = output.decode("utf-8", errors="replace")
    # JSON should be the last well-formed object in stdout. Try both parsers.
    ids = _parse_ctest_show_only(text)
    if not ids:
        ids = _parse_ctest_show_only_plain(text)
    return ids


def write_bz2(test_ids: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(test_ids).encode("utf-8")
    with bz2.open(path, "wb") as f:
        f.write(payload)


def install_c_test_ids(
    source_dir: Path,
    repo_names: list[str] | None = None,
) -> int:
    """Copy generated test ID .bz2 files into commit0's C data directory.

    The C evaluation harness (commit0/harness/get_c_test_ids.py) reads test
    IDs from ``<commit0>/data/c_test_ids/`` -- note the ``c_test_ids`` dir,
    distinct from Python's ``data/test_ids/``. Mirrors the Python pipeline's
    ``install_test_ids`` (runbook Step 6 ``--install``).
    """
    try:
        import commit0

        data_dir = Path(os.path.dirname(commit0.__file__)) / "data" / "c_test_ids"
    except ImportError:
        logger.error("commit0 package not found -- cannot install C test IDs")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    installed = 0

    wanted = {r.lower() for r in repo_names} if repo_names else None
    for bz2_file in sorted(source_dir.glob("*.bz2")):
        # name may carry a ``#fail_to_pass``/``#pass_to_pass`` suffix; match on
        # the base repo stem only when a filter is supplied.
        base_stem = bz2_file.stem.split("#")[0]
        if wanted is not None and base_stem not in wanted:
            continue

        dest = data_dir / bz2_file.name
        shutil.copy2(bz2_file, dest)
        logger.info("  Installed: %s -> %s", bz2_file.name, dest)
        installed += 1

    return installed


def validate_base_commit_docker(
    repo_name: str,
    image_tag: str | None = None,
) -> tuple[int, str]:
    """Validate that the stubbed base_commit still enumerates CTest tests.

    The commit0 C repo image (commit0.repo.<name>.<hash>:v0) is built at
    reference_commit then ``git reset --hard base_commit`` (stubbed code), so
    enumerating tests in that image already reflects the stubbed state. Stub
    bodies (STUB_PANIC) keep function signatures intact, so the project should
    still compile and CMake ``add_test()`` registrations should remain -- i.e.
    ``ctest --show-only`` must still report > 0 tests.

    Returns (tests_collected, note). tests_collected == 0 means the stubs broke
    the build / test registration and the repo will not work with the pipeline.
    Mirrors the Python pipeline's ``validate_base_commit_docker`` (runbook
    Step 7 ``--validate-base``).
    """
    if image_tag is None:
        image_tag = _find_docker_image(repo_name)
        if image_tag is None:
            return 0, "no docker image found"
    ids = _enumerate_docker(repo_name, image_tag)
    if not ids:
        return 0, "stubbed base_commit collected 0 tests"
    return len(ids), "ok"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate C test IDs (.bz2)")
    parser.add_argument(
        "entries_file",
        nargs="?",
        help="dataset JSON from create_dataset_c.py (or use --repo-dir/--name)",
    )
    parser.add_argument("--repo-dir", type=Path, default=None)
    parser.add_argument("--name", type=str, default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./test_ids_c"),
        help="Where to write <repo>.bz2 files",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Enumerate tests by running ctest inside the built Docker image",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install generated .bz2 files into commit0's data/c_test_ids/ directory",
    )
    parser.add_argument(
        "--validate-base",
        action="store_true",
        help="Validate the stubbed base_commit image still enumerates tests "
        "(requires --docker).",
    )
    args = parser.parse_args()

    if args.validate_base and not args.docker:
        parser.error("--validate-base requires --docker")

    if args.repo_dir is not None:
        if args.name is None:
            args.name = args.repo_dir.name
        ids = _enumerate_local(args.repo_dir)
        if not ids:
            logger.warning("No tests discovered for %s", args.name)
        target = args.output_dir / f"{args.name.lower()}.bz2"
        write_bz2(ids, target)
        logger.info("Wrote %d test IDs to %s", len(ids), target)
        if args.install:
            installed = install_c_test_ids(args.output_dir)
            logger.info(
                "Installed %d test ID file(s) into commit0/data/c_test_ids",
                installed,
            )
        return

    if not args.entries_file:
        parser.error("Either entries_file or --repo-dir is required")

    entries = json.loads(Path(args.entries_file).read_text())
    if isinstance(entries, dict) and "data" in entries:
        entries = entries["data"]

    total_ids = 0
    for entry in entries:
        repo = entry.get("repo", "")
        repo_name = repo.split("/")[-1]
        if not repo_name:
            continue

        if args.docker:
            image_tag = _find_docker_image(repo_name)
            if not image_tag:
                logger.warning(
                    "No Docker image found for %s; run commit0-c build first",
                    repo_name,
                )
                continue
            ids = _enumerate_docker(repo_name, image_tag)
            if args.validate_base:
                count, note = validate_base_commit_docker(repo_name, image_tag)
                if count == 0:
                    logger.error(
                        "validate-base FAILED for %s: %s -- stubs likely "
                        "broke the build / test registration",
                        repo_name,
                        note,
                    )
                else:
                    logger.info(
                        "validate-base OK for %s: stubbed base_commit "
                        "enumerates %d tests",
                        repo_name,
                        count,
                    )
        else:
            local_dir = Path("repos") / repo_name
            if not local_dir.exists():
                logger.warning(
                    "Local repo dir not found: %s — pass --docker or clone first",
                    local_dir,
                )
                continue
            ids = _enumerate_local(local_dir)

        if not ids:
            logger.warning("No tests discovered for %s", repo_name)
            continue

        target = args.output_dir / f"{repo_name.lower()}.bz2"
        write_bz2(ids, target)
        logger.info("Wrote %d test IDs to %s", len(ids), target)
        total_ids += len(ids)

    logger.info("Total test IDs written: %d", total_ids)

    if args.install:
        installed = install_c_test_ids(args.output_dir)
        logger.info(
            "Installed %d test ID file(s) into commit0/data/c_test_ids",
            installed,
        )

if __name__ == "__main__":
    main()
