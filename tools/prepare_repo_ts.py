"""Prepare TypeScript repos for the commit0 dataset.

Mirrors tools/prepare_repo.py but for TypeScript:
1. Fork to GitHub org
2. Clone repo
3. Detect TS source directory
4. Run ts-morph stubbing via stub_ts_runner.py
5. Commit stubbed version
6. Push to fork

Reuses git helpers from tools.prepare_repo -- ZERO modifications to existing files.
"""

from __future__ import annotations
import uuid as _uuid_mod
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from kaiju.paths import datasets_dir, spec_path as consolidated_spec_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

from tools.prepare_repo import (
    git,
    full_clone,
    push_to_fork,
    get_head_sha,
    get_default_branch,
)
from tools._git_auth import setup_git_credentials, fork_repo
from tools.stub_ts_runner import run_stub_ts

# Lazy import for spec scraping (optional dependency) -- mirrors prepare_repo_go.py
_scrape_spec_sync = None


def _get_scrape_func():
    """Lazy-load scrape_spec_sync to avoid importing optional deps at module level."""
    global _scrape_spec_sync
    if _scrape_spec_sync is None:
        from tools.scrape_pdf import scrape_spec_sync

        _scrape_spec_sync = scrape_spec_sync
    return _scrape_spec_sync


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STUBBER_PROBE_PKG = "ts-morph"
_STUBBER_ROOT_PKGS = ("ts-morph", "ts-node", "typescript", "@types/node")
_WORKSPACE_INDICATORS = (
    "pnpm-workspace.yaml",
    "pnpm-workspace.yml",
    "lerna.json",
    "rush.json",
)


def _validate_stubber_deps() -> None:
    """Verify the TS stubber's npm deps are installed at the project root.
    Tries `npm install` on first miss; raises with actionable error otherwise.
    """
    probe = _PROJECT_ROOT / "node_modules" / _STUBBER_PROBE_PKG
    if probe.exists():
        return
    root_pkg = _PROJECT_ROOT / "package.json"
    if not root_pkg.exists():
        raise EnvironmentError(
            f"Stubber deps missing and no package.json at {_PROJECT_ROOT}. "
            f"Expected: {', '.join(_STUBBER_ROOT_PKGS)}"
        )
    if shutil.which("npm") is None:
        raise EnvironmentError(
            "Stubber needs `npm` to install ts-morph/ts-node/typescript at the "
            f"project root ({_PROJECT_ROOT}) but `npm` is not on PATH."
        )
    logger.warning(
        "Stubber dependencies missing at %s; running `npm install` once...",
        _PROJECT_ROOT,
    )
    result = subprocess.run(
        ["npm", "install"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0 or not probe.exists():
        raise EnvironmentError(
            f"npm install at {_PROJECT_ROOT} did not produce {probe}. "
            f"Stderr tail: {result.stderr[-500:].strip()}"
        )
    logger.info("Stubber deps installed at %s", _PROJECT_ROOT)


def _ensure_pkg_manager(pkg_manager: str) -> None:
    """Ensure the package manager is on PATH; auto-activate via corepack if needed."""
    if shutil.which(pkg_manager) is not None:
        return
    if pkg_manager == "npm":
        raise EnvironmentError(
            "`npm` is not on PATH but the repo needs it. Install Node.js."
        )
    corepack = shutil.which("corepack")
    if corepack and pkg_manager in ("pnpm", "yarn"):
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
    raise EnvironmentError(
        f"`{pkg_manager}` is not on PATH. Install it manually:\n"
        f"  npm install -g {pkg_manager}   (or)   corepack enable && "
        f"corepack prepare {pkg_manager}@latest --activate"
    )


def _list_workspace_packages(repo_dir: Path) -> list[str]:
    """Find workspace packages under packages/, apps/, libs/, modules/."""
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
    """Detect monorepo and return (is_monorepo, [workspace_package_paths])."""
    for marker in _WORKSPACE_INDICATORS:
        if (repo_dir / marker).exists():
            return True, _list_workspace_packages(repo_dir)
    pkg = repo_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        if data.get("workspaces"):
            return True, _list_workspace_packages(repo_dir)
    return False, []


def _assert_monorepo_safety(
    repo_dir: Path,
    src_dir: str,
    src_dir_override: str | None,
) -> None:
    """Refuse to stub a whole monorepo when --src-dir is not explicitly given."""
    if src_dir_override:
        return
    if src_dir != ".":
        return
    is_monorepo, packages = _detect_monorepo(repo_dir)
    if not is_monorepo:
        return
    suggestion = packages[0] if packages else "packages/<name>/src"
    sample = ", ".join(packages[:5]) or "(none detected)"
    raise EnvironmentError(
        f"Detected a monorepo at {repo_dir} but no --src-dir was given.\n"
        f"Stubbing the whole monorepo will exhaust V8's heap.\n"
        f"Pass --src-dir <path>, e.g. --src-dir {suggestion}.\n"
        f"Detected workspace packages: {sample}"
    )


def resolve_commits_from_remote(fork_name: str, branch: str) -> tuple[str, str] | None:
    """Resolve (base, reference) commits from an existing remote branch.

    Used as a fallback when the initial push fails but the fork already
    has the dataset branch from a prior run. Mirrors
    ``resolve_commits_from_remote`` in ``prepare_repo_go.py``.

    Returns ``(base_sha, reference_sha)`` on success, ``None`` otherwise.
    Requires the ``gh`` CLI to be authenticated.
    """
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
        if not commit_data.get("parents"):
            return None
        parent_sha = commit_data["parents"][0]["sha"]
        return (sha, parent_sha)
    except Exception as e:
        logger.debug("Non-critical failure during remote commit resolution: %s", e)
        return None


DEFAULT_ORG = "Zahgon"

KNOWN_TEST_PACKAGES = {
    "jest",
    "@jest/globals",
    "ts-jest",
    "@types/jest",
    "vitest",
    "@vitest/coverage-v8",
    "mocha",
    "chai",
    "@types/mocha",
    "ava",
}

from commit0.harness.constants_ts import TS_DATASET_BRANCH


def _exec_prefix(pkg_manager: str) -> str:
    """Return the local-binary runner for the given package manager."""
    return {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}.get(pkg_manager, "npx")


def detect_ts_src_dir(repo_dir: Path) -> str:
    """Auto-detect the TypeScript source directory within a repo.

    Heuristics (in priority order):
    1. src/ directory containing .ts files
    2. lib/ directory containing .ts files
    3. Root directory if tsconfig.json exists and has .ts files at root
    4. First directory containing index.ts

    Returns
    -------
        Relative path from repo_dir (e.g. "src", "lib", "."), or empty string
        if no TypeScript source is found.

    """
    # Check for tsconfig.json first -- must exist for TS repos
    tsconfig = repo_dir / "tsconfig.json"
    if not tsconfig.exists():
        logger.warning("No tsconfig.json found in %s", repo_dir)
        # Some repos use tsconfig in a subdirectory, still check for .ts files
        pass

    # 1. src/ with .ts files
    src_dir = repo_dir / "src"
    if src_dir.is_dir() and list(src_dir.glob("**/*.ts")):
        return "src"

    # 2. lib/ with .ts files
    lib_dir = repo_dir / "lib"
    if lib_dir.is_dir() and list(lib_dir.glob("**/*.ts")):
        return "lib"

    # 3. Root with .ts files (flat layout)
    root_ts = [f for f in repo_dir.glob("*.ts") if not f.name.endswith(".d.ts")]
    if root_ts:
        return "."

    # 4. First direct child with index.ts
    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        if (child / "index.ts").exists():
            return child.name

    for workspace_root in ("packages", "apps", "source", "modules"):
        ws_dir = repo_dir / workspace_root
        if ws_dir.is_dir():
            ts_files = [
                f
                for f in ws_dir.rglob("*.ts")
                if not f.name.endswith(".d.ts")
                and "node_modules" not in f.parts
                and "dist" not in f.parts
            ]
            if ts_files:
                return workspace_root

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in {
            "node_modules",
            "dist",
            "build",
            "coverage",
        }:
            continue
        ts_files = [
            f
            for f in child.rglob("*.ts")
            if not f.name.endswith(".d.ts") and "node_modules" not in f.parts
        ]
        if ts_files:
            return child.name

    return ""


_TEST_SCAN_SKIP_DIRS = {
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

_TEST_DIR_NAMES = {"__tests__", "test", "tests", "__test__", "spec", "specs"}
_TEST_FILE_SUFFIXES = (
    ".test.ts",
    ".spec.ts",
    ".test.tsx",
    ".spec.tsx",
    ".test.js",
    ".spec.js",
    ".test.jsx",
    ".spec.jsx",
    ".test.mts",
    ".spec.mts",
    ".test.cjs",
    ".spec.cjs",
)

# Single-file test convention: a bare `test.ts`/`test.js` at the repo root (AVA and
# many small packages). NOT covered by the `.test.*`/`.spec.*` SUFFIXES above, so
# without this the recursive scan misses root-level tests (mirrors the JS fix).
_TEST_FILE_NAMES: frozenset[str] = frozenset(
    {
        "test.ts", "test.tsx", "test.mts", "test.cts",
        "test.js", "test.mjs", "test.cjs", "test.jsx",
    }
)


def _walk_repo_filtered(repo_dir: Path):
    """Yield (dirpath, dirnames, filenames) walking *repo_dir* while skipping vendor dirs.

    Walks are always rooted at *repo_dir*. Hidden dirs (leading dot) are skipped except
    for the repo root itself. ``node_modules`` and other vendor dirs in
    ``_TEST_SCAN_SKIP_DIRS`` are pruned in-place.
    """
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        # Prune in-place so os.walk does not descend into vendor / build dirs.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _TEST_SCAN_SKIP_DIRS and not d.startswith(".")
        ]
        yield Path(dirpath), dirnames, filenames


def _detect_test_dirs_from_config(repo_dir: Path) -> list[Path]:
    """Config-driven test detection.

    Parses package.json (jest / vitest / mocha blocks), jest.config.*, vitest.config.*,
    and .mocharc.* for test-file globs or roots. Returns absolute dirs that exist and
    contain at least one TS / JS source file. No throwing -- returns [] on any error.
    """
    candidate_strings: list[str] = []

    # ---- package.json: jest.* + vitest.* + mocha --------------------------------------
    pkg_path = repo_dir / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text())
        except (json.JSONDecodeError, OSError):
            pkg = {}
        jest_block = pkg.get("jest", {}) if isinstance(pkg, dict) else {}
        if isinstance(jest_block, dict):
            for key in ("testMatch", "testRegex", "roots", "testPathIgnorePatterns"):
                val = jest_block.get(key)
                if isinstance(val, list):
                    candidate_strings.extend(str(v) for v in val)
                elif isinstance(val, str):
                    candidate_strings.append(val)
        mocha_block = pkg.get("mocha", {}) if isinstance(pkg, dict) else {}
        if isinstance(mocha_block, dict):
            spec = mocha_block.get("spec")
            if isinstance(spec, list):
                candidate_strings.extend(str(v) for v in spec)
            elif isinstance(spec, str):
                candidate_strings.append(spec)
        vitest_block = pkg.get("vitest", {}) if isinstance(pkg, dict) else {}
        if isinstance(vitest_block, dict):
            for key in ("include", "dir", "root"):
                val = vitest_block.get(key)
                if isinstance(val, list):
                    candidate_strings.extend(str(v) for v in val)
                elif isinstance(val, str):
                    candidate_strings.append(val)

    # ---- jest.config.* / vitest.config.* / .mocharc.* (scrape test-related keys only) --
    CONFIG_FILES = [
        "jest.config.js",
        "jest.config.cjs",
        "jest.config.mjs",
        "jest.config.ts",
        "jest.config.json",
        "vitest.config.ts",
        "vitest.config.js",
        "vitest.config.mts",
        "vitest.config.cjs",
        "vitest.workspace.ts",
        ".mocharc.json",
        ".mocharc.cjs",
        ".mocharc.js",
        ".mocharc.yml",
        ".mocharc.yaml",
    ]
    # Only scrape string values adjacent to test-location keys.
    # Avoids false positives from collectCoverageFrom, transform, etc.
    _TEST_LOCATION_KEYS = (
        "testMatch",
        "testRegex",
        "testPathPattern",
        "roots",
        "testDir",
        "include",
        "dir",
        "spec",
    )
    _cfg_key_value_re = re.compile(
        r"(?:" + "|".join(re.escape(k) for k in _TEST_LOCATION_KEYS) + r")"
        r"""['"]*\s*[:=]\s*"""
        r"""[\[]*\s*['"` ]?([^'"`\n\],]{1,300})['"` \]]?""",
    )
    for name in CONFIG_FILES:
        cfg_path = repo_dir / name
        if not cfg_path.exists():
            continue
        try:
            text = cfg_path.read_text()
        except OSError:
            continue
        for match in _cfg_key_value_re.findall(text):
            candidate_strings.append(match.strip())

    # ---- Convert glob/regex strings into concrete directories -------------------------
    resolved: list[Path] = []
    seen: set[Path] = set()
    for s in candidate_strings:
        s = s.strip()
        if not s or s.startswith("!"):
            continue
        # Strip leading <rootDir>/, ./, /
        s = re.sub(r"^<rootDir>/?", "", s)
        s = re.sub(r"^\./", "", s)
        s = s.lstrip("/")
        # Strip file-name / glob tail to get the "dir" part.
        # Take everything up to the first wildcard or file-extension token.
        dir_part = re.split(r"[*?(]|\.(?:t|j)sx?$|\.spec|\.test", s, maxsplit=1)[0]
        dir_part = dir_part.rstrip("/")
        if not dir_part:
            continue
        candidate = (repo_dir / dir_part).resolve()
        # Reject escapes and non-existent dirs.
        try:
            candidate.relative_to(repo_dir.resolve())
        except ValueError:
            continue
        if not candidate.is_dir():
            continue
        if candidate == repo_dir.resolve():
            # Root-level config match is too coarse; keep for tier 2 to refine.
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)

    # Only keep dirs that actually contain TS / JS source files anywhere below.
    kept: list[Path] = []
    for d in resolved:
        for _, _, files in _walk_repo_filtered(d):
            if any(
                f.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".cjs"))
                for f in files
            ):
                kept.append(d)
                break
    return kept


def _detect_test_dirs_recursive_scan(repo_dir: Path) -> list[Path]:
    """Recursive filesystem scan.

    Looks for (a) *any* directory named __tests__ / test / tests / spec / specs that
    contains a TS or JS file, and (b) parent directories of *.test.* / *.spec.* files.
    Returns candidates ranked by number of test files (descending).
    """
    counts: dict[Path, int] = {}
    for dirpath, _, filenames in _walk_repo_filtered(repo_dir):
        # (a) Named test directories
        if dirpath != repo_dir and dirpath.name.lower() in _TEST_DIR_NAMES:
            ts_js_files = [
                f
                for f in filenames
                if f.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".cjs"))
            ]
            if ts_js_files:
                counts[dirpath] = counts.get(dirpath, 0) + len(ts_js_files)
        # (b) Files with .test.* / .spec.* suffix OR a bare root-level test file.
        for f in filenames:
            if f.endswith(_TEST_FILE_SUFFIXES) or f in _TEST_FILE_NAMES:
                counts[dirpath] = counts.get(dirpath, 0) + 1
    # Sort by count desc, then by shortest path (closer to root = more canonical).
    return [
        p for p, _ in sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0].parts)))
    ]


def detect_ts_test_dirs(repo_dir: Path) -> list[Path]:
    """Find test directories containing TypeScript test files.

    uses 3-tier detection (config-driven → recursive scan → empty).

    Returns
    -------
         List of absolute Paths to test directories, ranked by confidence. Empty when
         no tests found anywhere (callers must handle this explicitly).

    """
    # Tier 1: config-driven
    config_dirs = _detect_test_dirs_from_config(repo_dir)
    if config_dirs:
        return config_dirs

    # Tier 2: recursive scan
    return _detect_test_dirs_recursive_scan(repo_dir)


def detect_ts_test_dirs_with_provenance(repo_dir: Path) -> tuple[list[Path], str]:
    """Like :func:`detect_ts_test_dirs` but also returns the detection heuristic used.

    Heuristic name is one of: ``"config"``, ``"recursive-scan"``, ``"none"``.
    """
    config_dirs = _detect_test_dirs_from_config(repo_dir)
    if config_dirs:
        return config_dirs, "config"
    scan_dirs = _detect_test_dirs_recursive_scan(repo_dir)
    if scan_dirs:
        return scan_dirs, "recursive-scan"
    return [], "none"


def detect_package_manager(repo_dir: Path) -> str:
    """Detect the package manager from lockfiles.

    Returns: "npm" | "yarn" | "pnpm" | "bun"
    """
    if (repo_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_dir / "yarn.lock").exists():
        return "yarn"
    if (repo_dir / "bun.lockb").exists():
        return "bun"
    return "npm"


def detect_test_framework(repo_dir: Path) -> str:
    """Detect the test framework from package.json and config files.

    Priority: vitest > jest > node_test.

    Returns: "jest" | "vitest" | "node_test"
    """
    pkg_path = repo_dir / "package.json"
    if not pkg_path.exists():
        logger.warning("No package.json found in %s, defaulting to jest", repo_dir)
        return "jest"

    try:
        pkg = json.loads(pkg_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Cannot parse package.json in %s, defaulting to jest", repo_dir)
        return "jest"

    dev_deps = pkg.get("devDependencies", {})
    deps = pkg.get("dependencies", {})
    all_deps = {**deps, **dev_deps}

    if "vitest" in all_deps:
        return "vitest"
    if "jest" in all_deps or "@jest/globals" in all_deps:
        return "jest"

    vitest_configs = ["vitest.config.ts", "vitest.config.js", "vitest.config.mts"]
    if any((repo_dir / c).exists() for c in vitest_configs):
        return "vitest"

    jest_configs = [
        "jest.config.ts",
        "jest.config.js",
        "jest.config.mjs",
        "jest.config.cjs",
    ]
    if any((repo_dir / c).exists() for c in jest_configs):
        return "jest"

    if "jest" in pkg:
        return "jest"

    test_script = pkg.get("scripts", {}).get("test", "")
    if "vitest" in test_script:
        return "vitest"
    if "jest" in test_script:
        return "jest"

    if _detect_node_test_signals(pkg, repo_dir):
        return "node_test"

    return "jest"


_NODE_TEST_IMPORT_PATTERNS = (
    "from 'node:test'",
    'from "node:test"',
    "from 'node:test/reporters'",
    "@paulmillr/jsbt/test",
    "require('node:test'",
    'require("node:test"',
)


def _detect_node_test_signals(pkg: dict, repo_dir: Path) -> bool:
    dev_deps = pkg.get("devDependencies", {})
    deps = pkg.get("dependencies", {})
    all_deps = {**deps, **dev_deps}
    if "@paulmillr/jsbt" in all_deps:
        return True

    scripts = pkg.get("scripts", {})
    if isinstance(scripts, dict):
        for cmd in scripts.values():
            if isinstance(cmd, str) and "--test" in cmd and "node" in cmd:
                return True

    test_globs = ("**/*.test.ts", "**/*.test.tsx", "**/*.test.mts", "**/*.test.cts",
                  "**/*.test.js", "**/*.test.mjs", "**/*.test.cjs")
    scanned = 0
    for pattern in test_globs:
        for path in repo_dir.glob(pattern):
            if "node_modules" in path.parts:
                continue
            scanned += 1
            if scanned > 25:
                break
            try:
                head = path.read_text(errors="ignore")[:2000]
            except OSError:
                continue
            for sig in _NODE_TEST_IMPORT_PATTERNS:
                if sig in head:
                    return True
        if scanned > 25:
            break
    return False


_BLOCKED_HOMEPAGE_DOMAINS = (
    "github.com",
    "gitlab.com",
    "npmjs.com",
    "npmjs.org",
    # CDNs / JS package viewers -- return SPA shells or raw JS, not scrape-able docs
    "skypack.dev",
    "unpkg.com",
    "jsdelivr.net",
    "cdn.jsdelivr.net",
    "esm.sh",
    "bundle.run",
    "packagephobia.com",
    "bundlephobia.com",
)


def _detect_spec_url(repo_dir: Path) -> str:
    """Detect documentation URL from package.json homepage field.

    Falls back to npm registry metadata if local package.json has no homepage.
    Returns empty string if no usable URL found.
    """
    pkg_path = repo_dir / "package.json"
    pkg: dict = {}
    pkg_name = ""

    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text())
            pkg_name = pkg.get("name", "")
        except (json.JSONDecodeError, OSError):
            pass

    for field in ("homepage", "docs", "documentation"):
        val = pkg.get(field, "")
        if val and not any(d in val for d in _BLOCKED_HOMEPAGE_DOMAINS):
            return val

    if pkg_name:
        try:
            import urllib.request

            url = f"https://registry.npmjs.org/{pkg_name}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
                npm_homepage = data.get("homepage", "")
                if npm_homepage and not any(
                    d in npm_homepage for d in _BLOCKED_HOMEPAGE_DOMAINS
                ):
                    return npm_homepage
        except Exception:
            pass

    # No Skypack / CDN fallback -- those return SPA shells, not scrape-able docs.
    # Returning empty string lets the caller skip the scrape cleanly.
    return ""


def generate_setup_dict_ts(repo_dir: Path) -> tuple[dict, dict, str]:
    """Build setup and test dicts for a TypeScript repo.

    Returns: (setup_dict, test_dict, test_framework)
    """
    test_framework = detect_test_framework(repo_dir)
    pkg_manager = detect_package_manager(repo_dir)
    install_cmd = f"{pkg_manager} install"

    packages: list[str] = []
    pkg_path = repo_dir / "package.json"
    if pkg_path.exists():
        try:
            pkg = json.loads(pkg_path.read_text())
            dev_deps = pkg.get("devDependencies", {})
            packages = sorted(p for p in dev_deps if p in KNOWN_TEST_PACKAGES)
        except (json.JSONDecodeError, OSError):
            pass

    test_dirs, test_dir_detected_by = detect_ts_test_dirs_with_provenance(repo_dir)
    if test_dirs:
        # RELATIVE to repo root: "." for root-level tests, "test"/"tests" for a
        # top-level dir, "pkg/x/test" for a nested one. `.name` returned the repo
        # folder name for root tests (wrong) and dropped the path for nested (mirrors
        # the JS fix).
        try:
            test_dir = str(test_dirs[0].relative_to(repo_dir))
        except ValueError:
            test_dir = test_dirs[0].name
    else:
        raise RuntimeError(
            f"Could not detect a test directory for {repo_dir.name}. "
            "Inspect package.json (jest/vitest/mocha), jest.config.*, vitest.config.*, "
            ".mocharc.*, or the filesystem layout and set test_dir manually in the entries JSON."
        )

    spec_url = _detect_spec_url(repo_dir)

    # Detect node version via canonical detector (single source of truth shared with spec_ts.py).
    from commit0.harness.constants_ts import (
        DEFAULT_NODE_VERSION,
        SUPPORTED_NODE_VERSIONS,
    )
    from tools.node_version import (
        detect as _detect_node,
        resolve_engines_floor as _resolve_engines_floor,
    )
    from tools._versioning import NoSignalsError, VersionConflictError

    try:
        det = _detect_node(
            repo_dir,
            SUPPORTED_NODE_VERSIONS,
            fallback=DEFAULT_NODE_VERSION,
        )
        node_version_value = det.version or DEFAULT_NODE_VERSION
        version_source = det.source
        version_conflicts = det.conflicts
    except VersionConflictError as exc:
        logger.error("Node version conflict: %s", exc)
        # Prefer the declared engines.node floor over the hardcoded default so a
        # node>=22 repo does not get mis-targeted to node20 (breaks the build).
        floor = _resolve_engines_floor(repo_dir, SUPPORTED_NODE_VERSIONS)
        if floor is not None:
            node_version_value = floor
            version_source = "conflict-fallback:engines-floor"
        else:
            node_version_value = DEFAULT_NODE_VERSION
            version_source = "conflict-fallback"
        version_conflicts = [
            f"{src}: {reason}" for src, reason in exc.rejecting_sources.items()
        ]
    except NoSignalsError:  # only when strict=True; defensive
        node_version_value = DEFAULT_NODE_VERSION
        version_source = "default"
        version_conflicts = []

    setup_dict = {
        # kaiju-build-repo.yaml reads .setup.node_version for the base image tag.
        # spec_ts.py:_get_node_version also reads setup["node_version"].
        "node_version": node_version_value,
        "install": install_cmd,
        "packages": packages,
        "pre_install": [],
        "specification": spec_url,
        "version_source": version_source,
        "version_conflicts": version_conflicts,
    }

    prefix = _exec_prefix(pkg_manager)
    if test_framework == "vitest":
        test_cmd = f"{prefix} vitest run"
    elif test_framework == "node_test":
        test_cmd = "node --test"
    else:
        test_cmd = f"{prefix} jest"

    test_dict = {
        "test_cmd": test_cmd,
        "test_dir": test_dir,
        "test_dir_detected_by": test_dir_detected_by,
    }

    return setup_dict, test_dict, test_framework


_MAX_EXTRA_SCAN_DIRS = int(os.environ.get("KAIJU_TS_MAX_SCAN_DIRS", "20"))


def _collect_extra_scan_dirs(
    repo_dir: Path, src_dir_path: Path, test_dirs: list[Path]
) -> list[Path]:
    """Collect directories to scan for import-time names (not stubbed).

    Mirrors prepare_repo.py lines 313-337: scans sibling packages and test dirs.
    """
    extra: list[Path] = list(test_dirs)

    for child in sorted(repo_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child == src_dir_path or child.name == "node_modules":
            continue
        if child in extra:
            continue
        ts_files = list(child.glob("**/*.ts"))
        if ts_files:
            extra.append(child)

    if len(extra) > _MAX_EXTRA_SCAN_DIRS:
        logger.warning(
            "Capping extra_scan_dirs from %d to %d to prevent stubber OOM "
            "(override via KAIJU_TS_MAX_SCAN_DIRS env var)",
            len(extra),
            _MAX_EXTRA_SCAN_DIRS,
        )
        extra = extra[:_MAX_EXTRA_SCAN_DIRS]

    return extra


def create_ts_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    src_dir: str,
    branch_name: str = TS_DATASET_BRANCH,
) -> "tuple[str, str, int, bool | None]":
    """Create the commit0 branch with stubbed TypeScript code.

    Mirrors create_stubbed_branch from prepare_repo.py (lines 265-422).

    Returns
    -------
        (base_commit_sha, reference_commit_sha, functions_stubbed, base_compiles)

    """
    # Record reference + branch from the CURRENT HEAD (the pinned tag when --tag
    # checked one out). Checking out the default branch first silently discarded
    # the tag, so base_commit was built on the default tip while reference_commit
    # pointed at the tag (divergent history).
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    src_dir_path = repo_dir / src_dir if src_dir != "." else repo_dir
    if not src_dir_path.is_dir():
        raise ValueError(f"src_dir does not exist: {src_dir_path}")

    test_dirs = detect_ts_test_dirs(repo_dir)
    if src_dir == ".":
        extra_scan_dirs: list[Path] = []
        logger.info(
            "  src_dir is '.', skipping extra_scan_dirs (whole repo already in scan)"
        )
    else:
        extra_scan_dirs = _collect_extra_scan_dirs(repo_dir, src_dir_path, test_dirs)

    if extra_scan_dirs:
        logger.info(
            "  Scanning %d extra dirs for import-time names: %s",
            len(extra_scan_dirs),
            [d.name for d in extra_scan_dirs],
        )

    package_json = repo_dir / "package.json"
    if package_json.exists():
        pkg_manager = detect_package_manager(repo_dir)
        logger.info("  Installing dependencies via %s...", pkg_manager)
        _ensure_pkg_manager(pkg_manager)
        install_cmd = [pkg_manager, "install"]
        if pkg_manager != "bun":
            install_cmd.append("--ignore-scripts")
        subprocess.run(
            install_cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

    logger.info("  Stubbing TypeScript source in: %s", src_dir)
    report = run_stub_ts(
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
        report.get("functions_preserved", 0),
    )

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing -- source may already be stubs?")
        return reference_commit, reference_commit, 0, None

    functions_stubbed = report.get("functions_stubbed", 0)
    if functions_stubbed == 0:
        raise RuntimeError(
            f"No functions were stubbed for {full_name}; aborting pipeline"
            "Running the agent on a repo with zero stubs is wasteful and inflates pass "
            "rates with trivial baselines. Investigate the stubber output above."
        )

    diff_ts = git(repo_dir, "diff", "--cached", "--unified=0", "--", "*.ts", "*.tsx")
    stub_marker_count = sum(
        1
        for line in diff_ts.splitlines()
        if line.startswith("+")
        and not line.startswith("+++")
        and 'throw new Error("STUB")' in line
    )
    logger.info(
        "  Stub verification -- .ts/.tsx STUB markers added: %d (expected >= 1)",
        stub_marker_count,
    )

    if stub_marker_count < 1:
        raise RuntimeError(
            f"Stubbing verification failed for {full_name}: functions_stubbed="
            f"{functions_stubbed} but the staged .ts/.tsx diff contains zero "
            'added lines matching `throw new Error("STUB")`. The '
            "apparent stubs did not land in TypeScript source files."
        )

    # Post-stub sanity: run `tsc --noEmit` to catch catastrophic syntactic
    # damage before we commit the stubbed branch. Non-blocking -- many target
    # repos have pre-existing type errors we can't fix here. We only care
    # about bailing when the stubber emitted unparseable TypeScript.
    base_compiles = _run_post_stub_tsc_check(repo_dir)

    git(repo_dir, "commit", "-m", "Commit 0")
    base_commit = get_head_sha(repo_dir)
    logger.info("  Base commit (stubbed): %s", base_commit[:12])

    return base_commit, reference_commit, functions_stubbed, base_compiles


_TSC_FATAL_CODES = (
    "TS1005",  # ';' expected, unexpected token
    "TS1128",  # Declaration or statement expected
    "TS1109",  # Expression expected
    "TS1003",  # Identifier expected
    "TS1131",  # Property or signature expected
    "TS1135",  # Argument expression expected
    "TS1136",  # Property assignment expected
    "TS1144",  # '{' or ';' expected
    "TS1160",  # Unterminated template literal
    "TS1161",  # Unterminated regular expression literal
)


def _run_post_stub_tsc_check(repo_dir: Path) -> "bool | None":
    """Run ``npx tsc --noEmit`` against the stubbed tree; warn on type errors,
    raise only on fatal PARSE errors (truly broken syntax from the stubber).

    Returns ``base_compiles`` (mirrors python/c/go/rust/java):
      * True  -> tsc ran and the stubbed source is syntactically valid (clean, or
                 only semantic/type errors which the model is meant to resolve).
      * None  -> couldn't check (no tsconfig / npx missing / timeout) -> unknown.
      * (raises) -> fatal parse errors: the stubber emitted unparseable code, so
                 the base is corrupt; refuse to commit it.
    """
    tsconfig = repo_dir / "tsconfig.json"
    if not tsconfig.exists():
        logger.debug("  Skipping tsc check: no tsconfig.json")
        return None
    try:
        result = subprocess.run(
            ["npx", "--no-install", "tsc", "--noEmit", "--skipLibCheck"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("  Skipping tsc check: npx not available")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("  tsc --noEmit timed out after 600s (non-fatal)")
        return None

    if result.returncode == 0:
        logger.info("  Post-stub tsc check: clean")
        return True

    combined = f"{result.stdout}\n{result.stderr}"
    # Filter out errors from node_modules/ -- third-party type declarations
    # leak through even with --skipLibCheck (which skips *type-check* but
    # not parse). We only care about errors in the stubbed source tree.
    project_err_lines = [
        ln
        for ln in combined.splitlines()
        if "error TS" in ln and "node_modules/" not in ln
    ]
    project_errors = "\n".join(project_err_lines)
    fatal_hits = sum(project_errors.count(code) for code in _TSC_FATAL_CODES)
    err_line_count = len(project_err_lines)
    if fatal_hits > 0:
        raise RuntimeError(
            f"Post-stub tsc check produced {fatal_hits} fatal parse error(s) "
            f"({err_line_count} total TS errors in project code). The stubber emitted "
            f"unparseable TypeScript -- refusing to commit stubbed branch.\n"
            f"First 2000 chars of project tsc errors:\n{project_errors[:2000]}"
        )
    if err_line_count == 0:
        logger.info("  Post-stub tsc check: clean (node_modules/ errors ignored)")
        return True
    logger.warning(
        "  Post-stub tsc check: %d type errors in project code (non-fatal -- "
        "target repo may have pre-existing type issues)",
        err_line_count,
    )
    # Only type/semantic errors remain -> the source PARSES (syntactically valid);
    # the model is meant to fix the semantics. Base is well-formed.
    return True


def _capture_ts_test_ids(
    repo_dir: Path,
    repo: str,
    test_dir: str,
    framework: str,
    reference_commit: str | None = None,
) -> None:
    """Capture the canonical TS test inventory and save it as the AUTHORITATIVE
    denominator the evaluator reads.

    Runs ``collect_ts_test_ids_local`` (vitest/jest ``--list``/``--listTests``)
    in *repo_dir* and saves the discovered IDs to
    ``commit0/data/test_ids/<repo>.bz2`` where ``<repo>`` is the key
    ``get_ts_test_ids.main`` looks up (``repo.lower().replace(".", "-")``,
    applied by ``save_test_ids``). Without it, ``evaluate_ts`` has no inventory
    and falls back to the observed test count.

    Test discovery needs ``node_modules`` (npx must resolve vitest/jest), so this
    MUST run after dependency install (``create_ts_stubbed_branch`` installs them).
    Collection runs against the reference (un-stubbed) commit when available so
    imports resolve during listing. Best-effort: any failure just leaves the
    evaluator to fall back to the observed count -- never aborts prep.
    """
    try:
        from tools.generate_test_ids_ts import (
            collect_ts_test_ids_local,
            _normalize_ts_test_ids,
        )
        from tools.generate_test_ids import save_test_ids
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "  Could not import TS test-id capture (%s); skipping inventory.", e
        )
        return

    restore_ref: str | None = None
    if reference_commit:
        try:
            restore_ref = get_head_sha(repo_dir)
            git(repo_dir, "checkout", reference_commit, "--", ".")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "  Could not checkout reference commit for TS test-id capture "
                "(%s); collecting against current tree.",
                e,
            )
            restore_ref = None

    try:
        ids = collect_ts_test_ids_local(
            repo_dir=repo_dir,
            test_dir=test_dir,
            framework=framework,
        )
        ids = _normalize_ts_test_ids(ids, test_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "  TS test-id collection failed for %s (%s); the evaluator will use "
            "the observed test count.",
            repo,
            e,
        )
        ids = []
    finally:
        if restore_ref:
            try:
                git(repo_dir, "checkout", restore_ref, "--", ".")
            except Exception as e:  # noqa: BLE001
                logger.warning("  Could not restore working tree after capture: %s", e)

    if not ids:
        logger.warning(
            "  No TS test IDs discovered for %s; the evaluator will use the "
            "observed test count.",
            repo,
        )
        return

    out_dir = _PROJECT_ROOT / "commit0" / "data" / "test_ids"
    try:
        repo_key = repo.split("/")[-1]
        path = save_test_ids(ids, repo_key, out_dir)
        logger.info("  Saved %d canonical TS test IDs -> %s", len(ids), path)
    except Exception as e:  # noqa: BLE001
        logger.warning("  Failed to save TS test IDs for %s (%s).", repo, e)


def prepare_ts_repo(
    full_name: str,
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    src_dir_override: str | None = None,
    release_tag: str | None = None,
    dry_run: bool = False,
    specs_dir: str = "./specs",
) -> dict | None:
    """Full pipeline for a single TypeScript repo.

    Fork -> Clone -> Detect src -> Stub -> Commit -> Push -> Return entry.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token and not dry_run:
        raise EnvironmentError("GITHUB_TOKEN is required for non-dry-run mode")

    logger.info("Processing %s (org=%s)", full_name, org)

    if dry_run:
        fork_name = f"{org}/{full_name.split('/')[-1]}"
        logger.info("  [DRY RUN] Would fork to %s", fork_name)
    else:
        fork_name = fork_repo(full_name, org, token=token)

    repo_dir = full_clone(full_name, clone_dir, tag=release_tag)
    if release_tag:
        logger.info("  Pinned to tag: %s", release_tag)

    src_dir = src_dir_override or detect_ts_src_dir(repo_dir)
    if not src_dir:
        logger.error("  Cannot detect TypeScript source dir for %s", full_name)
        return None
    logger.info("  Source directory: %s", src_dir)
    _assert_monorepo_safety(repo_dir, src_dir, src_dir_override)

    setup_dict, test_dict, test_framework = generate_setup_dict_ts(repo_dir)
    logger.info(
        "  Test framework: %s, Package manager: %s",
        test_framework,
        setup_dict["install"].split()[0],
    )

    base_commit, reference_commit, functions_stubbed, base_compiles = create_ts_stubbed_branch(
        repo_dir, full_name, src_dir
    )

    # Capture the canonical test inventory -> commit0/data/test_ids/<repo>.bz2 (the
    # AUTHORITATIVE denominator evaluate_ts reads). Runs AFTER
    # create_ts_stubbed_branch because test listing needs node_modules, which that
    # step installs. Best-effort -- never aborts prep.
    _capture_ts_test_ids(
        repo_dir=repo_dir,
        repo=full_name,
        test_dir=test_dict.get("test_dir", "__tests__"),
        framework=test_framework,
        reference_commit=reference_commit,
    )

    if not dry_run:
        branch_name = TS_DATASET_BRANCH
        git(repo_dir, "checkout", branch_name)
        try:
            push_to_fork(repo_dir, fork_name, branch=branch_name, token=token, force_with_lease=False)
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
                    f"Push to {fork_name} FAILED and no usable '{branch_name}' branch "
                    f"exists on the fork. The container build clones this fork and "
                    f"fetches base/reference commits from it, so a dataset built from "
                    f"un-pushed local commits is UNBUILDABLE ('not our ref'). Ensure "
                    f"your token has WRITE access to the fork org (run_trajectory.sh: "
                    f"--org / $KAIJU_FORK_ORG).\nOriginal push error: {e}"
                ) from e

    # ------------------------------------------------------------------
    # Scrape spec PDF and commit into repo (mirrors prepare_repo_go.py).
    # Must run AFTER stubbed-branch creation + initial push so the spec
    # is committed onto the dataset branch; base_commit is then rebased
    # to the post-spec HEAD so Docker clones include spec.pdf.bz2.
    # ------------------------------------------------------------------
    if setup_dict.get("specification"):
        repo_name = full_name.split("/")[-1]
        docs_url = setup_dict["specification"]
        logger.info("  Scraping spec from: %s", docs_url)
        try:
            scrape_fn = _get_scrape_func()
            spec_path = scrape_fn(
                base_url=docs_url,
                name=repo_name,
                output_dir=str(specs_dir),
                compress=True,
            )
            if spec_path:
                logger.info("  Spec saved: %s", spec_path)
                branch_name = TS_DATASET_BRANCH
                git(repo_dir, "checkout", branch_name)
                dest = repo_dir / "spec.pdf.bz2"
                shutil.copy2(spec_path, dest)
                git(repo_dir, "add", "spec.pdf.bz2")
                git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_name}")
                base_commit = get_head_sha(repo_dir)
                logger.info("  Updated base_commit with spec: %s", base_commit[:12])
                if not dry_run:
                    try:
                        push_to_fork(
                            repo_dir,
                            fork_name,
                            branch=branch_name,
                            token=token,
                            force_with_lease=False,
                        )
                    except Exception as e:
                        logger.warning("  Spec push failed: %s", e)
            else:
                logger.warning("  Spec scraping returned no output")
        except ImportError:
            logger.warning(
                "  Skipping spec scrape -- install: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests && playwright install chromium"
            )
        except Exception as e:
            logger.warning("  Spec scraping failed (non-fatal): %s", e)

    # README-based spec fallback: runs when URL scraping found nothing
    if not (repo_dir / "spec.pdf.bz2").exists() and not dry_run:
        repo_short = full_name.split("/")[-1]
        try:
            from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec

            readme_spec_path, readme_spec_url = _scrape_readme_spec(
                repo_dir, specs_dir, repo_short
            )
        except ImportError:
            readme_spec_path, readme_spec_url = None, ""
        if readme_spec_path:
            if readme_spec_url:
                setup_dict["specification"] = readme_spec_url
            try:
                git(repo_dir, "checkout", TS_DATASET_BRANCH)
                shutil.copy2(str(readme_spec_path), str(repo_dir / "spec.pdf.bz2"))
                git(repo_dir, "add", "spec.pdf.bz2")
                git(repo_dir, "commit", "-m", f"Add README-based spec for {repo_short}")
                base_commit = get_head_sha(repo_dir)
                logger.info("  README spec committed: %s", base_commit[:12])
                push_to_fork(repo_dir, fork_name, branch=TS_DATASET_BRANCH, token=token, force_with_lease=False)
            except Exception as e:
                logger.warning("  README spec fallback failed: %s", e)

    return {
        "instance_id": f"commit-0/{full_name.split('/')[-1]}",
        "id": str(_uuid_mod.uuid4()),
        "repo": fork_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "src_dir": src_dir,
        "language": "typescript",
        "test_framework": test_framework,
        "functions_stubbed": functions_stubbed,
        # Tri-state (True=stubbed base parses via tsc / None=couldn't check). A
        # fatal parse error raises earlier, so a committed base is never corrupt.
        "base_compiles": base_compiles,
        "setup": setup_dict,
        "test": test_dict,
    }


def main() -> None:
    """CLI entry point for prepare_repo_ts.py.

    Supports two modes (mirrors prepare_repo_go.py):

    * ``--repo owner/name``   -- prepare a single repo.
    * ``input_file``          -- batch mode. Accepts a ``validated.json``-shaped
                                  file (list of ``{full_name|repo, tag?}`` dicts,
                                  or ``{"data": [...]}``). Iterates through all
                                  entries; honours ``--max-repos``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Prepare TypeScript repos for commit0 dataset"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Batch input JSON (e.g. validated_ts.json). Mutually exclusive with --repo.",
    )
    parser.add_argument(
        "--repo", default=None, help="owner/name of a single GitHub repo"
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"GitHub org for fork (default: {DEFAULT_ORG})",
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
        help="Output entries JSON file (default: stdout for single-repo, required for batch)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip fork and push")
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Batch mode: maximum number of repos to prepare",
    )
    parser.add_argument(
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory to save scraped spec PDFs (default: ./specs)",
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
        result = prepare_ts_repo(
            full_name=args.repo,
            clone_dir=args.clone_dir,
            org=args.org,
            src_dir_override=args.src_dir,
            release_tag=args.tag,
            dry_run=args.dry_run,
            specs_dir=args.specs_dir,
        )
        if result is None:
            logger.error("Failed to prepare %s", args.repo)
            sys.exit(1)
        entries.append(result)
    else:
        candidates = json.loads(Path(args.input_file).read_text())
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
                result = prepare_ts_repo(
                    full_name=full_name,
                    clone_dir=args.clone_dir,
                    org=args.org,
                    src_dir_override=candidate.get("src_dir_override")
                    or candidate.get("src_dir"),
                    release_tag=candidate.get("tag") or args.tag,
                    dry_run=args.dry_run,
                    specs_dir=args.specs_dir,
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
        with open(_entries_path, "w") as f:
            json.dump(entries, f, indent=2)
        logger.info("Wrote %d entries to %s (consolidated)", len(entries), _entries_path)
        for _entry in entries:
            _short = _entry.get("original_repo", "").split("/")[-1]
            _candidates = [args.clone_dir / _short / "spec.pdf.bz2"]
            for _src in _candidates:
                if _src.exists():
                    _dest = consolidated_spec_path(_entry["id"])
                    shutil.copy2(str(_src), str(_dest))
                    logger.info("  Snapshotted spec.pdf.bz2 → %s", _dest)
                    break
        if args.output:
            _legacy_path = Path(args.output)
            with open(_legacy_path, "w") as f:
                json.dump(entries, f, indent=2)
            logger.info("Also wrote legacy copy to %s", _legacy_path)
    elif args.output:
        output_path = Path(args.output)
        with open(output_path, "w") as f:
            json.dump(entries, f, indent=2)
        logger.info("Wrote %d entries to %s", len(entries), output_path)
    elif len(entries) == 1:
        print(json.dumps(entries[0], indent=2))
    else:
        print(json.dumps(entries, indent=2))

    # Generate the commit0-ts build config (parity with prepare_repo_go/rust) so
    # `commit0 ts build --commit0-config-file .commit0_ts.yaml` works without the
    # operator hand-writing dataset_name/split/repo_split/base_dir.
    if entries:
        try:
            _ds_name = (
                f"./{Path(args.output).name}" if args.output else "ts_custom_dataset.json"
            )
            _cfg = _PROJECT_ROOT / ".commit0_ts.yaml"
            _first = entries[0]
            _cfg.write_text(
                f"# commit0 TS config for {_first.get('original_repo', '?')}\n"
                f"dataset_name: {_ds_name}\n"
                "dataset_split: test\n"
                "repo_split: all\n"
                "base_dir: repos_ts\n"
                f"# fork: {_first.get('repo', '?')}\n"
            )
            logger.info("Generated config: %s", _cfg)
        except Exception as _cfg_err:  # noqa: BLE001 - best-effort
            logger.warning("commit0-ts config generation failed: %s", _cfg_err)

    if entries:
        logger.info(
            "Done: %s",
            ", ".join(e["instance_id"] for e in entries),
        )


if __name__ == "__main__":
    main()
