"""Generate Java test ID files (.bz2) for commit0 Java repos.

Discovers Java test IDs via source scanning (using get_java_test_ids from
commit0.harness.get_java_test_ids) and optionally from build tool output
inside Docker containers. Saves them as bz2-compressed files compatible
with commit0's evaluation harness.

Usage:
    # From dataset entries JSON:
    python -m tools.generate_test_ids_java dataset_entries.json --output-dir ./test_ids

    # From a local repo directory:
    python -m tools.generate_test_ids_java --repo-dir /path/to/repo --name mylib --output-dir ./test_ids

    # Using Docker (builds image first if needed):
    python -m tools.generate_test_ids_java dataset_entries.json --docker --output-dir ./test_ids

    # Install into commit0 data directory:
    python -m tools.generate_test_ids_java dataset_entries.json --install
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import docker
import docker.errors
import requests.exceptions

from commit0.harness.constants_java import (
    JAVA_BASE_IMAGE_PREFIX,
    detect_build_system,
)
from commit0.harness.spec_java import Commit0JavaSpec
from commit0.harness.docker_utils import get_docker_platform
from commit0.harness.get_java_test_ids import (
    get_java_test_ids,
)

_MVN_SKIP_FLAGS = Commit0JavaSpec._MVN_SKIP_FLAGS

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def collect_test_ids_local(
    repo_dir: Path,
    strategy: str = "auto",
) -> list[str]:
    """Delegate to get_java_test_ids() from commit0.harness.get_java_test_ids.

    Args:
    ----
        repo_dir: Path to the Java repository root.
        strategy: "auto" (try build tool then fallback to source), "source",
                  "maven", or "gradle".

    Returns:
    -------
        List of test IDs in ``fully.qualified.ClassName#methodName`` format.

    """
    repo_path = str(repo_dir)
    try:
        build_system = detect_build_system(repo_path)
    except ValueError:
        build_system = "maven"

    instance = {"repo_path": repo_path, "build_system": build_system}

    test_ids = get_java_test_ids(instance, strategy=strategy)
    logger.info("  Local collection found %d test IDs (strategy=%s)", len(test_ids), strategy)
    return test_ids


def _find_docker_image(repo_name: str) -> str | None:
    """Find the Docker image for a Java repo.

    Image naming convention (from build_java.py):
        commit0-java-{repo.split('/')[-1]}:latest
    e.g. "apache/commons-lang" -> "commit0-java-commons-lang:latest"
    """
    short_name = repo_name.split("/")[-1].lower()
    expected_tag = f"{JAVA_BASE_IMAGE_PREFIX}-{short_name}:latest"
    try:
        client = docker.from_env()
        for image in client.images.list():
            for tag in image.tags:
                if tag.lower() == expected_tag:
                    return tag
        return None
    except Exception:
        logger.debug("Failed to find Docker image for %s", repo_name, exc_info=True)
        return None


def _parse_compiled_test_classes_output(stdout: str) -> list[str]:
    """Parse `find` output of compiled .class test files into class-level test IDs.

    Matches Surefire's default include patterns: Test*, *Test, *Tests, *TestCase.
    Skips inner/anonymous classes (containing '$').
    """
    test_ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith(".class"):
            continue
        name = Path(line).stem
        if "$" in name:
            continue
        if not (
            name.startswith("Test")
            or name.endswith("Test")
            or name.endswith("Tests")
            or name.endswith("TestCase")
        ):
            continue
        match = re.search(r"test-classes/(.+)\.class$", line)
        if not match:
            match = re.search(r"classes/java/test/(.+)\.class$", line)
        if match:
            fqcn = match.group(1).replace("/", ".")
            test_ids.append(fqcn)
    return test_ids


def _parse_find_test_files_output(stdout: str) -> list[str]:
    """Parse `find` output of .java test files into class-level test IDs."""
    test_ids: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith(".java"):
            continue
        name = Path(line).stem
        if "$" in name:
            continue
        if not (
            name.startswith("Test")
            or name.endswith("Test")
            or name.endswith("Tests")
            or name.endswith("TestCase")
        ):
            continue
        # Standard: src/test/java/org/foo/BarTest.java
        match = re.search(r"test/java/(.+)\.java$", line)
        if not match:
            # Monorepo shorthand: module/test/org/foo/BarTest.java
            match = re.search(r"/test/(.+)\.java$", line)
        if match:
            fqcn = match.group(1).replace("/", ".")
            test_ids.append(fqcn)
    return test_ids


def collect_test_ids_docker(
    repo_name: str,
    build_system: str = "maven",
    image_name: str | None = None,
    reference_commit: str | None = None,
    timeout: int = 300,
    repo_full_name: str | None = None,
) -> list[str]:
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"{JAVA_BASE_IMAGE_PREFIX}-{repo_name.lower().split('/')[-1]}:latest"

    # Docker images have origin removed and sit on stubbed base branch.
    # Re-add origin, fetch reference commit, and checkout reference code
    # so compile-scan runs against compilable code.
    github_repo = repo_full_name or repo_name
    preamble_parts: list[str] = ["cd /testbed"]
    if reference_commit:
        preamble_parts += [
            f"git remote add origin https://github.com/{github_repo} 2>/dev/null || true",
            f"git fetch --depth 1 origin {reference_commit} && git tag -f {reference_commit} FETCH_HEAD 2>/dev/null || true",
            f"git reset --hard {reference_commit}",
        ]

    client = docker.from_env()

    if build_system == "maven":
        compile_cmd = f"mvn test-compile -B -q {_MVN_SKIP_FLAGS} 2>&1"
        find_classes_cmd = "find . -path '*/target/test-classes/*.class' -type f 2>/dev/null"
    else:
        compile_cmd = "gradle testClasses --no-daemon -q 2>&1"
        find_classes_cmd = "find . -path '*/build/classes/java/test/*.class' -type f 2>/dev/null"

    preamble = " && ".join(preamble_parts)
    bash_cmd = f"{preamble} && {compile_cmd} && {find_classes_cmd}; true"

    try:
        raw = client.containers.run(
            image_name,
            command=["bash", "-c", bash_cmd],
            remove=True,
            platform=get_docker_platform(),
        )
        stdout = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    except docker.errors.ContainerError as e:
        raw_err = e.stderr
        stdout = raw_err.decode("utf-8", errors="replace") if isinstance(raw_err, bytes) else (raw_err or "")
    except (docker.errors.ImageNotFound, requests.exceptions.ReadTimeout) as e:
        logger.warning("  Docker collection failed for %s: %s", repo_name, e)
        return []

    test_ids = _parse_compiled_test_classes_output(stdout)

    if test_ids:
        logger.info("  Docker compile-scan discovery found %d test IDs", len(test_ids))
        return test_ids

    logger.info("  Compile-scan yielded 0 IDs, falling back to source scan in container")
    find_cmd = (
        f"{preamble} && "
        r"find . -type f -name '*.java' \( -path '*/test/*' -o -path '*/tests/*' \) "
        r"\( -name 'Test*.java' -o -name '*Test.java' -o -name '*Tests.java' -o -name '*TestCase.java' \) "
        "2>/dev/null; true"
    )
    try:
        raw = client.containers.run(
            image_name,
            command=["bash", "-c", find_cmd],
            remove=True,
            platform=get_docker_platform(),
        )
        stdout = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    except (docker.errors.ContainerError, docker.errors.ImageNotFound, requests.exceptions.ReadTimeout) as e:
        logger.warning("  Docker source-scan fallback failed: %s", e)
        return []

    test_ids = _parse_find_test_files_output(stdout)
    logger.info("  Docker source-scan fallback found %d test IDs", len(test_ids))
    return test_ids


def validate_base_commit_docker(
    repo_name: str,
    build_system: str = "maven",
    image_name: str | None = None,
    timeout: int = 300,
) -> tuple[bool, str]:
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"{JAVA_BASE_IMAGE_PREFIX}-{repo_name.lower().split('/')[-1]}:latest"

    client = docker.from_env()

    if build_system == "maven":
        compile_cmd = f"mvn compile test-compile -B -q {_MVN_SKIP_FLAGS} 2>&1"
    else:
        compile_cmd = "gradle compileJava compileTestJava --no-daemon -q 2>&1"

    bash_cmd = f"cd /testbed && {compile_cmd}; echo EXIT_CODE=$?"

    try:
        raw = client.containers.run(
            image_name,
            command=f"bash -c '{bash_cmd}'",
            remove=True,
            platform=get_docker_platform(),
        )
        stdout = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    except docker.errors.ContainerError as e:
        raw_err = e.stderr
        stdout = raw_err.decode("utf-8", errors="replace") if isinstance(raw_err, bytes) else (raw_err or "")
    except (docker.errors.ImageNotFound, requests.exceptions.ReadTimeout):
        return False, "timeout or image not found"

    exit_match = re.search(r"EXIT_CODE=(\d+)", stdout)
    exit_code = int(exit_match.group(1)) if exit_match else 1

    if exit_code == 0:
        logger.info("  Base commit compilation succeeded")
        return True, ""
    else:
        stderr_snippet = stdout[-500:] if stdout else ""
        return False, stderr_snippet


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
        logger.error("commit0 package not found - cannot install test IDs")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    installed = 0

    for bz2_file in sorted(source_dir.glob("*.bz2")):
        name = bz2_file.stem
        if repo_names and name not in [r.lower().replace(".", "-") for r in repo_names]:
            continue

        dest = data_dir / bz2_file.name
        shutil.copy2(bz2_file, dest)
        logger.info("  Installed: %s -> %s", bz2_file.name, dest)
        installed += 1

    return installed


def _find_repo_dir(
    clone_dir: Path | None,
    fork_repo: str,
    original_repo: str,
) -> Path | None:
    base = clone_dir or Path("./repos_staging")
    candidates = [fork_repo]
    if original_repo and original_repo != fork_repo:
        candidates.append(original_repo)

    for name in candidates:
        for dir_name in [name.replace("/", "__"), name.split("/")[-1]]:
            candidate = base / dir_name
            if candidate.is_dir():
                return candidate

    return None


def generate_for_dataset(
    dataset_path: Path,
    output_dir: Path,
    use_docker: bool = False,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
    validate_base: bool = False,
    build_system_override: str | None = None,
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
            "\n[%d/%d] Collecting test IDs for %s...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            instance_id,
        )

        entry_build_system = (
            build_system_override
            or entry.get("build_system")
            or entry.get("setup", {}).get("build_system", "maven")
        )

        if use_docker:
            test_ids = collect_test_ids_docker(
                repo_name=repo_name,
                build_system=entry_build_system,
                reference_commit=entry.get("reference_commit"),
                timeout=timeout,
                repo_full_name=repo,
            )
        else:
            repo_dir = _find_repo_dir(clone_dir, repo, entry.get("original_repo", ""))

            if not repo_dir or not repo_dir.is_dir():
                logger.warning(
                    "  Repo dir not found - skipping (tried fork + original name)"
                )
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

            test_ids = collect_test_ids_local(repo_dir=repo_dir)

        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(test_ids), out_file)
            results[repo_name] = len(test_ids)

            if validate_base and use_docker:
                base_compiled, stderr = validate_base_commit_docker(
                    repo_name=repo_name,
                    build_system=entry_build_system,
                    timeout=timeout,
                )
                if not base_compiled:
                    logger.warning(
                        "  BASE COMMIT VALIDATION FAILED: stubbed code does not compile."
                        " Pipeline will produce 0%% pass rate."
                    )
                    logger.warning("  Last output: %s", stderr[:200])
                    results[repo_name] = -len(test_ids)
                else:
                    logger.info(
                        "  Base commit validation: compilation succeeded at base_commit"
                    )
        else:
            logger.warning("  No test IDs collected for %s", repo_name)
            results[repo_name] = 0

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Java test ID files for commit0 Java repos"
    )
    parser.add_argument(
        "dataset_file",
        nargs="?",
        help="Input dataset_entries.json or custom_dataset.json",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        help="Generate for a single local repo directory",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="Repo name (required with --repo-dir)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./test_ids",
        help="Output directory for .bz2 files (default: ./test_ids)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default=None,
        help="Directory where repos are cloned (default: ./repos_staging)",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run test discovery inside Docker containers (requires built images)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install generated .bz2 files into commit0's data directory",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per repo for test collection (default: 300s)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to process",
    )
    parser.add_argument(
        "--validate-base",
        action="store_true",
        help="Validate that base_commit (stubbed code) compiles. Requires --docker.",
    )
    parser.add_argument(
        "--build-system",
        type=str,
        default=None,
        choices=["maven", "gradle"],
        help="Override build system detection (default: auto-detect or from dataset)",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    if args.repo_dir:
        if not args.name:
            parser.error("--name is required with --repo-dir")
        repo_dir = Path(args.repo_dir)
        logger.info("Collecting Java test IDs from %s...", repo_dir)

        strategy = "auto"
        if args.build_system:
            strategy = args.build_system

        test_ids = collect_test_ids_local(repo_dir=repo_dir, strategy=strategy)
        if test_ids:
            out_file = save_test_ids(test_ids, args.name, output_dir)
            logger.info("Saved %d test IDs to %s", len(test_ids), out_file)
        else:
            logger.error("No test IDs collected")
            sys.exit(1)

    elif args.dataset_file:
        dataset_path = Path(args.dataset_file)
        if not dataset_path.exists():
            parser.error(f"File not found: {dataset_path}")

        clone_dir = Path(args.clone_dir) if args.clone_dir else None

        results = generate_for_dataset(
            dataset_path=dataset_path,
            output_dir=output_dir,
            use_docker=args.docker,
            clone_dir=clone_dir,
            timeout=args.timeout,
            max_repos=args.max_repos,
            validate_base=args.validate_base,
            build_system_override=args.build_system,
        )

        total = sum(abs(v) for v in results.values())
        repos_with_tests = sum(1 for v in results.values() if v > 0)
        failed_validation = sum(1 for v in results.values() if v < 0)
        logger.info(
            "\nDone: %d test IDs across %d repos (%d repos had no tests, %d failed base validation)",
            total,
            len(results),
            len(results) - repos_with_tests - failed_validation,
            failed_validation,
        )
    else:
        parser.error("Provide either dataset_file or --repo-dir")
        return

    if args.install:
        installed = install_test_ids(output_dir)
        logger.info("Installed %d test ID files into commit0 data directory", installed)


if __name__ == "__main__":
    main()
