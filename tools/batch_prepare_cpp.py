"""Batch prepare C++ repositories for the commit0 dataset.

Reads a CSV file and runs the full pipeline for each repo:
fork, clone, generate compile_commands.json, stub, verify compilation,
push branches, create dataset JSON, setup, Docker build, and test ID generation.

CSV columns: library_name, Github url, Organization Name, build_system,
             test_framework, cpp_standard, dependencies

Usage:
    python -m tools.batch_prepare_cpp dataset/batch_cpp.csv \
        --output batch_cpp_dataset.json
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

from tools.prepare_repo_cpp import (
    DEFAULT_ORG,
    clone_repo,
    create_dataset_entry,
    detect_build_system,
    fork_repo,
    generate_compile_commands,
    get_head_sha,
    git,
    stub_source_dir,
    update_cpp_split,
    verify_compiles,
)
from tools.create_dataset_cpp import create_cpp_dataset, validate_cpp_entry
from tools.generate_test_ids_cpp import (
    generate_for_dataset,
    install_test_ids,
)

GITIGNORE_ENTRIES = [".aider*", "logs/"]
DEFAULT_CLONE_DIR = "./repos_staging"


def parse_csv(csv_path: Path) -> list[dict[str, str]]:
    """Parse the batch CSV file.

    Expected columns:
        library_name, Github url, Organization Name,
        build_system, test_framework, cpp_standard, dependencies
    """
    repos: list[dict[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = row.get("Github url", "").strip()
            if not url:
                continue
            # Extract owner/repo from URL
            parts = url.rstrip("/").split("/")
            if len(parts) < 2:
                print(f"  [WARN] Skipping invalid URL: {url}")
                continue
            full_name = f"{parts[-2]}/{parts[-1]}"
            repos.append({
                "full_name": full_name,
                "library_name": row.get("library_name", parts[-1]).strip(),
                "org": row.get("Organization Name", DEFAULT_ORG).strip(),
                "build_system": row.get("build_system", "cmake").strip().lower(),
                "test_framework": row.get("test_framework", "ctest").strip().lower(),
                "cpp_standard": row.get("cpp_standard", "17").strip(),
                "dependencies": row.get("dependencies", "").strip(),
            })
    return repos


def _make_test_cmd(build_system: str, test_framework: str) -> str:
    """Build a test command string for the given framework."""
    if test_framework in ("gtest", "ctest"):
        return "ctest --test-dir build --output-junit /testbed/test_results.xml --timeout 60"
    if test_framework == "catch2":
        return "./build/tests/test_all -r junit -o /testbed/test_results.xml"
    if test_framework == "doctest":
        return "./build/tests/test_all --reporters=junit --out=/testbed/test_results.xml"
    if test_framework == "boost_test":
        return "./build/tests/test_all --logger=JUNIT,message,/testbed/test_results.xml"
    # Fallback to CTest
    return "ctest --test-dir build --output-junit /testbed/test_results.xml --timeout 60"


def _detect_src_dir(repo_dir: Path) -> str:
    """Detect the most likely source directory for a C++ repo."""
    candidates = ["src", "include", "lib", "source"]
    for d in candidates:
        if (repo_dir / d).is_dir():
            return d
    # Fallback: look for .cpp/.hpp files at the root
    cpp_files = list(repo_dir.glob("*.cpp")) + list(repo_dir.glob("*.hpp"))
    if cpp_files:
        return "."
    return "src"


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
    build_system: str,
    test_framework: str,
    cpp_standard: str,
    dependencies: str,
    dry_run: bool = False,
    allow_broken_stubs: bool = False,
) -> dict[str, Any] | None:
    """Prepare a single C++ repo: fork, clone, stub, push, create entry.

    Returns the dataset entry dict, or None on failure.
    """
    repo_name = full_name.split("/")[-1]
    fork_name = f"{org}/{repo_name}"
    test_cmd = _make_test_cmd(build_system, test_framework)

    print(f"\n{'='*60}")
    print(f"  Preparing: {full_name}")
    print(f"  Fork target: {fork_name}")
    print(f"  Build system: {build_system}, Framework: {test_framework}")
    print(f"  C++ standard: {cpp_standard}")
    if dependencies:
        print(f"  Dependencies: {dependencies}")
    print(f"{'='*60}\n")

    if dry_run:
        print("  [DRY RUN] Would fork, clone, stub, and push.")
        return None

    # 1. Fork
    print("  [1/8] Forking...")
    try:
        fork_repo(full_name, org)
    except Exception as e:
        # Check if fork already exists (expected on re-runs)
        fork_check = subprocess.run(
            ["gh", "repo", "view", fork_name, "--json", "name"],
            capture_output=True, text=True
        )
        if fork_check.returncode != 0:
            print(f"  [ERROR] Fork failed and doesn't exist: {e}")
            return None
        print(f"  [INFO] Fork already exists: {fork_name}")

    # 2. Clone
    print("  [2/8] Cloning...")
    repo_dir = clone_repo(full_name, clone_dir)

    # 3. Detect or verify build system
    detected = detect_build_system(repo_dir)
    if detected != build_system:
        print(f"  [INFO] Detected build system '{detected}' differs from CSV '{build_system}'. Using CSV value.")

    # 4. Record reference commit, create branch
    print("  [3/8] Recording reference commit...")
    reference_commit = get_head_sha(repo_dir)
    branch = "commit0_all"
    try:
        git(repo_dir, "checkout", "-b", branch)
    except Exception:
        git(repo_dir, "checkout", branch)

    # 5. Generate compile_commands.json
    print("  [4/8] Generating compile_commands.json...")
    cc_ok = generate_compile_commands(repo_dir, build_system)
    if not cc_ok:
        print("  [WARN] compile_commands.json generation failed. Stubber may not work optimally.")

    # 6. Stub source
    src_dir = _detect_src_dir(repo_dir)
    print(f"  [5/8] Stubbing source directory: {src_dir}...")
    try:
        stubbed, skipped = stub_source_dir(repo_dir, src_dir, build_system)
        print(f"  Stubbed {stubbed} functions, skipped {skipped}.")
    except Exception as e:
        print(f"  [ERROR] Stubbing failed: {e}")
        if not allow_broken_stubs:
            return None

    # 7. Verify compilation
    print("  [6/8] Verifying stubbed code compiles...")
    compiles = verify_compiles(repo_dir, build_system)
    if not compiles:
        print("  [WARN] Stubbed code does not compile.")
        if not allow_broken_stubs:
            print("  [ERROR] Aborting (use --allow-broken-stubs to continue).")
            return None
        print("  [WARN] Continuing despite compilation failure (--allow-broken-stubs).")

    # 8. Commit and push
    print("  [7/8] Committing and pushing...")
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-m", "commit0: stub function bodies for C++ benchmark")
    base_commit = get_head_sha(repo_dir)

    print("  [8/8] Pushing to fork...")
    try:
        git(repo_dir, "push", "-u", "origin", branch, "--force")
    except Exception as e:
        print(f"  [WARN] Push failed: {e}")
        print("  Trying to add remote and push...")
        try:
            push_url = f"https://github.com/{fork_name}.git"
            git(repo_dir, "remote", "set-url", "origin", push_url)
            git(repo_dir, "push", "-u", "origin", branch, "--force")
        except Exception as e2:
            print(f"  [ERROR] Push failed again: {e2}")
            return None

    # Create entry
    packages = dependencies if dependencies else ""
    entry = create_dataset_entry(
        upstream=full_name,
        fork_name=fork_name,
        repo_name=repo_name,
        src_dir=src_dir,
        test_cmd=test_cmd,
        base_commit=base_commit,
        reference_commit=reference_commit,
        build_system=build_system,
        cpp_standard=cpp_standard,
        test_framework=test_framework,
        packages=packages,
    )

    # Validate
    issues = validate_cpp_entry(entry)
    if issues:
        print("  [WARN] Entry validation issues:")
        for issue in issues:
            print(f"    - {issue}")

    # Update CPP_SPLIT
    try:
        update_cpp_split(fork_name)
    except Exception as e:
        print(f"  [WARN] Could not update CPP_SPLIT: {e}")

    print(f"  [OK] {full_name} prepared successfully.")
    return entry


def run_commit0_setup(dataset_path: Path) -> bool:
    """Run commit0 setup for the C++ dataset."""
    print("\n  Running commit0 C++ setup...")
    cmd = [
        sys.executable, "-m", "commit0.cli_cpp",
        "setup", "all",
        "--dataset-name", str(dataset_path),
        "--dataset-split", "train",
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


def run_commit0_build(dataset_path: Path) -> bool:
    """Run commit0 Docker build for C++ repos."""
    print("\n  Building Docker images...")
    cmd = [
        sys.executable, "-m", "commit0.harness.build_cpp",
        "--dataset", str(dataset_path),
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
    """Add .aider* and logs/ to the repo .gitignore."""
    repo_path = repos_dir / repo_name
    gitignore_path = repo_path / ".gitignore"

    if not repo_path.is_dir():
        return

    existing = ""
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")

    additions = []
    for entry in GITIGNORE_ENTRIES:
        if entry not in existing:
            additions.append(entry)

    if additions:
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(additions) + "\n")


def generate_and_install_test_ids(
    dataset_path: Path,
    output_dir: Path | None = None,
) -> dict[str, int]:
    """Generate test IDs for all repos in the dataset and install them."""
    print("\n  Generating test IDs...")
    try:
        results = generate_for_dataset(
            dataset_path=str(dataset_path),
            output_dir=output_dir,
            strategy="auto",
            base_dir="repos",
        )
        install_test_ids(output_dir)
        print(f"  [OK] Test IDs generated: {results}")
        return results
    except Exception as e:
        print(f"  [ERROR] Test ID generation failed: {e}")
        return {}


def print_summary(
    entries: list[dict],
    test_id_results: dict[str, int],
    failures: dict[str, str],
    elapsed: float,
) -> None:
    """Print a formatted summary of the batch run."""
    print(f"\n{'='*60}")
    print("  BATCH PREPARE C++ SUMMARY")
    print(f"{'='*60}")
    print(f"  Total time: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  Repos prepared: {len(entries)}")
    print(f"  Repos failed: {len(failures)}")
    print()

    if entries:
        print("  Successful repos:")
        for entry in entries:
            repo = entry.get("repo", "unknown")
            tid = test_id_results.get(repo.split("/")[-1], 0)
            print(f"    {repo:40s} tests={tid}")

    if failures:
        print("\n  Failed repos:")
        for name, reason in failures.items():
            print(f"    {name:40s} {reason}")
    print(f"{'='*60}\n")


def main() -> None:
    """Main entry point for batch C++ repo preparation."""
    parser = argparse.ArgumentParser(
        description="Batch prepare C++ repositories for the commit0 dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CSV format:
  library_name,Github url,Organization Name,build_system,test_framework,cpp_standard,dependencies
  fmt,https://github.com/fmtlib/fmt,Cpp-commit0,cmake,gtest,17,
  yaml-cpp,https://github.com/jbeder/yaml-cpp,Cpp-commit0,cmake,gtest,17,
  CLI11,https://github.com/CLIUtils/CLI11,Cpp-commit0,cmake,catch2,17,
""",
    )
    parser.add_argument("csv_file", type=Path, help="Path to the CSV file with repo info")
    parser.add_argument("--output", type=Path, default=Path("batch_cpp_dataset.json"),
                        help="Output dataset JSON path (default: batch_cpp_dataset.json)")
    parser.add_argument("--clone-dir", type=Path, default=Path(DEFAULT_CLONE_DIR),
                        help="Directory for staging clones (default: ./repos_staging)")
    parser.add_argument("--org", type=str, default=DEFAULT_ORG,
                        help=f"GitHub org for forks (default: {DEFAULT_ORG})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be done without executing")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from state file, skipping already-completed repos")
    parser.add_argument("--skip-build", action="store_true",
                        help="Skip Docker image building")
    parser.add_argument("--skip-test-ids", action="store_true",
                        help="Skip test ID generation")
    parser.add_argument("--max-repos", type=int, default=0,
                        help="Max repos to process (0 = all)")
    parser.add_argument("--filter-repo", type=str, default="",
                        help="Only process repos matching this substring")
    parser.add_argument("--state-file", type=Path, default=Path(".batch_cpp_state.json"),
                        help="State file for resumability")
    parser.add_argument("--allow-broken-stubs", action="store_true",
                        help="Continue even if stubbed code does not compile")

    args = parser.parse_args()

    if not args.csv_file.exists():
        print(f"ERROR: CSV file not found: {args.csv_file}")
        sys.exit(1)

    # Parse CSV
    repos = parse_csv(args.csv_file)
    if not repos:
        print("ERROR: No valid repos found in CSV.")
        sys.exit(1)

    # Apply filters
    if args.filter_repo:
        repos = [r for r in repos if args.filter_repo in r["full_name"]]
    if args.max_repos > 0:
        repos = repos[:args.max_repos]

    print(f"\nBatch C++ Prepare: {len(repos)} repos from {args.csv_file}")
    print(f"  Output: {args.output}")
    print(f"  Clone dir: {args.clone_dir}")
    print(f"  Org: {args.org}")
    if args.dry_run:
        print("  MODE: DRY RUN")
    print()

    # Load state for resumability
    state = load_state(args.state_file) if args.resume else {"completed": {}, "failures": {}}

    start_time = time.time()
    entries: list[dict[str, Any]] = []
    failures: dict[str, str] = dict(state.get("failures", {}))

    # Restore previously completed entries
    for name, entry in state.get("completed", {}).items():
        if entry:
            entries.append(entry)
            print(f"  [SKIP] {name} (already completed)")

    # Process each repo
    for repo_info in repos:
        full_name = repo_info["full_name"]

        # Skip if already done
        if full_name in state.get("completed", {}):
            continue

        # Override org from CSV if present, else use CLI arg
        org = repo_info.get("org") or args.org

        try:
            entry = prepare_single_repo(
                full_name=full_name,
                clone_dir=args.clone_dir,
                org=org,
                build_system=repo_info["build_system"],
                test_framework=repo_info["test_framework"],
                cpp_standard=repo_info["cpp_standard"],
                dependencies=repo_info["dependencies"],
                dry_run=args.dry_run,
                allow_broken_stubs=args.allow_broken_stubs,
            )
            if entry:
                entries.append(entry)
                state.setdefault("completed", {})[full_name] = entry
            else:
                if not args.dry_run:
                    failures[full_name] = "prepare returned None"
                    state.setdefault("failures", {})[full_name] = "prepare returned None"
        except Exception as e:
            print(f"  [ERROR] {full_name}: {e}")
            failures[full_name] = str(e)
            state.setdefault("failures", {})[full_name] = str(e)

        # Save state after each repo
        if not args.dry_run:
            save_state(args.state_file, state)

    if args.dry_run:
        print("\n[DRY RUN] No files written.")
        return

    if not entries:
        print("\nNo repos prepared successfully. Exiting.")
        sys.exit(1)

    # Write dataset JSON
    print(f"\n  Writing dataset to {args.output}...")
    create_cpp_dataset(entries, str(args.output), dataset_name="batch_cpp")
    print(f"  [OK] Dataset written: {args.output} ({len(entries)} entries)")

    # Run commit0 setup
    setup_ok = run_commit0_setup(args.output)
    if not setup_ok:
        print("  [WARN] Setup failed. Continuing anyway...")

    # Add gitignore entries
    repos_dir = Path("repos")
    for entry in entries:
        repo_name = entry.get("repo", "").split("/")[-1]
        if repo_name:
            add_gitignore_entries(repos_dir, repo_name)

    # Docker build
    test_id_results: dict[str, int] = {}
    if not args.skip_build:
        build_ok = run_commit0_build(args.output)
        if not build_ok:
            print("  [WARN] Docker build failed. Test ID generation may fail.")

        # Test ID generation
        if not args.skip_test_ids:
            test_id_results = generate_and_install_test_ids(args.output)
    else:
        print("\n  [SKIP] Docker build (--skip-build)")

    elapsed = time.time() - start_time
    print_summary(entries, test_id_results, failures, elapsed)

    # Cleanup state file on full success
    if not failures and args.state_file.exists():
        args.state_file.unlink()
        print(f"  Cleaned up state file: {args.state_file}")


if __name__ == "__main__":
    main()
