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
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from kaiju.paths import REPO_ROOT, datasets_dir, spec_path as consolidated_spec_path
from typing import Iterator
import uuid as _uuid_mod

from commit0.harness.constants_js import (
    DEFAULT_NODE_VERSION,
    JS_BASE_BRANCH,
    JS_DATASET_BRANCH,
    SUPPORTED_NODE_VERSIONS,
    SUPPORTED_PACKAGE_MANAGERS,
)
from tools._git_auth import fork_repo, git, push_to_fork, setup_git_credentials
from tools.node_version import (
    detect as _detect_node_version,
    resolve_engines_floor as _resolve_engines_floor,
)
from tools.prepare_repo import (
    full_clone,
    get_default_branch,
    get_head_sha,
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

# Single-file test convention: a bare `test.js` (or `test.mjs`/`.cjs`) at the repo
# root, used by AVA and many small npm packages (e.g. sindresorhus/slugify). These
# are NOT covered by the `.test.js`/`.spec.js` SUFFIXES above (a bare `test.js`
# doesn't end with `.test.js`), so without this the test-dir detection returns
# nothing and prepare aborts with "Could not detect a test directory".
_TEST_FILE_NAMES: frozenset[str] = frozenset(
    {
        "test.js", "test.mjs", "test.cjs", "test.jsx",
        "test.ts", "test.mts", "test.cts", "test.tsx",
    }
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
        "ava",
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


# Install commands (frozen vs generating, per package manager, Yarn-generation
# aware) live in tools/_toolchain.resolve_install_command — the single source of
# truth shared with prepare_repo_ts so neither can drift into passing
# Yarn-Classic flags to a Yarn-Berry repo.

# Lockfile filename produced by each package manager, for detection + git add.
_LOCKFILE_BY_PM: dict[str, str] = {
    "npm": "package-lock.json",
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
    "bun": "bun.lockb",
}

# `.npmrc` directives that SUPPRESS lockfile generation. Repos that .gitignore
# their lockfile frequently also disable it here (`package-lock=false` for npm,
# `lockfile=false` for pnpm), so even a generating install writes no lockfile.
_NPMRC_LOCKFILE_DISABLE_RE = re.compile(
    r"^[ \t]*(?:package-lock|lockfile)[ \t]*=[ \t]*false[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


_STUB_MARKER = 'throw new Error("STUB")'


def _is_real_stub_marker(added_line: str) -> bool:
    """J5: skip diff-added lines that only MENTION the stub marker in a
    comment/docstring. Called from both the JS and TS preparers so both share
    the same context-aware detection (H4 lifted this to module scope so it can
    be imported by prepare_repo_ts)."""
    body = added_line[1:] if added_line.startswith("+") else added_line
    stripped = body.lstrip()
    if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
        return False
    idx = body.find(_STUB_MARKER)
    if idx < 0:
        return False
    line_comment_idx = body.find("//")
    if 0 <= line_comment_idx < idx:
        return False
    return True


def _neutralize_npmrc_lockfile_disable(repo_dir: Path) -> str | None:
    """Comment out any lockfile-disabling directive in ``.npmrc`` so a generating
    install can produce a lockfile. Package-manager-agnostic (any npmrc-reading PM:
    npm/pnpm/yarn). Returns the ORIGINAL file text so the caller can restore it
    verbatim afterwards, or ``None`` when no change was needed.
    """
    npmrc = repo_dir / ".npmrc"
    if not npmrc.exists():
        return None
    original = npmrc.read_text()
    neutralized = _NPMRC_LOCKFILE_DISABLE_RE.sub(
        lambda m: "# kaiju: neutralized for lockfile generation -> " + m.group(0).strip(),
        original,
    )
    if neutralized == original:
        return None
    npmrc.write_text(neutralized)
    return original


# A PM may emit more than one lockfile filename across versions (bun switched from
# binary `bun.lockb` to text `bun.lock` in 1.1+). Detection accepts ALL candidates;
# the generating install commits whichever was actually produced.
_EXTRA_LOCKFILE_NAMES: dict[str, tuple[str, ...]] = {
    "bun": ("bun.lock",),
}


def _lockfile_candidates(pm: str) -> tuple[str, ...]:
    return (_LOCKFILE_BY_PM[pm], *_EXTRA_LOCKFILE_NAMES.get(pm, ()))


def _has_committed_lockfile(repo_dir: Path) -> bool:
    all_names = {
        name for pm in _LOCKFILE_BY_PM for name in _lockfile_candidates(pm)
    }
    return any((repo_dir / name).exists() for name in all_names)


def _validate_generated_lockfile(lockfile_path: Path, pkg_manager: str) -> None:
    """J4: Reject obviously-broken lockfiles instead of committing them.

    A neutralized `.npmrc` retry (or a mid-install crash) can leave a lockfile
    that exists on disk but is truncated / partial / structurally invalid.
    The old check only asked `if lockfile_path is not None` — so a broken
    lockfile would get committed and every downstream `npm ci` would fail with
    an opaque parse error. Perform PM-specific structural sanity checks; raise
    RuntimeError on obvious brokenness so prep fails at the source instead.
    """
    try:
        stat = lockfile_path.stat()
    except OSError as exc:
        raise RuntimeError(
            f"lockfile {lockfile_path} disappeared before validation: {exc}"
        ) from exc
    if stat.st_size == 0:
        raise RuntimeError(
            f"lockfile {lockfile_path} is empty (0 bytes); {pkg_manager} install"
            " produced an unusable lockfile."
        )
    name = lockfile_path.name
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        try:
            data = json.loads(lockfile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"lockfile {lockfile_path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict) or "lockfileVersion" not in data:
            raise RuntimeError(
                f"lockfile {lockfile_path} lacks 'lockfileVersion' key; likely"
                " truncated or corrupt."
            )
        _pkgs = data.get("packages") or data.get("dependencies") or {}
        if not isinstance(_pkgs, dict) or not _pkgs:
            raise RuntimeError(
                f"lockfile {lockfile_path} has no packages/dependencies"
                " recorded; install did not resolve the graph."
            )
    elif name == "yarn.lock":
        head = lockfile_path.read_text(encoding="utf-8", errors="replace")[:4096]
        if "AUTOGENERATED" not in head and "__metadata" not in head:
            raise RuntimeError(
                f"lockfile {lockfile_path} missing yarn autogen header and"
                " __metadata block; likely corrupt."
            )
    elif name == "pnpm-lock.yaml":
        head = lockfile_path.read_text(encoding="utf-8", errors="replace")[:4096]
        if "lockfileVersion" not in head:
            raise RuntimeError(
                f"lockfile {lockfile_path} missing 'lockfileVersion' key; likely"
                " truncated."
            )
    elif name == "bun.lock":
        head = lockfile_path.read_text(encoding="utf-8", errors="replace")[:4096]
        if "lockfileVersion" not in head:
            raise RuntimeError(
                f"lockfile {lockfile_path} missing 'lockfileVersion' key; likely"
                " truncated."
            )
    elif name == "bun.lockb":
        if stat.st_size < 64:
            raise RuntimeError(
                f"lockfile {lockfile_path} is only {stat.st_size} bytes; a real"
                " bun.lockb is much larger."
            )


def _ensure_pkg_manager(pkg_manager: str) -> None:
    """Thin wrapper over tools._toolchain.ensure_pm (see TOOLCHAIN_PROVISIONING.md)."""
    from tools._toolchain import ToolchainError, ensure_pm
    try:
        ensure_pm(pkg_manager)
    except ToolchainError as exc:
        raise OSError(str(exc)) from exc


def _list_workspace_packages(repo_dir: Path) -> list[str]:
    """Enumerate workspace member packages, appending /src as heuristic src_dir.

    J8: previously this only checked hardcoded parent dirs (packages, apps, libs,
    modules). Monorepos with custom layouts (e.g. `workspaces: ["pkg/*"]` in
    package.json) were invisible, which caused whole-monorepo stubbing to
    exhaust memory. Now we ALSO parse package.json `workspaces` and expand the
    glob patterns via Path.glob() to catch non-standard layouts."
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(rel: str) -> None:
        rel = rel.strip("/")
        if not rel or rel in seen:
            return
        seen.add(rel)
        found.append(f"{rel}/src")

    for parent in ("packages", "apps", "libs", "modules"):
        parent_dir = repo_dir / parent
        if not parent_dir.is_dir():
            continue
        for child in sorted(parent_dir.iterdir()):
            if child.is_dir() and (child / "package.json").exists():
                _add(f"{parent}/{child.name}")

    pkg_json = repo_dir / "package.json"
    if pkg_json.exists():
        try:
            _data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _data = {}
        _ws = _data.get("workspaces") if isinstance(_data, dict) else None
        if isinstance(_ws, dict):
            _ws = _ws.get("packages") or []
        if isinstance(_ws, list):
            for pattern in _ws:
                if not isinstance(pattern, str) or not pattern:
                    continue
                # Path.glob handles `packages/*` and similar patterns.
                for match in sorted(repo_dir.glob(pattern.strip("/"))):
                    if match.is_dir() and (match / "package.json").exists():
                        try:
                            _add(str(match.relative_to(repo_dir)))
                        except ValueError:
                            continue
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


def _looks_like_typescript_repo(repo_dir: Path) -> bool:
    """True if this is a TypeScript project routed to the JS tool by mistake.

    Detects either an explicit ``tsconfig.json`` or a source tree that has
    ``.ts``/``.tsx`` files but no plain ``.js`` sources. Used to turn the opaque
    "Cannot detect JavaScript source dir" abort into an actionable message
    pointing the operator at ``prepare_repo_ts.py``.
    """
    if (repo_dir / "tsconfig.json").is_file():
        return True
    has_ts = False
    has_js = False
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _TEST_SCAN_SKIP_DIRS and not d.startswith(".")
        ]
        for f in filenames:
            if f.endswith((".ts", ".tsx")) and not f.endswith(".d.ts"):
                has_ts = True
            elif f.endswith((".js", ".mjs", ".cjs", ".jsx")):
                has_js = True
        if has_js:
            return False
    return has_ts and not has_js


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
            if f.endswith(_TEST_FILE_SUFFIXES) or f in _TEST_FILE_NAMES:
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
    except VersionConflictError as exc:
        # Prefer the declared engines.node floor over the hardcoded default so a
        # node>=22 repo isn't mis-targeted to node20 (which breaks the build).
        floor = _resolve_engines_floor(repo_dir, SUPPORTED_NODE_VERSIONS)
        conflicts = [
            f"{src}: {reason}" for src, reason in exc.rejecting_sources.items()
        ]
        if floor is not None:
            return int(floor), "conflict-fallback:engines-floor", conflicts
        return DEFAULT_NODE_VERSION, "conflict-fallback", conflicts
    except NoSignalsError:
        return DEFAULT_NODE_VERSION, "default", []


def _leading_dir_from_glob(glob: str, repo_dir: Path) -> str | None:
    """Return the leading non-wildcard directory of a test glob relative to the
    repo (e.g. ``test/**/*.spec.js`` -> ``test``), ``"."`` for a root/recursive
    glob (``**/*.test.js``), or ``None`` if the derived dir does not exist."""
    g = glob.strip().lstrip("./")
    if not g or g[0] in "*!":
        return "."
    lead: list[str] = []
    for part in g.split("/"):
        if part in ("", ".") or any(c in part for c in "*?[]{}"):
            break
        lead.append(part)
    # Drop a trailing filename component (has a dot) so `test/foo.test.js` -> `test`.
    if lead and "." in lead[-1]:
        lead = lead[:-1]
    if not lead:
        return "."
    candidate = "/".join(lead)
    return candidate if (repo_dir / candidate).is_dir() else None


_TEST_GLOB_KEYS_RE = re.compile(
    r"(?:include|testMatch|roots|files|testRegex)\s*:\s*"
    r"(\[[^\]]*\]|['\"`][^'\"`]+['\"`])"
)
_QUOTED_RE = re.compile(r"['\"`]([^'\"`]+)['\"`]")


def _detect_test_dir_from_config(repo_dir: Path) -> str | None:
    """Best-effort test-dir from framework config when filesystem heuristics find
    nothing. Consults package.json (``jest`` roots/testMatch, ``ava`` files) and the
    common config files (jest/vitest/vite/ava). Returns a repo-relative dir, ``"."``
    for root/recursive globs, or ``None`` when no test globs are found at all."""
    globs: list[str] = []
    pkg_path = repo_dir / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        if isinstance(pkg, dict):
            jest = pkg.get("jest")
            if isinstance(jest, dict):
                for key in ("roots", "testMatch", "testRegex"):
                    v = jest.get(key)
                    if isinstance(v, str):
                        globs.append(v)
                    elif isinstance(v, list):
                        globs += [x for x in v if isinstance(x, str)]
            ava = pkg.get("ava")
            if isinstance(ava, dict) and isinstance(ava.get("files"), list):
                globs += [x for x in ava["files"] if isinstance(x, str)]
    for name in (
        "jest.config.js", "jest.config.cjs", "jest.config.mjs", "jest.config.ts",
        "vitest.config.ts", "vitest.config.js", "vitest.config.mjs",
        "vite.config.ts", "vite.config.js",
        "ava.config.js", "ava.config.cjs", "ava.config.mjs",
    ):
        p = repo_dir / name
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _TEST_GLOB_KEYS_RE.finditer(text):
            globs += _QUOTED_RE.findall(m.group(1))
    for g in globs:
        d = _leading_dir_from_glob(g, repo_dir)
        if d and d != ".":
            return d
    return "." if globs else None


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
    if test_dirs:
        # RELATIVE to the repo root: "." when tests live at the root (single-file
        # `test.js` convention), "test"/"tests" for a top-level dir, "pkg/x/test" for
        # a nested one. Using `.name` before dropped the path (nested) and returned
        # the REPO folder name for root-level tests (wrong test_dir).
        test_dir = str(test_dirs[0].relative_to(repo_dir))
    else:
        # Filesystem heuristics found nothing (config-only test layouts: jest
        # roots/testMatch, vitest include, package.json "ava"). Consult the config
        # before giving up; if still nothing, default to "." so the config-driven
        # runner self-discovers its tests — a genuinely test-less repo then surfaces
        # downstream as 0 collected (infra) instead of aborting a valid repo here.
        cfg_dir = _detect_test_dir_from_config(repo_dir)
        if cfg_dir is not None and cfg_dir != ".":
            logger.warning(
                "  No test dir via filesystem heuristics; using config-derived "
                "test_dir=%r for %s", cfg_dir, repo_dir.name,
            )
            test_dir = cfg_dir
        else:
            logger.warning(
                "  Could not detect a test directory for %s via filesystem or "
                "config; defaulting test_dir='.' (framework self-discovers).",
                repo_dir.name,
            )
            test_dir = "."

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

    Best-effort for import/collection errors (falls back to the observed test
    count), but RAISES when the repo has test files yet 0 IDs were captured —
    that signals a toolchain/install failure and must not ship a non-canonical
    row silently (override: KAIJU_REQUIRE_INVENTORY=0).
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
        # Distinguish a genuinely test-free repo from an infra failure: test
        # files present but 0 IDs captured almost always means the dependency
        # install/toolchain failed (empty node_modules -> jest/vitest can't run).
        # Fail LOUD here instead of shipping an inventory-less row that dies at
        # the run-stage guard later. Override: KAIJU_REQUIRE_INVENTORY=0 (matches
        # the run stage) for the rare genuinely-uncollectable runner.
        has_test_files = bool(detect_js_test_dirs(repo_dir))
        require_inventory = os.environ.get("KAIJU_REQUIRE_INVENTORY", "1") != "0"
        if has_test_files and require_inventory:
            raise RuntimeError(
                f"JS test-id capture found test files for {repo_basename} but "
                f"discovered 0 test IDs. This dataset row would FAIL the run-stage "
                f"inventory guard (a wrong, non-reproducible denominator), almost "
                f"always due to a toolchain/install failure (empty node_modules?). "
                f"Check the dependency install log above. To ship a NON-CANONICAL "
                f"row anyway, re-run with KAIJU_REQUIRE_INVENTORY=0."
            )
        logger.warning(
            "  No JS test IDs discovered for %s%s; the evaluator will use the "
            "observed test count.",
            repo_basename,
            "" if has_test_files else " (no test files found in repo)",
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
        # resolve_install_command is Yarn-generation aware: Berry (v2+) rejects
        # the classic --frozen-lockfile/--ignore-scripts flags. A committed
        # lockfile -> frozen (reproducible) install; otherwise a generating
        # install that CREATES a lockfile we commit onto the stubbed branch so
        # downstream frozen installs (npm ci) are reproducible. This also
        # unblocks the many small JS libs that .gitignore their lockfile.
        if has_lockfile:
            logger.info("  Installing dependencies via %s (frozen)...", pkg_manager)
        else:
            logger.info(
                "  No committed lockfile; installing via %s (generating lockfile)...",
                pkg_manager,
            )
        from tools._toolchain import build_env_for_repo, resolve_install_command
        install_cmd = resolve_install_command(
            pkg_manager, repo_dir, frozen=has_lockfile
        )
        try:
            _install_env = build_env_for_repo(repo_dir)
        except Exception as _exc:
            logger.warning("  Node auto-switch skipped: %s", _exc)
            _install_env = None
        install_result = subprocess.run(
            install_cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=_install_env,
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
            candidates = _lockfile_candidates(pkg_manager)

            def _produced_lockfile() -> Path | None:
                for name in candidates:
                    p = repo_dir / name
                    if p.exists():
                        return p
                return None

            lockfile_path = _produced_lockfile()
            if lockfile_path is None:
                # The repo's `.npmrc` disabled lockfile generation. npm's
                # `--package-lock=true` already overrides this, but pnpm/yarn have
                # no clean CLI equivalent — so neutralize the directive, retry the
                # generating install once, then restore `.npmrc` verbatim (a
                # committed lockfile + `package-lock=false` is fine for `npm ci`).
                # J1 fix: previously the .npmrc restore at line 915 was only
                # reached on the happy path. If subprocess.run raised (timeout,
                # os error) or the retry failed, the RuntimeError at 917-923
                # exited BEFORE restore — leaving the repo's .npmrc neutralized
                # for every future build. Wrap in try/finally so restore always
                # runs even on exception.
                original_npmrc = _neutralize_npmrc_lockfile_disable(repo_dir)
                if original_npmrc is not None:
                    logger.info(
                        "  .npmrc suppressed the lockfile; neutralized it and "
                        "retrying generating install via %s...", pkg_manager,
                    )
                    try:
                        retry = subprocess.run(
                            install_cmd, cwd=str(repo_dir), capture_output=True,
                            text=True, timeout=600, check=False,
                        )
                        if retry.returncode != 0:
                            raise RuntimeError(
                                f"Retry install (after neutralizing .npmrc) failed for "
                                f"{full_name} via {pkg_manager} "
                                f"(returncode={retry.returncode}).\n"
                                f"  cmd: {' '.join(install_cmd)}\n"
                                f"  stderr tail: {retry.stderr[-500:].strip()}"
                            )
                    finally:
                        (repo_dir / ".npmrc").write_text(original_npmrc)
                    lockfile_path = _produced_lockfile()
            if lockfile_path is None:
                raise RuntimeError(
                    f"Generating install for {full_name} via {pkg_manager} did not "
                    f"produce any of {list(candidates)}; cannot commit a reproducible "
                    "lockfile into the stubbed branch. A committed lockfile is "
                    "required so the downstream frozen install (e.g. `npm ci`) can "
                    "rebuild node_modules deterministically inside the container."
                )
            # J4: reject partial/broken lockfiles before commit.
            _validate_generated_lockfile(lockfile_path, pkg_manager)
            lockfile_name = lockfile_path.name
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
        # 4-tuple to match the signature/caller (base_commit, reference_commit,
        # functions_stubbed, base_compiles); base_compiles=None (unknown — the
        # syntax gate never ran because nothing was stubbed). Returning a 3-tuple
        # here raised ValueError on unpack, turning a benign no-op into a crash.
        return reference_commit, reference_commit, 0, None

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
        and _is_real_stub_marker(line)
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


# Conventional directories that hold COMPILED output (not hand-written source).
_BUILD_OUTPUT_DIRS = frozenset(
    {"dist", "lib", "build", "es", "esm", "cjs", "umd", "out", "output", "_bundles"}
)


def _package_entry_points(pkg: dict) -> list[str]:
    """All consumer-facing entry paths declared in package.json."""
    pts: list[str] = []
    for key in ("main", "module", "browser", "unpkg", "jsdelivr"):
        v = pkg.get(key)
        if isinstance(v, str):
            pts.append(v)

    def _walk(x: object) -> None:
        if isinstance(x, str):
            pts.append(x)
        elif isinstance(x, dict):
            for vv in x.values():
                _walk(vv)
        elif isinstance(x, list):
            for vv in x:
                _walk(vv)

    _walk(pkg.get("exports"))
    return pts


def _detect_build_step_risk(repo_dir: Path, src_dir: str) -> str | None:
    """Return a reason string if the repo tests COMPILED OUTPUT rather than the
    stubbed source (so stubbing ``src_dir`` would be invisible to the tests — a
    silent-corruption / degenerate-row risk), else ``None``.

    Two signals: (a) the test script itself runs a build or targets a build dir;
    (b) a package entry point (main/module/exports) resolves into a build-output
    directory DIFFERENT from the detected ``src_dir`` (if ``src_dir`` IS that dir,
    it's source, not build — no risk)."""
    pkg_path = repo_dir / "package.json"
    if not pkg_path.exists():
        return None
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(pkg, dict):
        return None

    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    test_script = str(scripts.get("test", ""))
    src_top = "." if src_dir in (".", "") else src_dir.strip("./").split("/")[0]

    if re.search(r"\b(build|tsc|rollup|webpack|prepare|prepack)\b", test_script) or any(
        f"{d}/" in test_script for d in _BUILD_OUTPUT_DIRS
    ):
        return (
            f"test script runs a build / targets a build dir (scripts.test="
            f"{test_script!r}); tests would run against compiled output, not the "
            "stubbed source"
        )

    for ep in _package_entry_points(pkg):
        top = ep.strip("./").split("/")[0] if ep else ""
        if top and top in _BUILD_OUTPUT_DIRS and top != src_top:
            return (
                f"entry point {ep!r} resolves to build dir {top!r} (outside src_dir "
                f"{src_dir!r}); the test suite likely imports compiled output, so "
                "stubbing the source would not affect it"
            )
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
        if _looks_like_typescript_repo(repo_dir):
            logger.error(
                "  %s is a TypeScript repo (tsconfig.json / only .ts/.tsx "
                "sources, no .js) — the JavaScript preparer cannot stub it. "
                "Re-run with the TypeScript tool: "
                "python -m tools.prepare_repo_ts %s",
                full_name,
                full_name,
            )
        else:
            logger.error("  Cannot detect JavaScript source dir for %s", full_name)
        return None
    logger.info("  Source directory: %s", src_dir)
    _assert_monorepo_safety(repo_dir, src_dir, src_dir_override)

    # Reject build-step repos: if the suite tests COMPILED output (dist/lib/...),
    # stubbing src_dir is invisible to the tests -> a trivially-passing, degenerate
    # dataset row that LOOKS healthy. Skip loudly rather than emit silent corruption.
    build_risk = _detect_build_step_risk(repo_dir, src_dir)
    if build_risk:
        logger.error(
            "  Rejecting %s: build-step repo (%s). Skipping to avoid a "
            "degenerate/false-pass row.", full_name, build_risk,
        )
        return None

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
                raise RuntimeError(
                    f"Push to {fork_name} FAILED and no usable "
                    f"'{JS_DATASET_BRANCH}' branch exists on the fork. The "
                    f"container build clones this fork and fetches "
                    f"base/reference commits from it, so a dataset built from "
                    f"un-pushed local commits is UNBUILDABLE ('not our ref'). "
                    f"Ensure your token has WRITE access to the fork org "
                    f"(run_trajectory.sh: --org / $KAIJU_FORK_ORG).\n"
                    f"Original push error: {e}"
                ) from e

    # README-based spec doc — parity with TS (prepare_repo_ts.py). Generates
    # specs/<repo>_readme_spec.pdf.bz2 so the host-side copy_inference_inputs stages
    # a <repo>_spec.pdf.bz2 into each run's datasets/ dir, consistent with the other
    # languages. JS uses the README as its spec source (no URL scraping in the JS
    # flow), so we go straight to the README fallback. The repo is on the stubbed
    # dataset branch here, which still contains the README. Non-fatal.
    repo_short = full_name.split("/")[-1]
    try:
        from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec

        specs_dir = REPO_ROOT / "specs"
        specs_dir.mkdir(parents=True, exist_ok=True)
        readme_spec_path, _readme_spec_url = _scrape_readme_spec(
            repo_dir, specs_dir, repo_short
        )
        if readme_spec_path:
            logger.info("  README spec generated -> %s", readme_spec_path)
        else:
            logger.warning(
                "  README spec generation returned no output for %s — "
                "datasets/ will lack %s_spec.pdf.bz2", repo_short, repo_short,
            )
    except ImportError:
        logger.warning(
            "  Skipping README spec (install PyMuPDF; optionally playwright + "
            "chromium) — datasets/ will lack %s_spec.pdf.bz2", repo_short,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("  README spec generation failed (non-fatal): %s", e)

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
