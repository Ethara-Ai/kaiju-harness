"""Generate C++ test ID files (.bz2) for commit0 C++ repos.

Discovers C++ test IDs via CTest, GTest, Catch2, or doctest test listing.
Saves them as bz2-compressed files compatible with commit0's evaluation harness.

Usage:
    python -m tools.generate_test_ids_cpp --repo-dir /path/to/repo --name mylib --output-dir ./test_ids

    python -m tools.generate_test_ids_cpp dataset.json --output-dir ./test_ids

    python -m tools.generate_test_ids_cpp dataset.json --install

Requires:
    - cmake/meson installed for build-system-based discovery
    - Optionally: docker Python SDK for Docker-based collection
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
PROJECT_ROOT = TOOLS_DIR.parent
DATA_DIR = PROJECT_ROOT / "commit0" / "data"
DEFAULT_OUTPUT_DIR = DATA_DIR / "cpp_test_ids"


def collect_test_ids_ctest(
    repo_dir: Path,
    build_dir: str = "build",
) -> list[str]:
    build_path = repo_dir / build_dir
    if not build_path.exists():
        for alt in ["builddir", "cmake-build-release", "cmake-build-debug"]:
            alt_path = repo_dir / alt
            if alt_path.exists():
                build_path = alt_path
                break

    if not build_path.exists():
        logger.warning("No build directory found in %s", repo_dir)
        return []

    try:
        result = subprocess.run(
            ["ctest", "--test-dir", str(build_path), "--show-only=json-v1"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=repo_dir,
        )
        if result.returncode != 0:
            logger.warning(
                "ctest --show-only failed (rc=%d): %s",
                result.returncode,
                result.stderr[:500],
            )
            return []

        data = json.loads(result.stdout)
        tests = data.get("tests", [])
        return sorted(t["name"] for t in tests if "name" in t)

    except FileNotFoundError:
        logger.warning("ctest not found on PATH")
    except subprocess.TimeoutExpired:
        logger.warning("ctest --show-only timed out in %s", repo_dir)
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Failed to parse ctest JSON: %s", exc)
    except OSError as exc:
        logger.warning("ctest failed: %s", exc)

    return []


def collect_test_ids_gtest(
    repo_dir: Path,
    test_binary: str = "",
) -> list[str]:
    if not test_binary:
        build_path = repo_dir / "build"
        if not build_path.exists():
            return []
        candidates = []
        for root, dirs, files in os.walk(build_path):
            dirs[:] = [d for d in dirs if d not in {".git", "_deps", "CMakeFiles"}]
            for f in files:
                fp = os.path.join(root, f)
                if os.access(fp, os.X_OK) and "test" in f.lower():
                    candidates.append(fp)
        if not candidates:
            return []
        test_binary = candidates[0]

    try:
        result = subprocess.run(
            [test_binary, "--gtest_list_tests"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_dir,
        )
        if result.returncode != 0:
            return []

        test_ids: list[str] = []
        current_suite = ""
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            if not line.startswith(" ") and not line.startswith("\t"):
                current_suite = line.strip().rstrip(".")
            else:
                test_name = line.strip().split("#")[0].strip()
                if test_name:
                    test_ids.append(f"{current_suite}.{test_name}")
        return sorted(test_ids)

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def collect_test_ids_catch2(
    repo_dir: Path,
    test_binary: str = "",
) -> list[str]:
    if not test_binary:
        return []

    try:
        result = subprocess.run(
            [test_binary, "--list-tests"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_dir,
        )
        if result.returncode != 0:
            return []

        test_ids: list[str] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("All available") and not stripped.startswith("Matching"):
                test_ids.append(stripped)
        return sorted(test_ids)

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def collect_test_ids_local(
    repo_dir: Path,
    strategy: str = "auto",
    entry: dict | None = None,
) -> list[str]:
    repo_path = Path(repo_dir)

    if strategy in ("auto", "ctest"):
        ids = collect_test_ids_ctest(repo_path)
        if ids:
            logger.info("  CTest: found %d test IDs", len(ids))
            return ids

    if strategy in ("auto", "gtest"):
        ids = collect_test_ids_gtest(repo_path)
        if ids:
            logger.info("  GTest: found %d test IDs", len(ids))
            return ids

    if strategy in ("auto", "source"):
        ids = _collect_test_ids_from_source(repo_path, entry=entry)
        if ids:
            logger.info("  Source scan: found %d test IDs", len(ids))
            return ids

    logger.warning("No test IDs found for %s (strategy=%s)", repo_dir, strategy)
    return []


_GTEST_PATTERN = re.compile(
    r"(?:TEST|TEST_F|TEST_P|TYPED_TEST|TYPED_TEST_P)\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)"
)
_CATCH2_PATTERN = re.compile(
    r'(?:TEST_CASE|SCENARIO)\s*\(\s*"([^"]+)"'
)
_DOCTEST_PATTERN = re.compile(
    r'(?:TEST_CASE|SUBCASE)\s*\(\s*"([^"]+)"'
)
_BOOST_PATTERN = re.compile(
    r"(?:BOOST_AUTO_TEST_CASE|BOOST_FIXTURE_TEST_CASE|BOOST_DATA_TEST_CASE)\s*\(\s*(\w+)"
)
_CAF_PATTERN = re.compile(
    r'CAF_TEST\s*\(\s*(\w+)'
)

_CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h"}
_SKIP_DIRS = {"build", "cmake-build-debug", "cmake-build-release", "builddir",
              ".cache", "_deps", "third_party", "vendor", "extern", ".git"}


def _collect_test_ids_from_source(
    repo_dir: Path,
    entry: dict | None = None,
) -> list[str]:
    test_ids: list[str] = []

    scan_roots: list[Path] = []
    if entry:
        for key in ("test_dir", "src_dir"):
            sub = entry.get(key, "")
            if sub and (repo_dir / sub).is_dir():
                scan_roots.append(repo_dir / sub)
    if not scan_roots:
        scan_roots = [repo_dir]

    for scan_root in scan_roots:
        for root, dirs, files in os.walk(scan_root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _CPP_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                        content = fh.read()
                except OSError:
                    continue

                for m in _GTEST_PATTERN.finditer(content):
                    test_ids.append(f"{m.group(1)}.{m.group(2)}")
                for m in _CATCH2_PATTERN.finditer(content):
                    test_ids.append(m.group(1))
                for m in _DOCTEST_PATTERN.finditer(content):
                    test_ids.append(m.group(1))
                for m in _BOOST_PATTERN.finditer(content):
                    test_ids.append(m.group(1))
                for m in _CAF_PATTERN.finditer(content):
                    test_ids.append(m.group(1))

    return sorted(set(test_ids))


def save_test_ids(
    name: str,
    test_ids: list[str],
    output_dir: Path | None = None,
) -> Path:
    out_dir = output_dir or DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    bz2_path = out_dir / f"{name}.bz2"
    content = "\n".join(test_ids) + "\n" if test_ids else ""
    bz2_path.write_bytes(bz2.compress(content.encode()))
    logger.info("Saved %d test IDs to %s", len(test_ids), bz2_path)
    return bz2_path


def load_test_ids(name: str, test_ids_dir: Path | None = None) -> list[str]:
    ids_dir = test_ids_dir or DEFAULT_OUTPUT_DIR
    bz2_path = ids_dir / f"{name}.bz2"
    if not bz2_path.exists():
        return []
    try:
        raw = bz2.decompress(bz2_path.read_bytes()).decode()
        return [line.strip() for line in raw.splitlines() if line.strip()]
    except Exception as exc:
        logger.warning("Failed to load test IDs from %s: %s", bz2_path, exc)
        return []


def generate_for_dataset(
    dataset_path: str,
    output_dir: Path | None = None,
    strategy: str = "auto",
    base_dir: str = "repos",
) -> dict[str, int]:
    raw = Path(dataset_path).read_text().strip()
    entries = json.loads(raw)
    if isinstance(entries, dict):
        entries = [entries]

    results: dict[str, int] = {}

    for entry in entries:
        repo = entry.get("repo", "")
        repo_name = repo.split("/")[-1] if "/" in repo else repo

        repo_dir = Path(base_dir) / repo_name
        if not repo_dir.exists():
            logger.warning("Repo dir not found: %s, skipping", repo_dir)
            results[repo_name] = 0
            continue

        logger.info("Collecting test IDs for %s...", repo_name)
        ids = collect_test_ids_local(repo_dir, strategy=strategy, entry=entry)
        if ids:
            save_test_ids(repo_name, ids, output_dir)
        else:
            logger.warning(
                "  [SKIP] %s: extraction returned 0 tests; not installing bz2",
                repo_name,
            )
        results[repo_name] = len(ids)

    return results


def install_test_ids(output_dir: Path | None = None) -> None:
    src_dir = output_dir or DEFAULT_OUTPUT_DIR
    dest_dir = DATA_DIR / "cpp_test_ids"
    if src_dir == dest_dir:
        logger.info("Test IDs already in install location")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    for f in src_dir.glob("*.bz2"):
        shutil.copy2(str(f), str(dest_dir / f.name))
    logger.info("Installed test IDs to %s", dest_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate C++ test ID files for commit0"
    )
    parser.add_argument(
        "dataset",
        nargs="?",
        help="Path to dataset JSON file",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        help="Path to a single repo directory",
    )
    parser.add_argument(
        "--name",
        help="Repo name (required with --repo-dir)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--strategy",
        choices=["auto", "ctest", "gtest", "source"],
        default="auto",
        help="Test ID discovery strategy (default: auto)",
    )
    parser.add_argument(
        "--base-dir",
        default="repos",
        help="Base directory for repo clones (default: repos)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Copy test IDs to commit0/data/cpp_test_ids/",
    )

    args = parser.parse_args()

    if args.repo_dir:
        if not args.name:
            args.name = args.repo_dir.name
        ids = collect_test_ids_local(args.repo_dir, strategy=args.strategy)
        if ids:
            save_test_ids(args.name, ids, args.output_dir)
            print(f"Collected {len(ids)} test IDs for {args.name}")
        else:
            print(f"No test IDs found for {args.name}")
            sys.exit(1)

    elif args.dataset:
        results = generate_for_dataset(
            args.dataset,
            output_dir=args.output_dir,
            strategy=args.strategy,
            base_dir=args.base_dir,
        )
        total = sum(results.values())
        print(f"\nCollected {total} test IDs across {len(results)} repos:")
        for name, count in sorted(results.items()):
            status = "OK" if count > 0 else "NONE"
            print(f"  {status:4s} {name}: {count}")

    else:
        parser.print_help()
        sys.exit(1)

    if args.install:
        install_test_ids(args.output_dir)


if __name__ == "__main__":
    main()
