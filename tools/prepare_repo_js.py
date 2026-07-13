"""Prepare JavaScript repos for the commit0 dataset.

Per-repo pipeline:
1. Fork to GitHub org (or skip in --dry-run).
2. Full-clone the source repo.
3. Detect src_dir, package_manager, test_framework, and test_dir.
4. Install dependencies (lockfile-driven, --ignore-scripts).
5. Run the Babel-based stubber on src_dir; collect import-time names from
   the rest of the repo (tests + sibling packages).
6. Verify >=1 stub marker landed in .js/.mjs/.cjs/.jsx source.
7. Commit on a `commit0_dataset` branch off the `commit0` base, push to fork.
8. Emit an entries-JSON row consumed by `create_dataset_js.py`.

Reuses git/clone/push helpers from `tools.prepare_repo`. The TS-twin scrape
PDF flow is intentionally omitted from the MVP (Phase E may revisit).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from kaiju.paths import datasets_dir, spec_path as consolidated_spec_path
from typing import Iterator
import uuid as _uuid_mod

from commit0.harness.constants_js import (
    DEFAULT_NODE_VERSION,
    JS_BASE_BRANCH,
    JS_DATASET_BRANCH,
    SUPPORTED_NODE_VERSIONS,
    SUPPORTED_PACKAGE_MANAGERS,
)
from tools._git_auth import fork_repo, setup_git_credentials
from tools.node_version import detect as _detect_node_version
from tools.prepare_repo import (
    full_clone,
    get_default_branch,
    get_head_sha,
    git,
    push_to_fork,
)
from tools.stub_js_runner import run_stub_js
from tools.validate_js import (
    detect_pm_and_framework,
    validate_js_candidate,
)
from tools._versioning import NoSignalsError, VersionConflictError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


DEFAULT_ORG = "Zahgon"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STUBBER_DIR = _PROJECT_ROOT / "tools" / "jsstubber"
_STUBBER_PROBE_PKG = "@babel/parser"

_WORKSPACE_INDICATORS: tuple[str, ...] = (
    "pnpm-workspace.yaml",
    "pnpm-workspace.yml",
    "lerna.json",
    "rush.json",
)

_TEST_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        "dist",
        "build",
        "coverage",
        ".next",
        ".nuxt",
        "out",
        ".turbo",
        ".cache",
        ".yarn",
        ".pnp",
        ".pnpm-store",
        "lib",
        "es",
        "esm",
        "cjs",
    }
)

_TEST_DIR_NAMES: frozenset[str] = frozenset(
    {"__tests__", "test", "tests", "__test__", "spec", "specs"}
)

_TEST_FILE_SUFFIXES: tuple[str, ...] = (
    ".test.js",
    ".test.mjs",
    ".test.cjs",
    ".test.jsx",
    ".spec.js",
    ".spec.mjs",
    ".spec.cjs",
    ".spec.jsx",
)

KNOWN_TEST_PACKAGES: frozenset[str] = frozenset(
    {
        "jest",
        "@jest/globals",
        "ts-jest",
        "vitest",
        "mocha",
        "chai",
        "@vitest/coverage-v8",
    }
)


@contextmanager
def _stubber_install_lock() -> Iterator[None]:
    _STUBBER_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _STUBBER_DIR / ".install.lock"
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _validate_stubber_deps() -> None:
    probe = _STUBBER_DIR / "node_modules" / "@babel" / "parser"
    if probe.exists():
        return
    pkg = _STUBBER_DIR / "package.json"
    if not pkg.exists():
        raise OSError(
            f"Stubber deps missing and no package.json at {_STUBBER_DIR}. "
            f"Expected: {_STUBBER_PROBE_PKG}"
        )
    if shutil.which("npm") is None:
        raise OSError(
            f"Stubber needs `npm` to install {_STUBBER_PROBE_PKG} at "
            f"{_STUBBER_DIR} but `npm` is not on PATH."
        )
    with _stubber_install_lock():
        if probe.exists():
            return
        logger.warning(
            "Stubber dependencies missing at %s; running "
            "`npm install --no-audit --no-fund` once (lock acquired)...",
            _STUBBER_DIR,
        )
        result = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(_STUBBER_DIR),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0 or not probe.exists():
            raise OSError(
                f"npm install at {_STUBBER_DIR} did not produce {probe}. "
                f"stderr tail: {result.stderr[-500:].strip()}"
            )
        logger.info("Stubber deps installed at %s", _STUBBER_DIR)


_FROZEN_INSTALL_CMDS: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "ci", "--no-audit", "--no-fund", "--ignore-scripts"),
    "pnpm": ("pnpm", "install", "--frozen-lockfile", "--ignore-scripts"),
    "yarn": ("yarn", "install", "--frozen-lockfile", "--ignore-scripts"),
    "bun": ("bun", "install", "--frozen-lockfile", "--ignore-scripts"),
}

# Generating installs: run when NO committed lockfile exists. They resolve the
# dependency tree and WRITE a lockfile, which prepare then commits into the
# stubbed branch so downstream frozen installs (npm ci) are reproducible.
_GENERATING_INSTALL_CMDS: dict[str, tuple[str, ...]] = {
    "npm": ("npm", "install", "--no-audit", "--no-fund", "--ignore-scripts"),
    "pnpm": ("pnpm", "install", "--ignore-scripts"),
    "yarn": ("yarn", "install", "--ignore-scripts"),
    "bun": ("bun", "install", "--ignore-scripts"),
}

# Lockfile filename produced by each package manager, for detection + git add.
_LOCKFILE_BY_PM: dict[str, str] = {
    "npm": "package-lock.json",
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
    "bun": "bun.lockb",
}


def _frozen_install_cmd(pkg_manager: str) -> list[str]:
    try:
        return list(_FROZEN_INSTALL_CMDS[pkg_manager])
    except KeyError as exc:
        raise ValueError(
            f"Unsupported package manager for frozen install: {pkg_manager!r}"
        ) from exc


def _generating_install_cmd(pkg_manager: str) -> list[str]:
    try:
        return list(_GENERATING_INSTALL_CMDS[pkg_manager])
    except KeyError as exc:
        raise ValueError(
            f"Unsupported package manager for generating install: {pkg_manager!r}"
        ) from exc


def _has_committed_lockfile(repo_dir: Path) -> bool:
    return any((repo_dir / name).exists() for name in _LOCKFILE_BY_PM.values())


def _ensure_pkg_manager(pkg_manager: str) -> None:
    if shutil.which(pkg_manager) is not None:
        return
    if pkg_manager == "npm":
        raise OSError("`npm` is not on PATH but the repo needs it. Install Node.js.")
    corepack = shutil.which("corepack")
    if corepack and pkg_manager in {"pnpm", "yarn"}:
        logger.warning(
            "`%s` not on PATH; attempting `corepack prepare %s@latest --activate`",
            pkg_manager,
            pkg_manager,
        )
        result = subprocess.run(
            [corepack, "prepare", f"{pkg_manager}@latest", "--activate"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode == 0 and shutil.which(pkg_manager) is not None:
            logger.info("Activated %s via corepack", pkg_manager)
            return
    raise OSError(
        f"`{pkg_manager}` is not on PATH. Install manually:\n"
        f"  npm install -g {pkg_manager}   (or)   corepack enable && "
        f"corepack prepare {pkg_manager}@latest --activate"
    )


def _list_workspace_packages(repo_dir: Path) -> list[str]:
    found: list[str] = []
    for parent in ("packages", "apps", "libs", "modules"):
        parent_dir = repo_dir / parent
        if not parent_dir.is_dir():
            continue
        for child in sorted(parent_dir.iterdir()):
            if child.is_dir() and (child / "package.json").exists():
                found.append(f"{parent}/{child.name}/src")
    return found


def _detect_monorepo(repo_dir: Path) -> tuple[bool, list[str]]:
    for marker in _WORKSPACE_INDICATORS:
        if (repo_dir / marker).exists():
            return True, _list_workspace_packages(repo_dir)
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict) and data.get("workspaces"):
            return True, _list_workspace_packages(repo_dir)
    return False, []


def _assert_monorepo_safety(
    repo_dir: Path,
    src_dir: str,
    src_dir_override: str | None,
) -> None:
    if src_dir_override or src_dir != ".":
        return
    is_monorepo, packages = _detect_monorepo(repo_dir)
    if not is_monorepo:
        return
    suggestion = packages[0] if packages else "packages/<name>/src"
    sample = ", ".join(packages[:5]) or "(none detected)"
    raise OSError(
        f"Detected a monorepo at {repo_dir} but no --src-dir was given.\n"
        f"Stubbing the whole monorepo will exhaust V8's heap.\n"
        f"Pass --src-dir <path>, e.g. --src-dir {suggestion}.\n"
        f"Detected workspace packages: {sample}"
    )


def detect_js_src_dir(repo_dir: Path) -> str:
    """Auto-detect the JS source directory within a repo."""
    src_dir = repo_dir / "src"
    if src_dir.is_dir() and any(src_dir.rglob("*.js")):
        return "src"
    source_dir = repo_dir / "source"
    if source_dir.is_dir() and any(source_dir.rglob("*.js")):
        return "source"
    lib_dir = repo_dir / "lib"
    if lib_dir.is_dir() and any(lib_dir.rglob("*.js")):
        return "lib"

    root_js = [
        f
        for f in repo_dir.glob("*.js")
        if not f.name.startswith(".") and f.name != "index.test.js"
    ]
    if root_js:
        return "."

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in _TEST_SCAN_SKIP_DIRS:
            continue
        if (child / "index.js").exists() or (child / "index.mjs").exists():
            return child.name

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in _TEST_SCAN_SKIP_DIRS:
            continue
        if any(
            f.suffix in {".js", ".mjs", ".cjs", ".jsx"}
            and "node_modules" not in f.parts
            for f in child.rglob("*")
        ):
            return child.name
    return ""


def _walk_repo_filtered(repo_dir: Path):
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _TEST_SCAN_SKIP_DIRS and not d.startswith(".")
        ]
        yield Path(dirpath), dirnames, filenames


def detect_js_test_dirs(repo_dir: Path) -> list[Path]:
    """Find test directories ranked by confidence (test-file count desc)."""
    counts: dict[Path, int] = {}
    for dirpath, _, filenames in _walk_repo_filtered(repo_dir):
        if dirpath != repo_dir and dirpath.name.lower() in _TEST_DIR_NAMES:
            count = sum(
                1
                for f in filenames
                if f.endswith((".js", ".mjs", ".cjs", ".jsx"))
            )
            if count > 0:
                counts[dirpath] = counts.get(dirpath, 0) + count
        for f in filenames:
            if f.endswith(_TEST_FILE_SUFFIXES):
                counts[dirpath] = counts.get(dirpath, 0) + 1
    return [
        p
        for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0].parts)))
    ]


def _detect_node_version_for_repo(repo_dir: Path) -> tuple[int, str, list[str]]:
    try:
        det = _detect_node_version(
            repo_dir,
            SUPPORTED_NODE_VERSIONS,
            fallback=DEFAULT_NODE_VERSION,
        )
        return (
            int(det.version or DEFAULT_NODE_VERSION),
            det.source,
            list(det.conflicts),
        )
    except NoSignalsError:
        return DEFAULT_NODE_VERSION, "default", []


def generate_setup_dict_js(repo_dir: Path) -> tuple[dict, dict, str, str]:
    """Build setup/test dicts and return (setup, test, framework, package_manager)."""
    info = detect_pm_and_framework(repo_dir)
    pm = info["package_manager"]
    framework = info["test_framework"]
    if pm not in SUPPORTED_PACKAGE_MANAGERS:
        raise RuntimeError(f"Unsupported package_manager: {pm!r}")

    install_cmd = f"{pm} install"

    packages: list[str] = []
    pkg_path = repo_dir / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            dev_deps = pkg.get("devDependencies", {}) if isinstance(pkg, dict) else {}
            packages = sorted(p for p in dev_deps if p in KNOWN_TEST_PACKAGES)
        except (json.JSONDecodeError, OSError):
            pass

    test_dirs = detect_js_test_dirs(repo_dir)
    if not test_dirs:
        raise RuntimeError(
            f"Could not detect a test directory for {repo_dir.name}. "
            "Inspect package.json (jest/vitest/mocha), config files, or the "
            "filesystem layout, and set test_dir manually."
        )
    test_dir = test_dirs[0].name

    node_version, version_source, version_conflicts = _detect_node_version_for_repo(
        repo_dir
    )

    setup_dict = {
        "node_version": node_version,
        "install": install_cmd,
        "packages": packages,
        "pre_install": [],
        "specification": "",
        "version_source": version_source,
        "version_conflicts": version_conflicts,
    }

    test_dict = _build_test_dict(pm, framework, test_dir)
    return setup_dict, test_dict, framework, pm


def _build_test_dict(pm: str, framework: str, test_dir: str) -> dict:
    runner_prefix = {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}.get(pm, "npx")
    if framework == "jest":
        test_cmd = f"{runner_prefix} jest"
    elif framework == "vitest":
        test_cmd = f"{runner_prefix} vitest run"
    elif framework == "mocha":
        test_cmd = f"{runner_prefix} mocha"
    elif framework == "node_test":
        test_cmd = "node --test"
    else:
        test_cmd = f"{runner_prefix} {framework}"
    return {"test_cmd": test_cmd, "test_dir": test_dir}


def _collect_extra_scan_dirs(
    repo_dir: Path,
    src_dir_path: Path,
    test_dirs: list[Path],
) -> list[Path]:
    extra: list[Path] = list(test_dirs)
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child == src_dir_path or child.name in _TEST_SCAN_SKIP_DIRS:
            continue
        if child in extra:
            continue
        has_js = any(
            f.suffix in {".js", ".mjs", ".cjs", ".jsx"}
            and "node_modules" not in f.parts
            for f in child.rglob("*")
        )
        if has_js:
            extra.append(child)
    max_dirs = int(os.environ.get("KAIJU_JS_MAX_SCAN_DIRS", "20"))
    if len(extra) > max_dirs:
        logger.warning(
            "Capping extra_scan_dirs from %d to %d (override via KAIJU_JS_MAX_SCAN_DIRS)",
            len(extra),
            max_dirs,
        )
        extra = extra[:max_dirs]
    return extra


def _capture_js_test_ids(
    repo_dir: Path,
    repo_basename: str,
    test_framework: str,
    test_dir: str,
) -> None:
    """Capture the canonical JS test inventory by running the framework's
    list/discovery command on the pristine (un-stubbed) source, and save it to
    ``commit0/data/test_ids/<repo_basename>.bz2``.

    Must be called AFTER dependency install (``node_modules`` is required for
    ``jest --listTests`` / ``vitest list`` / etc.) and BEFORE stubbing, since
    stubbed source throws at import time and yields zero discovered tests.

    Best-effort: any failure just leaves ``evaluate_js`` to fall back to the
    observed test count as its denominator. Never raises.
    """
    try:
        from tools.generate_test_ids_js import (
            _normalize_js_test_ids,
            collect_js_test_ids_local,
        )
        from tools.generate_test_ids import save_test_ids
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "  Could not import JS test-id capture (%s); skipping. The evaluator "
            "will use the observed test count.",
            e,
        )
        return
    try:
        ids = collect_js_test_ids_local(
            repo_dir=repo_dir,
            test_dir=test_dir,
            framework=test_framework,
        )
        ids = _normalize_js_test_ids(ids, test_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "  JS test-id collection failed for %s (%s); the evaluator will use "
            "the observed test count.",
            repo_basename,
            e,
        )
        return
    if not ids:
        logger.warning(
            "  No JS test IDs discovered for %s; the evaluator will use the "
            "observed test count.",
            repo_basename,
        )
        return
    out_dir = (
        Path(__file__).resolve().parent.parent / "commit0" / "data" / "test_ids"
    )
    try:
        path = save_test_ids(ids, repo_basename, out_dir)
        logger.info("  Saved %d canonical JS test IDs -> %s", len(ids), path)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "  Failed to save JS test IDs for %s (%s).", repo_basename, e
        )


def _validate_and_repair_js_stubs(
    repo_dir: Path, reference_commit: str
) -> "bool | None":
    """Post-stub SYNTAX gate for JavaScript (the "correct stubbed code" guarantee).

    The Babel stubber can, on unusual-but-valid source (arrow bodies, getters/
    setters, class fields, unusual template literals), emit a syntactically BROKEN
    file. Committing that means the agent starts from un-parseable code and any 0%
    is infra, not the model. So we run ``node --check`` — Node's own parser, the
    authoritative + dependency-free oracle — on every STAGED stub file. Any file
    that fails is reverted to its pristine (pre-stub) blob, so the committed base
    ALWAYS parses; a reverted file simply isn't a task file (far better than a
    corrupt base). Mirrors C's differential-parse gate in spirit.

    Returns base_compiles: True (node ran, committed base parses), None (node
    unavailable -> unknown, no false gate). Raises only if EVERY stub file was
    broken (degenerate: the stubber produced nothing but invalid syntax).
    """
    if shutil.which("node") is None:
        logger.info("  node unavailable; skipping JS stub syntax gate (base_compiles=unknown).")
        return None
    staged = git(
        repo_dir, "diff", "--cached", "--name-only", "--",
        "*.js", "*.mjs", "*.cjs", "*.jsx",
    )
    files = [f for f in staged.splitlines() if f.strip()]
    checked = 0
    reverted = 0
    for rel in files:
        p = repo_dir / rel
        if not p.is_file():
            continue
        rc = subprocess.run(
            ["node", "--check", str(p)], capture_output=True, text=True
        )
        if rc.returncode == 0:
            checked += 1
            continue
        first = (rc.stderr.strip().splitlines() or [""])[0]
        logger.warning(
            "  JS stub produced INVALID syntax in %s (node --check: %s) — reverting "
            "this file to pristine so the base stays valid (it won't be a task file).",
            rel, first,
        )
        try:
            git(repo_dir, "checkout", reference_commit, "--", rel)
            git(repo_dir, "add", "--", rel)
            reverted += 1
        except Exception as e:  # noqa: BLE001
            logger.error("  Could not revert corrupted stub %s: %s", rel, e)
            return False
    if checked == 0 and reverted > 0:
        raise RuntimeError(
            "Every stubbed JS file failed `node --check` — the stubber produced "
            "only invalid syntax. Refusing to emit a corrupt base."
        )
    if reverted:
        logger.info(
            "  JS stub gate: %d valid stub file(s), %d reverted for invalid syntax.",
            checked, reverted,
        )
    else:
        logger.info("  JS stub gate: all %d stub file(s) parse (node --check).", checked)
    return True


def create_js_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    src_dir: str,
    pkg_manager: str,
    branch_name: str = JS_DATASET_BRANCH,
    base_branch_name: str = JS_BASE_BRANCH,
    test_framework: str | None = None,
    test_dir: str | None = None,
) -> "tuple[str, str, int, bool | None]":
    """Create the JS commit0 + commit0_dataset branches; returns
    (base, ref, n_stubbed, base_compiles)."""
    # Record reference + branch from the CURRENT HEAD (the pinned tag when --tag
    # checked one out). Checking out the default branch first silently discarded
    # the tag, so base_commit was built on the default tip while reference_commit
    # pointed at the tag (divergent history).
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    try:
        git(repo_dir, "branch", "-D", base_branch_name, check=False)
    except Exception:
        pass
    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", base_branch_name)
    git(repo_dir, "checkout", "-b", branch_name)

    src_dir_path = repo_dir / src_dir if src_dir != "." else repo_dir
    if not src_dir_path.is_dir():
        raise ValueError(f"src_dir does not exist: {src_dir_path}")

    test_dirs = detect_js_test_dirs(repo_dir)
    if src_dir == ".":
        extra_scan_dirs: list[Path] = []
        logger.info("  src_dir is '.', skipping extra_scan_dirs")
    else:
        extra_scan_dirs = _collect_extra_scan_dirs(repo_dir, src_dir_path, test_dirs)

    if extra_scan_dirs:
        logger.info(
            "  Scanning %d extra dirs for import-time names: %s",
            len(extra_scan_dirs),
            [d.name for d in extra_scan_dirs],
        )

    if (repo_dir / "package.json").exists():
        _ensure_pkg_manager(pkg_manager)
        has_lockfile = _has_committed_lockfile(repo_dir)
        if has_lockfile:
            # Reproducible path: repo committed a lockfile -> frozen install.
            logger.info("  Installing dependencies via %s (frozen)...", pkg_manager)
            install_cmd = _frozen_install_cmd(pkg_manager)
        else:
            # No committed lockfile: run a generating install to CREATE one, then
            # commit it into the stubbed branch so downstream frozen installs
            # (npm ci) are reproducible. This unblocks the many small JS libs
            # that intentionally .gitignore their lockfile.
            logger.info(
                "  No committed lockfile; installing via %s (generating lockfile)...",
                pkg_manager,
            )
            install_cmd = _generating_install_cmd(pkg_manager)
        install_result = subprocess.run(
            install_cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if install_result.returncode != 0:
            raise RuntimeError(
                f"Dependency install failed for {full_name} via {pkg_manager} "
                f"(returncode={install_result.returncode}). Stubber requires a "
                "populated node_modules to resolve the transitive import-time "
                "call graph; running the stubber on a partial install yields "
                "incomplete stub coverage and corrupt dataset rows.\n"
                f"  cmd: {' '.join(install_cmd)}\n"
                f"  stderr tail: {install_result.stderr[-500:].strip()}"
            )
        if not has_lockfile:
            lockfile_name = _LOCKFILE_BY_PM[pkg_manager]
            lockfile_path = repo_dir / lockfile_name
            if not lockfile_path.exists():
                raise RuntimeError(
                    f"Generating install for {full_name} via {pkg_manager} did not "
                    f"produce {lockfile_name}; cannot commit a reproducible lockfile "
                    "into the stubbed branch. A committed lockfile is required so the "
                    "downstream frozen install (e.g. `npm ci`) can rebuild "
                    "node_modules deterministically inside the harness container."
                )
            git(repo_dir, "add", "-f", "--", lockfile_name)
            git(repo_dir, "commit", "-m", f"Add generated {lockfile_name}")
            logger.info(
                "  Committed generated %s into %s", lockfile_name, branch_name
            )

    # Capture the canonical test inventory on pristine (un-stubbed) source,
    # after node_modules is installed. This gives evaluate_js a real denominator
    # via commit0/data/test_ids/<repo>.bz2 instead of falling back to the
    # observed count. Best-effort; never aborts prep.
    if test_framework and test_dir:
        _capture_js_test_ids(
            repo_dir=repo_dir,
            repo_basename=full_name.split("/")[-1],
            test_framework=test_framework,
            test_dir=test_dir,
        )

    logger.info("  Stubbing JavaScript source in: %s", src_dir)
    report = run_stub_js(
        src_dir=src_dir_path,
        extra_scan_dirs=extra_scan_dirs if extra_scan_dirs else None,
        verbose=True,
    )
    if report.get("errors"):
        logger.warning(
            "  Stubbing had %d errors: %s",
            len(report["errors"]),
            report["errors"][:3],
        )

    logger.info(
        "  Stub report: %d files processed, %d modified, %d functions stubbed, "
        "%d import-time preserved",
        report.get("files_processed", 0),
        report.get("files_modified", 0),
        report.get("functions_stubbed", 0),
        report.get("functions_skipped_import_time", 0),
    )

    git(repo_dir, "add", "-A")
    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing -- source may already be stubs?")
        return reference_commit, reference_commit, 0

    functions_stubbed = int(report.get("functions_stubbed", 0))
    if functions_stubbed == 0:
        raise RuntimeError(
            f"No functions were stubbed for {full_name}; aborting. Running the agent "
            "on a repo with zero stubs is wasteful and inflates pass rates with "
            "trivial baselines. Investigate the stubber output above."
        )

    diff_js = git(
        repo_dir,
        "diff",
        "--cached",
        "--unified=0",
        "--",
        "*.js",
        "*.mjs",
        "*.cjs",
        "*.jsx",
    )
    stub_marker_count = sum(
        1
        for line in diff_js.splitlines()
        if line.startswith("+")
        and not line.startswith("+++")
        and 'throw new Error("STUB")' in line
    )
    logger.info(
        "  Stub verification -- JS STUB markers added: %d (expected >= 1)",
        stub_marker_count,
    )
    if stub_marker_count < 1:
        raise RuntimeError(
            f"Stubbing verification failed for {full_name}: functions_stubbed="
            f"{functions_stubbed} but the staged .js/.mjs/.cjs/.jsx diff contains "
            'zero added lines matching `throw new Error("STUB")`. The apparent '
            "stubs did not land in JavaScript source files."
        )

    # Correct-stubbing gate: verify every stub file parses; revert any the stubber
    # corrupted so the committed base is always valid (see helper). Runs after the
    # marker check and BEFORE the commit so the base only ever contains valid code.
    base_compiles = _validate_and_repair_js_stubs(repo_dir, reference_commit)

    git(repo_dir, "commit", "-m", "Commit 0")
    base_commit = get_head_sha(repo_dir)
    logger.info("  Base commit (stubbed): %s", base_commit[:12])
    return base_commit, reference_commit, functions_stubbed, base_compiles


def _resolve_commits_from_remote(
    fork_name: str,
    branch: str,
) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/branches/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        branch_data = json.loads(result.stdout)
        sha = branch_data["commit"]["sha"]
        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/commits/{sha}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        commit_data = json.loads(result.stdout)
        if not commit_data.get("parents"):
            return None
        parent_sha = commit_data["parents"][0]["sha"]
        return (sha, parent_sha)
    except Exception as e:
        logger.debug("Non-critical failure during remote commit resolution: %s", e)
        return None


def prepare_js_repo(
    full_name: str,
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    src_dir_override: str | None = None,
    release_tag: str | None = None,
    dry_run: bool = False,
) -> dict | None:
    """Run the full per-repo pipeline; returns an entries-JSON row or None."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not dry_run:
        raise OSError("GITHUB_TOKEN is required for non-dry-run mode")

    logger.info("Processing %s (org=%s)", full_name, org)

    if dry_run:
        fork_name = f"{org}/{full_name.split('/')[-1]}"
        logger.info("  [DRY RUN] Would fork to %s", fork_name)
    else:
        fork_name = fork_repo(full_name, org, token=token)

    repo_dir = full_clone(full_name, clone_dir, tag=release_tag)
    if release_tag:
        logger.info("  Pinned to tag: %s", release_tag)

    ok, reasons = validate_js_candidate(repo_dir)
    if not ok:
        logger.error("  Validation failed for %s: %s", full_name, "; ".join(reasons))
        return None

    src_dir = src_dir_override or detect_js_src_dir(repo_dir)
    if not src_dir:
        logger.error("  Cannot detect JavaScript source dir for %s", full_name)
        return None
    logger.info("  Source directory: %s", src_dir)
    _assert_monorepo_safety(repo_dir, src_dir, src_dir_override)

    try:
        setup_dict, test_dict, test_framework, pkg_manager = generate_setup_dict_js(
            repo_dir
        )
    except VersionConflictError as exc:
        logger.error("Refusing %s: node conflict %s", full_name, exc)
        return None
    logger.info("  Test framework: %s, Package manager: %s", test_framework, pkg_manager)

    base_commit, reference_commit, functions_stubbed, base_compiles = create_js_stubbed_branch(
        repo_dir,
        full_name,
        src_dir,
        pkg_manager,
        test_framework=test_framework,
        test_dir=test_dict.get("test_dir"),
    )

    if not dry_run:
        git(repo_dir, "checkout", JS_DATASET_BRANCH)
        try:
            push_to_fork(repo_dir, fork_name, branch=JS_DATASET_BRANCH, token=token)
        except Exception as e:
            logger.error("  Push failed: %s", e)
            remote_commits = _resolve_commits_from_remote(fork_name, JS_DATASET_BRANCH)
            if remote_commits:
                base_commit, reference_commit = remote_commits
                logger.info(
                    "  Resolved commits from remote: base=%s, ref=%s",
                    base_commit[:12],
                    reference_commit[:12],
                )
            else:
                logger.error(
                    "  Push to %s failed AND no remote branch resolvable; "
                    "refusing to emit a dataset row whose base_commit=%s and "
                    "reference_commit=%s exist only in the local clone. "
                    "Downstream setup_js.clone_repo cannot fetch unpushed SHAs.",
                    fork_name,
                    base_commit[:12],
                    reference_commit[:12],
                )
                return None

    return {
        "instance_id": f"commit-0/{full_name.split('/')[-1]}",
        "id": str(_uuid_mod.uuid4()),
        "repo": fork_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "src_dir": src_dir,
        "language": "javascript",
        "test_framework": test_framework,
        "package_manager": pkg_manager,
        "functions_stubbed": functions_stubbed,
        # Tri-state: True = stubbed base parses (node --check), None = node
        # unavailable (unknown). An evaluator can downgrade a 0% when this is not
        # True (infra, not model). Mirrors python/c/go/rust/java.
        "base_compiles": base_compiles,
        "setup": setup_dict,
        "test": test_dict,
    }


def main() -> None:
    """CLI entry: prepare one or many JS repos for the commit0 dataset."""
    parser = argparse.ArgumentParser(description="Prepare JavaScript repos for commit0")
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Batch input JSON (validated_js.json). Mutually exclusive with --repo.",
    )
    parser.add_argument("--repo", default=None, help="owner/name of a single GitHub repo")
    parser.add_argument(
        "--org", default=DEFAULT_ORG, help=f"GitHub org for fork (default: {DEFAULT_ORG})"
    )
    parser.add_argument(
        "--src-dir",
        default=None,
        help="Override auto-detected src dir (single-repo mode only)",
    )
    parser.add_argument("--tag", default=None, help="Pin to a specific release tag")
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("repos_staging"),
        help="Directory for cloned repos (default: repos_staging)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output entries JSON file (stdout if omitted; required for batch)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip fork and push")
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Batch mode: maximum number of repos to prepare",
    )
    parser.add_argument(
        "--outputs-root",
        type=str,
        default=None,
        help="Root for consolidated outputs (overrides $KAIJU_OUTPUTS_ROOT; default: ./outputs)",
    )
    parser.add_argument(
        "--layout",
        choices=["flat", "consolidated"],
        default=None,
        help="Output layout: 'flat' (legacy) or 'consolidated' (outputs/<uuid>/…). Overrides $KAIJU_LOG_LAYOUT.",
    )

    args = parser.parse_args()

    if args.outputs_root is not None:
        os.environ["KAIJU_OUTPUTS_ROOT"] = args.outputs_root
    if args.layout is not None:
        os.environ["KAIJU_LOG_LAYOUT"] = args.layout
    _consolidated = os.environ.get("KAIJU_LOG_LAYOUT", "consolidated").lower() == "consolidated"

    setup_git_credentials(dry_run=args.dry_run)
    _validate_stubber_deps()

    if not args.repo and not args.input_file:
        parser.error("Provide either --repo owner/name or an input_file positional.")
    if args.repo and args.input_file:
        parser.error("--repo and input_file are mutually exclusive.")

    args.clone_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    if args.repo:
        result = prepare_js_repo(
            full_name=args.repo,
            clone_dir=args.clone_dir,
            org=args.org,
            src_dir_override=args.src_dir,
            release_tag=args.tag,
            dry_run=args.dry_run,
        )
        if result is None:
            logger.error("Failed to prepare %s", args.repo)
            sys.exit(1)
        entries.append(result)
    else:
        candidates = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
        if isinstance(candidates, dict) and "data" in candidates:
            candidates = candidates["data"]
        if not isinstance(candidates, list):
            parser.error(
                f"input_file {args.input_file} must contain a JSON list "
                'or {"data": [...]}.'
            )

        for i, candidate in enumerate(candidates):
            if args.max_repos and i >= args.max_repos:
                break
            full_name = candidate.get("full_name") or candidate.get("repo") or ""
            if not full_name:
                logger.warning("  Skipping entry %d: no full_name or repo", i)
                continue
            try:
                result = prepare_js_repo(
                    full_name=full_name,
                    clone_dir=args.clone_dir,
                    org=args.org,
                    src_dir_override=candidate.get("src_dir_override")
                    or candidate.get("src_dir"),
                    release_tag=candidate.get("tag") or args.tag,
                    dry_run=args.dry_run,
                )
            except Exception as e:
                logger.error("  FAILED %s: %s", full_name, e)
                continue
            if result:
                entries.append(result)

        logger.info(
            "Batch complete: %d/%d entries prepared",
            len(entries),
            min(len(candidates), args.max_repos or len(candidates)),
        )

    if _consolidated and entries and entries[0].get("id"):
        _uuid = entries[0]["id"]
        _out_dir = datasets_dir(_uuid)
        _entries_path = _out_dir / "entries.json"
        _entries_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        logger.info("Wrote %d entries to %s (consolidated)", len(entries), _entries_path)
        if args.output:
            Path(args.output).write_text(json.dumps(entries, indent=2), encoding="utf-8")
    elif args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        logger.info("Wrote %d entries to %s", len(entries), output_path)
    elif len(entries) == 1:
        print(json.dumps(entries[0], indent=2))
    else:
        print(json.dumps(entries, indent=2))

    if entries:
        logger.info("Done: %s", ", ".join(e["instance_id"] for e in entries))


if __name__ == "__main__":
    main()
