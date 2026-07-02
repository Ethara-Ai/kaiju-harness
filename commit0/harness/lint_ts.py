"""ESLint + tsc --noEmit runner for TypeScript repos."""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Optional, Union

from commit0.harness.constants import RepoInstance, SimpleInstance
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)


def _detect_exec_prefix(repo_dir: str) -> str:
    """Return the local-binary runner based on lockfiles in *repo_dir*."""
    d = Path(repo_dir)
    if (d / "pnpm-lock.yaml").exists():
        return "pnpm exec"
    if (d / "yarn.lock").exists():
        return "yarn"
    if (d / "bun.lockb").exists():
        return "bunx"
    return "npx"


_ESLINT_CONFIG_FILES = (
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
    "eslint.config.ts", "eslint.config.mts", "eslint.config.cts",
    ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
    ".eslintrc.yaml", ".eslintrc.yml", ".eslintrc",
)


def _repo_has_eslint_config(repo_dir: str) -> bool:
    d = Path(repo_dir)
    for name in _ESLINT_CONFIG_FILES:
        if (d / name).exists():
            return True
    pkg = d / "package.json"
    if pkg.exists():
        try:
            import json
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "eslintConfig" in data:
                return True
        except (OSError, ValueError):
            pass
    return False


def _package_json_lint_script(repo_dir: str) -> Optional[str]:
    pkg = Path(repo_dir) / "package.json"
    if not pkg.exists():
        return None
    try:
        import json
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    if not isinstance(scripts, dict):
        return None
    for key in ("lint", "lint:check", "lint:ci"):
        script = scripts.get(key)
        if isinstance(script, str) and script.strip():
            return key
    return None


def run_project_lint_script(
    repo_dir: str, script_name: str
) -> tuple[int, str]:
    prefix = _detect_exec_prefix(repo_dir)
    if prefix == "npx":
        cmd = ["npm", "run", script_name, "--silent"]
    elif prefix == "yarn":
        cmd = ["yarn", "run", script_name]
    elif prefix == "pnpm exec":
        cmd = ["pnpm", "run", script_name]
    elif prefix == "bunx":
        cmd = ["bun", "run", script_name]
    else:
        cmd = ["npm", "run", script_name, "--silent"]

    logger.info("Running project lint script: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, cwd=repo_dir,
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return 1, f"Project lint script '{script_name}' timed out after 300s"
    return result.returncode, (result.stdout + result.stderr)


def run_eslint(
    repo_dir: str,
    files: Optional[list[str]] = None,
    config_path: Optional[str] = None,
) -> tuple[int, str]:
    """Run ESLint on *repo_dir* (or specific *files*).

    Returns ``(return_code, combined_stdout_stderr)``.
    """
    project_lint_key = _package_json_lint_script(repo_dir)
    if project_lint_key:
        node_modules = Path(repo_dir) / "node_modules"
        if not node_modules.is_dir():
            msg = (
                f"Project scripts.{project_lint_key} present but node_modules "
                f"not installed in {repo_dir}. Skipping project lint. "
                "Run 'npm install' in the repo before linting."
            )
            logger.info(msg)
            return 0, msg
        logger.info(
            "Using project's package.json scripts.%s instead of default ESLint",
            project_lint_key,
        )
        return run_project_lint_script(repo_dir, project_lint_key)

    if not config_path and not _repo_has_eslint_config(repo_dir):
        msg = (
            "ESLint skipped: no eslint.config.* / .eslintrc.* / package.json "
            f"scripts.lint found in {repo_dir}. Project does not define linting."
        )
        logger.info(msg)
        return 0, msg

    prefix = _detect_exec_prefix(repo_dir)
    cmd: list[str] = prefix.split() + [
        "eslint",
        "--no-error-on-unmatched-pattern",
        "--format",
        "stylish",
    ]
    if config_path:
        cmd.extend(["--config", config_path])
    if files:
        cmd.extend(files)
    else:
        cmd.append(".")

    logger.info("Running ESLint: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ESLint timed out after 300s in %s", repo_dir)
        return 1, "ESLint timed out after 300 seconds"
    output = result.stdout + result.stderr
    if result.returncode != 0:
        logger.warning("ESLint exited with code %d", result.returncode)
    return result.returncode, output


def run_tsc_noEmit(repo_dir: str) -> tuple[int, str]:
    """Run ``npx tsc --noEmit`` in *repo_dir*.

    Returns ``(return_code, combined_stdout_stderr)``.
    """
    prefix = _detect_exec_prefix(repo_dir)
    cmd = prefix.split() + ["tsc", "--noEmit"]
    logger.info("Running tsc --noEmit: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        logger.warning("tsc --noEmit timed out after 300s in %s", repo_dir)
        return 1, "tsc --noEmit timed out after 300 seconds"
    output = result.stdout + result.stderr
    if result.returncode != 0:
        logger.warning("tsc --noEmit exited with code %d", result.returncode)
    return result.returncode, output


def main(
    repo_or_repo_dir: str,
    dataset_name: str,
    dataset_split: str,
    base_dir: str,
    files: Optional[list[str]] = None,
    verbose: int = 1,
) -> None:
    """Run ESLint then tsc --noEmit on a TypeScript repo.

    Exit code is ``max(eslint_rc, tsc_rc)``.
    """
    dataset: Iterator[Union[RepoInstance, SimpleInstance]] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )  # type: ignore

    if repo_or_repo_dir.endswith("/"):
        repo_or_repo_dir = repo_or_repo_dir[:-1]

    repo_dir: Optional[str] = None
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if repo_name in os.path.basename(repo_or_repo_dir) or repo_or_repo_dir.endswith(
            repo_name
        ):
            candidate = repo_or_repo_dir
            if not os.path.isdir(candidate):
                candidate = os.path.join(base_dir, repo_name)
            repo_dir = candidate
            break

    if repo_dir is None:
        repo_dir = repo_or_repo_dir
        if not os.path.isdir(repo_dir):
            repo_dir = os.path.join(base_dir, os.path.basename(repo_or_repo_dir))

    if not os.path.isdir(repo_dir):
        logger.error("Repository directory not found: %s", repo_dir)
        sys.exit(1)

    logger.info("Linting TypeScript repo at %s", repo_dir)

    rc_eslint, output_eslint = run_eslint(repo_dir, files=files)
    if verbose > 0 and output_eslint.strip():
        print(output_eslint)

    rc_tsc, output_tsc = run_tsc_noEmit(repo_dir)
    if verbose > 0 and output_tsc.strip():
        print(output_tsc)

    final_rc = max(rc_eslint, rc_tsc)
    logger.info(
        "Lint results — ESLint: %d, tsc: %d, final: %d", rc_eslint, rc_tsc, final_rc
    )
    sys.exit(final_rc)
