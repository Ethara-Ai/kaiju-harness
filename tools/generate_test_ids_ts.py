"""Generate TypeScript test ID files (.bz2) for commit0 repos.

Runs `vitest list --json` or `jest --listTests` against each repo to discover
all test node IDs, then saves them as bz2-compressed files compatible with
commit0's evaluation harness.

Usage:
    # From dataset entries JSON:
    python -m tools.generate_test_ids_ts dataset_entries.json --output-dir ./test_ids_ts

    # From a local repo directory:
    python -m tools.generate_test_ids_ts --repo-dir /path/to/repo --name mylib --output-dir ./test_ids_ts

    # Using Docker (builds image first if needed):
    python -m tools.generate_test_ids_ts dataset_entries.json --docker --output-dir ./test_ids_ts

    # Install into commit0 data directory:
    python -m tools.generate_test_ids_ts dataset_entries.json --install
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess
import sys
import os
from pathlib import Path

from tools.generate_test_ids import (
    _find_docker_image,
    _find_repo_dir,
    install_test_ids,
    save_test_ids,
)

from commit0.harness.constants_ts import CONTAINER_WORKDIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# File extensions recognized as test files (used by the Jest parser)
_TS_TEST_EXTENSIONS = (
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
)

# Vitest emits ANSI color codes even with --json/--reporter=json; strip them
# before json.loads so 'Extra data' errors don't swallow the output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_vitest_list_output(
    stdout: str,
    repo_root: str = CONTAINER_WORKDIR,
) -> list[str]:
    """Parse ``vitest list --json`` output into a list of test IDs.

    Vitest may print startup messages (config loading, deprecation warnings)
    before the JSON array.  The parser finds the first ``[`` and last ``]``
    in *stdout*, extracts the JSON substring, and builds IDs in the format
    ``"{relative_file} > {test_name}"``.

    Parameters
    ----------
        stdout: Raw stdout from ``vitest list --json``.
        repo_root: Absolute path prefix to strip from file paths.

    Returns
    -------
        List of test IDs.  Empty list on any parse failure.

    """
    # 1. Guard: empty input
    if not stdout or not stdout.strip():
        return []

    # 1b. Strip ANSI escape sequences (vitest emits colors even with --json)
    stdout = _ANSI_RE.sub("", stdout)

    # 2. Find the JSON array boundaries
    first_bracket = stdout.find("[")
    if first_bracket == -1:
        logger.warning("No JSON array found in vitest list output")
        return []

    last_bracket = stdout.rfind("]")
    if last_bracket == -1 or last_bracket <= first_bracket:
        logger.warning("Malformed JSON array in vitest list output")
        return []

    # 3. Extract the JSON substring
    json_str = stdout[first_bracket : last_bracket + 1]

    # 4. Parse JSON
    try:
        entries = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse vitest JSON output: %s", e)
        return []

    # 5. Validate: must be a list
    if not isinstance(entries, list):
        logger.warning("vitest list output is not a JSON array")
        return []

    # 6. Build test IDs from each entry
    test_ids: list[str] = []
    root_prefix = repo_root.rstrip("/") + "/"

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        name = entry.get("name", "")
        file_path = entry.get("file", "")

        if not name or not file_path:
            continue

        # 7. Strip repo_root prefix from file path to get relative path
        if file_path.startswith(root_prefix):
            relative_file = file_path[len(root_prefix) :]
        elif file_path.startswith("/"):
            # Absolute path but different root; use as-is without leading slash
            relative_file = file_path.lstrip("/")
        else:
            relative_file = file_path

        # 8. Build hierarchical ID with " > " separator
        test_id = f"{relative_file} > {name}"
        test_ids.append(test_id)

    return test_ids


def _parse_jest_list_output(
    stdout: str,
    repo_root: str = CONTAINER_WORKDIR,
) -> list[str]:
    """Parse ``jest --listTests`` output into a list of test file paths.

    Handles two output modes:

    1. **Plain text** (standard): one absolute file path per line.
    2. **JSON array**: some Jest configurations/wrappers output a JSON array
       of strings.

    Parameters
    ----------
        stdout: Raw stdout from ``jest --listTests``.
        repo_root: Absolute path prefix to strip from file paths.

    Returns
    -------
        List of relative file paths.  Empty list on parse failure.

    """
    # 1. Guard: empty input
    if not stdout or not stdout.strip():
        return []

    stripped = stdout.strip()
    root_prefix = repo_root.rstrip("/") + "/"

    # 2. Try JSON mode first: some Jest configs/wrappers output JSON array
    if stripped.startswith("["):
        try:
            paths = json.loads(stripped)
            if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                test_ids: list[str] = []
                for p in paths:
                    if p.startswith(root_prefix):
                        relative = p[len(root_prefix) :]
                    elif p.startswith("/"):
                        relative = p.lstrip("/")
                    else:
                        relative = p
                    test_ids.append(relative)
                return test_ids
        except json.JSONDecodeError:
            pass  # Fall through to line-by-line parsing

    # 3. Line-by-line mode: standard --listTests output
    test_ids = []
    for line in stripped.split("\n"):
        line = line.strip()
        if not line:
            continue

        # 4. Skip known noise lines
        if line.startswith("Determining "):
            continue
        if line.startswith("PASS ") or line.startswith("FAIL "):
            continue

        # 5. Check if line looks like a file path
        is_test_file = any(line.endswith(ext) for ext in _TS_TEST_EXTENSIONS)
        if not is_test_file:
            # Accept lines that are just paths (no extension check)
            # if they contain a "/" and don't contain spaces (heuristic)
            if "/" not in line or " " in line:
                continue

        # 6. Strip repo_root prefix
        if line.startswith(root_prefix):
            relative = line[len(root_prefix) :]
        elif line.startswith("/"):
            relative = line.lstrip("/")
        else:
            relative = line

        test_ids.append(relative)

    return test_ids


# ---------------------------------------------------------------------------
# Framework detection & command building
# ---------------------------------------------------------------------------


def _detect_framework_from_entry(entry: dict) -> str:  # type: ignore[type-arg]
    """Detect the test framework from a dataset entry.

    Resolution order:
    1. Explicit ``test_framework`` field (``"vitest"`` or ``"jest"``).
    2. Infer from ``test.test_cmd`` (looks for ``"vitest"`` or ``"jest"``).
    3. Default to ``"jest"`` (most common, safe ``--listTests``).

    Parameters
    ----------
        entry: A single dataset entry dict.

    Returns
    -------
        ``"vitest"`` or ``"jest"``.

    """
    # 1. Primary: explicit test_framework field
    framework = entry.get("test_framework", "").lower().strip()
    if framework in ("vitest", "jest"):
        return framework

    # 2. Secondary: infer from test_cmd
    test_info = entry.get("test", {})
    test_cmd = test_info.get("test_cmd", "") if isinstance(test_info, dict) else ""
    test_cmd_lower = test_cmd.lower()

    if "vitest" in test_cmd_lower:
        return "vitest"
    if "jest" in test_cmd_lower:
        return "jest"

    # 3. Fallback: default to jest
    logger.debug(
        "Could not detect framework for %s, defaulting to jest",
        entry.get("repo", "unknown"),
    )
    return "jest"


def _parse_jest_json_results(
    stdout: str,
    repo_root: str = CONTAINER_WORKDIR,
) -> list[str]:
    """Parse ``jest --json`` full-run output into individual test IDs.

    Unlike ``_parse_jest_list_output`` which only gets file paths from
    ``--listTests``, this parses the ``assertionResults[].fullName`` from a
    full Jest ``--json`` run to get individual test names.

    Format: ``{relative_file} > {fullName}``

    Falls back to ``_parse_jest_list_output`` if the JSON doesn't contain
    ``testResults`` (e.g. when Jest crashes before running).
    """
    if not stdout or not stdout.strip():
        return []

    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return _parse_jest_list_output(stdout, repo_root)

    try:
        report = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return _parse_jest_list_output(stdout, repo_root)

    if "testResults" not in report:
        return _parse_jest_list_output(stdout, repo_root)

    root_prefix = repo_root.rstrip("/") + "/"
    test_ids: list[str] = []

    for suite in report["testResults"]:
        file_path = suite.get("testFilePath") or suite.get("name", "")
        if file_path.startswith(root_prefix):
            relative_file = file_path[len(root_prefix) :]
        elif file_path.startswith("/"):
            relative_file = file_path.lstrip("/")
        else:
            relative_file = file_path

        for assertion in suite.get("assertionResults", []):
            full_name = assertion.get("fullName", "")
            if full_name:
                test_ids.append(f"{relative_file} > {full_name}")
            else:
                title = assertion.get("title", "")
                ancestors = assertion.get("ancestorTitles", [])
                if title:
                    name = " > ".join(ancestors + [title])
                    test_ids.append(f"{relative_file} > {name}")

    if not test_ids:
        return _parse_jest_list_output(stdout, repo_root)

    return test_ids


def _build_collect_command(
    framework: str,
    test_dir: str,
) -> list[str]:
    """Build the argument list for test discovery.

    Parameters
    ----------
        framework: ``"vitest"`` or ``"jest"``.
        test_dir: Test directory relative to repo root.

    Returns
    -------
        Argument list for ``subprocess.run`` (no ``shell=True`` needed).

    Raises
    ------
        ValueError: If *framework* is not ``"vitest"`` or ``"jest"``.

    """
    if framework == "vitest":
        return ["npx", "vitest", "list", "--json", test_dir]

    if framework == "jest":
        return ["npx", "jest", "--json", "--forceExit", test_dir]

    raise ValueError(f"Unknown framework: {framework!r}. Expected 'vitest' or 'jest'.")


def _dispatch_parse(
    stdout: str,
    framework: str,
    repo_root: str = CONTAINER_WORKDIR,
) -> list[str]:
    """Route raw stdout to the correct framework parser.

    Parameters
    ----------
        stdout: Raw stdout from the collection command.
        framework: ``"vitest"`` or ``"jest"``.
        repo_root: Absolute path prefix to strip.

    Returns
    -------
        Parsed test IDs.

    """
    if framework == "vitest":
        return _parse_vitest_list_output(stdout, repo_root)
    elif framework == "jest":
        return _parse_jest_json_results(stdout, repo_root)
    else:
        logger.warning(
            "Unknown framework %r, attempting jest parser as fallback", framework
        )
        return _parse_jest_json_results(stdout, repo_root)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_ts_test_ids(
    test_ids: list[str],
    test_dir: str,
) -> list[str]:
    """Ensure every test ID's file portion starts with *test_dir/*.

    For Vitest IDs (containing ``" > "``), only the file portion before the
    first ``" > "`` is checked/prefixed.  For Jest IDs (plain file paths),
    the entire ID is checked.

    Parameters
    ----------
        test_ids: Raw test IDs from the parser.
        test_dir: Expected test directory prefix (e.g. ``"__tests__"``).

    Returns
    -------
        Normalized test IDs with consistent prefixes.

    """
    # 1. No normalization needed if test_dir is root
    if not test_dir or test_dir == ".":
        return test_ids

    prefix = test_dir.rstrip("/") + "/"
    normalized: list[str] = []

    for tid in test_ids:
        if not tid.strip():
            continue

        # 2. For Vitest IDs with " > " separator, only check the file portion
        if " > " in tid:
            file_part, _, rest = tid.partition(" > ")
            if not file_part.startswith(prefix) and not file_part.startswith("/"):
                file_part = prefix + file_part
            tid = f"{file_part} > {rest}"
        else:
            # 3. Jest IDs are just file paths
            if not tid.startswith(prefix) and not tid.startswith("/"):
                tid = prefix + tid

        normalized.append(tid)

    return normalized


# ---------------------------------------------------------------------------
# Local collection
# ---------------------------------------------------------------------------


def collect_ts_test_ids_local(
    repo_dir: Path,
    test_dir: str = "__tests__",
    framework: str = "jest",
    timeout: int = 300,
) -> list[str]:
    """Discover test IDs by running the framework CLI in a local repo.

    Uses ``subprocess.run(shell=True)`` to execute the collection command.
    For Vitest, falls back to ``vitest run --reporter=json`` if the ``list``
    subcommand (v2+) returns nothing.

    Parameters
    ----------
        repo_dir: Absolute path to the cloned repo.
        test_dir: Test directory within the repo.
        framework: ``"vitest"`` or ``"jest"``.
        timeout: Subprocess timeout in seconds.

    Returns
    -------
        Discovered test IDs.  Empty list on failure.

    """
    # 1. Build the collection command
    cmd = _build_collect_command(framework, test_dir)

    # 2. Run via subprocess
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "  %s test collection timed out after %ds in %s",
            framework,
            timeout,
            repo_dir,
        )
        return []
    except FileNotFoundError:
        logger.warning("  npx not found. Is Node.js installed?")
        return []

    # 3. Parse the output
    test_ids = _dispatch_parse(result.stdout, framework, str(repo_dir.resolve()))

    # 4. Fallback for Vitest: try --reporter=json if list subcommand failed
    if not test_ids and framework == "vitest":
        logger.info(
            "  vitest list returned 0 IDs, trying vitest run --reporter=json fallback"
        )
        fallback_cmd = ["npx", "vitest", "run", "--reporter=json", test_dir]
        try:
            result_fb = subprocess.run(
                fallback_cmd,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            test_ids = _parse_vitest_list_output(result_fb.stdout, str(repo_dir))
        except subprocess.TimeoutExpired:
            logger.debug("vitest run --reporter=json fallback timed out")
        except FileNotFoundError:
            pass

    # 5. Log stderr if collection returned nothing
    if not test_ids and result.stderr:
        logger.debug("  stderr from %s: %s", framework, result.stderr[:500])

    return test_ids


# ---------------------------------------------------------------------------
# Docker collection
# ---------------------------------------------------------------------------


def collect_ts_test_ids_docker(
    repo_name: str,
    test_dir: str = "__tests__",
    framework: str = "jest",
    image_name: str | None = None,
    reference_commit: str | None = None,
    timeout: int = 300,
) -> list[str]:
    """Discover test IDs by running the framework CLI inside a Docker container.

    If *reference_commit* is provided, checks out the original (un-stubbed)
    source first so that imports resolve correctly during test discovery.

    Parameters
    ----------
        repo_name: Repository name for Docker image lookup.
        test_dir: Test directory within the repo.
        framework: ``"vitest"`` or ``"jest"``.
        image_name: Docker image tag.  Auto-detected if ``None``.
        reference_commit: Git commit to checkout before collection.
        timeout: Container timeout in seconds.

    Returns
    -------
        Discovered test IDs.  Empty list on failure.

    """
    import docker
    import docker.errors
    import requests.exceptions
    from commit0.harness.docker_utils import get_docker_platform

    _COMMIT_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

    # 1. Resolve Docker image
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    # 2. Validate and build the checkout prefix
    if reference_commit:
        if not _COMMIT_SHA_RE.fullmatch(reference_commit):
            raise ValueError(
                f"Invalid reference_commit {reference_commit!r}: "
                "expected a hex SHA (7-40 chars)."
            )
    checkout = (
        f"git checkout {shlex.quote(reference_commit)} -- . && "
        if reference_commit
        else ""
    )

    # 3. Build the collection command (as shell string for Docker bash -c)
    cmd_parts = _build_collect_command(framework, test_dir)
    collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

    # 4. Compose the full bash command for Docker
    # Opt-in: build workspace siblings first for monorepo repos (e.g. prisma).
    # Set KAIJU_TS_BUILD_WORKSPACES=1 to enable. Default off (build is slow).
    workspace_build = ""
    if os.environ.get("KAIJU_TS_BUILD_WORKSPACES") == "1":
        workspace_build = (
            "if [ -f pnpm-workspace.yaml ] || grep -q '\"workspaces\"' "
            "package.json 2>/dev/null; then "
            "pnpm -r build > /dev/null 2>&1 || true; fi; "
        )
    bash_cmd = (
        f"cd {CONTAINER_WORKDIR} && {checkout}{workspace_build}"
        f"{collect_cmd} 2>/dev/null; true"
    )

    # 5. Run in Docker container
    client = docker.from_env()

    try:
        try:
            raw = client.containers.run(
                image_name,
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
            logger.warning("  Docker collection timed out after %ds", timeout)
            return []

        # 6. Parse output
        test_ids = _dispatch_parse(stdout, framework)

        # 7. Fallback for Vitest: try --reporter=json inside Docker
        if not test_ids and framework == "vitest":
            logger.info("  vitest list returned 0 IDs in Docker, trying fallback")
            fallback_cmd = (
                f"cd {CONTAINER_WORKDIR} && {checkout}"
                f"npx vitest run --reporter=json {shlex.quote(test_dir)} 2>/dev/null; true"
            )
            try:
                raw_fb = client.containers.run(
                    image_name,
                    command=["bash", "-c", fallback_cmd],
                    remove=True,
                    platform=get_docker_platform(),
                )
                stdout_fb = (
                    raw_fb.decode("utf-8", errors="replace")
                    if isinstance(raw_fb, bytes)
                    else raw_fb
                )
                test_ids = _parse_vitest_list_output(stdout_fb)
            except (docker.errors.ContainerError, requests.exceptions.ReadTimeout):
                logger.debug("  vitest fallback also failed in Docker")

        # 8. Ultimate fallback: glob test files inside the container.
        # Returns relative file paths as test IDs when vitest can't enumerate
        # (e.g. monorepo sibling packages aren't built, configs missing, etc.).
        if not test_ids:
            logger.info("  Falling back to glob-based test file enumeration")
            glob_cmd = (
                f"cd {CONTAINER_WORKDIR} && {checkout}"
                r"find . -type f \( "
                r"-name '*.test.ts' -o -name '*.test.tsx' "
                r"-o -name '*.test.js' -o -name '*.test.jsx' "
                r"-o -name '*.test.mts' -o -name '*.test.cts' "
                r"-o -name '*.spec.ts' -o -name '*.spec.tsx' "
                r"-o -name '*.spec.js' -o -name '*.spec.jsx' "
                r"-o -name '*.spec.mts' -o -name '*.spec.cts' "
                r"-o -name '*.vitest.ts' -o -name '*.vitest.tsx' "
                r"-o -name '*.vitest.js' -o -name '*.vitest.jsx' "
                r"\) "
                r"-not -path '*/node_modules/*' "
                r"-not -path '*/dist/*' "
                r"-not -path '*/build/*' "
                r"-not -path '*/.git/*' "
                r"-not -path '*/coverage/*' "
                r"-not -path '*/.next/*' "
                r"2>/dev/null | sed 's|^\./||'; true"
            )
            try:
                raw_glob = client.containers.run(
                    image_name,
                    command=["bash", "-c", glob_cmd],
                    remove=True,
                    platform=get_docker_platform(),
                )
                glob_out = (
                    raw_glob.decode("utf-8", errors="replace")
                    if isinstance(raw_glob, bytes)
                    else raw_glob
                )
                files = [ln.strip() for ln in glob_out.splitlines() if ln.strip()]
                if files:
                    logger.info(
                        "  Glob fallback found %d test files", len(files)
                    )
                    test_ids = files
            except (docker.errors.ContainerError, requests.exceptions.ReadTimeout):
                logger.debug("  Glob fallback also failed")

        return test_ids
    finally:
        client.close()


def validate_ts_base_commit_docker(
    repo_name: str,
    test_dir: str = "__tests__",
    framework: str = "jest",
    image_name: str | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Validate that the base commit (stubbed code) can discover tests.

    Runs the collection command inside Docker **without** checking out the
    reference commit.  This tests whether the Docker image at its base
    state can still discover tests.

    Parameters
    ----------
        repo_name: Repository name for Docker image lookup.
        test_dir: Test directory within the repo.
        framework: ``"vitest"`` or ``"jest"``.
        image_name: Docker image tag.  Auto-detected if ``None``.
        timeout: Container timeout in seconds.

    Returns
    -------
        Tuple of (test_count, stderr_snippet) for diagnostics.

    """
    import docker
    import docker.errors
    import requests.exceptions
    from commit0.harness.docker_utils import get_docker_platform

    # 1. Resolve Docker image
    if image_name is None:
        image_name = _find_docker_image(repo_name)
        if image_name is None:
            image_name = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    # 2. Build collection command WITHOUT checkout
    cmd_parts = _build_collect_command(framework, test_dir)
    collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    bash_cmd = f"cd {CONTAINER_WORKDIR} && {collect_cmd} 2>&1; true"

    # 3. Run in Docker
    client = docker.from_env()

    try:
        try:
            raw = client.containers.run(
                image_name,
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
        except requests.exceptions.ReadTimeout:
            return 0, "timeout"

        # 4. Parse and count
        test_ids = _dispatch_parse(stdout, framework)
        stderr_snippet = stdout[-500:] if stdout else ""

        return len(test_ids), stderr_snippet
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Dataset orchestrator
# ---------------------------------------------------------------------------


def generate_for_ts_dataset(
    dataset_path: Path,
    output_dir: Path,
    use_docker: bool = False,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
    validate_base: bool = False,
    framework_override: str | None = None,
) -> dict[str, int]:
    """Generate test IDs for all repos in a dataset entries JSON file.

    Parameters
    ----------
        dataset_path: Path to ``dataset_entries.json``.
        output_dir: Output directory for ``.bz2`` files.
        use_docker: Run inside Docker containers.
        clone_dir: Directory containing cloned repos.
        timeout: Per-repo timeout in seconds.
        max_repos: Limit number of repos processed.
        validate_base: Run base commit validation after collection.
        framework_override: Force a specific framework.  ``None`` auto-detects.

    Returns
    -------
        Mapping of repo names to test ID counts.  Negative count means
        base validation failed.

    """
    # 1. Load dataset JSON
    data = json.loads(dataset_path.read_text(encoding="utf-8"))

    if isinstance(data, dict) and "data" in data:
        entries = data["data"]
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"Unknown dataset format in {dataset_path}")

    results: dict[str, int] = {}

    # 2. Iterate over entries
    for i, entry in enumerate(entries):
        if max_repos and i >= max_repos:
            break

        repo = entry.get("repo", "")
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        test_dir = entry.get("test", {}).get("test_dir", "__tests__")
        instance_id = entry.get("instance_id", repo_name)

        # 3. Detect framework
        if framework_override and framework_override != "auto":
            framework = framework_override
        else:
            framework = _detect_framework_from_entry(entry)

        logger.info(
            "\n[%d/%d] Collecting %s test IDs for %s (framework=%s)...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            framework,
            instance_id,
            framework,
        )

        # 4. Collect test IDs (Docker or local)
        if use_docker:
            test_ids = collect_ts_test_ids_docker(
                repo_name=repo_name,
                test_dir=test_dir,
                framework=framework,
                reference_commit=entry.get("reference_commit"),
                timeout=timeout,
            )
            test_ids = _normalize_ts_test_ids(test_ids, test_dir)
        else:
            repo_dir = _find_repo_dir(clone_dir, repo, entry.get("original_repo", ""))

            if not repo_dir or not repo_dir.is_dir():
                logger.warning(
                    "  Repo dir not found for %s -- skipping",
                    instance_id,
                )
                results[repo_name] = 0
                continue

            # 5. Checkout reference commit if available
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
                    logger.warning(
                        "  Could not checkout reference_commit %s: %s — skipping repo",
                        reference_commit,
                        e,
                    )
                    continue

            test_ids = collect_ts_test_ids_local(
                repo_dir=repo_dir,
                test_dir=test_dir,
                framework=framework,
                timeout=timeout,
            )
            test_ids = _normalize_ts_test_ids(test_ids, test_dir)

        # 7. Save results
        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(test_ids), out_file)
            results[repo_name] = len(test_ids)

            # 8. Validate base commit if requested
            if validate_base and use_docker:
                base_collected, stderr = validate_ts_base_commit_docker(
                    repo_name=repo_name,
                    test_dir=test_dir,
                    framework=framework,
                    timeout=timeout,
                )
                if base_collected == 0:
                    logger.warning(
                        "  BASE COMMIT VALIDATION FAILED: 0 tests collected at base_commit "
                        "(stubbed code). The import chain is broken -- pipeline will produce "
                        "0%% pass rate."
                    )
                    logger.warning("  Last output: %s", stderr[:200])
                    results[repo_name] = -len(test_ids)
                else:
                    logger.info(
                        "  Base commit validation OK: %d tests collected at base_commit",
                        base_collected,
                    )
        else:
            logger.warning("  No test IDs collected for %s", repo_name)
            results[repo_name] = 0

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate TypeScript test ID files (.bz2) for commit0 repos"
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
        "--test-dir",
        type=str,
        default="__tests__",
        help="Test directory within repo (default: __tests__)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./test_ids_ts",
        help="Output directory for .bz2 files (default: ./test_ids_ts)",
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
        help="Run test collection inside Docker containers (requires built images)",
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
        help=(
            "After collecting IDs at reference_commit, validate that base_commit "
            "(stubbed code) can also collect tests. Requires --docker."
        ),
    )
    parser.add_argument(
        "--framework",
        type=str,
        choices=["jest", "vitest", "auto"],
        default="auto",
        help="Test framework to use (default: auto-detect from dataset entry)",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    # Single-repo mode
    if args.repo_dir:
        if not args.name:
            parser.error("--name is required with --repo-dir")

        framework = args.framework if args.framework != "auto" else "jest"
        repo_dir = Path(args.repo_dir)
        logger.info("Collecting %s test IDs from %s...", framework, repo_dir)

        test_ids = collect_ts_test_ids_local(
            repo_dir=repo_dir,
            test_dir=args.test_dir,
            framework=framework,
            timeout=args.timeout,
        )
        test_ids = _normalize_ts_test_ids(test_ids, args.test_dir)

        if test_ids:
            out_file = save_test_ids(test_ids, args.name, output_dir)
            logger.info("Saved %d test IDs to %s", len(test_ids), out_file)
        else:
            logger.error("No test IDs collected")
            sys.exit(1)

    # Dataset mode
    elif args.dataset_file:
        dataset_path = Path(args.dataset_file)
        if not dataset_path.exists():
            parser.error(f"File not found: {dataset_path}")

        clone_dir = Path(args.clone_dir) if args.clone_dir else None
        fw = args.framework if args.framework != "auto" else None

        results = generate_for_ts_dataset(
            dataset_path=dataset_path,
            output_dir=output_dir,
            use_docker=args.docker,
            clone_dir=clone_dir,
            timeout=args.timeout,
            max_repos=args.max_repos,
            validate_base=args.validate_base,
            framework_override=fw,
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

    # Install step
    if args.install:
        installed = install_test_ids(output_dir)
        logger.info("Installed %d test ID files into commit0 data directory", installed)


if __name__ == "__main__":
    main()
