"""Validate candidate repos for a commit0 dataset.

Clones repos, analyzes structure, detects src_dir/test_dir,
optionally runs pytest in Docker to measure runtime + coverage.

Usage:
    # Structural analysis only (fast, no Docker):
    python -m tools.validate candidates.json --output validated.json

    # Full validation with Docker-based test execution:
    python -m tools.validate candidates.json --output validated.json --run-tests

    # Single repo:
    python -m tools.validate --repo pallets/flask --output validated.json

    # Custom clone directory (repos persist for prepare_repo.py):
    python -m tools.validate candidates.json --clone-dir ./repos_staging --output validated.json
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from commit0.harness.docker_utils import get_docker_platform


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ─── Clone ────────────────────────────────────────────────────────────────────


def clone_repo(
    full_name: str,
    clone_dir: Path,
    branch: str = "main",
    depth: int = 1,
) -> Path:
    """Shallow-clone a GitHub repo. Returns path to cloned directory."""
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
        logger.info("  Using cached clone: %s", repo_dir)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    cmd = [
        "git",
        "clone",
        "--depth",
        str(depth),
        "--branch",
        branch,
        url,
        str(repo_dir),
    ]

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return repo_dir
    except subprocess.CalledProcessError:
        # Branch might not be "main" — try without --branch
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        cmd_no_branch = ["git", "clone", "--depth", str(depth), url, str(repo_dir)]
        subprocess.run(
            cmd_no_branch,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        return repo_dir


# ─── Structure Analysis ──────────────────────────────────────────────────────


def find_src_dir(repo_dir: Path, repo_name: str) -> str | None:
    """Find the main source directory.

    Heuristic order:
    1. src/<package_name>/ (src layout)
    2. <package_name>/ matching repo name (standard layout)
    3. Any top-level dir with __init__.py that's not tests/test/docs
    """
    # Normalize repo name for matching (e.g., "python-rsa" → "rsa", "web3.py" → "web3")
    candidates = _package_name_candidates(repo_name)

    # Check src layout first
    src_dir = repo_dir / "src"
    if src_dir.is_dir():
        for name in candidates:
            pkg = src_dir / name
            if pkg.is_dir() and (pkg / "__init__.py").exists():
                return f"src/{name}"
        # Fallback: any package in src/
        for child in sorted(src_dir.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists():
                return f"src/{child.name}"

    # Standard layout: <package_name>/ at repo root
    for name in candidates:
        pkg = repo_dir / name
        if pkg.is_dir() and (pkg / "__init__.py").exists():
            return name

    # Fallback: any top-level package that's not tests/docs/etc.
    skip = {
        "tests",
        "test",
        "docs",
        "doc",
        "examples",
        "example",
        "benchmarks",
        "scripts",
        "tools",
        ".github",
    }
    for child in sorted(repo_dir.iterdir()):
        if child.is_dir() and child.name not in skip and not child.name.startswith("."):
            if (child / "__init__.py").exists():
                return child.name

    return None


def find_test_dir(repo_dir: Path) -> str | None:
    """Find the test directory."""
    for name in ["tests", "test"]:
        d = repo_dir / name
        if d.is_dir():
            return name

    # Check src layout
    src = repo_dir / "src"
    if src.is_dir():
        for name in ["tests", "test"]:
            d = src / name
            if d.is_dir():
                return f"src/{name}"

    return None


def _package_name_candidates(repo_name: str) -> list[str]:
    """Generate possible package names from a repo name."""
    names = [repo_name]

    # Strip common suffixes/prefixes
    stripped = repo_name
    for suffix in [".py", "-python", "-py"]:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
    for prefix in ["python-", "py-"]:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :]

    if stripped != repo_name:
        names.append(stripped)

    # Replace hyphens/dots with underscores
    normalized = repo_name.replace("-", "_").replace(".", "_")
    if normalized not in names:
        names.append(normalized)

    # Lowercase variants
    names_lower = []
    for n in names:
        if n.lower() not in [x.lower() for x in names_lower]:
            names_lower.append(n)
            if n != n.lower():
                names_lower.append(n.lower())

    return names_lower


def count_python_files(repo_dir: Path) -> dict:
    """Count Python files and estimate function count."""
    py_files = 0
    test_files = 0
    src_files = 0
    total_functions = 0
    total_lines = 0
    test_patterns = re.compile(r"(^test_|_test\.py$|^conftest\.py$)")

    for py_file in repo_dir.rglob("*.py"):
        # Skip hidden dirs and common non-source dirs
        parts = py_file.relative_to(repo_dir).parts
        if any(
            p.startswith(".") or p in {"__pycache__", "node_modules", ".git"}
            for p in parts
        ):
            continue

        py_files += 1

        if test_patterns.search(py_file.name) or any(
            p in {"tests", "test"} for p in parts
        ):
            test_files += 1
        else:
            src_files += 1

        try:
            source = py_file.read_text(errors="replace")
            total_lines += source.count("\n")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    total_functions += 1
        except (SyntaxError, ValueError):
            pass

    return {
        "total_py_files": py_files,
        "src_files": src_files,
        "test_files": test_files,
        "total_functions": total_functions,
        "total_lines": total_lines,
    }


def detect_python_version(repo_dir: Path) -> str | None:
    """Detect the canonical Python ``X.Y`` version for ``repo_dir``.

    Delegates to :func:`tools.python_version.detect` — the single source of
    truth shared with :mod:`tools.prepare_repo`. Returns ``None`` if no
    signals exist or if signals conflict (so the caller can flag the repo).
    """
    from commit0.harness.constants import SUPPORTED_PYTHON_VERSIONS
    from tools.python_version import (
        NoSignalsError,
        VersionConflictError,
        detect as _detect,
    )
    try:
        return _detect(repo_dir, SUPPORTED_PYTHON_VERSIONS).version
    except (VersionConflictError, NoSignalsError):
        return None


def detect_install_method(repo_dir: Path) -> dict:
    """Detect how to install the package."""
    result = {
        "has_pyproject": False,
        "has_setup_py": False,
        "has_setup_cfg": False,
        "build_backend": None,
        "extra_deps": [],
    }

    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        result["has_pyproject"] = True
        content = pyproject.read_text(errors="replace")

        # Detect build backend
        m = re.search(r'build-backend\s*=\s*["\']([^"\']+)["\']', content)
        if m:
            result["build_backend"] = m.group(1)

        # Detect optional dependency groups with "test" or "dev"
        for group_name in re.findall(
            r"\[(?:project\.optional-dependencies|tool\..*\.extras)\]\s*\n([^[]+)",
            content,
        ):
            pass
        # Simpler: look for test/dev extras
        if re.search(r"\b(test|testing|tests)\b\s*=\s*\[", content):
            result["extra_deps"].append("test")
        if re.search(r"\b(dev|develop|development)\b\s*=\s*\[", content):
            result["extra_deps"].append("dev")

    if (repo_dir / "setup.py").exists():
        result["has_setup_py"] = True
    if (repo_dir / "setup.cfg").exists():
        result["has_setup_cfg"] = True

    return result


def detect_test_deps(repo_dir: Path) -> list[str]:
    """Detect test dependencies beyond pytest."""
    deps: set[str] = set()

    # Scan config files for pytest plugins and test deps
    for config_file in [
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "requirements-test.txt",
        "requirements-tests.txt",
        "requirements-dev.txt",
    ]:
        path = repo_dir / config_file
        if not path.exists():
            continue
        content = path.read_text(errors="replace").lower()
        for dep in [
            "pytest-cov",
            "pytest-xdist",
            "pytest-asyncio",
            "pytest-mock",
            "pytest-timeout",
            "pytest-httpx",
            "pytest-httpserver",
            "hypothesis",
            "coverage",
            "tox",
            "nox",
        ]:
            if dep in content:
                deps.add(dep)

    return sorted(deps)


def check_documentation(repo_dir: Path) -> dict:
    """Check for documentation presence and quality."""
    result = {
        "has_readme": False,
        "has_docs_dir": False,
        "has_sphinx": False,
        "has_mkdocs": False,
        "readme_size": 0,
    }

    for name in ["README.md", "README.rst", "README.txt", "README"]:
        readme = repo_dir / name
        if readme.exists():
            result["has_readme"] = True
            result["readme_size"] = readme.stat().st_size
            break

    docs_dir = repo_dir / "docs"
    if not docs_dir.is_dir():
        docs_dir = repo_dir / "doc"
    if docs_dir.is_dir():
        result["has_docs_dir"] = True
        if (docs_dir / "conf.py").exists():
            result["has_sphinx"] = True

    if (repo_dir / "mkdocs.yml").exists() or (repo_dir / "mkdocs.yaml").exists():
        result["has_mkdocs"] = True

    return result


def analyze_repo(repo_dir: Path, full_name: str) -> dict:
    """Full structural analysis of a cloned repo."""
    repo_name = full_name.split("/")[-1]

    src_dir = find_src_dir(repo_dir, repo_name)
    test_dir = find_test_dir(repo_dir)
    file_counts = count_python_files(repo_dir)
    python_version = detect_python_version(repo_dir)
    install_info = detect_install_method(repo_dir)
    test_deps = detect_test_deps(repo_dir)
    doc_info = check_documentation(repo_dir)

    # Compute real Python percentage from file counts
    all_files = sum(
        1
        for _ in repo_dir.rglob("*")
        if _.is_file()
        and not any(
            p.startswith(".") or p in {"__pycache__", "node_modules"}
            for p in _.relative_to(repo_dir).parts
        )
    )

    return {
        "full_name": full_name,
        "src_dir": src_dir,
        "test_dir": test_dir,
        "python_version": python_version,
        "install": install_info,
        "test_deps": test_deps,
        "docs": doc_info,
        "file_counts": file_counts,
        "all_files_count": all_files,
        "validation": {
            "has_src_dir": src_dir is not None,
            "has_test_dir": test_dir is not None,
            "has_installable_package": install_info["has_pyproject"]
            or install_info["has_setup_py"],
            "has_documentation": doc_info["has_docs_dir"]
            or doc_info["has_sphinx"]
            or doc_info["has_mkdocs"],
            "estimated_stub_count": file_counts["total_functions"],
            "estimated_complexity": _estimate_complexity(file_counts),
        },
    }


def _estimate_complexity(file_counts: dict) -> str:
    """Rough complexity estimate based on codebase size."""
    funcs = file_counts["total_functions"]
    if funcs < 100:
        return "small"
    elif funcs < 500:
        return "medium"
    elif funcs < 2000:
        return "large"
    else:
        return "massive"


# ─── Docker-Based Test Execution ─────────────────────────────────────────────


def run_tests_in_docker(
    repo_dir: Path,
    full_name: str,
    python_version: str = "3.12",
    timeout: int = 1800,
) -> dict:
    """Run pytest inside a Docker container.

    Uses a generic Python image with the repo mounted.
    Measures: test discovery, test runtime, coverage.
    """
    result = {
        "tests_collected": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "tests_error": 0,
        "test_runtime_seconds": 0.0,
        "coverage_pct": None,
        "docker_error": None,
    }

    container_name = f"validate-{full_name.replace('/', '-')}-{int(time.time())}"
    image = f"python:{python_version}-slim-bookworm"

    # Build install + test script
    install_script = _build_install_script(repo_dir)
    _SHELL_METACHAR_RE = re.compile(r"[;|`$&<>]")
    if _SHELL_METACHAR_RE.search(install_script):
        logger.warning(
            "install_script contains shell metacharacters: %s", install_script
        )

    script = f"""#!/bin/bash
set -e
cd /workspace

# Install dependencies
{install_script}

# Install pytest + coverage
pip install pytest pytest-cov pytest-json-report 2>/dev/null

# Collect tests (fast check)
echo "=== COLLECTING TESTS ==="
python -m pytest --collect-only -q 2>&1 | tail -5 || true

# Run tests with coverage and JSON report
echo "=== RUNNING TESTS ==="
timeout {timeout} python -m pytest \\
    --json-report --json-report-file=/tmp/report.json \\
    --cov --cov-report=json:/tmp/coverage.json \\
    -x -q --timeout=300 2>&1 || true

# Output results
echo "=== RESULTS ==="
cat /tmp/report.json 2>/dev/null || echo '{{}}'
echo "=== COVERAGE ==="
cat /tmp/coverage.json 2>/dev/null || echo '{{}}'
"""

    try:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                get_docker_platform(),
                "--name",
                container_name,
                "-v",
                f"{repo_dir.resolve()}:/workspace:ro",
                image,
                "bash",
                "-c",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 120,  # Extra buffer for Docker overhead
        )

        output = proc.stdout

        # Parse test report
        report_match = re.search(
            r"=== RESULTS ===\n(.+?)\n=== COVERAGE ===", output, re.DOTALL
        )
        if report_match:
            try:
                report = json.loads(report_match.group(1).strip())
                summary = report.get("summary", {})
                result["tests_collected"] = summary.get("collected", 0)
                result["tests_passed"] = summary.get("passed", 0)
                result["tests_failed"] = summary.get("failed", 0)
                result["tests_error"] = summary.get("error", 0)
                result["test_runtime_seconds"] = round(report.get("duration", 0), 1)
            except json.JSONDecodeError:
                logger.debug("Failed to parse pytest JSON report")

        # Parse coverage
        cov_match = re.search(r"=== COVERAGE ===\n(.+)", output, re.DOTALL)
        if cov_match:
            try:
                cov = json.loads(cov_match.group(1).strip())
                result["coverage_pct"] = round(
                    cov.get("totals", {}).get("percent_covered", 0), 1
                )
            except json.JSONDecodeError:
                logger.debug("Failed to parse coverage JSON")

        if proc.returncode != 0 and result["tests_collected"] == 0:
            result["docker_error"] = (
                proc.stderr[:500] if proc.stderr else "Unknown error"
            )

    except subprocess.TimeoutExpired:
        result["docker_error"] = f"Timeout after {timeout}s"
        # Kill and remove container to avoid orphans
        subprocess.run(["docker", "kill", container_name], capture_output=True)
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    except Exception as e:
        logger.warning("Non-critical failure during Docker validation: %s", e)
        result["docker_error"] = str(e)[:500]

    return result


def _build_install_script(repo_dir: Path) -> str:
    """Build the pip install command for a repo."""
    has_pyproject = (repo_dir / "pyproject.toml").exists()
    has_setup_py = (repo_dir / "setup.py").exists()

    if has_pyproject:
        # Try to detect extras
        content = (repo_dir / "pyproject.toml").read_text(errors="replace")
        extras = []
        for name in ["test", "testing", "tests", "dev", "develop"]:
            if re.search(rf"\b{name}\b\s*=\s*\[", content):
                extras.append(name)

        if extras:
            extras_str = ",".join(extras)
            return f'pip install -e ".[{extras_str}]" 2>&1 | tail -5'
        return "pip install -e . 2>&1 | tail -5"

    elif has_setup_py:
        return "pip install -e . 2>&1 | tail -5"

    else:
        # No installable package — just install requirements
        req_files = [
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "requirements-tests.txt",
        ]
        cmds = []
        for f in req_files:
            if (repo_dir / f).exists():
                cmds.append(f"pip install -r {f} 2>&1 | tail -3")
        return "\n".join(cmds) if cmds else "echo 'No install method found'"


# ─── Main ────────────────────────────────────────────────────────────────────


def validate_candidates(
    candidates: list[dict],
    clone_dir: Path,
    run_tests: bool = False,
    max_repos: int | None = None,
) -> list[dict]:
    """Validate a list of candidate repos."""
    results: list[dict] = []

    for i, candidate in enumerate(candidates):
        if max_repos and i >= max_repos:
            break

        full_name = candidate["full_name"]
        default_branch = candidate.get("default_branch", "main")
        logger.info(
            "\n[%d/%d] Validating %s (%d stars)...",
            i + 1,
            min(len(candidates), max_repos or len(candidates)),
            full_name,
            candidate.get("stars", 0),
        )

        result = {
            **candidate,  # Preserve discover.py fields
            "analysis": None,
            "test_results": None,
            "status": "pending",
        }

        # Clone
        try:
            repo_dir = clone_repo(full_name, clone_dir, branch=default_branch)
        except Exception as e:
            logger.error("  Clone failed: %s", e)
            result["status"] = "clone_failed"
            result["error"] = str(e)[:300]
            results.append(result)
            continue

        # Structural analysis
        try:
            analysis = analyze_repo(repo_dir, full_name)
            result["analysis"] = analysis
        except Exception as e:
            logger.error("  Analysis failed: %s", e)
            result["status"] = "analysis_failed"
            result["error"] = str(e)[:300]
            results.append(result)
            continue

        # Log key findings
        v = analysis["validation"]
        logger.info("  src_dir: %s", analysis["src_dir"])
        logger.info("  test_dir: %s", analysis["test_dir"])
        logger.info(
            "  functions: %d (%s)", v["estimated_stub_count"], v["estimated_complexity"]
        )
        logger.info(
            "  files: %d py (%d src, %d test)",
            analysis["file_counts"]["total_py_files"],
            analysis["file_counts"]["src_files"],
            analysis["file_counts"]["test_files"],
        )
        logger.info(
            "  installable: %s, docs: %s",
            v["has_installable_package"],
            v["has_documentation"],
        )

        # Determine pass/fail
        issues: list[str] = []
        if not v["has_src_dir"]:
            issues.append("no_src_dir")
        if not v["has_test_dir"]:
            issues.append("no_test_dir")
        if not v["has_installable_package"]:
            issues.append("no_installable_package")

        # Docker-based test execution (optional)
        if run_tests and not issues:
            logger.info("  Running tests in Docker...")
            from commit0.harness.constants import DEFAULT_PYTHON_VERSION
            python_ver = analysis["python_version"] or DEFAULT_PYTHON_VERSION

            test_results = run_tests_in_docker(
                repo_dir, full_name, python_version=python_ver
            )
            result["test_results"] = test_results

            logger.info(
                "  Tests: %d collected, %d passed, %d failed, %.1fs runtime",
                test_results["tests_collected"],
                test_results["tests_passed"],
                test_results["tests_failed"],
                test_results["test_runtime_seconds"],
            )
            if test_results.get("coverage_pct") is not None:
                logger.info("  Coverage: %.1f%%", test_results["coverage_pct"])
            if test_results.get("docker_error"):
                logger.warning("  Docker error: %s", test_results["docker_error"])
                issues.append("test_execution_failed")

            if test_results["tests_collected"] == 0:
                issues.append("no_tests_collected")
            elif test_results["test_runtime_seconds"] > 1800:
                issues.append("tests_too_slow")

        result["status"] = "pass" if not issues else "fail"
        result["issues"] = issues
        results.append(result)

        status_emoji = "✓" if result["status"] == "pass" else "✗"
        logger.info(
            "  Result: %s %s %s",
            status_emoji,
            result["status"],
            issues if issues else "",
        )

    return results


def print_validation_summary(results: list[dict]) -> None:
    """Print summary table of validation results."""
    passed = [r for r in results if r["status"] == "pass"]
    failed = [r for r in results if r["status"] != "pass"]

    print(f"\n{'=' * 100}")
    print(
        f"VALIDATION SUMMARY: {len(passed)} passed, {len(failed)} failed, {len(results)} total"
    )
    print(f"{'=' * 100}\n")

    print(
        f"{'#':>3}  {'Repository':<40} {'Stars':>7} {'Status':>8} "
        f"{'Funcs':>6} {'Complexity':>11} {'src_dir':<20} {'Issues'}"
    )
    print("-" * 120)

    for i, r in enumerate(results, 1):
        analysis = r.get("analysis") or {}
        v = analysis.get("validation") or {}
        status = r.get("status", "?")
        funcs = v.get("estimated_stub_count", "?")
        complexity = v.get("estimated_complexity", "?")
        src_dir = (analysis.get("src_dir") or "N/A")[:19]
        issues = ", ".join(r.get("issues", []))

        status_str = f"{'✓' if status == 'pass' else '✗'} {status}"
        print(
            f"{i:>3}  {r['full_name']:<40} {r.get('stars', 0):>7,} {status_str:>8} "
            f"{funcs:>6} {complexity:>11} {src_dir:<20} {issues}"
        )

    print(f"\n{'=' * 100}")

    if passed:
        print(f"\nPASSED ({len(passed)}):")
        for r in sorted(passed, key=lambda x: x.get("stars", 0), reverse=True):
            v = (r.get("analysis") or {}).get("validation") or {}
            print(
                f"  {r['full_name']:<40} {r.get('stars', 0):>7,} stars  {v.get('estimated_stub_count', '?'):>5} functions  ({v.get('estimated_complexity', '?')})"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate candidate repos for commit0 dataset"
    )
    parser.add_argument(
        "candidates_file",
        nargs="?",
        help="Input candidates.json from discover.py",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Validate a single repo (e.g., pallets/flask)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="validated.json",
        help="Output JSON file (default: validated.json)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default=None,
        help="Directory to clone repos into (default: temp dir)",
    )
    parser.add_argument(
        "--run-tests",
        action="store_true",
        help="Run pytest in Docker for each repo (slow but thorough)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to validate",
    )

    args = parser.parse_args()

    # Load candidates
    if args.repo:
        candidates = [
            {
                "full_name": args.repo,
                "name": args.repo.split("/")[-1],
                "owner": args.repo.split("/")[0],
                "stars": 0,
                "default_branch": "main",
            }
        ]
    elif args.candidates_file:
        candidates = json.loads(Path(args.candidates_file).read_text(encoding="utf-8"))
    else:
        parser.error("Provide either candidates_file or --repo")
        return

    # Setup clone directory
    if args.clone_dir:
        clone_dir = Path(args.clone_dir)
        clone_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        clone_dir = Path(tempfile.mkdtemp(prefix="commit0_validate_"))
        cleanup = True

    logger.info("Clone directory: %s", clone_dir)
    logger.info("Validating %d candidates...", len(candidates))

    try:
        results = validate_candidates(
            candidates,
            clone_dir=clone_dir,
            run_tests=args.run_tests,
            max_repos=args.max_repos,
        )

        # Save results
        output_path = Path(args.output)
        output_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        logger.info("Saved %d results to %s", len(results), output_path)

        print_validation_summary(results)

    finally:
        if cleanup:
            logger.info("Cleaning up temp clone dir: %s", clone_dir)
            shutil.rmtree(clone_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
