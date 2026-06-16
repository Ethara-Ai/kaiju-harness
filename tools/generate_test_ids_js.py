"""Generate JavaScript test ID files (.bz2) for commit0 repos.

Discovers tests by running the detected framework (jest/mocha/vitest/node_test)
in list-only / dry-run mode, parses the framework-specific output, and saves
bz2-compressed test ID lists compatible with the commit0 evaluation harness.

Usage:
    python -m tools.generate_test_ids_js dataset_entries.json --output-dir ./test_ids_js
    python -m tools.generate_test_ids_js --repo-dir /path/to/repo --name mylib --output-dir ./test_ids_js
    python -m tools.generate_test_ids_js dataset_entries.json --docker --output-dir ./test_ids_js
    python -m tools.generate_test_ids_js dataset_entries.json --install
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess
import sys
from pathlib import Path

from commit0.harness.constants_js import (
    CONTAINER_WORKDIR,
    SUPPORTED_TEST_FRAMEWORKS,
)
from tools.generate_test_ids import (
    _find_docker_image,
    _find_repo_dir,
    install_test_ids,
    save_test_ids,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


_JS_TEST_EXTENSIONS: tuple[str, ...] = (
    ".test.js",
    ".test.mjs",
    ".test.cjs",
    ".test.jsx",
    ".spec.js",
    ".spec.mjs",
    ".spec.cjs",
    ".spec.jsx",
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

_TAP_TEST_RE = re.compile(r"^(?:ok|not ok)\s+\d+\s+-\s+(.*?)(?:\s+#.*)?$")
_TAP_SUBTEST_RE = re.compile(r"^\s+#\s+Subtest:\s+(.+?)\s*$")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _strip_repo_prefix(file_path: str, repo_root: str) -> str:
    root_prefix = repo_root.rstrip("/") + "/"
    if file_path.startswith(root_prefix):
        return file_path[len(root_prefix) :]
    if file_path.startswith("/"):
        return file_path.lstrip("/")
    return file_path


def _parse_jest_json_results(stdout: str, repo_root: str = CONTAINER_WORKDIR) -> list[str]:
    """Parse `jest --json` full-run output into `{file} > {fullName}` IDs."""
    if not stdout or not stdout.strip():
        return []
    text = _strip_ansi(stdout)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return _parse_jest_list_output(stdout, repo_root)
    try:
        report = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return _parse_jest_list_output(stdout, repo_root)
    if "testResults" not in report:
        return _parse_jest_list_output(stdout, repo_root)

    test_ids: list[str] = []
    for suite in report["testResults"]:
        file_path = suite.get("testFilePath") or suite.get("name", "")
        relative_file = _strip_repo_prefix(file_path, repo_root)
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


def _parse_jest_list_output(stdout: str, repo_root: str = CONTAINER_WORKDIR) -> list[str]:
    """Parse `jest --listTests` plain-text or JSON-array output into file IDs."""
    if not stdout or not stdout.strip():
        return []
    stripped = _strip_ansi(stdout).strip()

    if stripped.startswith("["):
        try:
            paths = json.loads(stripped)
            if isinstance(paths, list) and all(isinstance(p, str) for p in paths):
                return [_strip_repo_prefix(p, repo_root) for p in paths]
        except json.JSONDecodeError:
            pass

    test_ids: list[str] = []
    for raw in stripped.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("Determining ", "PASS ", "FAIL ")):
            continue
        is_test_file = any(line.endswith(ext) for ext in _JS_TEST_EXTENSIONS)
        if not is_test_file:
            if "/" not in line or " " in line:
                continue
        test_ids.append(_strip_repo_prefix(line, repo_root))
    return test_ids


def _parse_vitest_list_output(stdout: str, repo_root: str = CONTAINER_WORKDIR) -> list[str]:
    """Parse `vitest list --json` output into `{file} > {name}` IDs."""
    if not stdout or not stdout.strip():
        return []
    text = _strip_ansi(stdout)
    first = text.find("[")
    last = text.rfind("]")
    if first < 0 or last < 0 or last <= first:
        return []
    try:
        entries = json.loads(text[first : last + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []

    test_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        file_path = entry.get("file", "")
        if not name or not file_path:
            continue
        relative_file = _strip_repo_prefix(file_path, repo_root)
        test_ids.append(f"{relative_file} > {name}")
    return test_ids


def _parse_mocha_output(stdout: str, repo_root: str = CONTAINER_WORKDIR) -> list[str]:
    """Parse `mocha --reporter json` output into `{file} > {fullTitle}` IDs."""
    if not stdout or not stdout.strip():
        return []
    text = _strip_ansi(stdout)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return []
    try:
        report = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    tests = report.get("tests") if isinstance(report, dict) else None
    if not isinstance(tests, list):
        passes = report.get("passes", []) if isinstance(report, dict) else []
        failures = report.get("failures", []) if isinstance(report, dict) else []
        pending = report.get("pending", []) if isinstance(report, dict) else []
        tests = list(passes) + list(failures) + list(pending)
    test_ids: list[str] = []
    for test in tests:
        if not isinstance(test, dict):
            continue
        full_title = test.get("fullTitle") or test.get("title", "")
        file_path = test.get("file", "")
        if not full_title:
            continue
        if file_path:
            relative_file = _strip_repo_prefix(file_path, repo_root)
            test_ids.append(f"{relative_file} > {full_title}")
        else:
            test_ids.append(full_title)
    return test_ids


def _parse_node_test_output(stdout: str, repo_root: str = CONTAINER_WORKDIR) -> list[str]:
    """Parse `node --test --test-reporter=tap` TAP14 output into hierarchical IDs."""
    if not stdout or not stdout.strip():
        return []
    text = _strip_ansi(stdout)
    test_ids: list[str] = []
    seen: set[str] = set()
    subtest_stack: list[str] = []

    for raw in text.splitlines():
        sub = _TAP_SUBTEST_RE.match(raw)
        if sub:
            subtest_stack.append(sub.group(1).strip())
            continue
        m = _TAP_TEST_RE.match(raw.strip())
        if m:
            name = m.group(1).strip()
            if not name:
                continue
            if subtest_stack and subtest_stack[-1] == name:
                full = " > ".join(subtest_stack)
                subtest_stack.pop()
            else:
                full = " > ".join(subtest_stack + [name])
            if full not in seen:
                seen.add(full)
                test_ids.append(full)
    return test_ids


def _detect_framework_from_entry(entry: dict) -> str:
    """Resolve framework from entry.test_framework, test.test_cmd, then default."""
    framework = str(entry.get("test_framework", "") or "").lower().strip()
    if framework in SUPPORTED_TEST_FRAMEWORKS:
        return framework
    test_info = entry.get("test", {})
    test_cmd = test_info.get("test_cmd", "") if isinstance(test_info, dict) else ""
    cmd_lower = str(test_cmd).lower()
    if "vitest" in cmd_lower:
        return "vitest"
    if "jest" in cmd_lower:
        return "jest"
    if "mocha" in cmd_lower:
        return "mocha"
    if "node --test" in cmd_lower or "node:test" in cmd_lower:
        return "node_test"
    logger.debug(
        "Could not detect framework for %s, defaulting to jest",
        entry.get("repo", "unknown"),
    )
    return "jest"


def _build_collect_command(framework: str, test_dir: str) -> list[str]:
    """Build the argument list (no shell) for test discovery per framework."""
    if framework == "jest":
        return ["npx", "jest", "--json", "--forceExit", test_dir]
    if framework == "vitest":
        return ["npx", "vitest", "list", "--json", test_dir]
    if framework == "mocha":
        return ["npx", "mocha", "--reporter", "json", "--dry-run", test_dir]
    if framework == "node_test":
        return ["node", "--test", "--test-reporter=tap", test_dir]
    raise ValueError(
        f"Unknown framework: {framework!r}. "
        f"Expected one of {sorted(SUPPORTED_TEST_FRAMEWORKS)}."
    )


def _dispatch_parse(
    stdout: str,
    framework: str,
    repo_root: str = CONTAINER_WORKDIR,
) -> list[str]:
    """Route raw stdout to the framework-specific parser."""
    if framework == "jest":
        return _parse_jest_json_results(stdout, repo_root)
    if framework == "vitest":
        return _parse_vitest_list_output(stdout, repo_root)
    if framework == "mocha":
        return _parse_mocha_output(stdout, repo_root)
    if framework == "node_test":
        return _parse_node_test_output(stdout, repo_root)
    logger.warning("Unknown framework %r, falling back to jest parser", framework)
    return _parse_jest_json_results(stdout, repo_root)


def _normalize_js_test_ids(test_ids: list[str], test_dir: str) -> list[str]:
    """Ensure each test ID's file portion starts with `<test_dir>/`."""
    if not test_dir or test_dir == ".":
        return test_ids
    prefix = test_dir.rstrip("/") + "/"
    normalized: list[str] = []
    for tid in test_ids:
        if not tid.strip():
            continue
        if " > " in tid:
            file_part, _, rest = tid.partition(" > ")
            if not file_part.startswith(prefix) and not file_part.startswith("/"):
                file_part = prefix + file_part
            tid = f"{file_part} > {rest}"
        else:
            if not tid.startswith(prefix) and not tid.startswith("/"):
                tid = prefix + tid
        normalized.append(tid)
    return normalized


def collect_js_test_ids_local(
    repo_dir: Path,
    test_dir: str = "__tests__",
    framework: str = "jest",
    timeout: int = 300,
) -> list[str]:
    """Discover test IDs by running the framework CLI in a local repo."""
    try:
        cmd = _build_collect_command(framework, test_dir)
    except ValueError as e:
        logger.warning("  %s", e)
        return []
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "  %s test collection timed out after %ds in %s", framework, timeout, repo_dir
        )
        return []
    except FileNotFoundError:
        logger.warning("  npx/node not found. Is Node.js installed?")
        return []

    test_ids = _dispatch_parse(result.stdout, framework, str(repo_dir.resolve()))

    if not test_ids and framework == "vitest":
        logger.info("  vitest list returned 0 IDs, trying vitest run --reporter=json")
        fallback = ["npx", "vitest", "run", "--reporter=json", test_dir]
        try:
            r2 = subprocess.run(
                fallback,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            test_ids = _parse_vitest_list_output(r2.stdout, str(repo_dir.resolve()))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if not test_ids and result.stderr:
        logger.debug("  stderr from %s: %s", framework, result.stderr[:500])
    return test_ids


def collect_js_test_ids_docker(
    repo_name: str,
    test_dir: str = "__tests__",
    framework: str = "jest",
    image_name: str | None = None,
    reference_commit: str | None = None,
    timeout: int = 300,
) -> list[str]:
    """Discover test IDs by running the framework CLI inside a Docker container."""
    import docker
    import docker.errors
    import requests.exceptions

    from commit0.harness.docker_utils import get_docker_platform

    if image_name is None:
        image_name = _find_docker_image(repo_name) or (
            f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"
        )

    if reference_commit is not None and not _COMMIT_SHA_RE.fullmatch(reference_commit):
        raise ValueError(
            f"Invalid reference_commit {reference_commit!r}: expected hex SHA (7-40 chars)."
        )
    checkout = (
        f"git checkout {shlex.quote(reference_commit)} -- . && "
        if reference_commit
        else ""
    )

    cmd_parts = _build_collect_command(framework, test_dir)
    collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    bash_cmd = f"cd {CONTAINER_WORKDIR} && {checkout}{collect_cmd} 2>/dev/null; true"

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

        test_ids = _dispatch_parse(stdout, framework)

        if not test_ids:
            logger.info("  Falling back to glob-based test file enumeration")
            glob_cmd = (
                f"cd {CONTAINER_WORKDIR} && {checkout}"
                r"find . -type f \( "
                r"-name '*.test.js' -o -name '*.test.mjs' -o -name '*.test.cjs' "
                r"-o -name '*.test.jsx' "
                r"-o -name '*.spec.js' -o -name '*.spec.mjs' -o -name '*.spec.cjs' "
                r"-o -name '*.spec.jsx' "
                r"\) "
                r"-not -path '*/node_modules/*' "
                r"-not -path '*/dist/*' "
                r"-not -path '*/build/*' "
                r"-not -path '*/.git/*' "
                r"-not -path '*/coverage/*' "
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
                    logger.info("  Glob fallback found %d test files", len(files))
                    test_ids = files
            except (docker.errors.ContainerError, requests.exceptions.ReadTimeout):
                logger.debug("  Glob fallback failed")

        return test_ids
    finally:
        client.close()


def validate_js_base_commit_docker(
    repo_name: str,
    test_dir: str = "__tests__",
    framework: str = "jest",
    image_name: str | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Validate the base (stubbed) commit can still enumerate tests inside Docker."""
    import docker
    import docker.errors
    import requests.exceptions

    from commit0.harness.docker_utils import get_docker_platform

    if image_name is None:
        image_name = _find_docker_image(repo_name) or (
            f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"
        )
    cmd_parts = _build_collect_command(framework, test_dir)
    collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    bash_cmd = f"cd {CONTAINER_WORKDIR} && {collect_cmd} 2>&1; true"

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
        test_ids = _dispatch_parse(stdout, framework)
        snippet = stdout[-500:] if stdout else ""
        return len(test_ids), snippet
    finally:
        client.close()


def generate_for_js_dataset(
    dataset_path: Path,
    output_dir: Path,
    use_docker: bool = False,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
    validate_base: bool = False,
    framework_override: str | None = None,
) -> dict[str, int]:
    """Generate test IDs for every repo in a JS dataset entries JSON."""
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
        test_dir = entry.get("test", {}).get("test_dir", "__tests__")
        instance_id = entry.get("instance_id", repo_name)
        framework = (
            framework_override
            if framework_override and framework_override != "auto"
            else _detect_framework_from_entry(entry)
        )

        logger.info(
            "\n[%d/%d] Collecting %s test IDs for %s ...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            framework,
            instance_id,
        )

        if use_docker:
            test_ids = collect_js_test_ids_docker(
                repo_name=repo_name,
                test_dir=test_dir,
                framework=framework,
                reference_commit=entry.get("reference_commit"),
                timeout=timeout,
            )
            test_ids = _normalize_js_test_ids(test_ids, test_dir)
        else:
            repo_dir = _find_repo_dir(clone_dir, repo, entry.get("original_repo", ""))
            if not repo_dir or not repo_dir.is_dir():
                logger.warning("  Repo dir not found for %s -- skipping", instance_id)
                results[repo_name] = 0
                continue
            ref = entry.get("reference_commit")
            if ref:
                try:
                    subprocess.run(
                        ["git", "checkout", ref],
                        cwd=repo_dir,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=True,
                    )
                except Exception as e:
                    logger.warning(
                        "  Could not checkout reference_commit %s: %s -- skipping",
                        ref,
                        e,
                    )
                    continue
            test_ids = collect_js_test_ids_local(
                repo_dir=repo_dir,
                test_dir=test_dir,
                framework=framework,
                timeout=timeout,
            )
            test_ids = _normalize_js_test_ids(test_ids, test_dir)

        if test_ids:
            out_file = save_test_ids(test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(test_ids), out_file)
            results[repo_name] = len(test_ids)
            if validate_base and use_docker:
                base_count, snippet = validate_js_base_commit_docker(
                    repo_name=repo_name,
                    test_dir=test_dir,
                    framework=framework,
                    timeout=timeout,
                )
                if base_count == 0:
                    logger.warning(
                        "  BASE COMMIT VALIDATION FAILED: 0 tests at base_commit "
                        "(stubbed). Pipeline will produce 0%% pass rate."
                    )
                    logger.warning("  Last output: %s", snippet[:200])
                    results[repo_name] = -len(test_ids)
                else:
                    logger.info(
                        "  Base commit validation OK: %d tests at base_commit",
                        base_count,
                    )
        else:
            logger.warning("  No test IDs collected for %s", repo_name)
            results[repo_name] = 0

    return results


def main() -> None:
    """CLI entry: generate per-repo test ID .bz2 files for a JS dataset."""
    parser = argparse.ArgumentParser(
        description="Generate JavaScript test ID files (.bz2) for commit0 repos"
    )
    parser.add_argument(
        "dataset_file",
        nargs="?",
        help="Input dataset_entries.json or custom_dataset.json",
    )
    parser.add_argument("--repo-dir", type=str, help="Single local repo directory")
    parser.add_argument("--name", type=str, help="Repo name (required with --repo-dir)")
    parser.add_argument("--test-dir", type=str, default="__tests__")
    parser.add_argument("--output-dir", type=str, default="./test_ids_js")
    parser.add_argument("--clone-dir", type=str, default=None)
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-repos", type=int, default=None)
    parser.add_argument("--validate-base", action="store_true")
    parser.add_argument(
        "--framework",
        type=str,
        choices=[*sorted(SUPPORTED_TEST_FRAMEWORKS), "auto"],
        default="auto",
    )
    args = parser.parse_args()
    output_dir = Path(args.output_dir)

    if args.repo_dir:
        if not args.name:
            parser.error("--name is required with --repo-dir")
        framework = args.framework if args.framework != "auto" else "jest"
        repo_dir = Path(args.repo_dir)
        logger.info("Collecting %s test IDs from %s...", framework, repo_dir)
        test_ids = collect_js_test_ids_local(
            repo_dir=repo_dir,
            test_dir=args.test_dir,
            framework=framework,
            timeout=args.timeout,
        )
        test_ids = _normalize_js_test_ids(test_ids, args.test_dir)
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
        fw = args.framework if args.framework != "auto" else None
        results = generate_for_js_dataset(
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
        with_tests = sum(1 for v in results.values() if v > 0)
        failed = sum(1 for v in results.values() if v < 0)
        logger.info(
            "\nDone: %d test IDs across %d repos (%d empty, %d failed base validation)",
            total,
            len(results),
            len(results) - with_tests - failed,
            failed,
        )
    else:
        parser.error("Provide either dataset_file or --repo-dir")
        return

    if args.install:
        installed = install_test_ids(output_dir)
        logger.info("Installed %d test ID files into commit0 data directory", installed)


if __name__ == "__main__":
    main()
