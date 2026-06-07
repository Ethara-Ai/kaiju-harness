"""Validate candidate Go repos for a commit0 Go dataset.

Clones repos, checks go.mod, src structure, test files,
optionally runs `go test` in Docker to measure runtime.

Usage:
    python -m tools.validate_go go_candidates.json --output validated.json
    python -m tools.validate_go go_candidates.json --output validated.json --run-tests
    python -m tools.validate_go --repo sourcegraph/conc --output validated.json
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def clone_repo(
    full_name: str,
    clone_dir: Path,
    branch: str = "main",
    depth: int = 1,
) -> Path:
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
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
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        logger.warning("Clone failed for %s: %s", full_name, e)
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    return repo_dir


def detect_go_structure(repo_dir: Path) -> dict:
    result = {
        "has_go_mod": False,
        "go_version": None,
        "module_path": None,
        "src_dir": ".",
        "test_file_count": 0,
        "go_file_count": 0,
        "has_vendor": False,
        "has_makefile": False,
        "packages": [],
    }

    go_mod = repo_dir / "go.mod"
    if go_mod.exists():
        result["has_go_mod"] = True
        content = go_mod.read_text()
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("module "):
                result["module_path"] = line.split(None, 1)[1]
            elif line.startswith("go ") and not line.startswith("go."):
                result["go_version"] = line.split()[1]

    result["has_vendor"] = (repo_dir / "vendor").is_dir()
    result["has_makefile"] = (repo_dir / "Makefile").exists()

    packages = set()
    for go_file in repo_dir.rglob("*.go"):
        rel = go_file.relative_to(repo_dir)
        parts = rel.parts
        if any(p.startswith(".") or p == "vendor" or p == "testdata" for p in parts):
            continue
        result["go_file_count"] += 1
        if go_file.name.endswith("_test.go"):
            result["test_file_count"] += 1
        pkg = str(rel.parent) if len(parts) > 1 else "."
        packages.add(pkg)

    result["packages"] = sorted(packages)
    return result


def validate_candidate(
    candidate: dict,
    clone_dir: Path,
    run_tests: bool = False,
) -> dict | None:
    full_name = candidate["full_name"]
    branch = candidate.get("default_branch", "main")
    logger.info("Validating %s ...", full_name)

    try:
        repo_dir = clone_repo(full_name, clone_dir, branch)
    except Exception:
        return None

    structure = detect_go_structure(repo_dir)

    if not structure["has_go_mod"]:
        logger.info("  SKIP %s: no go.mod", full_name)
        return None

    if structure["test_file_count"] < 3:
        logger.info(
            "  SKIP %s: only %d test files", full_name, structure["test_file_count"]
        )
        return None

    entry = {
        **candidate,
        **structure,
        "test_runtime_seconds": None,
        "test_pass": None,
    }

    if run_tests:
        logger.info("  Running go test for %s ...", full_name)
        try:
            proc = subprocess.run(
                ["go", "test", "-count=1", "-timeout=300s", "./..."],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=360,
            )
            entry["test_pass"] = proc.returncode == 0
        except subprocess.TimeoutExpired:
            entry["test_pass"] = False
            entry["test_runtime_seconds"] = 360
        except FileNotFoundError:
            logger.warning("  go binary not found. Skipping test run.")

    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Go repos for commit0")
    parser.add_argument("candidates", nargs="?", help="Path to go_candidates.json")
    parser.add_argument("--repo", help="Single repo to validate (full_name)")
    parser.add_argument("--output", default="validated.json")
    parser.add_argument("--clone-dir", default=None)
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()

    if not args.candidates and not args.repo:
        parser.error("Provide candidates JSON or --repo")

    clone_dir = (
        Path(args.clone_dir)
        if args.clone_dir
        else Path(tempfile.mkdtemp(prefix="commit0_go_validate_"))
    )
    clone_dir.mkdir(parents=True, exist_ok=True)

    if args.repo:
        candidates = [{"full_name": args.repo, "default_branch": "main", "stars": 0}]
    else:
        candidates = json.loads(Path(args.candidates).read_text())

    logger.info("Validating %d candidates...", len(candidates))
    validated = []
    for c in candidates:
        result = validate_candidate(c, clone_dir, args.run_tests)
        if result is not None:
            validated.append(result)

    Path(args.output).write_text(json.dumps(validated, indent=2) + "\n")
    logger.info("Wrote %d validated entries to %s", len(validated), args.output)


if __name__ == "__main__":
    main()
