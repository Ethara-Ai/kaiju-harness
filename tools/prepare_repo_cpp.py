"""Prepare C++ repos for a commit0 dataset.

For each repo:
1. Fork to zahgon GitHub org
2. Clone locally, record reference_commit (HEAD)
3. Create 'commit0_all' branch
4. Detect build system (cmake/meson/autotools/make)
5. Generate compile_commands.json
6. Run cppstubber on source files
7. Verify stubbed code compiles
8. Commit stubbed version as base_commit
9. Push commit0_all branch to fork
10. Collect test IDs
11. Save test IDs as .bz2
12. Append entry to cpp_dataset.json
13. Generate per-repo YAML config

Usage:
    python3 -m tools.prepare_repo_cpp \
        --upstream fmtlib/fmt \
        --src-dir src \
        --test-cmd "ctest --test-dir build --output-on-failure"

    # Dry run (no fork, no push):
    python3 -m tools.prepare_repo_cpp \
        --upstream fmtlib/fmt \
        --src-dir src \
        --test-cmd "ctest --test-dir build --output-on-failure" \
        --dry-run

Requires:
    - gh CLI installed (for forking)
    - cppstubber binary built at tools/cppstubber/build/cppstubber (or tree-sitter fallback)
    - cmake/meson/autotools installed (for build system detection)
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from kaiju.paths import datasets_dir, spec_path as consolidated_spec_path
import uuid as _uuid_mod

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
PROJECT_ROOT = TOOLS_DIR.parent
CPPSTUBBER = TOOLS_DIR / "cppstubber" / "build" / "cppstubber"
DATA_DIR = PROJECT_ROOT / "commit0" / "data"
TEST_IDS_DIR = DATA_DIR / "cpp_test_ids"
CONSTANTS_CPP_FILE = PROJECT_ROOT / "commit0" / "harness" / "constants_cpp.py"
SPECS_DIR = PROJECT_ROOT / "specs_cpp"

DEFAULT_ORG = "Zahgon"

_CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".h"}
_SKIP_DIRS = {"build", "cmake-build-debug", "cmake-build-release", "builddir",
              ".cache", "_deps", "third_party", "vendor", "extern", ".git"}




def get_head_sha(repo_dir: Path) -> str:
    return git(repo_dir, "rev-parse", "HEAD")


def get_default_branch(repo_dir: Path) -> str:
    try:
        ref = git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
        return ref.split("/")[-1]
    except subprocess.CalledProcessError:
        for branch in ["main", "master"]:
            try:
                git(repo_dir, "rev-parse", f"refs/remotes/origin/{branch}")
                return branch
            except subprocess.CalledProcessError:
                continue
        return "main"




def clone_repo(full_name: str, clone_dir: Path) -> Path:
    repo_name = full_name.split("/")[-1]
    repo_dir = clone_dir / repo_name

    if repo_dir.exists():
        logger.info("Clone already exists: %s", repo_dir)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    logger.info("Cloning %s...", full_name)
    subprocess.run(
        ["git", "clone", url, str(repo_dir)],
        capture_output=True, text=True, timeout=600, check=True,
    )
    return repo_dir


def detect_build_system(repo_dir: Path) -> str:
    if (repo_dir / "CMakeLists.txt").exists():
        return "cmake"
    if (repo_dir / "meson.build").exists():
        return "meson"
    if (repo_dir / "configure.ac").exists() or (repo_dir / "configure.in").exists():
        return "autotools"
    if (repo_dir / "Makefile").exists():
        return "make"
    raise RuntimeError(
        f"No supported build system found in {repo_dir}. "
        "Expected: CMakeLists.txt, meson.build, configure.ac, or Makefile"
    )


def generate_compile_commands(
    repo_dir: Path,
    build_system: str,
    cmake_options: list[str] | None = None,
) -> bool:
    logger.info("Generating compile_commands.json (build_system=%s)...", build_system)

    if build_system == "cmake":
        build_dir = repo_dir / "build"
        build_dir.mkdir(exist_ok=True)
        cmake_cfg_cmd: list[str] = ["cmake", "-B", "build", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]
        if cmake_options:
            cmake_cfg_cmd.extend(cmake_options)
        result = subprocess.run(
            cmake_cfg_cmd,
            cwd=repo_dir, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            logger.warning("cmake configure failed: %s", result.stderr[:500])
            return False
        cc_json = build_dir / "compile_commands.json"
        if cc_json.exists():
            shutil.copy2(str(cc_json), str(repo_dir / "compile_commands.json"))
            return True

    elif build_system == "meson":
        builddir = repo_dir / "builddir"
        if not builddir.exists():
            result = subprocess.run(
                ["meson", "setup", "builddir"],
                cwd=repo_dir, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                logger.warning("meson setup failed: %s", result.stderr[:500])
                return False
        cc_json = builddir / "compile_commands.json"
        if cc_json.exists():
            shutil.copy2(str(cc_json), str(repo_dir / "compile_commands.json"))
            return True

    elif build_system == "autotools":
        for step in [["autoreconf", "-fi"], ["./configure"]]:
            result = subprocess.run(
                step, cwd=repo_dir, capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                logger.warning("%s failed: %s", step[0], result.stderr[:300])
        result = subprocess.run(
            ["bear", "--", "make", "-j4"],
            cwd=repo_dir, capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            logger.warning("bear -- make failed: %s", result.stderr[:500])

    elif build_system == "make":
        result = subprocess.run(
            ["bear", "--", "make", "-j4"],
            cwd=repo_dir, capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            logger.warning("bear -- make failed: %s", result.stderr[:500])

    return (repo_dir / "compile_commands.json").exists()


def _collect_cpp_files(directory: Path) -> list[str]:
    CPP_EXTS = {".cpp", ".cc", ".cxx", ".c++",
                ".hpp", ".hh", ".hxx", ".h++", ".h"}
    SKIP_DIRS = {"build", "cmake-build-debug", "cmake-build-release", "builddir",
                 ".cache", "_deps", "third_party", "vendor", "extern", ".git", "test", "tests"}
    result = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if Path(f).suffix in CPP_EXTS:
                result.append(str(Path(root) / f))
    return sorted(result)


def _strip_null_bytes(directory: Path) -> int:
    ALL_CPP_EXTS = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".h"}
    cleaned = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if Path(f).suffix in ALL_CPP_EXTS:
                p = Path(root) / f
                data = p.read_bytes()
                stripped = data.rstrip(b"\x00")
                if len(stripped) < len(data):
                    p.write_bytes(stripped)
                    cleaned += 1
    return cleaned


def stub_source_dir(repo_dir: Path, src_dir_relative: str, build_system: str) -> tuple[int, int]:
    src_dir = repo_dir / src_dir_relative
    if not src_dir.is_dir():
        logger.error("Source directory not found: %s", src_dir)
        return 0, 0

    has_compdb = (repo_dir / "compile_commands.json").exists()

    cpp_files = _collect_cpp_files(src_dir) if has_compdb else []
    use_compdb_mode = CPPSTUBBER.exists() and has_compdb and bool(cpp_files)

    if use_compdb_mode:
        logger.info("Running cppstubber on %d files in %s (compile_commands.json, per-file)",
                     len(cpp_files), src_dir_relative)
        aggregated_stdout = []
        aggregated_stderr = []
        crashed = 0
        compdb_dir = repo_dir / "build"
        if not (compdb_dir / "compile_commands.json").exists():
            compdb_dir = repo_dir
        for cf in cpp_files:
            cf_abs = str(Path(cf).resolve())
            cmd = [str(CPPSTUBBER), "-p", str(compdb_dir.resolve()), "--in-place",
                   "--extra-arg=--gcc-toolchain=/usr", cf_abs]
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60, cwd=repo_dir,
                )
                if r.stdout:
                    aggregated_stdout.append(r.stdout)
                if r.returncode < 0:
                    crashed += 1
                    aggregated_stderr.append(f"{Path(cf).name}: crashed signal={-r.returncode}")
                elif r.stderr and r.returncode != 0:
                    aggregated_stderr.append(f"{Path(cf).name}: {r.stderr[:150]}")
            except subprocess.TimeoutExpired:
                aggregated_stderr.append(f"{Path(cf).name}: TIMEOUT after 60s")
        if aggregated_stderr:
            logger.info("cppstubber issues (first 3 of %d): %s",
                        len(aggregated_stderr), aggregated_stderr[:3])
        if crashed:
            logger.warning("cppstubber crashed on %d file(s) — partial coverage", crashed)
        class _PseudoResult:
            returncode = 0
            stdout = "\n".join(aggregated_stdout)
            stderr = ""
        result = _PseudoResult()
    elif CPPSTUBBER.exists():
        logger.info("Running cppstubber on %s (--input-dir mode)", src_dir_relative)
        try:
            result = subprocess.run(
                [str(CPPSTUBBER), "--input-dir", str(src_dir), "--in-place"],
                capture_output=True, text=True, timeout=300, cwd=repo_dir,
            )
        except subprocess.TimeoutExpired:
            logger.error("cppstubber timed out on %s", src_dir_relative)
            return 0, 1
    else:
        logger.warning("cppstubber not available, using tree-sitter fallback")
        try:
            from tools.stub_cpp import stub_cpp_directory, count_stubs
            stub_cpp_directory(str(src_dir))
            stub_count = count_stubs(str(src_dir))
            logger.info("Tree-sitter fallback: %d stubs placed", stub_count)
            return stub_count, 0
        except ImportError:
            logger.error("Neither cppstubber nor tree-sitter fallback available")
            return 0, 1
        except Exception as exc:
            logger.error("Tree-sitter stubbing failed: %s", exc)
            return 0, 1

    ok, fail = 0, 0
    for line in (result.stdout + result.stderr).splitlines():
        m_ok = re.search(r"(\d+)\s+functions?\s+stubbed", line) or \
               re.search(r"[Ff]unctions?\s+stubbed:\s*(\d+)", line)
        m_files = re.search(r"(\d+)\s+files?\s+processed", line) or \
                  re.search(r"[Ff]iles?\s+processed:\s*(\d+)", line)
        if m_files:
            ok = int(m_files.group(1))
        if m_ok:
            ok = int(m_ok.group(1))

    if result.returncode != 0:
        logger.warning("cppstubber exited %d: %s", result.returncode, result.stderr.strip()[:500])

    logger.info("Stubbed source (cppstubber): %d items", ok)
    return ok, fail


def verify_compiles(
    repo_dir: Path,
    build_system: str,
    cmake_options: list[str] | None = None,
) -> bool:
    logger.info("Verifying compilation (build_system=%s)...", build_system)

    if build_system == "cmake" and cmake_options:
        reconfigure = subprocess.run(
            ["cmake", "-B", "build", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", *cmake_options],
            cwd=repo_dir, capture_output=True, text=True, timeout=300,
        )
        if reconfigure.returncode != 0:
            logger.warning(
                "cmake reconfigure with %s failed: %s",
                cmake_options, reconfigure.stderr[:500],
            )

    if build_system == "cmake":
        cmd = ["cmake", "--build", "build", "-j4"]
    elif build_system == "meson":
        cmd = ["ninja", "-C", "builddir"]
    elif build_system in ("autotools", "make"):
        cmd = ["make", "-j4"]
    else:
        cmd = ["make", "-j4"]

    result = subprocess.run(
        cmd, cwd=repo_dir, capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        logger.error("Compilation failed:\n%s", result.stderr[:2000])
        return False
    logger.info("Compilation check passed")
    return True


def collect_test_ids(repo_dir: Path, test_cmd: str, build_system: str) -> list[str]:
    logger.info("Collecting test IDs...")
    test_ids: list[str] = []

    if build_system == "cmake":
        build_dir = repo_dir / "build"
        if build_dir.exists():
            try:
                result = subprocess.run(
                    ["ctest", "--test-dir", str(build_dir), "--show-only=json-v1"],
                    capture_output=True, text=True, timeout=60, cwd=repo_dir,
                )
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    tests = data.get("tests", [])
                    test_ids = [t["name"] for t in tests if "name" in t]
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                logger.warning("CTest JSON listing failed: %s", exc)

    if not test_ids:
        build_dir = repo_dir / "build"
        if build_dir.exists():
            for root, dirs, files in os.walk(build_dir):
                dirs[:] = [d for d in dirs if d not in {"CMakeFiles", "_deps"}]
                for f in files:
                    fp = os.path.join(root, f)
                    if os.access(fp, os.X_OK) and "test" in f.lower():
                        try:
                            result = subprocess.run(
                                [fp, "--gtest_list_tests"],
                                capture_output=True, text=True, timeout=30,
                            )
                            if result.returncode == 0:
                                current_suite = ""
                                for line in result.stdout.splitlines():
                                    if not line.strip():
                                        continue
                                    if not line.startswith(" ") and not line.startswith("\t"):
                                        current_suite = line.strip().rstrip(".")
                                    else:
                                        name = line.strip().split("#")[0].strip()
                                        if name:
                                            test_ids.append(f"{current_suite}.{name}")
                        except (subprocess.TimeoutExpired, OSError):
                            pass
                        if test_ids:
                            break
                if test_ids:
                    break

    if not test_ids:
        from tools.generate_test_ids_cpp import _collect_test_ids_from_source
        test_ids = _collect_test_ids_from_source(repo_dir)

    logger.info("Collected %d test IDs", len(test_ids))
    return sorted(set(test_ids))


def save_test_ids(repo_name: str, test_ids: list[str]) -> Path:
    TEST_IDS_DIR.mkdir(parents=True, exist_ok=True)
    bz2_path = TEST_IDS_DIR / f"{repo_name}.bz2"
    content = "\n".join(test_ids) + "\n" if test_ids else ""
    bz2_path.write_bytes(bz2.compress(content.encode()))
    logger.info("Saved test IDs to %s", bz2_path)
    return bz2_path


def create_dataset_entry(
    upstream: str,
    fork_name: str,
    repo_name: str,
    src_dirs: list[str],
    test_cmd: str,
    base_commit: str,
    reference_commit: str,
    build_system: str = "cmake",
    cpp_standard: str = "17",
    test_framework: str = "",
    packages: str = "",
    spec_url: str = "",
    version_source: str = "default",
    version_conflicts: list[str] | None = None,
    cmake_options: list[str] | None = None,
) -> dict:
    primary_src = src_dirs[0] if src_dirs else "."
    test_dir = primary_src.rsplit("/src", 1)[0] if "/src" in primary_src else "."

    cmake_opts_str = (" " + " ".join(cmake_options)) if cmake_options else ""
    install_cmake = (
        f"cmake -B build{cmake_opts_str} -DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
        " && cmake --build build -j$(nproc)"
    )

    return {
        "instance_id": f"commit-0/{repo_name}",
        "id": str(_uuid_mod.uuid4()),
        "repo": fork_name,
        "original_repo": upstream,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "setup": {
            "build_system": build_system,
            "cpp_standard": cpp_standard,
            "packages": packages,
            "specification": spec_url,
            "pre_install": [],
            "install": install_cmake
            if build_system == "cmake"
            else "meson setup builddir && ninja -C builddir"
            if build_system == "meson"
            else "make -j$(nproc)",
            "version_source": version_source,
            "version_conflicts": version_conflicts or [],
        },
        "test": {
            "test_cmd": test_cmd,
            "test_dir": test_dir,
            "test_framework": test_framework,
        },
        "src_dir": ",".join(src_dirs),
        "language": "cpp",
    }


def get_dataset_path(repo_name: str) -> Path:
    return PROJECT_ROOT / f"{repo_name}_cpp_dataset.json"


def append_to_dataset(entry: dict, repo_name: str) -> Path:
    dataset_file = get_dataset_path(repo_name)

    existing = []
    if dataset_file.exists():
        raw = dataset_file.read_text().strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, list):
                existing = data
            elif isinstance(data, dict):
                existing = [data]

    existing = [e for e in existing if e.get("instance_id") != entry["instance_id"]]
    existing.append(entry)

    content = json.dumps(existing, indent=2) + "\n"
    dataset_file.write_text(content)
    logger.info("Updated %s (%d entries)", dataset_file, len(existing))
    return dataset_file


def update_cpp_split(fork_name: str) -> None:
    if not CONSTANTS_CPP_FILE.exists():
        logger.warning("constants_cpp.py not found at %s", CONSTANTS_CPP_FILE)
        return

    content = CONSTANTS_CPP_FILE.read_text()

    if f'"{fork_name}"' in content:
        logger.info("CPP_SPLIT already contains %s", fork_name)
        return

    pattern = r'(CPP_SPLIT:\s*Dict\[str,\s*list\[str\]\]\s*=\s*\{[^}]*"all":\s*\[)(.*?)(\s*\],)'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        logger.warning("Could not parse CPP_SPLIT in constants_cpp.py")
        return

    before = match.group(1)
    existing_entries = match.group(2)
    after = match.group(3)

    new_entry = f'\n        "{fork_name}",'
    new_content = content[:match.start()] + before + existing_entries + new_entry + after + content[match.end():]

    CONSTANTS_CPP_FILE.write_text(new_content)
    logger.info("Added %s to CPP_SPLIT", fork_name)




def scrape_spec(repo_dir: Path, repo_short: str, spec_url: str, specs_dir: Path) -> bool:
    """Scrape spec PDF and commit into repo. Returns True if successful."""
    dest = repo_dir / "spec.pdf.bz2"
    if dest.exists():
        logger.info("spec.pdf.bz2 already exists, skipping")
        return True

    cached = specs_dir / f"{repo_short}.pdf.bz2"
    if cached.exists():
        shutil.copy2(cached, dest)
        git(repo_dir, "add", "spec.pdf.bz2")
        git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_short}")
        logger.info("Used cached spec from %s", cached)
        return True

    if not spec_url:
        logger.info("No spec URL provided, skipping spec generation")
        return False

    try:
        from tools.scrape_pdf import scrape_spec_sync
        logger.info("Scraping spec from: %s", spec_url)
        spec_path = scrape_spec_sync(
            base_url=spec_url,
            name=repo_short,
            output_dir=str(specs_dir),
            compress=True,
        )
        if spec_path:
            shutil.copy2(spec_path, dest)
            git(repo_dir, "add", "spec.pdf.bz2")
            git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_short}")
            logger.info("Spec saved and committed")
            return True
        logger.warning("Spec scraping returned no output")
        return False
    except ImportError:
        logger.warning("scrape_pdf not available (install: pip install playwright PyMuPDF PyPDF2 beautifulsoup4)")
        return False
    except Exception as e:
        logger.warning("Spec scraping failed: %s", e)
        return False


def generate_commit0_yaml(repo_name: str, entry: dict) -> Path:
    yaml_path = PROJECT_ROOT / ".commit0_cpp.yaml"
    dataset_file = f"./{repo_name}_cpp_dataset.json"

    content = f"""# commit0 C++ config for {repo_name}
dataset_name: {dataset_file}
dataset_split: test
repo_split: all
base_dir: repos

# Repo details
# upstream: {entry["original_repo"]}
# fork: {entry["repo"]}
# language: cpp
# build_system: {entry["setup"]["build_system"]}
# test_cmd: {entry["test"]["test_cmd"]}
# src_dir: {entry["src_dir"]}
"""
    yaml_path.write_text(content)
    logger.info("Generated config: %s", yaml_path)
    return yaml_path


def prepare_cpp_repo(
    upstream: str,
    src_dirs: list[str],
    test_cmd: str,
    org: str = DEFAULT_ORG,
    clone_dir: Path | None = None,
    dry_run: bool = False,
    cpp_standard: str = "17",
    packages: str = "",
    skip_compile_check: bool = False,
    skip_spec: bool = False,
    spec_url: str = "",
    build_system: str = "auto",
    cmake_options: list[str] | None = None,
) -> dict | None:
    repo_name = upstream.split("/")[-1]

    if clone_dir is None:
        clone_dir = Path("repos_staging")

    logger.info("=" * 60)
    logger.info("Preparing: %s", upstream)
    logger.info("=" * 60)

    if dry_run:
        fork_name = f"{org}/{repo_name}"
        logger.info("[DRY RUN] Would fork %s to %s", upstream, org)
    else:
        fork_name = fork_repo(upstream, org)

    repo_dir = clone_repo(fork_name, clone_dir)

    reference_commit = get_head_sha(repo_dir)
    logger.info("Reference commit: %s", reference_commit[:12])

    if build_system == "auto":
        build_system = detect_build_system(repo_dir)
    logger.info("Build system: %s", build_system)

    default_branch = get_default_branch(repo_dir)
    try:
        git(repo_dir, "checkout", "-b", "commit0_all")
    except subprocess.CalledProcessError:
        git(repo_dir, "checkout", "commit0_all")
        git(repo_dir, "reset", "--hard", default_branch)

    has_cc = generate_compile_commands(repo_dir, build_system, cmake_options=cmake_options)
    if has_cc:
        logger.info("compile_commands.json generated")
    else:
        logger.warning("compile_commands.json not generated (stubber may use fallback)")

    ok, fail = 0, 0
    for sd in src_dirs:
        logger.info("Stubbing source dir: %s", sd)
        sd_ok, sd_fail = stub_source_dir(repo_dir, sd, build_system)
        ok += sd_ok
        fail += sd_fail
    if ok == 0:
        logger.error("No files/functions were stubbed across any of %s. Aborting.", src_dirs)
        return None

    cleaned = 0
    for sd in src_dirs:
        cleaned += _strip_null_bytes(repo_dir / sd)
    if cleaned:
        logger.info("Stripped trailing null bytes from %d files", cleaned)

    if not skip_compile_check:
        if not verify_compiles(repo_dir, build_system, cmake_options=cmake_options):
            logger.error("Stubbed code does not compile. Aborting.")
            return None

    gitignore = repo_dir / ".gitignore"
    gitignore_text = gitignore.read_text() if gitignore.exists() else ""
    if "compile_commands.json" not in gitignore_text:
        with gitignore.open("a") as f:
            f.write("\ncompile_commands.json\n")

    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-m", "Commit 0")

    readme_spec_url = ""
    if not skip_spec:
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        scrape_spec(repo_dir, repo_name, spec_url, SPECS_DIR)
        if not (repo_dir / "spec.pdf.bz2").exists() and not dry_run:
            try:
                from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
                readme_spec_path, readme_spec_url = _scrape_readme_spec(repo_dir, SPECS_DIR, repo_name)
            except ImportError:
                readme_spec_path = None
            if readme_spec_path:
                try:
                    git(repo_dir, "checkout", "commit0_all")
                    shutil.copy2(str(readme_spec_path), str(repo_dir / "spec.pdf.bz2"))
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add README-based spec for {repo_name}")
                    logger.info("  README spec committed")
                except Exception as e:
                    logger.warning("  README spec fallback failed: %s", e)

    base_commit = get_head_sha(repo_dir)
    logger.info("Base commit: %s", base_commit[:12])

    if dry_run:
        logger.info("[DRY RUN] Would push commit0_all to %s", fork_name)
    else:
        logger.info("Pushing commit0_all to %s...", fork_name)
        push_to_fork(repo_dir, fork_name, "commit0_all", remote_name="origin")

    git(repo_dir, "checkout", default_branch)
    test_ids = collect_test_ids(repo_dir, test_cmd, build_system)
    save_test_ids(repo_name, test_ids)
    git(repo_dir, "checkout", "commit0_all")

    test_framework = ""
    for tf_name, tf_marker in [
        ("gtest", "gtest"),
        ("catch2", "catch2"),
        ("doctest", "doctest"),
        ("boost_test", "boost/test"),
    ]:
        cmake_file = repo_dir / "CMakeLists.txt"
        if cmake_file.exists():
            cmake_text = cmake_file.read_text(errors="ignore").lower()
            if tf_marker in cmake_text:
                test_framework = tf_name
                break

    # Canonical C++ standard detection (CMakeLists/Makefile/meson). CLI
    # kwarg wins only when user explicitly overrode the default.
    from tools.cpp_version import detect_cpp as _detect_cpp

    cpp_det = _detect_cpp(repo_dir)
    detected_std = cpp_det.version or cpp_standard
    version_source = cpp_det.source
    version_conflicts = cpp_det.conflicts
    if cpp_standard == "17":  # default — honor detection
        cpp_standard = detected_std
    logger.info(
        "C++ detection: standard=%s (src=%s) conflicts=%s",
        cpp_standard, version_source, version_conflicts or "(none)",
    )

    entry = create_dataset_entry(
        upstream=upstream,
        fork_name=fork_name,
        repo_name=repo_name,
        src_dirs=src_dirs,
        test_cmd=test_cmd,
        base_commit=base_commit,
        reference_commit=reference_commit,
        build_system=build_system,
        cpp_standard=cpp_standard,
        version_source=version_source,
        version_conflicts=version_conflicts,
        test_framework=test_framework,
        packages=packages,
        spec_url=readme_spec_url or spec_url,
        cmake_options=cmake_options,
    )

    if not dry_run:
        append_to_dataset(entry, repo_name)
        update_cpp_split(fork_name)
        generate_commit0_yaml(repo_name, entry)
    else:
        logger.info("[DRY RUN] Dataset entry:\n%s", json.dumps(entry, indent=2))

    logger.info("=" * 60)
    logger.info("SUCCESS: %s prepared", repo_name)
    logger.info("  fork:       %s", fork_name)
    logger.info("  reference:  %s", reference_commit[:12])
    logger.info("  base:       %s", base_commit[:12])
    logger.info("  test IDs:   %d", len(test_ids))
    logger.info("  stubbed:    %d", ok)
    logger.info("  build:      %s", build_system)
    logger.info("  framework:  %s", test_framework or "(auto-detect)")
    logger.info("=" * 60)

    return entry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a C++ repo for commit0 dataset"
    )
    parser.add_argument(
        "--repo", default=None,
        help="Upstream repo (e.g. fmtlib/fmt). Falls back to --upstream.",
    )
    parser.add_argument(
        "--upstream", default=None,
        help="Alias for --repo (kept for backwards compatibility).",
    )
    parser.add_argument(
        "--src-dir", required=True,
        help="Comma-separated source dir(s) (e.g. 'src' or 'src,include/fmt')",
    )
    parser.add_argument(
        "--test-cmd", required=True,
        help='Test command (e.g. "ctest --test-dir build --output-on-failure")',
    )
    parser.add_argument(
        "--org", default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--clone-dir", type=Path, default=Path("repos_staging"),
        help="Directory for local clones (default: ./repos_staging)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip fork, push, and dataset writes",
    )
    parser.add_argument(
        "--cpp-standard", default="17",
        help="C++ standard (default: 17)",
    )
    parser.add_argument(
        "--packages", default="",
        help="Additional system packages needed",
    )
    parser.add_argument(
        "--skip-compile-check", action="store_true",
        help="Skip compilation check after stubbing",
    )
    parser.add_argument(
        "--skip-spec", action="store_true",
        help="Skip spec generation",
    )
    parser.add_argument(
        "--spec-url", default="",
        help="Documentation URL to scrape as spec PDF (e.g. https://fmt.dev/latest/)",
    )
    parser.add_argument(
        "--build-system", default="auto",
        choices=["auto", "cmake", "meson", "autotools", "make"],
        help="Build system to use (default: auto-detect)",
    )
    parser.add_argument(
        "--cmake-options", nargs="+", default=None, metavar="OPT",
        help=(
            "Extra CMake -D options to inject into BOTH the dataset's setup.install line "
            "(used by the Docker build) AND the local cmake configure/verify steps. "
            "Example: --cmake-options -DCXXOPTS_ENABLE_WARNINGS=OFF -DCXXOPTS_BUILD_TESTS=ON. "
            "Required for repos that ship -Werror by default."
        ),
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

    if args.repo is None:
        args.repo = args.upstream
    if not args.repo:
        parser.error("--repo (or --upstream) is required")

    setup_git_credentials(dry_run=args.dry_run)

    src_dirs = [d.strip() for d in args.src_dir.split(",") if d.strip()]
    if not src_dirs:
        parser.error("--src-dir must contain at least one non-empty path")

    entry = prepare_cpp_repo(
        upstream=args.repo,
        src_dirs=src_dirs,
        test_cmd=args.test_cmd,
        org=args.org,
        clone_dir=args.clone_dir,
        dry_run=args.dry_run,
        cpp_standard=args.cpp_standard,
        packages=args.packages,
        skip_compile_check=args.skip_compile_check,
        skip_spec=args.skip_spec,
        spec_url=args.spec_url,
        build_system=args.build_system,
        cmake_options=args.cmake_options,
    )

    if entry is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
