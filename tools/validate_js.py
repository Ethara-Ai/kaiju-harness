"""Validate candidate JavaScript repos for the commit0 dataset.

Usage:
    python -m tools.validate_js js_candidates.json --output validated_js.json
    python -m tools.validate_js --repo sindresorhus/p-queue --output validated_js.json
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


SUPPORTED_PMS_BY_LOCKFILE: dict[str, str] = {
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
}

REQUIRED_PKG_FIELDS: tuple[str, ...] = ("name", "version")

NATIVE_BINDING_DEPS: tuple[str, ...] = (
    "node-gyp",
    "node-pre-gyp",
    "node-addon-api",
    "prebuild-install",
)


def validate_js_candidate(repo: Path) -> tuple[bool, list[str]]:
    """Validate a JS repo against MVP requirements; returns (ok, reasons)."""
    reasons: list[str] = []

    pkg_path = repo / "package.json"
    if not pkg_path.exists():
        return False, ["no package.json"]

    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, [f"unreadable package.json: {e}"]

    if not isinstance(pkg, dict):
        return False, ["package.json is not a JSON object"]

    for field in REQUIRED_PKG_FIELDS:
        if field not in pkg:
            reasons.append(f"missing package.json field: {field!r}")

    lockfiles = [name for name in SUPPORTED_PMS_BY_LOCKFILE if (repo / name).exists()]

    deps_raw = pkg.get("dependencies")
    dev_raw = pkg.get("devDependencies")
    deps = {**(deps_raw or {}), **(dev_raw or {})}
    for binding in NATIVE_BINDING_DEPS:
        if binding in deps:
            reasons.append(f"native binding not supported: {binding}")

    scripts = pkg.get("scripts") or {}
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    has_test_script = bool(test_script and str(test_script).strip())
    if not has_test_script:
        reasons.append("missing scripts.test")

    # Lockfile gate: accept a committed lockfile (reproducible frozen install)
    # OR a repo that is "generatable" -- a package.json with a real test script,
    # from which prepare can synthesize a lockfile via a generating install and
    # commit it into the stubbed branch. Only reject when neither holds.
    if len(lockfiles) > 1:
        reasons.append(f"multiple lockfiles: {lockfiles}")
    elif not lockfiles and not has_test_script:
        reasons.append("no lockfile and not generatable (no package.json test script)")

    engines = pkg.get("engines") or {}
    has_node_engine = isinstance(engines, dict) and "node" in engines
    has_browser_field = bool(pkg.get("browser"))
    if has_browser_field and not has_node_engine:
        reasons.append("browser-only (no engines.node)")

    return (len(reasons) == 0, reasons)


def detect_pm_and_framework(repo: Path) -> dict[str, str]:
    """Detect package_manager and test_framework from lockfile + deps."""
    pm = "npm"
    for name, candidate_pm in SUPPORTED_PMS_BY_LOCKFILE.items():
        if (repo / name).exists():
            pm = candidate_pm
            break

    framework = "jest"
    pkg_path = repo / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        if isinstance(pkg, dict):
            deps_raw = pkg.get("dependencies")
            dev_raw = pkg.get("devDependencies")
            deps = {**(deps_raw or {}), **(dev_raw or {})}
            if "jest" in deps:
                framework = "jest"
            elif "vitest" in deps:
                framework = "vitest"
            elif "mocha" in deps:
                framework = "mocha"
            else:
                scripts = pkg.get("scripts") or {}
                test_script = (
                    str(scripts.get("test", "")) if isinstance(scripts, dict) else ""
                )
                if (
                    test_script.startswith("node --test")
                    or " node --test" in test_script
                ):
                    framework = "node_test"
    return {"package_manager": pm, "test_framework": framework}


def _clone_repo(
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


def _validate_one(candidate: dict, clone_dir: Path) -> dict | None:
    full_name = candidate.get("full_name") or candidate.get("repo") or ""
    if not full_name:
        logger.warning("Entry missing full_name/repo: %s", candidate)
        return None
    branch = candidate.get("default_branch", "main")
    logger.info("Validating %s ...", full_name)
    try:
        repo_dir = _clone_repo(full_name, clone_dir, branch)
    except Exception:
        return None
    ok, reasons = validate_js_candidate(repo_dir)
    if not ok:
        logger.info("  SKIP %s: %s", full_name, "; ".join(reasons))
        return None
    info = detect_pm_and_framework(repo_dir)
    return {**candidate, **info, "validated": True}


def main() -> None:
    """CLI entry: validate one or many candidates."""
    parser = argparse.ArgumentParser(description="Validate JS repos for commit0")
    parser.add_argument("candidates", nargs="?", help="Path to js_candidates.json")
    parser.add_argument("--repo", default=None, help="Single repo full_name to validate")
    parser.add_argument("--output", default="validated_js.json")
    parser.add_argument("--clone-dir", default=None)
    args = parser.parse_args()

    if not args.candidates and not args.repo:
        parser.error("Provide candidates JSON or --repo")

    clone_dir = (
        Path(args.clone_dir)
        if args.clone_dir
        else Path(tempfile.mkdtemp(prefix="commit0_js_validate_"))
    )
    clone_dir.mkdir(parents=True, exist_ok=True)

    if args.repo:
        candidates: list[dict] = [
            {"full_name": args.repo, "default_branch": "main", "stars": 0}
        ]
    else:
        candidates = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        if isinstance(candidates, dict) and "data" in candidates:
            candidates = candidates["data"]
        if not isinstance(candidates, list):
            parser.error(f"{args.candidates}: expected list or {{'data': [...]}}.")

    logger.info("Validating %d candidates...", len(candidates))
    validated: list[dict] = []
    for c in candidates:
        result = _validate_one(c, clone_dir)
        if result is not None:
            validated.append(result)

    Path(args.output).write_text(json.dumps(validated, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %d validated entries to %s", len(validated), args.output)


if __name__ == "__main__":
    main()
