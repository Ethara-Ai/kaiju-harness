"""ESLint + ``node --check`` runner for JavaScript repos.

JavaScript has no ``tsc --noEmit`` compile gate: ``node --check`` parses each
file without executing it and is the closest available syntax-only signal.
If the repo ships no ESLint configuration, the runner emits the marker
``LINT_NO_CONFIG`` rather than synthesising rules the author never opted into.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from commit0.harness.constants import RepoInstance, SimpleInstance
from commit0.harness.constants_js import JS_SOURCE_EXTS, RUN_JS_TEST_LOG_DIR
from commit0.harness.utils import load_dataset_from_config

logger = logging.getLogger(__name__)


LINT_NO_CONFIG_MARKER = "LINT_NO_CONFIG"
ESLINT_TIMEOUT_MARKER = "__ESLINT_TIMEOUT__"
ESLINT_TIMEOUT_EXIT_CODE = 124


@dataclass
class JsLintResult:
    repo_dir: str
    eslint_exit_code: int = 0
    eslint_output: str = ""
    node_check_exit_code: int = 0
    node_check_output: str = ""
    eslint_skipped: bool = False
    skipped_reason: str | None = None
    eslint_status_marker: str | None = None
    files_checked: list[str] = field(default_factory=list)

    @property
    def final_exit_code(self) -> int:
        return max(self.eslint_exit_code, self.node_check_exit_code)


_ESLINT_CONFIG_NAMES: tuple[str, ...] = (
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.mjs",
    ".eslintrc.json",
    ".eslintrc.yaml",
    ".eslintrc.yml",
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.mjs",
    "eslint.config.ts",
)


def _detect_exec_prefix(repo_dir: str) -> list[str]:
    d = Path(repo_dir)
    if (d / "pnpm-lock.yaml").exists():
        return ["pnpm", "exec"]
    if (d / "yarn.lock").exists():
        return ["yarn"]
    if (d / "bun.lockb").exists():
        return ["bunx"]
    return ["npx"]


def _has_eslint_config(repo_dir: str) -> bool:
    d = Path(repo_dir)
    for name in _ESLINT_CONFIG_NAMES:
        if (d / name).exists():
            return True
    pkg_path = d / "package.json"
    if not pkg_path.exists():
        return False
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(pkg, dict) and "eslintConfig" in pkg


def _enumerate_source_files(
    repo_dir: str, files: list[str] | None
) -> list[str]:
    if files:
        accepted: list[str] = []
        for f in files:
            if not f:
                continue
            if os.path.isabs(f):
                logger.warning("Rejecting absolute --files path: %s", f)
                continue
            norm = os.path.normpath(f)
            if norm == ".." or norm.startswith(".." + os.sep) or (os.sep + ".." + os.sep) in norm:
                logger.warning("Rejecting path traversal in --files: %s", f)
                continue
            accepted.append(f)
        return accepted
    out: list[str] = []
    for root, _dirs, names in os.walk(repo_dir):
        rel_root = os.path.relpath(root, repo_dir)
        parts = set(rel_root.split(os.sep))
        if parts & {"node_modules", "dist", "build", ".git", ".next", "coverage"}:
            continue
        for name in names:
            if name.endswith(JS_SOURCE_EXTS):
                out.append(os.path.relpath(os.path.join(root, name), repo_dir))
    out.sort()
    return out


def run_eslint(
    repo_dir: str,
    files: list[str] | None = None,
) -> tuple[int, str, bool]:
    """Run ESLint on *repo_dir* and return ``(returncode, output, skipped)``.

    When the repo has no ESLint configuration the runner emits the
    ``LINT_NO_CONFIG`` marker, returns ``(0, marker, True)``, and does not
    invoke ``eslint`` — per the JS pipeline's opt-in policy on lint rules.
    """
    if not _has_eslint_config(repo_dir):
        logger.info(
            "No ESLint config in %s — skipping ESLint and emitting %s",
            repo_dir,
            LINT_NO_CONFIG_MARKER,
        )
        return 0, LINT_NO_CONFIG_MARKER, True

    prefix = _detect_exec_prefix(repo_dir)
    cmd: list[str] = prefix + [
        "eslint",
        "--no-error-on-unmatched-pattern",
        "--format",
        "stylish",
    ]
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
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ESLint timed out after 300s in %s", repo_dir)
        return (
            ESLINT_TIMEOUT_EXIT_CODE,
            f"{ESLINT_TIMEOUT_MARKER} ESLint timed out after 300 seconds",
            False,
        )

    output = result.stdout + result.stderr
    if result.returncode != 0:
        logger.warning("ESLint exited with code %d", result.returncode)
    return result.returncode, output, False


def run_node_check(repo_dir: str, files: list[str]) -> tuple[int, str]:
    """Run ``node --check`` against each JS source file in *files*.

    ``node --check`` parses without executing, so it is safe to run against
    untrusted source. Returns the worst exit code across all files plus the
    concatenated stderr from any failures.
    """
    if not files:
        return 0, ""

    worst = 0
    chunks: list[str] = []
    for rel_file in files:
        abs_file = os.path.join(repo_dir, rel_file)
        if not os.path.isfile(abs_file):
            continue
        try:
            result = subprocess.run(
                ["node", "--check", abs_file],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            chunks.append(f"{rel_file}: node --check timed out after 30s")
            worst = max(worst, 1)
            continue
        if result.returncode != 0:
            worst = max(worst, result.returncode)
            chunks.append(
                f"{rel_file}:\n{result.stdout}{result.stderr}".rstrip()
            )
    return worst, "\n".join(chunks)


def _locate_repo_dir(
    dataset_name: str,
    dataset_split: str,
    base_dir: str,
    repo_or_repo_dir: str,
) -> str:
    if repo_or_repo_dir.endswith("/"):
        repo_or_repo_dir = repo_or_repo_dir[:-1]

    if os.path.isdir(repo_or_repo_dir):
        return repo_or_repo_dir

    dataset: Iterator[RepoInstance | SimpleInstance] = load_dataset_from_config(
        dataset_name, split=dataset_split
    )
    for example in dataset:
        repo_name = example["repo"].split("/")[-1]
        if repo_name in os.path.basename(
            repo_or_repo_dir
        ) or repo_or_repo_dir.endswith(repo_name):
            candidate = os.path.join(base_dir, repo_name)
            if os.path.isdir(candidate):
                return candidate

    fallback = os.path.join(base_dir, os.path.basename(repo_or_repo_dir))
    return fallback


def main(
    repo_or_repo_dir: str,
    dataset_name: str,
    dataset_split: str,
    base_dir: str,
    files: list[str] | None = None,
    verbose: int = 1,
) -> None:
    """Run ESLint followed by ``node --check`` on a JavaScript repo.

    Exit code is the worse of the two stages. When the repo has no ESLint
    configuration, only ``node --check`` runs and the ``LINT_NO_CONFIG``
    marker is printed to stdout.
    """
    repo_dir = _locate_repo_dir(dataset_name, dataset_split, base_dir, repo_or_repo_dir)
    if not os.path.isdir(repo_dir):
        logger.error("Repository directory not found: %s", repo_dir)
        sys.exit(1)

    logger.info("Linting JavaScript repo at %s", repo_dir)
    source_files = _enumerate_source_files(repo_dir, files)

    rc_eslint, output_eslint, eslint_skipped = run_eslint(repo_dir, files=files)
    if verbose > 0 and output_eslint.strip():
        print(output_eslint)

    rc_node, output_node = run_node_check(repo_dir, source_files)
    if verbose > 0 and output_node.strip():
        print(output_node)

    eslint_status_marker: str | None = None
    if rc_eslint == ESLINT_TIMEOUT_EXIT_CODE and ESLINT_TIMEOUT_MARKER in output_eslint:
        eslint_status_marker = ESLINT_TIMEOUT_MARKER
    result = JsLintResult(
        repo_dir=repo_dir,
        eslint_exit_code=rc_eslint,
        eslint_output=output_eslint,
        node_check_exit_code=rc_node,
        node_check_output=output_node,
        eslint_skipped=eslint_skipped,
        skipped_reason=LINT_NO_CONFIG_MARKER if eslint_skipped else None,
        eslint_status_marker=eslint_status_marker,
        files_checked=source_files,
    )
    status_path = (RUN_JS_TEST_LOG_DIR / "lint" / f"{os.path.basename(repo_dir)}.json").resolve()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "repo_dir": result.repo_dir,
                "eslint_exit_code": result.eslint_exit_code,
                "node_check_exit_code": result.node_check_exit_code,
                "eslint_skipped": result.eslint_skipped,
                "skipped_reason": result.skipped_reason,
                "eslint_status_marker": result.eslint_status_marker,
                "files_checked_count": len(result.files_checked),
                "final_exit_code": result.final_exit_code,
            },
            indent=2,
        )
    )
    logger.info(
        "Lint results — ESLint: %d (skipped=%s), node --check: %d, final: %d, status: %s",
        result.eslint_exit_code,
        result.eslint_skipped,
        result.node_check_exit_code,
        result.final_exit_code,
        status_path,
    )
    sys.exit(result.final_exit_code)


__all__ = [
    "ESLINT_TIMEOUT_EXIT_CODE",
    "ESLINT_TIMEOUT_MARKER",
    "JsLintResult",
    "LINT_NO_CONFIG_MARKER",
    "main",
    "run_eslint",
    "run_node_check",
]
