"""Batch prepare repos for the Commit0 benchmark pipeline.

Reads a CSV of libraries, orchestrates the full preparation pipeline for each:
  1. Fork to GitHub org
  2. Clone at release tag
  3. AST-stub source code
  4. Push stubbed branch to fork
  5. Create dataset JSON entry
  6. Run `commit0 setup` to clone into repos/
  7. Add .gitignore entries (.aider*, logs/)
  8. Run `commit0 build` to build Docker images
  9. Generate test IDs (with verbose pytest output)
  10. Validate base_commit test collection inside Docker
  11. Install test IDs into commit0/data/test_ids/

Produces:
  - Per-batch dataset JSON (bare list format)
  - State file for resumability
  - Summary report

Usage:
    python -m tools.batch_prepare dataset/batch1.csv --output batch1_dataset.json
    python -m tools.batch_prepare dataset/batch1.csv --dry-run
    python -m tools.batch_prepare dataset/batch1.csv --resume  # resume from state file
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_ORG = "Zahgon"
DEFAULT_REMOVAL_MODE = "all"
GITIGNORE_ENTRIES = [".aider*", "logs/"]


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", 300)
    return subprocess.run(cmd, **kwargs)  # type: ignore[arg-type]


def parse_csv(csv_path: Path) -> list[dict[str, str]]:
    """Parse the assignments CSV into a list of repo dicts.

    Expected columns: library_name, Github url, Organization Name.
    Extracts owner/repo from the GitHub URL.
    """
    rows: list[dict[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # T18: DictReader tolerates missing columns by returning None from .get(),
        # which silently zeroes out every row. If the CSV lacks the required
        # headers (typo, wrong file, missing header line), fail loudly here.
        _fields = set(reader.fieldnames or [])
        _required = {"Github url"}
        _missing = _required - _fields
        if _missing:
            raise ValueError(
                f"CSV {csv_path} is missing required column(s): {sorted(_missing)}."
                f" Found columns: {sorted(_fields)}"
            )
        for row in reader:
            github_url = (row.get("Github url") or "").strip().rstrip("/")
            if github_url.endswith(".git"):
                github_url = github_url[:-4]
            if not github_url:
                lib = (row.get("library_name") or "").strip()
                if lib:
                    logger.warning("Skipping %s: empty GitHub URL", lib)
                continue

            # T9: Full scheme+host validation. The old regex matched any URL
            # containing 'github.com/' anywhere (e.g. attacker.com/github.com/fake/repo), so a
            # poisoned CSV could inject arbitrary owner/repo. Parse the URL
            # and require scheme http(s) with host exactly github.com or
            # www.github.com.
            try:
                _parsed = urlparse(github_url)
            except ValueError as _e:
                logger.warning("Skipping: unparseable URL %s (%s)", github_url, _e)
                continue
            if _parsed.scheme not in ("http", "https"):
                logger.warning(
                    "Skipping: URL %s must be http(s), got scheme %r",
                    github_url, _parsed.scheme,
                )
                continue
            if (_parsed.hostname or "").lower() not in ("github.com", "www.github.com"):
                logger.warning(
                    "Skipping: URL %s host must be github.com, got %r",
                    github_url, _parsed.hostname,
                )
                continue
            _path = (_parsed.path or "").strip("/")
            _parts = _path.split("/")
            if len(_parts) < 2 or not _parts[0] or not _parts[1]:
                logger.warning("Skipping: cannot parse owner/repo from %s", github_url)
                continue
            full_name = f"{_parts[0]}/{_parts[1]}"
            rows.append(
                {
                    "library_name": (row.get("library_name") or "").strip(),
                    "full_name": full_name,
                    "org_name": (row.get("Organization Name") or "").strip(),
                    "github_url": github_url,
                    "rnd": (row.get("RnD") or "").strip(),
                }
            )

    return rows


def load_state(state_path: Path) -> dict[str, dict]:
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {}


def save_state(state_path: Path, state: dict[str, dict]) -> None:
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _get_latest_tag(repo_dir: Path) -> str | None:
    try:
        tags = _run(["git", "tag", "--sort=-creatordate"], cwd=repo_dir, timeout=30)
        if tags.returncode == 0 and tags.stdout.strip():
            return tags.stdout.strip().split("\n")[0]
    except Exception:
        logger.debug("Failed to get latest tag for %s", repo_dir, exc_info=True)
    return None


def _remove_workflows(repo_dir: Path) -> bool:
    workflows_dir = repo_dir / ".github" / "workflows"
    if workflows_dir.is_dir():
        shutil.rmtree(workflows_dir)
        _run(["git", "add", "-A"], cwd=repo_dir)
        _run(
            ["git", "commit", "--amend", "--no-edit", "--allow-empty"],
            cwd=repo_dir,
        )
        return True
    return False


def prepare_single_repo(
    full_name: str,
    clone_dir: Path,
    org: str,
    removal_mode: str,
    dry_run: bool = False,
    tag: str | None = None,
    fallback: bool = True,
) -> dict | None:
    """Run the full preparation pipeline for a single repo.

    Returns a dataset entry dict, or None on failure.
    """
    from tools.prepare_repo import (
        create_dataset_entry,
        create_stubbed_branch,
        fork_repo,
        full_clone,
        generate_setup_dict,
        generate_test_dict,
        get_default_branch,
        get_head_sha,
        git,
        push_to_fork,
        quick_import_check,
    )

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise EnvironmentError(
            "GITHUB_TOKEN (or GH_TOKEN) is required but not set. "
            "Export GITHUB_TOKEN before running batch_prepare."
        )
    repo_name = full_name.split("/")[-1]

    # 1. Fork
    if dry_run:
        fork_name = f"{org}/{repo_name}"
        logger.info("  [DRY RUN] Would fork to %s", fork_name)
    else:
        try:
            fork_name = fork_repo(full_name, org, token=token)
        except Exception as e:
            logger.error("  Fork failed: %s", e)
            return None

    # 2. Clone
    try:
        repo_dir = full_clone(full_name, clone_dir, tag=tag)
        if tag:
            logger.info("  Pinned to tag: %s", tag)
        else:
            detected_tag = _get_latest_tag(repo_dir)
            if detected_tag:
                logger.info("  Auto-detected latest tag: %s", detected_tag)
                git(repo_dir, "checkout", detected_tag, check=False)
    except Exception as e:
        logger.error("  Clone failed: %s", e)
        return None

    # 3. Detect src_dir before stubbing
    from tools.validate import find_src_dir, find_test_dir

    src_dir = find_src_dir(repo_dir, repo_name)
    test_dir_detected = find_test_dir(repo_dir)

    # 4. Stub
    try:
        base_commit, reference_commit = create_stubbed_branch(
            repo_dir,
            full_name,
            src_dir,
            removal_mode=removal_mode,
        )
    except Exception as e:
        logger.error("  Stubbing failed: %s", e)
        return None

    # 4a. Quick import check with adaptive fallback cascade
    if fallback and src_dir:
        ok, err = quick_import_check(repo_dir, src_dir)
        if not ok:
            logger.warning("  Import check failed (mode=%s): %s", removal_mode, err)
            FALLBACK_MODES = ["all", "docstring", "combined"]
            remaining = [m for m in FALLBACK_MODES if m != removal_mode]

            for fallback_mode in remaining:
                logger.info("  Fallback: re-stubbing with mode=%s", fallback_mode)
                try:
                    default_branch = get_default_branch(repo_dir)
                    git(repo_dir, "checkout", default_branch, check=False)
                    base_commit, reference_commit = create_stubbed_branch(
                        repo_dir,
                        full_name,
                        src_dir,
                        removal_mode=fallback_mode,
                    )
                    ok2, err2 = quick_import_check(repo_dir, src_dir)
                    if ok2:
                        logger.info("  Fallback mode=%s succeeded!", fallback_mode)
                        removal_mode = fallback_mode
                        break
                    else:
                        logger.warning(
                            "  Fallback mode=%s also failed: %s", fallback_mode, err2
                        )
                except Exception as e:
                    logger.warning("  Fallback mode=%s error: %s", fallback_mode, e)
            else:
                logger.error(
                    "  All fallback modes failed for %s — proceeding with original",
                    full_name,
                )
        else:
            logger.info("  Import check passed (mode=%s)", removal_mode)

    # 5. Remove GitHub Actions workflows (avoids PAT workflow scope error)
    branch_name = "commit0_all"
    git(repo_dir, "checkout", branch_name, check=False)
    if _remove_workflows(repo_dir):
        base_commit = get_head_sha(repo_dir)
        logger.info(
            "  Removed .github/workflows, updated base_commit: %s", base_commit[:12]
        )

    # 6. Generate setup/test dicts (checkout original code for analysis)
    default_branch = get_default_branch(repo_dir)
    git(repo_dir, "checkout", default_branch, check=False)
    setup_dict = generate_setup_dict(repo_dir, full_name)
    test_dict = generate_test_dict(repo_dir, test_dir_detected)

    # 7. Push to fork
    if not dry_run:
        try:
            git(repo_dir, "checkout", branch_name)
            push_to_fork(repo_dir, fork_name, branch=branch_name, token=token)
        except Exception as e:
            logger.error("  Push failed: %s", e)

    # 8. Create dataset entry
    entry = create_dataset_entry(
        full_name=full_name,
        fork_name=fork_name if not dry_run else f"{org}/{repo_name}",
        base_commit=base_commit,
        reference_commit=reference_commit,
        src_dir=src_dir or "",
        setup_dict=setup_dict,
        test_dict=test_dict,
    )

    return entry


def run_commit0_setup(dataset_path: Path) -> bool:
    cmd = [
        sys.executable,
        "-m",
        "commit0",
        "setup",
        "all",
        "--dataset-name",
        str(dataset_path),
        "--dataset-split",
        "train",
    ]
    result = _run(cmd, cwd=PROJECT_ROOT, timeout=600)
    return result.returncode == 0


def run_commit0_build(dataset_path: Path) -> bool:
    """Build Docker images. Reads dataset from .commit0.yaml written by setup."""
    cmd = [
        sys.executable,
        "-m",
        "commit0",
        "build",
    ]
    result = _run(cmd, cwd=PROJECT_ROOT, timeout=1800)
    if result.returncode != 0:
        # `_run` captures stdout/stderr; a shard-wide build failure previously
        # returned only False, so the actual diagnostic (missing base image,
        # docker daemon error, a repo's build error) was discarded and the run
        # log recorded a bare "commit0 build failed". Persist the real output so
        # the failure is root-causable. Also drop it to a file next to the
        # dataset for post-mortem when the console log rolls.
        logger.error(
            "commit0 build failed (rc=%d).\n----- stdout -----\n%s\n"
            "----- stderr -----\n%s",
            result.returncode,
            (result.stdout or "").strip() or "(empty)",
            (result.stderr or "").strip() or "(empty)",
        )
        try:
            _log = Path(dataset_path).with_suffix(".build.log")
            _log.write_text(
                f"# commit0 build rc={result.returncode}\n"
                f"## stdout\n{result.stdout or ''}\n## stderr\n{result.stderr or ''}\n",
                encoding="utf-8",
            )
            logger.error("commit0 build output saved to %s", _log)
        except Exception as _e:  # noqa: BLE001 - logging must not mask the failure
            logger.debug("could not persist build log: %s", _e)
    return result.returncode == 0


def add_gitignore_entries(repos_dir: Path, repo_name: str) -> bool:
    repo_dir = repos_dir / repo_name
    if not repo_dir.is_dir():
        logger.warning("  Repo dir not found: %s", repo_dir)
        return False

    gitignore = repo_dir / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    added = False
    for entry in GITIGNORE_ENTRIES:
        if entry not in existing:
            existing += f"\n{entry}"
            added = True

    if added:
        gitignore.write_text(existing.rstrip() + "\n", encoding="utf-8")
        _run(["git", "add", ".gitignore"], cwd=repo_dir)
        _run(
            ["git", "commit", "-m", "Add .aider* and logs/ to .gitignore"], cwd=repo_dir
        )

    return True


def generate_and_install_test_ids(
    dataset_path: Path,
    output_dir: Path,
    validate_base: bool = True,
) -> dict[str, int]:
    from tools.generate_test_ids import (
        generate_for_dataset,
        install_test_ids,
    )

    results = generate_for_dataset(
        dataset_path=dataset_path,
        output_dir=output_dir,
        prefer=["docker"],
        validate_base=validate_base,
    )

    installed = install_test_ids(output_dir)
    logger.info("Installed %d test ID files", installed)

    return results


def print_summary(
    entries: list[dict],
    test_id_results: dict[str, int],
    failures: dict[str, str],
    elapsed: float,
    allow_broken_stubs: bool = False,
) -> None:
    print(f"\n{'=' * 100}")
    print("BATCH PREPARATION SUMMARY")
    print(f"{'=' * 100}")
    print(f"Total repos:    {len(entries) + len(failures)}")
    print(f"Succeeded:      {len(entries)}")
    print(f"Failed:         {len(failures)}")
    print(f"Elapsed:        {elapsed:.0f}s ({elapsed / 60:.1f}min)")
    print(f"{'=' * 100}\n")

    validation_failures = 0
    if entries:
        print(f"{'#':>3}  {'Repo':<40} {'Tests':>7}  {'Status'}")
        print("-" * 70)
        for i, e in enumerate(entries, 1):
            repo_name = e["repo"].split("/")[-1]
            test_count = test_id_results.get(repo_name, 0)
            if test_count < 0:
                status = (
                    f"FAIL: base_commit collects 0 tests ({abs(test_count)} at ref)"
                )
                validation_failures += 1
            elif test_count > 0:
                status = "OK"
            else:
                status = "WARN: 0 test IDs"
            print(f"{i:>3}  {e['original_repo']:<40} {abs(test_count):>7}  {status}")
        print()

    if validation_failures > 0:
        print(
            f"⚠ {validation_failures} repo(s) have BROKEN stubs (0 tests at base_commit)."
        )
        print("  These repos will produce 0% in all pipeline stages.")
        print(
            "  Fix: Re-stub with smart mode or selectively preserve critical functions.\n"
        )
        if not allow_broken_stubs:
            sys.exit(1)

    if failures:
        print("FAILED REPOS:")
        for name, reason in failures.items():
            print(f"  {name}: {reason}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch prepare repos for Commit0 pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m tools.batch_prepare dataset/batch1.csv
  python -m tools.batch_prepare dataset/batch1.csv --dry-run
  python -m tools.batch_prepare dataset/batch1.csv --resume --removal-mode all
  python -m tools.batch_prepare dataset/batch1.csv --skip-build  # prepare only, no Docker
""",
    )
    parser.add_argument("csv_file", help="Input CSV file with library assignments")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output dataset JSON file (default: <csv_stem>_dataset.json)",
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
        "--removal-mode",
        type=str,
        default=DEFAULT_REMOVAL_MODE,
        choices=["all", "docstring", "combined"],
        help=f"Stub removal mode (default: {DEFAULT_REMOVAL_MODE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip forking, pushing, setup, build (just clone + stub + generate entries)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from state file, skipping already-prepared repos",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip commit0 setup/build/test-ids steps (prepare entries only)",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip base_commit test collection validation",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to process",
    )
    parser.add_argument(
        "--filter-repo",
        type=str,
        default=None,
        help="Process only repos matching this substring (e.g., 'tenacity')",
    )
    parser.add_argument(
        "--state-file",
        type=str,
        default=None,
        help="State file path (default: <output_stem>_state.json)",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Disable adaptive fallback: don't retry with different removal modes on import failure",
    )
    parser.add_argument(
        "--allow-broken-stubs",
        action="store_true",
        help="Continue even if some repos have 0 tests at base_commit (default: exit 1)",
    )
    parser.add_argument(
        "--single-arch",
        action="store_true",
        help="Build for native architecture only (skip multi-arch OCI tarball)",
    )

    args = parser.parse_args()

    if args.single_arch:
        import platform as _plat

        machine = _plat.machine()
        native = "linux/arm64" if machine in ("arm64", "aarch64") else "linux/amd64"
        os.environ["COMMIT0_BUILD_PLATFORMS"] = native
        logger.info("Single-arch mode: COMMIT0_BUILD_PLATFORMS=%s", native)

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        parser.error(f"CSV file not found: {csv_path}")

    csv_stem = csv_path.stem.replace(" ", "_")
    output_path = (
        Path(args.output) if args.output else PROJECT_ROOT / f"{csv_stem}_dataset.json"
    )
    state_path = (
        Path(args.state_file)
        if args.state_file
        else output_path.with_suffix(".state.json")
    )
    clone_dir = Path(args.clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=True)

    repos = parse_csv(csv_path)
    logger.info("Parsed %d repos from %s", len(repos), csv_path)

    if args.filter_repo:
        repos = [r for r in repos if args.filter_repo.lower() in r["full_name"].lower()]
        logger.info("Filtered to %d repos matching '%s'", len(repos), args.filter_repo)

    if args.max_repos:
        repos = repos[: args.max_repos]

    state = load_state(state_path) if args.resume else {}

    entries: list[dict] = []
    failures: dict[str, str] = {}
    start_time = time.monotonic()

    for i, repo_info in enumerate(repos):
        full_name = repo_info["full_name"]
        logger.info("\n[%d/%d] Processing %s...", i + 1, len(repos), full_name)

        if (
            args.resume
            and full_name in state
            and state[full_name].get("status") == "prepared"
        ):
            logger.info("  Skipping (already prepared)")
            entry = state[full_name].get("entry")
            if entry:
                entries.append(entry)
            continue

        entry = prepare_single_repo(
            full_name=full_name,
            clone_dir=clone_dir,
            org=args.org,
            removal_mode=args.removal_mode,
            dry_run=args.dry_run,
            fallback=not args.no_fallback,
        )

        if entry is None:
            failures[full_name] = "preparation failed"
            state[full_name] = {
                "status": "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state_path, state)
            continue

        entries.append(entry)
        state[full_name] = {
            "status": "prepared",
            "entry": entry,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        save_state(state_path, state)
        logger.info(
            "  Entry created: %s (base=%s)",
            entry["instance_id"],
            entry["base_commit"][:12],
        )

    if not entries:
        logger.error("No repos prepared successfully")
        return

    output_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    logger.info("\nSaved %d entries to %s", len(entries), output_path)

    test_id_results: dict[str, int] = {}

    if not args.skip_build and not args.dry_run:
        abs_dataset = output_path.resolve()

        logger.info("\n--- Running commit0 setup ---")
        if not run_commit0_setup(abs_dataset):
            logger.error("commit0 setup failed")
        else:
            logger.info("commit0 setup complete")

        repos_dir = PROJECT_ROOT / "repos"
        for entry in entries:
            repo_name = entry["repo"].split("/")[-1]
            add_gitignore_entries(repos_dir, repo_name)

        logger.info("\n--- Running commit0 build ---")
        if not run_commit0_build(abs_dataset):
            logger.error("commit0 build failed")
        else:
            logger.info("commit0 build complete")

        logger.info("\n--- Generating test IDs ---")
        test_ids_dir = PROJECT_ROOT / "test_ids"
        test_id_results = generate_and_install_test_ids(
            dataset_path=abs_dataset,
            output_dir=test_ids_dir,
            validate_base=not args.skip_validation,
        )

    elapsed = time.monotonic() - start_time
    print_summary(
        entries,
        test_id_results,
        failures,
        elapsed,
        allow_broken_stubs=args.allow_broken_stubs,
    )

    logger.info("Dataset: %s", output_path)
    logger.info("State:   %s", state_path)
    if not args.skip_build and not args.dry_run:
        logger.info(
            "\nNext: bash run_pipeline.sh --model kimi --dataset %s --repo-split all --max-iteration 3",
            output_path,
        )


if __name__ == "__main__":
    main()
