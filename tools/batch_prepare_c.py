"""Batch prepare C repositories for the commit0 dataset.

Reads a CSV file and runs the dataset-preparation pipeline for each repo:
clone, generate compile_commands.json, stub (cstubber/libclang), optional
fork/push, optional spec-PDF scrape, create the dataset JSON, run commit0-c
setup, Docker build, and test-ID generation + install.

Mirrors tools/batch_prepare.py (Python) / tools/batch_prepare_cpp.py (C++).
Like those, it STOPS after test-ID generation -- it never runs the 3-stage
agent pipeline (run_pipeline_c.sh).

CSV columns: library_name, Github url, Organization Name
Optional CSV columns: cmake_flags, spec_url

Usage:
    python -m tools.batch_prepare_c dataset/batch_c.csv \
        --output batch_c_dataset.json --clone-dir ./repos_staging
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tools.create_dataset_c import validate_dataset
from tools.prepare_repo_c import prepare_one

GITIGNORE_ENTRIES = [".aider*", "logs/", "build/"]
DEFAULT_CLONE_DIR = "./repos_staging"


def parse_csv(csv_path: Path) -> list[dict[str, str]]:
    """Parse the batch CSV file.

    Required columns: library_name, Github url, Organization Name
    Optional columns: cmake_flags, spec_url
    """
    repos: list[dict[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("Github url") or "").strip()
            if not url:
                continue
            parts = url.rstrip("/").split("/")
            if len(parts) < 2:
                print(f"  [WARN] Skipping invalid URL: {url}")
                continue
            full_name = f"{parts[-2]}/{parts[-1]}"
            repos.append(
                {
                    "full_name": full_name,
                    "library_name": (
                        row.get("library_name") or parts[-1]
                    ).strip(),
                    "org": (row.get("Organization Name") or "").strip(),
                    "cmake_flags": (row.get("cmake_flags") or "").strip(),
                    "spec_url": (row.get("spec_url") or "").strip(),
                }
            )
    return repos


def load_state(state_file: Path) -> dict[str, Any]:
    """Load resumable state from a JSON file."""
    if state_file.exists():
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)
    return {"completed": {}, "failures": {}}


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    """Save state to a JSON file for resumability."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def prepare_single_repo(
    full_name: str,
    clone_dir: Path,
    org: str,
    cmake_flags: str,
    spec_url: str,
    scrape_spec: bool = False,
    specs_dir: Path = Path("specs"),
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Prepare a single C repo via prepare_repo_c.prepare_one().

    Returns the dataset entry dict, or None on failure / dry-run.
    """
    print(f"\n{'=' * 60}")
    print(f"  Preparing: {full_name}")
    if org:
        print(f"  Fork org: {org}")
    print("  Build system: cmake, Framework: ctest")
    if cmake_flags:
        print(f"  CMake flags: {cmake_flags}")
    if scrape_spec and spec_url:
        print(f"  Spec URL: {spec_url}")
    print(f"{'=' * 60}\n")

    if dry_run:
        print("  [DRY RUN] Would clone, stub, and create dataset entry.")
        return None

    # We only reach here when NOT in preview mode (the `dry_run` early-return
    # above), so we are actually preparing — push by default (dry_run=False),
    # matching the main preparer's opt-out policy (QC-C1-002). A batch preview
    # is `--dry-run`; there is no batch "prepare-but-don't-push" mode (use the
    # single-repo `tools.prepare_repo_c --dry-run` for that).
    entry = prepare_one(
        full_name,
        clone_dir,
        fork_org=org or None,
        dry_run=False,
        branch="commit0_all",
        cmake_flags=cmake_flags,
        skip_spec=not scrape_spec,
        spec_url=spec_url,
        specs_dir=specs_dir,
    )
    if entry is None:
        print(
            f"  [ERROR] {full_name}: prepare_one returned None "
            f"(validation failed or empty stub)"
        )
        return None

    print(f"  [OK] {full_name} prepared successfully.")
    return entry


def _write_commit0_config(config_path: Path, dataset_path: Path) -> None:
    """Write a minimal .commit0.c.yaml (mirrors run_pipeline_c.sh preflight)."""
    config_path.write_text(
        f"dataset_name: {dataset_path}\n"
        f"dataset_split: train\n"
        f"repo_split: all\n"
        f"base_dir: repos\n",
        encoding="utf-8",
    )


def run_commit0_setup(dataset_path: Path, config_path: Path) -> bool:
    """Run commit0-c setup for the C dataset."""
    print("\n  Running commit0 C setup...")
    _write_commit0_config(config_path, dataset_path)
    cmd = [
        sys.executable,
        "-m",
        "commit0.cli_c",
        "setup",
        "all",
        "--dataset-name",
        str(dataset_path),
        "--dataset-split",
        "train",
        "--base-dir",
        "repos",
        "--commit0-config-file",
        str(config_path),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600)
        print("  [OK] Setup complete.")
        return True
    except subprocess.TimeoutExpired:
        print("  [ERROR] Setup timed out (600s).")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] Setup failed: {e}")
        return False


def run_commit0_build(config_path: Path) -> bool:
    """Run commit0-c Docker build for C repos."""
    print("\n  Building Docker images...")
    cmd = [
        sys.executable,
        "-m",
        "commit0.cli_c",
        "build",
        "--commit0-config-file",
        str(config_path),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        print("  [OK] Docker build complete.")
        return True
    except subprocess.TimeoutExpired:
        print("  [ERROR] Docker build timed out (3600s).")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] Docker build failed: {e}")
        return False


def add_gitignore_entries(repos_dir: Path, repo_name: str) -> None:
    """Append .aider*, logs/, build/ to the repo .gitignore if missing."""
    repo_path = repos_dir / repo_name
    gitignore_path = repo_path / ".gitignore"
    if not repo_path.is_dir():
        return
    existing = ""
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
    additions = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if additions:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(additions) + "\n")


def generate_and_install_test_ids(dataset_path: Path) -> bool:
    """Generate + install test IDs for all repos in the dataset.

    Shells out to tools.generate_test_ids_c so the install-path logic
    (commit0/data/c_test_ids/) lives in one place.
    """
    print("\n  Generating + installing test IDs...")
    cmd = [
        sys.executable,
        "-m",
        "tools.generate_test_ids_c",
        str(dataset_path),
        "--docker",
        "--install",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=3600)
        print("  [OK] Test IDs generated + installed.")
        return True
    except subprocess.TimeoutExpired:
        print("  [ERROR] Test ID generation timed out (3600s).")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] Test ID generation failed: {e}")
        return False


def print_summary(
    entries: list[dict],
    failures: dict[str, str],
    elapsed: float,
) -> None:
    """Print a formatted summary of the batch run."""
    print(f"\n{'=' * 60}")
    print("  BATCH PREPARE C SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total time: {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"  Repos prepared: {len(entries)}")
    print(f"  Repos failed: {len(failures)}")
    print()
    if entries:
        print("  Successful repos:")
        for entry in entries:
            print(f"    {entry.get('repo', 'unknown')}")
    if failures:
        print("\n  Failed repos:")
        for name, reason in failures.items():
            print(f"    {name:40s} {reason}")
    print(f"{'=' * 60}\n")


def main() -> None:
    """Main entry point for batch C repo preparation."""
    parser = argparse.ArgumentParser(
        description="Batch prepare C repositories for the commit0 dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CSV format (required columns + optional cmake_flags, spec_url):
  library_name,Github url,Organization Name,cmake_flags,spec_url
  cJSON,https://github.com/DaveGamble/cJSON,C-commit0,,
""",
    )
    parser.add_argument(
        "csv_file", type=Path, help="Path to the CSV file with repo info"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("batch_c_dataset.json"),
        help="Output dataset JSON path (default: batch_c_dataset.json)",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path(DEFAULT_CLONE_DIR),
        help="Directory for staging clones (default: ./repos_staging)",
    )
    parser.add_argument(
        "--org",
        type=str,
        default="",
        help="GitHub org for forks (overridden by CSV 'Organization Name')",
    )
    # QC-C1-002: push is DEFAULT-ON. A batch run forks+pushes every prepared
    # repo (an un-pushed row is UNBUILDABLE); use --dry-run for a preview.
    parser.add_argument(
        "--scrape-spec",
        action="store_true",
        help="Scrape + commit the spec PDF (requires spec_url in CSV)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path("specs"),
        help="Local spec-PDF cache directory (default: specs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from state file, skipping already-completed repos",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip Docker image building and test-ID generation",
    )
    parser.add_argument(
        "--skip-test-ids",
        action="store_true",
        help="Skip test-ID generation",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=0,
        help="Max repos to process (0 = all)",
    )
    parser.add_argument(
        "--filter-repo",
        type=str,
        default="",
        help="Only process repos matching this substring",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".batch_c_state.json"),
        help="State file for resumability",
    )
    parser.add_argument(
        "--commit0-config-file",
        type=Path,
        default=Path(".commit0.c.yaml"),
        help="commit0-c config file for setup/build",
    )

    args = parser.parse_args()

    if not args.csv_file.exists():
        print(f"ERROR: CSV file not found: {args.csv_file}")
        sys.exit(1)

    repos = parse_csv(args.csv_file)
    if not repos:
        print("ERROR: No valid repos found in CSV.")
        sys.exit(1)

    if args.filter_repo:
        repos = [r for r in repos if args.filter_repo in r["full_name"]]
    if args.max_repos > 0:
        repos = repos[: args.max_repos]

    print(f"\nBatch C Prepare: {len(repos)} repos from {args.csv_file}")
    print(f"  Output: {args.output}")
    print(f"  Clone dir: {args.clone_dir}")
    if args.dry_run:
        print("  MODE: DRY RUN")
    print()

    state = (
        load_state(args.state_file)
        if args.resume
        else {"completed": {}, "failures": {}}
    )

    start_time = time.time()
    entries: list[dict[str, Any]] = []
    failures: dict[str, str] = dict(state.get("failures", {}))

    for name, entry in state.get("completed", {}).items():
        if entry:
            entries.append(entry)
            print(f"  [SKIP] {name} (already completed)")

    for repo_info in repos:
        full_name = repo_info["full_name"]
        if full_name in state.get("completed", {}):
            continue
        org = repo_info.get("org") or args.org
        try:
            entry = prepare_single_repo(
                full_name=full_name,
                clone_dir=args.clone_dir,
                org=org,
                cmake_flags=repo_info["cmake_flags"],
                spec_url=repo_info["spec_url"],
                scrape_spec=args.scrape_spec,
                specs_dir=args.specs_dir,
                dry_run=args.dry_run,
            )
            if entry:
                entries.append(entry)
                state.setdefault("completed", {})[full_name] = entry
            elif not args.dry_run:
                failures[full_name] = "prepare returned None"
                state.setdefault("failures", {})[full_name] = (
                    "prepare returned None"
                )
        except Exception as e:
            print(f"  [ERROR] {full_name}: {e}")
            failures[full_name] = str(e)
            state.setdefault("failures", {})[full_name] = str(e)
        if not args.dry_run:
            save_state(args.state_file, state)

    if args.dry_run:
        print("\n[DRY RUN] No files written.")
        return

    if not entries:
        print("\nNo repos prepared successfully. Exiting.")
        sys.exit(1)

    print(f"\n  Writing dataset to {args.output}...")
    valid, issues = validate_dataset(entries)
    if issues:
        print(f"  [WARN] {len(issues)} dataset validation issue(s):")
        for issue in issues:
            print(f"    - {issue}")
    args.output.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(
        f"  [OK] Dataset written: {args.output} "
        f"({len(entries)} entries, {len(valid)} valid)"
    )

    setup_ok = run_commit0_setup(args.output, args.commit0_config_file)
    if not setup_ok:
        print("  [WARN] Setup failed. Continuing anyway...")

    repos_dir = Path("repos")
    for entry in entries:
        repo_name = entry.get("repo", "").split("/")[-1]
        if repo_name:
            add_gitignore_entries(repos_dir, repo_name)

    if not args.skip_build:
        build_ok = run_commit0_build(args.commit0_config_file)
        if not build_ok:
            print("  [WARN] Docker build failed. Test IDs may fail.")
        if not args.skip_test_ids:
            generate_and_install_test_ids(args.output)
    else:
        print("\n  [SKIP] Docker build (--skip-build)")

    elapsed = time.time() - start_time
    print_summary(entries, failures, elapsed)

    # batch_prepare_c intentionally STOPS here -- it never runs the 3-stage
    # agent pipeline (run_pipeline_c.sh), matching batch_prepare.py behavior.

    if not failures and args.state_file.exists():
        args.state_file.unlink()
        print(f"  Cleaned up state file: {args.state_file}")


if __name__ == "__main__":
    main()
