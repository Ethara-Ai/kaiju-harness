"""Prepare repos for a commit0 dataset.

For each validated candidate:
1. Fork to Ethara-Ai GitHub org
2. Create a 'commit0_all' branch
3. Apply AST stubbing (replace function bodies with pass)
4. Commit stubbed version as base_commit
5. Reset to original as reference_commit
6. Generate setup/test dict entries
7. Output dataset entries (RepoInstance-compatible)

Usage:
    # From validated.json (output of validate.py):
    python -m tools.prepare_repo validated.json --output dataset_entries.json

    # Single repo:
    python -m tools.prepare_repo --repo pallets/flask --clone-dir ./repos_staging --output dataset_entries.json

    # Dry run (no GitHub fork, no push):
    python -m tools.prepare_repo validated.json --dry-run --output dataset_entries.json

Requires:
    - GITHUB_TOKEN env var with repo/fork permissions
    - gh CLI installed (for forking)
    - stub.py working (imported as module)
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from kaiju.paths import datasets_dir, spec_path as consolidated_spec_path
from urllib.parse import urlparse
import uuid as _uuid_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# GitHub org to fork repos into
DEFAULT_ORG = "Zahgon"

# T6: GitHub repo-name shape guard. Enforces owner/repo format with GitHub's
# character policy (alnum + `_.-`, no leading `-` or `.`). Prevents path
# traversal via `..`, backslashes, or absolute paths ending up in
# `clone_dir / full_name.replace('/','__')`. If a poisoned dataset row set
# full_name to '../../etc/passwd', the path join would escape clone_dir.
_GITHUB_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}/[A-Za-z0-9_.-]{1,100}$"
)


def _validate_github_full_name(full_name: str) -> None:
    """Raise ValueError if full_name isn't a well-formed owner/repo string."""
    if not isinstance(full_name, str) or not _GITHUB_REPO_RE.match(full_name):
        raise ValueError(
            f"invalid GitHub repo name {full_name!r}: expected owner/repo with"
            " allowed chars [A-Za-z0-9_.-]"
        )
    if ".." in full_name or full_name.startswith(".") or full_name.startswith("-"):
        raise ValueError(
            f"unsafe GitHub repo name {full_name!r}: leading dot/dash or '..'"
            " segment not allowed (path-traversal guard)"
        )

# Import stub module
TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR.parent))
from tools.stub import (
    StubTransformer,
    is_test_file,
    should_skip_file,
    collect_import_time_names,
    collect_test_imported_names,
)
from tools.python_version import (
    NoSignalsError,
    VersionConflictError,
    detect as detect_python_version_result,
)
from tools.system_deps_scanner import scan_repo_for_system_deps

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)
from commit0.harness.constants import REMOTE_BRANCH

# Lazy import for spec scraping (optional dependency)
_scrape_spec_sync = None


def _get_scrape_func():
    """Lazy-load scrape_spec_sync to avoid importing optional deps at module level."""
    global _scrape_spec_sync
    if _scrape_spec_sync is None:
        from tools.scrape_pdf import scrape_spec_sync

        _scrape_spec_sync = scrape_spec_sync
    return _scrape_spec_sync




# ─── Git Helpers ──────────────────────────────────────────────────────────────




def get_head_sha(repo_dir: Path) -> str:
    """Get current HEAD commit SHA."""
    return git(repo_dir, "rev-parse", "HEAD")


def get_default_branch(repo_dir: Path) -> str:
    """Get the default branch name."""
    try:
        ref = git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.split("/")[-1]
    except subprocess.CalledProcessError:
        logger.debug(
            "Could not determine default branch via symbolic-ref, trying common names"
        )
        # Fallback: check common names
        for branch in ["main", "master"]:
            try:
                git(repo_dir, "rev-parse", f"refs/remotes/origin/{branch}")
                return branch
            except subprocess.CalledProcessError:
                continue
        return "main"


# ─── Fork & Clone ────────────────────────────────────────────────────────────




def full_clone(
    full_name: str, clone_dir: Path, branch: str | None = None, tag: str | None = None
) -> Path:
    """Full clone (not shallow) of a repo. Returns repo dir."""
    _validate_github_full_name(full_name)  # T6: path-traversal guard
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
        shallow_file = repo_dir / ".git" / "shallow"
        if shallow_file.exists():
            logger.info("  Unshallowing existing clone...")
            git(repo_dir, "fetch", "--unshallow", check=False, timeout=300)
        if tag:
            git(repo_dir, "fetch", "--tags", timeout=120)
            git(repo_dir, "checkout", tag, check=False)
        else:
            # Reused non-tag clone: a PRIOR prep may have left HEAD on the stub
            # branch (commit0_all). Reset to a pristine default-branch tip so
            # create_stubbed_branch records the ORIGINAL (un-stubbed) code as
            # reference_commit, not the stubbed commit. (Since create_stubbed_branch
            # no longer checks out default itself — that discarded pinned tags — the
            # pristine reset for reuse lives here.)
            try:
                _def = get_default_branch(repo_dir)
                git(repo_dir, "fetch", "origin", _def, "--prune", check=False, timeout=120)
                git(repo_dir, "checkout", "-f", _def, check=False)
                git(repo_dir, "reset", "--hard", f"origin/{_def}", check=False)
            except Exception as _e:  # noqa: BLE001 - best-effort; fresh clones are unaffected
                logger.warning("  Could not reset reused clone to pristine default: %s", _e)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    ref = tag or branch
    cmd = ["git", "clone", url, str(repo_dir)]
    if ref:
        cmd = ["git", "clone", "--branch", ref, url, str(repo_dir)]

    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
    except subprocess.CalledProcessError:
        if ref and repo_dir.exists():
            shutil.rmtree(repo_dir)
        cmd = ["git", "clone", url, str(repo_dir)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
        if tag:
            git(repo_dir, "checkout", tag, check=False)

    return repo_dir


# ─── Stub & Commit ───────────────────────────────────────────────────────────


def _dir_exists_exact(parent: Path, name: str) -> bool:
    """Check if a directory exists with exact case (even on case-insensitive macOS)."""
    if not (parent / name).is_dir():
        return False
    try:
        return name in os.listdir(parent)
    except OSError:
        return False


def detect_src_dir(repo_dir: Path, full_name: str) -> str:
    """Auto-detect the source directory within a repo."""
    package_name = full_name.split("/")[-1].replace("-", "_")

    # 1. Check src/{package_name}/ layout (exact case)
    src_parent = repo_dir / "src"
    if _dir_exists_exact(src_parent, package_name):
        return f"src/{package_name}"

    # 2. Check src/{package_name}/ with lowercase
    if package_name != package_name.lower() and _dir_exists_exact(
        src_parent, package_name.lower()
    ):
        return f"src/{package_name.lower()}"

    # 3. Flat layout: {package_name}/ at repo root (must contain __init__.py)
    if (
        _dir_exists_exact(repo_dir, package_name)
        and (repo_dir / package_name / "__init__.py").exists()
    ):
        return package_name

    if (
        package_name != package_name.lower()
        and _dir_exists_exact(repo_dir, package_name.lower())
        and (repo_dir / package_name.lower() / "__init__.py").exists()
    ):
        return package_name.lower()

    # 4. Fallback: scan for directories with __init__.py that aren't test dirs
    test_names = {"test", "tests", "testing", "test_utils", "conftest"}
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name.startswith("_"):
            continue
        if child.name.lower() in test_names:
            continue
        if (child / "__init__.py").exists():
            return child.name

    # 5. Check inside src/ for any package
    src_dir = repo_dir / "src"
    if src_dir.is_dir():
        for child in sorted(src_dir.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name.startswith("_"):
                continue
            if (child / "__init__.py").exists():
                return f"src/{child.name}"

    # 6. Single-file module: {package_name}.py at repo root (e.g. pycodestyle.py)
    single_file = repo_dir / f"{package_name}.py"
    if single_file.is_file():
        return "."

    return ""


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    src_dir: str | None,
    branch_name: str | None = None,
    removal_mode: str = "all",
) -> tuple[str, str]:
    """Create the commit0 branch with stubbed code.

    Returns (base_commit_sha, reference_commit_sha).

    Workflow:
    1. Record the current HEAD as reference_commit
    2. Create branch 'commit0_{removal_mode}'
    3. Run stub.py on source files
    4. Commit stubbed version as base_commit
    """
    if branch_name is None:
        branch_name = REMOTE_BRANCH
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    # Create the stub branch from the CURRENT HEAD — which is the pinned release
    # tag when full_clone checked one out. Previously this checked out the default
    # branch first, silently discarding the tag: base_commit was then built on the
    # default-branch tip while reference_commit pointed at the tag (divergent
    # history), so the evaluated base was NOT the released version. Branching from
    # HEAD keeps base = stubbed(reference).
    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception as e:
        logger.debug(
            "Non-critical failure during branch cleanup of %s: %s", branch_name, e
        )
    git(repo_dir, "checkout", "-b", branch_name)

    if src_dir:
        stub_target = repo_dir / src_dir
    else:
        stub_target = repo_dir

    if not stub_target.is_dir():
        raise ValueError(f"src_dir does not exist: {stub_target}")

    logger.info(
        "  Stubbing source in: %s (mode=%s)",
        stub_target.relative_to(repo_dir),
        removal_mode,
    )

    extra_scan_dirs: list[Path] = []
    test_dir_names = {"test", "tests", "testing"}
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child == stub_target:
            continue
        if child.name.lower() in test_dir_names:
            extra_scan_dirs.append(child)
            continue
        if (child / "__init__.py").exists():
            extra_scan_dirs.append(child)
    src_parent = repo_dir / "src"
    if src_parent.is_dir() and stub_target.parent == src_parent:
        for child in sorted(src_parent.iterdir()):
            if not child.is_dir() or child == stub_target:
                continue
            if (child / "__init__.py").exists():
                extra_scan_dirs.append(child)
    if extra_scan_dirs:
        logger.info(
            "  Scanning %d extra dirs: %s",
            len(extra_scan_dirs),
            [d.name for d in extra_scan_dirs],
        )

    import_time_names = collect_import_time_names(
        stub_target, extra_scan_dirs=extra_scan_dirs
    )
    if import_time_names:
        logger.info(
            "  Preserving %d import-time functions: %s",
            len(import_time_names),
            ", ".join(sorted(import_time_names)[:15]),
        )
    # Names the TEST suite imports from the package must survive stubbing as
    # stubs (signature kept) — deleting an undocumented one (combined mode)
    # would break test collection with an ImportError -> false 0/N.
    keep_as_stub_names = collect_test_imported_names(repo_dir, {stub_target.name})
    # import-time names are fully preserved, so they take precedence; only the
    # rest need force-stubbing.
    keep_as_stub_names -= import_time_names
    if keep_as_stub_names:
        logger.info(
            "  Keeping %d test-imported name(s) as stubs: %s",
            len(keep_as_stub_names),
            ", ".join(sorted(keep_as_stub_names)[:15]),
        )
    stubber = StubTransformer(
        keep_docstrings=False,
        removal_mode=removal_mode,
        import_time_names=import_time_names,
        keep_as_stub_names=keep_as_stub_names,
    )

    stubbed_count = 0
    errors = 0

    if src_dir in (".", ""):
        py_files = sorted(stub_target.glob("*.py"))
    else:
        py_files = sorted(stub_target.rglob("*.py"))

    for py_file in py_files:
        rel = py_file.relative_to(repo_dir)

        # Skip entry-point / package-structure files (__init__.py, __main__.py,
        # conftest.py) as well as test files — matching the CLI stub path
        # (should_skip_file). __main__.py in particular is an entry point the
        # test suite often imports from (`from pkg.__main__ import parse_args`);
        # stubbing it can DELETE those undocumented helpers and break test
        # collection with an ImportError -> false 0/N.
        if should_skip_file(py_file):
            continue

        try:
            original = py_file.read_text(errors="replace")
            result = stubber.transform_source(original, str(rel))

            if result is not None and result != original:
                # Syntax backstop: the CLI path (stub_file) re-parses the stub and
                # reverts on SyntaxError; the prepare path bypassed it, so a
                # corrupted deep submodule could be committed as base and only a
                # top-level import would (maybe) catch it. Validate here too.
                try:
                    ast.parse(result)
                except SyntaxError as se:
                    logger.warning("  Stub of %s is not valid python (%s); keeping original", rel, se)
                    errors += 1
                    continue
                py_file.write_text(result, encoding="utf-8")
                stubbed_count += 1
        # T12/T19: previously `except Exception as e` swallowed everything and
        # incremented a silent error counter. Narrow to the exceptions the
        # stubber legitimately raises (I/O, AST/parse, type/attr errors from
        # visitor mismatches). A truly unexpected exception should bubble up
        # so the batch fails loudly instead of committing an incomplete stub.
        except (OSError, SyntaxError, ValueError, TypeError, AttributeError) as e:
            logger.warning("  Error stubbing %s: %s", rel, e)
            errors += 1

    logger.info("  Stubbed %d files (%d errors)", stubbed_count, errors)

    # Non-degenerate gate (general, deps-free): if NO source file had function
    # bodies stubbed, the agent has nothing to implement — the base already
    # passes, so the trajectory is worthless. This catches over-preservation
    # (an import-time/re-export/main-guard quirk shielding every function) or a
    # bad src_dir, EVEN when unrelated edits (docstring/spec) made the raw diff
    # non-empty. Fail loudly at prepare instead of emitting a silent 100%-at-draft.
    if stubbed_count == 0:
        raise RuntimeError(
            f"Degenerate stub for {full_name}: 0 files had function bodies stubbed "
            f"(errors={errors}). The task would be trivially already-solved. Check "
            f"src_dir detection and import-time/keep-as-stub preservation in tools/stub.py."
        )

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing — source may already be stubs?")
        base_commit = reference_commit
    else:
        # Verify that stubbing actually modified code (should have both + and - lines)
        git(repo_dir, "diff", "--cached", "--stat")
        diff_patch = git(repo_dir, "diff", "--cached")
        additions = sum(
            1
            for line in diff_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1
            for line in diff_patch.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        logger.info(
            "  Diff stats — lines added: %d, lines removed: %d", additions, deletions
        )
        # A correct stub changes code. Removal-heavy modes (bodies deleted, no
        # `pass` inserted) can legitimately have additions==0, so only fail when
        # NOTHING changed at all — requiring both >0 false-failed removal-only repos.
        if additions == 0 and deletions == 0:
            raise RuntimeError(
                f"Stubbing verification failed for {full_name}: "
                f"additions={additions}, deletions={deletions} (no change at all)."
            )

        git(
            repo_dir,
            "commit",
            "-m",
            "Commit 0",
        )
        base_commit = get_head_sha(repo_dir)

    logger.info("  Base commit (stubbed): %s", base_commit[:12])

    # F2b: batch py_compile gate on the whole stubbed src tree. Per-file
    # ast.parse already gated syntax BEFORE writing each stub above. This is a
    # batch-level defense that catches (a) rare edge cases where sibling files
    # interact badly, (b) files added by non-stubber paths (spec, gitignore) that
    # accidentally contain invalid syntax, (c) mismatches between the interpreter
    # running prep vs the interpreter that will run the batch. Logs WARNING but
    # does NOT abort — the per-file gate is authoritative; this is diagnostic
    # signal for the operator so a degraded dataset row is visible in prep logs.
    _abs_src = repo_dir / src_dir if not (repo_dir / src_dir).is_absolute() else Path(src_dir)
    if _abs_src.exists() and any(_abs_src.rglob("*.py")):
        try:
            _proc = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", "-j", "0", str(_abs_src)],
                capture_output=True, text=True, timeout=300,
            )
            if _proc.returncode != 0:
                _tail = (_proc.stderr or _proc.stdout or "").strip().splitlines()[-20:]
                logger.warning(
                    "  F2b batch py_compile FAILED for %s (rc=%d). Per-file ast.parse "
                    "gate passed, but batch compileall rejected the tree — investigate "
                    "the following files before shipping this dataset row:\n    %s",
                    full_name, _proc.returncode, "\n    ".join(_tail),
                )
            else:
                logger.info("  F2b batch py_compile OK (whole stubbed tree compiles).")
        except (subprocess.TimeoutExpired, OSError) as _e:
            logger.warning("  F2b batch py_compile could not run: %s", _e)

    return base_commit, reference_commit


def quick_import_check(repo_dir: Path, src_dir: str) -> tuple[bool, str]:
    """Check if stubbed code can be imported without errors.

    Returns (success, error_message).
    """
    # Derive package name from src_dir
    # src_dir could be "src/package_name" or "package_name"
    parts = src_dir.split("/")
    package_name = parts[-1]

    # src_dir="." / "" is a single-file module at the repo root (e.g. pycodestyle.py):
    # the importable name is the repo directory name, not "." — `import .` is a
    # SyntaxError and would falsely record base_compiles=False for a valid stub.
    if package_name in (".", ""):
        package_name = repo_dir.name

    # Some packages use hyphens in dir names but underscores in imports
    import_name = package_name.replace("-", "_")

    try:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        # For src-layout packages (e.g. src/wtforms/), Python needs PYTHONPATH.
        # MUST be ABSOLUTE: the subprocess runs with cwd=repo_dir, so a relative
        # "repos_staging/x/src" would resolve against cwd and double-nest, making
        # the import spuriously fail (a false base_compiles=False for every
        # src-layout repo when --clone-dir is relative).
        src_layout_dir = (repo_dir / "src").resolve()
        if src_layout_dir.is_dir():
            env["PYTHONPATH"] = str(src_layout_dir)
        result = subprocess.run(
            [sys.executable, "-c", f"import {import_name}"],
            cwd=str(repo_dir.resolve()),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if result.returncode == 0:
            return True, ""
        error = (
            result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
        )
        # Missing external dep (e.g. parso for jedi) is inconclusive, not a stub failure
        if "No module named" in error:
            missing = ""
            if "'" in error:
                parts_q = error.split("'")
                if len(parts_q) >= 2:
                    missing = parts_q[1]
            if missing and not missing.startswith(import_name):
                logger.info(
                    "  Import check inconclusive: external dep '%s' missing (not a stub issue)",
                    missing,
                )
                return True, f"inconclusive: external dep '{missing}' not installed"
        return False, error
    except subprocess.TimeoutExpired:
        return False, "import timed out after 30s"
    except Exception as e:
        logger.warning("Non-critical failure during import check: %s", e)
        return False, str(e)


# ─── Setup/Test Dict Generation ──────────────────────────────────────────────


def _parse_dep_name(dep_str: str) -> str:
    """Extract package name from a PEP 508 dependency string."""
    return re.split(r"[><=!~;\s\[]", dep_str.strip())[0].strip().lower()


def _add_dep(deps: dict[str, str], raw: str) -> None:
    """Add a dependency to *deps*, keyed by normalized name, preserving the full spec."""
    # Strip inline comments (e.g., "tornado>=6.3.2 # pinned by Snyk")
    spec = raw.split("#")[0].strip()
    if not spec:
        return
    name = _parse_dep_name(spec)
    if name:
        deps.setdefault(name, spec)


def extract_all_dependencies(repo_dir: Path) -> tuple[list[str], list[str]]:
    """Extract both runtime and test dependencies from all config formats.

    Reads pyproject.toml, setup.cfg, setup.py, and requirements*.txt.

    Returns
    -------
        (runtime_deps, test_deps) — each is a sorted list of full dependency
        strings (preserving version pins, extras, and markers).  Sorting uses
        the normalized package name as key.

    """
    runtime: dict[str, str] = {}
    test: dict[str, str] = {
        "pytest": "pytest",
        "pytest-json-report": "pytest-json-report",
    }
    test_group_names = {"test", "testing", "tests", "dev"}

    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib

            with open(pyproject, "rb") as f:
                data = tomllib.load(f)

            for dep in data.get("project", {}).get("dependencies", []):
                _add_dep(runtime, dep)

            optional_deps = data.get("project", {}).get("optional-dependencies", {})
            for group_name in test_group_names:
                for dep_str in optional_deps.get(group_name, []):
                    _add_dep(test, dep_str)

            dep_groups = data.get("dependency-groups", {})
            for group_name in test_group_names:
                for dep_entry in dep_groups.get(group_name, []):
                    if isinstance(dep_entry, str):
                        _add_dep(test, dep_entry)
        except Exception as e:
            logger.debug("  Could not parse pyproject.toml for deps: %s", e)

    setup_cfg = repo_dir / "setup.cfg"
    if setup_cfg.exists():
        try:
            import configparser

            cfg = configparser.ConfigParser()
            cfg.read(setup_cfg, encoding="utf-8")

            if cfg.has_option("options", "install_requires"):
                for line in cfg.get("options", "install_requires").strip().splitlines():
                    _add_dep(runtime, line)

            if cfg.has_section("options.extras_require"):
                for group_name in test_group_names:
                    if cfg.has_option("options.extras_require", group_name):
                        for line in (
                            cfg.get("options.extras_require", group_name)
                            .strip()
                            .splitlines()
                        ):
                            _add_dep(test, line)
        except Exception as e:
            logger.debug("  Could not parse setup.cfg for deps: %s", e)

    setup_py = repo_dir / "setup.py"
    if setup_py.exists():
        try:
            content = setup_py.read_text(errors="replace")
            m = re.search(r"install_requires\s*=\s*\[(.*?)\]", content, re.DOTALL)
            if m:
                for dep_match in re.findall(r"""['"]([^'"]+)['"]""", m.group(1)):
                    _add_dep(runtime, dep_match)

            m = re.search(r"tests_require\s*=\s*\[(.*?)\]", content, re.DOTALL)
            if m:
                for dep_match in re.findall(r"""['"]([^'"]+)['"]""", m.group(1)):
                    _add_dep(test, dep_match)
        except Exception as e:
            logger.debug("  Could not parse setup.py for deps: %s", e)

    req_runtime_files = ["requirements.txt"]
    req_test_files = [
        "requirements-test.txt",
        "requirements-tests.txt",
        "requirements-dev.txt",
        "requirements_test.txt",
        "requirements_dev.txt",
    ]
    for filename in req_runtime_files:
        _read_requirements_file(repo_dir / filename, runtime)
    for filename in req_test_files:
        _read_requirements_file(repo_dir / filename, test)

    return (
        sorted(runtime.values(), key=lambda s: _parse_dep_name(s).lower()),
        sorted(test.values(), key=lambda s: _parse_dep_name(s).lower()),
    )


def _read_requirements_file(req_file: Path, target: dict[str, str]) -> None:
    """Read a requirements.txt-style file and add full dep specs to *target*."""
    if not req_file.exists():
        return
    try:
        for line in req_file.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            _add_dep(target, line)
    except Exception as e:
        logger.debug("Non-critical failure during requirements file parsing: %s", e)


def extract_test_dependencies(repo_dir: Path) -> list[str]:
    """Extract test dependencies from project config files.

    Backward-compatible wrapper around :func:`extract_all_dependencies`.
    Returns the union of runtime and test deps as a single sorted list
    of full dependency strings (with version pins preserved).
    """
    runtime_deps, test_deps = extract_all_dependencies(repo_dir)
    merged: dict[str, str] = {}
    for spec in runtime_deps + test_deps:
        name = _parse_dep_name(spec)
        if name:
            merged.setdefault(name, spec)
    return sorted(merged.values(), key=lambda s: _parse_dep_name(s).lower())


def _write_kaiju_breadcrumb(
    *,
    repo_dir: Path,
    full_name: str,
    reference_commit: str,
    setup_dict: dict,
    test_dict: dict,
) -> Path | None:
    """Drop a ``.kaiju/entries.json`` breadcrumb inside ``repo_dir``.

    Lets downstream invocations of :mod:`tools.generate_test_ids` in
    ``--repo-dir`` mode discover the per-repo Python version, test
    directory, and reference commit — without depending on the calling
    orchestrator to pass them explicitly. See Oracle review in
    ``MISSING_TEST_IDS_BZ2_ISSUE.md`` and ``tools/generate_test_ids.py``
    discovery chain.

    Writes only the fields downstream consumers need (stripped subset).
    Never raises — prepare_repo is the lifecycle owner, breadcrumb is a
    best-effort hint.
    """
    try:
        kaiju_dir = repo_dir / ".kaiju"
        kaiju_dir.mkdir(parents=True, exist_ok=True)
        target = kaiju_dir / "entries.json"
        payload = {
            "repo": full_name,
            "reference_commit": reference_commit,
            "setup": setup_dict,
            "test": test_dict,
            "_schema": "kaiju-breadcrumb/1",
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target
    except OSError as exc:
        logger.warning(
            "Could not write .kaiju/entries.json breadcrumb in %s: %s",
            repo_dir, exc,
        )
        return None



# A quoted, UPPER_SNAKE env-var name in setup.py that toggles OFF native/Cython
# extensions (e.g. BlackSheep's BLACKSHEEP_NO_EXTENSIONS). Used to keep the base
# pure-Python — see _detect_extension_skip_env.
_EXT_SKIP_ENV_RE = re.compile(
    r"""["']([A-Z][A-Z0-9_]*"""
    r"""(?:NO_EXTENSION|NO_CYTHON|DISABLE_EXT|SKIP_CYTHON|WITHOUT_CYTHON|PURE_PYTHON)"""
    r"""[A-Z0-9_]*)["']"""
)


def _detect_extension_skip_env(setup_py: "Path") -> str | None:
    """Return the env-var name setup.py uses to SKIP native/Cython extensions, if any.

    Cython repos (e.g. BlackSheep) ship pure-Python ``.py`` modules AND compiled
    ``.pyx``/``.c`` siblings. Building the extensions (a) FAILS when the generated
    ``.c`` files aren't committed (``<mod>.c: No such file or directory``) and,
    worse, (b) the compiled ``.so`` SHADOWS the stubbed ``.py`` at import time, so
    tests would run the ORIGINAL compiled code, not the agent's stub — a silent
    cheat. Many such repos expose an env switch (for PyPy) to disable extensions
    and fall back to the ``.py`` modules; setting it makes the stubbed source
    authoritative and the build succeed.
    """
    try:
        text = setup_py.read_text(errors="replace")
    except OSError:
        return None
    # Only treat it as a skip switch if the file actually builds extensions.
    if "Extension(" not in text and "ext_modules" not in text and "cythonize" not in text:
        return None
    m = _EXT_SKIP_ENV_RE.search(text)
    return m.group(1) if m else None


def generate_setup_dict(repo_dir: Path, full_name: str) -> dict:
    """Generate the 'setup' dict for a RepoInstance.

    Inspects pyproject.toml/setup.py/setup.cfg for install instructions.
    """
    setup: dict = {
        "install": "",
        "packages": "",
        "pip_packages": [],
        "pre_install": [],
        "python": "",
        "specification": "",
        "version_source": "",
        "version_conflicts": [],
        "system_deps_hint": [],
    }

    full_name.split("/")[-1]

    # Detect Python version via canonical detector (see tools/python_version.py)
    from commit0.harness.constants import (
        DEFAULT_PYTHON_VERSION,
        SUPPORTED_PYTHON_VERSIONS,
    )
    try:
        det = detect_python_version_result(
            repo_dir,
            SUPPORTED_PYTHON_VERSIONS,
            fallback=DEFAULT_PYTHON_VERSION,
        )
        setup["python"] = det.version or DEFAULT_PYTHON_VERSION
        setup["version_source"] = det.source
        setup["version_conflicts"] = det.conflicts
    except VersionConflictError as exc:
        logger.error("Python version conflict for %s: %s", full_name, exc)
        # Fall back to default but record the conflict so downstream tooling sees it
        setup["python"] = DEFAULT_PYTHON_VERSION
        setup["version_source"] = "conflict-fallback"
        setup["version_conflicts"] = [
            f"{src}: {reason}" for src, reason in exc.rejecting_sources.items()
        ]
    except NoSignalsError:  # only raised with strict=True; defensive
        setup["python"] = DEFAULT_PYTHON_VERSION
        setup["version_source"] = "default"

    # Detect install method
    pyproject = repo_dir / "pyproject.toml"
    setup_py = repo_dir / "setup.py"

    if pyproject.exists():
        content = pyproject.read_text(errors="replace")

        # Detect extras
        extras = []
        for name in ["test", "testing", "tests", "dev", "develop", "all"]:
            if re.search(rf"\b{name}\b\s*=\s*\[", content):
                extras.append(name)

        if extras:
            # Prefer test extras over dev (less bloat)
            test_extras = [e for e in extras if e in ("test", "testing", "tests")]
            if test_extras:
                setup["install"] = f'pip install -e ".[{",".join(test_extras)}]"'
            else:
                setup["install"] = f'pip install -e ".[{extras[0]}]"'
        else:
            setup["install"] = 'pip install -e "."'

        setup["pip_packages"] = extract_test_dependencies(repo_dir)

    elif setup_py.exists():
        setup["install"] = 'pip install -e "."'
        setup["pip_packages"] = extract_test_dependencies(repo_dir)

    else:
        # Requirements files
        req_files = []
        for f in [
            "requirements.txt",
            "requirements-dev.txt",
            "requirements-test.txt",
            "requirements-tests.txt",
        ]:
            if (repo_dir / f).exists():
                req_files.append(f)
        if req_files:
            setup["install"] = " && ".join(f"pip install -r {f}" for f in req_files)
        setup["pip_packages"] = extract_test_dependencies(repo_dir)

    # Cython/native-extension repos: if setup.py can disable extensions via an env
    # var, PREFIX the install with it so (1) the build doesn't fail on missing
    # generated .c files and (2) the compiled .so doesn't shadow the stubbed .py at
    # import time (which would test the ORIGINAL code — a silent cheat). The prefix
    # rides on setup["install"], so it applies at BOTH docker-build and eval time.
    if setup_py.exists() and setup.get("install"):
        _skip_var = _detect_extension_skip_env(setup_py)
        if _skip_var:
            setup["install"] = f"{_skip_var}=1 {setup['install']}"
            logger.info(
                "  Native-extension repo: disabling extensions via %s=1 so the "
                "stubbed pure-Python source is authoritative (not the compiled "
                "original) and the build doesn't need generated .c files.", _skip_var,
            )

    # PyPI pre-flight: drop deps that cannot install on public PyPI (non-existent
    # / private / typo names) and warn on unsatisfiable version pins BEFORE the
    # 60-180s docker build turns them into a cryptic "No matching distribution".
    # Best-effort + fail-open; disable with KAIJU_SKIP_PYPI_PREFLIGHT=1.
    _extracted = setup.get("pip_packages", [])
    if _extracted:
        try:
            from tools.pypi_preflight import check_pip_packages

            _kept, _report = check_pip_packages(_extracted, logger=logger)
            if _report.dropped:
                logger.warning(
                    "  PyPI pre-flight dropped %d non-installable dep(s) for %s: %s",
                    len(_report.dropped), full_name,
                    ", ".join(name for name, _ in _report.dropped),
                )
            setup["pip_packages"] = _kept
        except Exception as _e:  # noqa: BLE001 - never block prepare on the check
            logger.debug("PyPI pre-flight skipped (%s)", _e)

    from commit0.harness.dockerfiles import detect_system_dependencies

    apt_pkgs = detect_system_dependencies(setup.get("pip_packages", []))
    pre_install = []
    if apt_pkgs:
        pre_install.append(f"apt-get install -y {' '.join(apt_pkgs)}")
    setup["pre_install"] = pre_install

    # Detect system-level dependencies in test imports (QGIS, GTK, Qt, etc.)
    # This is a hint, not authoritative — downstream collection still catches misses.
    try:
        sys_deps = scan_repo_for_system_deps(repo_dir)
        if sys_deps:
            setup["system_deps_hint"] = sys_deps
    except Exception:  # noqa: BLE001 - scanner must never block prepare
        logger.exception("system_deps_scanner crashed on %s", full_name)

    # Documentation URL
    homepage = _find_docs_url(repo_dir, full_name)
    if homepage:
        setup["specification"] = homepage

    return setup


def generate_test_dict(repo_dir: Path, test_dir: str | None) -> dict:
    """Generate the 'test' dict for a RepoInstance."""
    test = {
        "test_cmd": "pytest",
        "test_dir": test_dir or "tests",
    }

    # Check for custom pytest config
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(errors="replace")
        # Look for testpaths
        m = re.search(r"testpaths\s*=\s*\[([^\]]+)\]", content)
        if m:
            paths = re.findall(r'"([^"]+)"', m.group(1))
            if paths:
                test["test_dir"] = paths[0]

    # Check for pytest.ini or setup.cfg with [tool:pytest]
    for cfg_name in ["pytest.ini", "setup.cfg"]:
        cfg = repo_dir / cfg_name
        if cfg.exists():
            content = cfg.read_text(errors="replace")
            m = re.search(r"testpaths\s*=\s*(.+)", content)
            if m:
                test["test_dir"] = m.group(1).strip().split()[0]
                break

    return test


def _detect_python_version(repo_dir: Path) -> str | None:
    """Backward-compatible thin wrapper around :func:`tools.python_version.detect`."""
    from commit0.harness.constants import (
        DEFAULT_PYTHON_VERSION,
        SUPPORTED_PYTHON_VERSIONS,
    )
    try:
        return detect_python_version_result(
            repo_dir,
            SUPPORTED_PYTHON_VERSIONS,
            fallback=DEFAULT_PYTHON_VERSION,
        ).version
    except (VersionConflictError, NoSignalsError):
        return None


def _find_docs_url(repo_dir: Path, full_name: str) -> str:
    """Try to find a scrapeable documentation URL.

    Returns empty string if no valid docs URL can be determined.
    """
    pyproject = repo_dir / "pyproject.toml"
    candidates: list[tuple[str, str]] = []
    found_any_url = False

    if pyproject.exists():
        content = pyproject.read_text(errors="replace")
        doc_match = re.search(
            r'[Dd]ocumentation\s*=\s*["\']([^"\']+)["\']', content
        )
        if doc_match:
            candidates.append((doc_match.group(1), "documentation"))
            found_any_url = True

        home_match = re.search(
            r'[Hh]omepage\s*=\s*["\']([^"\']+)["\']', content
        )
        if home_match:
            candidates.append((home_match.group(1), "homepage"))
            found_any_url = True

    if not found_any_url:
        repo_name = full_name.split("/")[-1]
        candidates.append((f"https://{repo_name}.readthedocs.io/", "readthedocs_guess"))

    for url, source in candidates:
        if not _is_scrapeable_url(url, source):
            continue
        return url

    logger.warning("  No scrapeable docs URL found for %s", full_name)
    return ""


_BLOCKED_DOMAINS = frozenset(
    ["github.com", "github.io", "gitlab.com", "bitbucket.org", "pypi.org"]
)


def _is_scrapeable_url(url: str, source: str) -> bool:
    """Determine if a docs URL is likely to be successfully scraped."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    # Reject code hosting sites — Playwright gets blocked by bot detection
    if any(blocked in domain for blocked in _BLOCKED_DOMAINS):
        logger.info("  Skipping %s URL (blocked domain): %s", source, url)
        return False

    # Homepage fallback is unreliable — often a marketing site, not docs
    if source == "homepage":
        logger.info("  Skipping homepage URL (unreliable for docs): %s", url)
        return False

    # readthedocs guess — verify it exists with a quick HEAD request
    if source == "readthedocs_guess":
        try:
            import requests as _requests

            resp = _requests.head(url, timeout=10, allow_redirects=True)
            if resp.status_code >= 400:
                logger.info(
                    "  Skipping readthedocs guess (HTTP %d): %s", resp.status_code, url
                )
                return False
        except Exception:
            logger.info("  Skipping readthedocs guess (unreachable): %s", url)
            return False

    return True


# ─── Dataset Entry ────────────────────────────────────────────────────────────


def create_dataset_entry(
    full_name: str,
    fork_name: str,
    base_commit: str,
    reference_commit: str,
    src_dir: str,
    setup_dict: dict,
    test_dict: dict,
    pinned_tag: str | None = None,
    base_compiles: "bool | None" = None,
) -> dict:
    repo_name = full_name.split("/")[-1]

    entry = {
        "instance_id": f"commit-0/{repo_name}",
        "id": str(_uuid_mod.uuid4()),
        "repo": fork_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "setup": setup_dict,
        "test": test_dict,
        "src_dir": src_dir or "",
        # A11: does the STUBBED base import cleanly? True/False/None(inconclusive).
        # Mirrors go/rust base_compiles so a 0% from a broken base is distinguishable
        # from a genuine model failure at eval time.
        "base_compiles": base_compiles,
    }
    if pinned_tag:
        entry["pinned_tag"] = pinned_tag
    return entry


# ─── Push to Fork ────────────────────────────────────────────────────────────


def resolve_commits_from_remote(fork_name: str, branch: str) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{fork_name}/branches/{branch}"],
            capture_output=True,
            text=True,
            timeout=30,
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
        )
        if result.returncode != 0:
            return None
        commit_data = json.loads(result.stdout)
        parent_sha = commit_data["parents"][0]["sha"]

        return (sha, parent_sha)
    except Exception as e:
        logger.debug("Non-critical failure during remote commit resolution: %s", e)
        return None




# ─── Main ────────────────────────────────────────────────────────────────────


def prepare_repos(
    candidates: list[dict],
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    dry_run: bool = False,
    max_repos: int | None = None,
    removal_mode: str = "all",
    specs_dir: str = "./specs",
) -> list[dict]:
    """Prepare repos for the dataset."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN is required but not set. "
            "Export GITHUB_TOKEN before running prepare_repos."
        )
    entries: list[dict] = []

    try:
        _get_scrape_func()
    except ImportError as e:
        logger.warning(
            "Spec scraping dependencies not installed (%s). "
            "Datasets will be created without spec PDFs. "
            "Install with: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests",
            e,
        )

    for i, candidate in enumerate(candidates):
        if max_repos and i >= max_repos:
            break

        # Skip candidates that didn't pass validation
        status = candidate.get("status", "")
        if status in ("fail", "clone_failed", "analysis_failed", "pending"):
            logger.info(
                "Skipping candidate %s (status=%s)", candidate["full_name"], status
            )
            continue

        full_name = candidate["full_name"]
        analysis = candidate.get("analysis") or {}
        src_dir = analysis.get("src_dir")
        test_dir = analysis.get("test_dir")

        logger.info(
            "\n[%d/%d] Preparing %s...",
            i + 1,
            min(len(candidates), max_repos or len(candidates)),
            full_name,
        )

        # Fork
        if dry_run:
            fork_name = f"{org}/{full_name.split('/')[-1]}"
            logger.info("  [DRY RUN] Would fork to %s", fork_name)
        else:
            try:
                fork_name = fork_repo(full_name, org, token=token)
            except Exception as e:
                logger.error("  Fork failed: %s", e)
                continue

        # Full clone (pinned to release tag if available)
        release_tag = candidate.get("release_tag") or analysis.get("release_tag")
        try:
            repo_dir = full_clone(full_name, clone_dir, tag=release_tag)
            if release_tag:
                logger.info("  Pinned to tag: %s", release_tag)
        except Exception as e:
            logger.error("  Clone failed: %s", e)
            continue

        src_dir = candidate.get("src_dir_override") or src_dir
        if not src_dir:
            src_dir = detect_src_dir(repo_dir, full_name)
            if src_dir:
                logger.info("  Auto-detected src_dir: %s", src_dir)

        if not src_dir:
            # T16: this used to log "FATAL" but call continue — which the batch
            # driver reads as a soft skip. The FATAL label made it look like a
            # hard stop and hid the real state (candidate rejected, batch
            # continues). Reword so log-scanners see it correctly, and record
            # the rejection reason on the entry so downstream tools can report.
            logger.error(
                "  SKIP: src_dir is empty for %s. Cannot determine source"
                " directory. Use --src-dir to specify manually. Candidate is"
                " excluded from this batch; other repos will still be processed.",
                full_name,
            )
            continue

        # Create stubbed branch
        try:
            base_commit, reference_commit = create_stubbed_branch(
                repo_dir,
                full_name,
                src_dir,
                removal_mode=removal_mode,
            )
        except Exception as e:
            logger.error("  Stubbing failed: %s", e)
            continue

        # A11: does the STUBBED base still import? The repo HEAD is the stubbed
        # base here (before the analysis checkout below). A correct stub keeps
        # imports/signatures intact, so it should import.
        #   * clean import                 -> True
        #   * "inconclusive" (a missing EXTERNAL dep) -> True: quick_import_check
        #     only reports inconclusive when the package's OWN stubbed code imported
        #     fine and the missing module is a third-party dep — which IS present in
        #     the eval env. So the stubbed base is a valid starting point. (Combined
        #     with the ast.parse gate in the stub loop, syntax validity is already
        #     guaranteed, so this won't hide a corrupted stub.)
        #   * real import failure          -> False
        #   * check couldn't run           -> None (unknown)
        try:
            _imp_ok, _imp_msg = quick_import_check(repo_dir, src_dir or "")
            base_compiles = bool(_imp_ok)  # True for clean import AND external-dep-inconclusive
            logger.info("  A11 stubbed-base import: base_compiles=%s%s",
                        base_compiles, f" ({_imp_msg})" if _imp_msg else "")
        except Exception as _imp_e:  # noqa: BLE001 - best-effort provenance
            base_compiles = None
            logger.warning("  A11 import check could not run (%s); recording unknown.", _imp_e)

        # Generate setup/test dicts on the SAME un-stubbed commit the base is
        # derived from (reference_commit) — the pinned release tag when one was
        # checked out, else the default-branch tip. Checking out the default branch
        # here analyzed the WRONG code (deps / python version / test layout) for
        # tag-pinned repos, so the entry's setup/test could mismatch its base.
        git(repo_dir, "checkout", reference_commit)

        setup_dict = generate_setup_dict(repo_dir, full_name)
        test_dict = generate_test_dict(repo_dir, test_dir)

        # Scrape spec PDF and commit into repo
        spec_path = None
        if setup_dict.get("specification") and not dry_run:
            repo_name = full_name.split("/")[-1]
            docs_url = setup_dict["specification"]
            logger.info("  Scraping spec from: %s", docs_url)
            try:
                scrape_fn = _get_scrape_func()
                spec_path = scrape_fn(
                    base_url=docs_url,
                    name=repo_name,
                    output_dir=specs_dir,
                    compress=True,
                )
                if spec_path:
                    logger.info("  Spec saved: %s", spec_path)
                    branch_name = REMOTE_BRANCH
                    git(repo_dir, "checkout", branch_name)
                    dest = repo_dir / "spec.pdf.bz2"
                    shutil.copy2(spec_path, dest)
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_name}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info("  Updated base_commit with spec: %s", base_commit[:12])
                else:
                    logger.warning("  Spec scraping returned no output")
            except Exception as e:
                logger.warning("  Spec scraping failed: %s", e)
        if spec_path is None and not dry_run:
            _repo_name = full_name.split("/")[-1]
            try:
                from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
                readme_spec_path, readme_spec_url = _scrape_readme_spec(repo_dir, specs_dir, _repo_name)
            except ImportError:
                readme_spec_path, readme_spec_url = None, ""
            if readme_spec_path:
                if readme_spec_url:
                    setup_dict["specification"] = readme_spec_url
                try:
                    git(repo_dir, "checkout", REMOTE_BRANCH)
                    shutil.copy2(str(readme_spec_path), str(repo_dir / "spec.pdf.bz2"))
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add README-based spec for {_repo_name}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info("  README spec committed: %s", base_commit[:12])
                except Exception as e:
                    logger.warning("  README spec fallback failed: %s", e)

        _final_spec_source = "docs" if spec_path else ("readme" if 'readme_spec_path' in locals() and readme_spec_path else "none")
        from tools.scrape_pdf import enforce_strict_spec_mode as _enforce_strict_spec
        _enforce_strict_spec(_final_spec_source, full_name.split("/")[-1])

        # Push to fork
        if not dry_run:
            branch_name = REMOTE_BRANCH
            try:
                git(repo_dir, "checkout", branch_name)
                push_to_fork(repo_dir, fork_name, branch=branch_name, token=token)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                remote_commits = resolve_commits_from_remote(fork_name, branch_name)
                if remote_commits:
                    base_commit, reference_commit = remote_commits
                    logger.info(
                        "  Resolved commits from remote: base=%s, ref=%s",
                        base_commit[:12],
                        reference_commit[:12],
                    )
                else:
                    raise RuntimeError(
                        f"Push to {fork_name} FAILED and no usable '{branch_name}' "
                        f"branch exists on the fork. The container build clones this "
                        f"fork and fetches base/reference commits from it, so a "
                        f"dataset built from un-pushed local commits is UNBUILDABLE "
                        f"('not our ref'). Ensure your token has WRITE access to the "
                        f"fork org (run_trajectory.sh: --org / $KAIJU_FORK_ORG).\n"
                        f"Original push error: {e}"
                    ) from e

        # Create dataset entry
        entry = create_dataset_entry(
            full_name=full_name,
            fork_name=fork_name,
            base_commit=base_commit,
            reference_commit=reference_commit,
            src_dir=src_dir or "",
            setup_dict=setup_dict,
            test_dict=test_dict,
            pinned_tag=release_tag,
            base_compiles=base_compiles,
        )

        # Write a breadcrumb file inside the cloned repo so that
        # `tools.generate_test_ids --repo-dir` can later auto-discover the
        # exact test_dir / python / reference_commit it should use — even
        # when invoked by an external orchestrator (e.g. Argo) that doesn't
        # pass the relevant flags. See MISSING_TEST_IDS_BZ2_ISSUE.md.
        _write_kaiju_breadcrumb(
            repo_dir=repo_dir,
            full_name=full_name,
            reference_commit=reference_commit,
            setup_dict=setup_dict,
            test_dict=test_dict,
        )

        logger.info("  Entry created: instance_id=%s", entry["instance_id"])
        logger.info(
            "  base_commit=%s, reference_commit=%s",
            base_commit[:12],
            reference_commit[:12],
        )
        entries.append(entry)

    return entries


def print_entries_summary(entries: list[dict]) -> None:
    """Print summary of prepared dataset entries."""
    print(f"\n{'=' * 90}")
    print(f"PREPARED ENTRIES: {len(entries)}")
    print(f"{'=' * 90}\n")

    print(
        f"{'#':>3}  {'instance_id':<35} {'original_repo':<35} {'python':>7} {'base_commit':>12}"
    )
    print("-" * 100)

    for i, e in enumerate(entries, 1):
        print(
            f"{i:>3}  {e['instance_id']:<35} {e['original_repo']:<35} "
            f"{e['setup'].get('python', '?'):>7} {e['base_commit'][:12]:>12}"
        )

    print(f"\n{'=' * 90}")


def _run_detect_only(
    candidates: list[dict],
    clone_dir: Path,
    report_path: str | None,
) -> None:
    """Run version + system-dep detection on each candidate without prepping."""
    from commit0.harness.constants import (
        DEFAULT_PYTHON_VERSION,
        SUPPORTED_PYTHON_VERSIONS,
    )

    rows: list[dict] = []
    for c in candidates:
        full_name = c.get("full_name") or c.get("repo", "")
        try:
            _validate_github_full_name(full_name)  # T6: path-traversal guard
        except ValueError as _exc:
            rows.append({"repo": full_name, "version": None, "source": f"invalid-name: {_exc}"})
            continue
        repo_dir = clone_dir / full_name.replace("/", "__")
        if not repo_dir.is_dir():
            rows.append(
                {
                    "repo": full_name,
                    "version": None,
                    "source": "missing-clone",
                    "conflicts": [],
                    "system_deps": [],
                    "all_signals": {},
                }
            )
            continue
        try:
            det = detect_python_version_result(
                repo_dir,
                SUPPORTED_PYTHON_VERSIONS,
                fallback=DEFAULT_PYTHON_VERSION,
            )
            version = det.version
            source = det.source
            conflicts = det.conflicts
            signals = det.all_signals
        except VersionConflictError as exc:
            version = None
            source = "conflict"
            conflicts = [
                f"{src}: {reason}" for src, reason in exc.rejecting_sources.items()
            ]
            signals = {}
        except NoSignalsError:
            version = None
            source = "no-signals"
            conflicts = []
            signals = {}

        try:
            sys_deps = scan_repo_for_system_deps(repo_dir)
        except Exception:  # noqa: BLE001
            sys_deps = []

        rows.append(
            {
                "repo": full_name,
                "version": version,
                "source": source,
                "conflicts": conflicts,
                "system_deps": sys_deps,
                "all_signals": signals,
            }
        )
        logger.info(
            "  %-45s py=%s src=%s sys_deps=%s",
            full_name,
            version or "?",
            source,
            sys_deps or "-",
        )

    if report_path:
        out = Path(report_path)
        if out.suffix == ".csv":
            import csv

            with out.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    ["repo", "version", "source", "conflicts", "system_deps"]
                )
                for r in rows:
                    writer.writerow(
                        [
                            r["repo"],
                            r["version"] or "",
                            r["source"],
                            "; ".join(r["conflicts"]),
                            "; ".join(r["system_deps"]),
                        ]
                    )
        else:
            out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        logger.info("Wrote detection report to %s", out)

def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare repos for commit0 dataset")
    parser.add_argument(
        "validated_file",
        nargs="?",
        help="Input validated.json from validate.py",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Prepare a single repo (e.g., pallets/flask)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="dataset_entries.json",
        help="Output JSON file (default: dataset_entries.json)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default="./repos_staging",
        help="Directory to clone repos into (default: ./repos_staging)",
    )
    parser.add_argument(
        "--org",
        type=str,
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip forking and pushing (just clone, stub, generate entries)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to prepare",
    )
    parser.add_argument(
        "--removal-mode",
        type=str,
        choices=["all", "docstring", "combined"],
        default="all",
        help="Stub removal mode: all (replace ALL function bodies with a stub, keep every "
        "signature — DEFAULT; keeps the base structurally complete), docstring (only "
        "functions with docstrings), combined (stub documented + REMOVE undocumented "
        "functions entirely — commit0-paper methodology; can break the base).",
    )
    parser.add_argument(
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default=None,
        help="Pin to a specific git tag (overrides auto-detected release_tag)",
    )
    parser.add_argument(
        "--commit",
        type=str,
        default=None,
        help="Pin to a specific git commit SHA",
    )
    parser.add_argument(
        "--src-dir",
        type=str,
        default=None,
        help="Source directory within repo (e.g., 'src/flask'). Auto-detected if omitted.",
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Skip cloning/forking; just run version + system-dep detection and exit.",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        help="Write detection results to this JSON/CSV file (CSV if path ends in .csv).",
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

    # Load candidates
    if args.repo:
        candidates = [
            {
                "full_name": args.repo,
                "name": args.repo.split("/")[-1],
                "owner": args.repo.split("/")[0],
                "stars": 0,
                "default_branch": "main",
                "status": "pass",
                "analysis": None,
                "release_tag": args.tag or args.commit,
                "src_dir_override": args.src_dir,
            }
        ]
    elif args.validated_file:
        # T3 fix: guard against malformed validated file.
        try:
            candidates = json.loads(Path(args.validated_file).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            parser.error(f"Validated JSON at {args.validated_file} is malformed: {e}")
            return
    else:
        parser.error("Provide either validated_file or --repo")
        return

    # If analysis is missing (e.g., --repo mode), do quick analysis
    for c in candidates:
        if c.get("analysis") is None and c.get("status") != "fail":
            c["status"] = "pass"
            # Analysis will happen during prepare using src_dir detection

    clone_dir = Path(args.clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=True)

    if args.detect_only:
        _run_detect_only(candidates, clone_dir, args.report)
        return

    entries = prepare_repos(
        candidates,
        clone_dir=clone_dir,
        org=args.org,
        dry_run=args.dry_run,
        max_repos=args.max_repos,
        removal_mode=args.removal_mode,
        specs_dir=args.specs_dir,
    )

    # Save entries
    if _consolidated and entries and entries[0].get("id"):
        _uuid = entries[0]["id"]
        _out_dir = datasets_dir(_uuid)
        _entries_path = _out_dir / "entries.json"
        _entries_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        logger.info("Wrote %d entries to %s (consolidated)", len(entries), _entries_path)
        if args.output:
            output_path = Path(args.output)
            output_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            logger.info("Also wrote legacy copy to %s", output_path)
    else:
        output_path = Path(args.output)
        # A failed prepare (fork collision, clone error, etc.) yields 0 entries.
        # Do NOT overwrite an existing, non-empty dataset with an empty list —
        # that silently destroys a previously-good scoring artifact. Only write
        # empty output when there is nothing to clobber.
        if not entries and output_path.is_file():
            try:
                _prior = json.loads(output_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                _prior = None
            if _prior:
                logger.warning(
                    "Prepared 0 entries; REFUSING to overwrite existing non-empty "
                    "%s (%d prior entries kept). Fix the prepare failure above and retry.",
                    output_path, len(_prior),
                )
                print_entries_summary(entries)
                return
        output_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        logger.info("Saved %d entries to %s", len(entries), output_path)

    print_entries_summary(entries)


if __name__ == "__main__":
    main()
