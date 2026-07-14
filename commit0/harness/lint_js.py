"""ESLint + ``node --check`` runner for JavaScript repos.

JavaScript has no ``tsc --noEmit`` compile gate: ``node --check`` parses each
file without executing it and is the closest available syntax-only signal.

When the repo ships NO ESLint configuration, the runner falls back to a bundled
default ruleset (:data:`_DEFAULT_ESLINT_CONFIG`) run via the base-image global
``eslint`` — so SDE stage 2 (lint) ALWAYS produces a real signal, consistent with
rust ``cargo clippy``, go ``go vet`` and python ``ruff`` (all of which lint with
built-in defaults regardless of repo config). The legacy opt-in behaviour (emit
``LINT_NO_CONFIG`` and skip) survives only as a graceful degradation when no
``eslint`` binary is available (e.g. a base image built before this change).
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

# Bundled default flat config used when a repo ships no ESLint config of its own.
# Self-contained (no plugin imports) so it loads from here against any repo.
_DEFAULT_ESLINT_CONFIG = Path(__file__).resolve().parent / "eslint_default.config.mjs"


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


_ESLINT_FLAT_CONFIG_NAMES: tuple[str, ...] = (
    "eslint.config.js",
    "eslint.config.cjs",
    "eslint.config.mjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
)
_ESLINT_LEGACY_CONFIG_NAMES: tuple[str, ...] = (
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.mjs",
    ".eslintrc.json",
    ".eslintrc.yaml",
    ".eslintrc.yml",
)
_ESLINT_CONFIG_NAMES: tuple[str, ...] = (
    _ESLINT_FLAT_CONFIG_NAMES + _ESLINT_LEGACY_CONFIG_NAMES
)


def _eslint_config_style(repo_dir: str) -> str | None:
    """Return 'flat', 'legacy', or None for the repo's ESLint config style."""
    d = Path(repo_dir)
    if any((d / n).exists() for n in _ESLINT_FLAT_CONFIG_NAMES):
        return "flat"
    if any((d / n).exists() for n in _ESLINT_LEGACY_CONFIG_NAMES):
        return "legacy"
    pkg_path = d / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(pkg, dict) and "eslintConfig" in pkg:
                return "legacy"
        except (OSError, json.JSONDecodeError):
            pass
    return None


def _detect_exec_prefix(repo_dir: str) -> list[str]:
    d = Path(repo_dir)
    if (d / "pnpm-lock.yaml").exists():
        return ["pnpm", "exec"]
    if (d / "yarn.lock").exists():
        return ["yarn"]
    if (d / "bun.lockb").exists() or (d / "bun.lock").exists():
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

    If the repo ships its own ESLint config, that config is used (via the repo's
    package-manager ``exec`` prefix, so the repo's own eslint runs). Otherwise the
    bundled default ruleset is used via the base-image GLOBAL ``eslint`` — so lint
    always produces a signal, matching rust/go/python. Only if no ``eslint`` binary
    exists at all do we degrade to ``LINT_NO_CONFIG`` (``skipped=True``).
    """
    has_config = _has_eslint_config(repo_dir)
    env = dict(os.environ)

    if has_config:
        # Repo-owned config: run the repo's local eslint via its exec prefix.
        prefix = _detect_exec_prefix(repo_dir)
        cmd: list[str] = prefix + ["eslint"]
        # Reconcile config style with the installed ESLint major: ESLint 9 defaults
        # to FLAT and errors on a legacy-only repo; ESLint 8 needs opt-in for flat.
        style = _eslint_config_style(repo_dir)
        if style == "legacy":
            env["ESLINT_USE_FLAT_CONFIG"] = "false"
        elif style == "flat":
            env["ESLINT_USE_FLAT_CONFIG"] = "true"
        mode_desc = f"{style or 'unknown'} config"
    else:
        # No repo config: lint with the bundled default ruleset via the GLOBAL
        # eslint (base image), NOT the repo's local one — its version is unknown and
        # may predate the flags below. `--no-config-lookup` (eslint>=9.9) stops
        # eslint from searching the repo for a config.
        cmd = [
            "eslint",
            "--no-config-lookup",
            "--config",
            str(_DEFAULT_ESLINT_CONFIG),
            # Deny warnings so any default-ruleset finding yields a non-zero exit —
            # otherwise warn-level rules exit 0 and aider's lint gate treats the run
            # as clean and skips the fix. Mirrors rust `cargo clippy -- -D warnings`.
            "--max-warnings",
            "0",
        ]
        env["ESLINT_USE_FLAT_CONFIG"] = "true"
        mode_desc = "default config (repo ships none)"

    cmd += ["--no-error-on-unmatched-pattern", "--format", "stylish"]
    if files:
        cmd.extend(files)
    else:
        cmd.append(".")

    logger.info("Running ESLint (%s): %s", mode_desc, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        # No eslint binary available (e.g. base image built before global eslint
        # was added). Degrade gracefully to the legacy opt-in marker instead of
        # crashing the whole lint stage.
        logger.warning(
            "eslint not found for %s — emitting %s (re-run with --rebuild-agent-image "
            "to install the global eslint that backs the default-config lint)",
            repo_dir, LINT_NO_CONFIG_MARKER,
        )
        return 0, LINT_NO_CONFIG_MARKER, True
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

    Exit code is the worse of the two stages. A repo with its own ESLint config is
    linted with it; a repo with none is linted with the bundled default ruleset
    (via the global eslint). Only when no eslint binary exists does the runner fall
    back to printing the ``LINT_NO_CONFIG`` marker.
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
