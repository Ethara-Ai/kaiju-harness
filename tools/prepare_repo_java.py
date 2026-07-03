"""Prepare Java repos for a commit0 dataset.

For each repo entry in java_dataset.json:
1. Fork to GitHub org (default: Zahgon)
2. Full clone at base_commit tag
3. Create 'base' branch
4. Stub Java sources (replace method bodies with UnsupportedOperationException)
5. Commit stubbed version
6. Scrape spec PDF into repo
7. Push 'base' branch to fork
8. Generate dataset entry with repo pointing to fork

Usage:
    # Single repo from java_dataset.json:
    python tools/prepare_repo_java.py java_dataset.json \\
        --repo apache/commons-io --output commons_io_entries.json

    # All repos:
    python tools/prepare_repo_java.py java_dataset.json --output java_entries.json

    # Dry run (no fork, no push):
    python tools/prepare_repo_java.py java_dataset.json \\
        --repo apache/commons-io --dry-run --output java_entries.json

Requires:
    - gh CLI authenticated with repo/fork permissions
    - JDK 17 + Maven (for JavaStubber)
    - javastubber JAR built (auto-built if missing)
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
import uuid as _uuid_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_ORG = "Zahgon"

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR.parent))

from commit0.harness.constants_java import JAVA_REMOTE_BRANCH as REMOTE_BRANCH

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)


# ─── Git Helpers ──────────────────────────────────────────────────────────────




def get_head_sha(repo_dir: Path) -> str:
    return git(repo_dir, "rev-parse", "HEAD")


# ─── Fork ─────────────────────────────────────────────────────────────────────




# ─── Clone ────────────────────────────────────────────────────────────────────


def full_clone(full_name: str, clone_dir: Path, tag: str) -> Path:
    """Full clone of a repo at a specific tag. Returns repo dir."""
    repo_short = full_name.split("/")[-1]
    repo_dir = clone_dir / repo_short

    if repo_dir.exists():
        logger.info("  Repo dir exists, cleaning...")
        shutil.rmtree(repo_dir)

    url = f"https://github.com/{full_name}.git"
    logger.info("  Cloning %s at tag %s...", full_name, tag)

    try:
        subprocess.run(
            ["git", "clone", "--branch", tag, url, str(repo_dir)],
            capture_output=True, text=True, timeout=600, check=True,
        )
    except subprocess.CalledProcessError:
        # Tag might not be directly cloneable, clone then checkout
        subprocess.run(
            ["git", "clone", url, str(repo_dir)],
            capture_output=True, text=True, timeout=600, check=True,
        )
        git(repo_dir, "checkout", tag)

    # Fetch both tags (base + reference) to ensure they exist locally
    git(repo_dir, "fetch", "--tags", timeout=300)

    return repo_dir


# ─── Java Source Detection ────────────────────────────────────────────────────


def find_java_source_dirs(repo_dir: Path) -> list[Path]:
    """Locate Java source directories, handling both standard and monorepo layouts."""
    standard = repo_dir / "src" / "main" / "java"
    if standard.exists():
        return [standard]

    candidates = list(repo_dir.rglob("src/main/java"))
    if candidates:
        return [c for c in candidates if "test" not in str(c).lower()]

    # Monorepo layout (e.g. guava): <submodule>/src/ with .java files
    monorepo_dirs = []
    for child in sorted(repo_dir.iterdir()):
        if child.is_dir() and (child / "src").is_dir():
            java_files = list((child / "src").rglob("*.java"))
            if java_files:
                monorepo_dirs.append(child / "src")
    return monorepo_dirs


# ─── Stub & Commit ───────────────────────────────────────────────────────────


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    entry: dict,
) -> tuple[str, str]:
    """Create the remote branch with stubbed code.

    Returns (base_commit_sha, reference_commit_sha).
    All source changes (workflow removal + gitignore + stubs) go into a single
    "Commit 0", matching Python prepare_repo.py. Spec PDF is a separate commit.
    """
    base_tag = entry["base_commit"]

    git(repo_dir, "checkout", base_tag, check=False)

    reference_tag = entry.get("reference_commit", base_tag)
    try:
        reference_commit = git(repo_dir, "rev-parse", reference_tag)
    except Exception:
        reference_commit = get_head_sha(repo_dir)

    try:
        git(repo_dir, "branch", "-D", REMOTE_BRANCH, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", REMOTE_BRANCH)
    logger.info("  Created branch: %s (from tag %s)", REMOTE_BRANCH, base_tag)

    workflows_dir = repo_dir / ".github" / "workflows"
    if workflows_dir.exists():
        git(repo_dir, "rm", "-r", ".github/workflows")
        logger.info("  Removed .github/workflows")

    gitignore_path = repo_dir / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text().splitlines()
    added = []
    for ignore_entry in [".aider*", "logs/", ".commit0_scripts/", ".github/workflows/"]:
        if ignore_entry not in existing_lines:
            added.append(ignore_entry)
    if added:
        with open(gitignore_path, "a") as f:
            for line in added:
                f.write(f"\n{line}")
            f.write("\n")
        logger.info("  Added %s to .gitignore", added)

    from tools.stub_java import stub_java_sources

    src_dirs = find_java_source_dirs(repo_dir)
    if not src_dirs:
        raise RuntimeError(f"No Java source dirs found in {repo_dir}")

    total_stubs = 0
    total_files = 0
    for src_dir in src_dirs:
        result = stub_java_sources(src_dir=str(src_dir))
        total_stubs += result.get("totalStubs", 0)
        total_files += result.get("totalFiles", 0)
    logger.info("  Stubbed %d methods across %d files", total_stubs, total_files)

    if total_stubs == 0:
        raise RuntimeError(f"No stubs generated for {full_name}")

    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-m", "Commit 0")
    base_commit = get_head_sha(repo_dir)
    logger.info("  Base commit (Commit 0): %s", base_commit[:12])

    return base_commit, reference_commit





# ─── Spec Scraping ───────────────────────────────────────────────────────────


def scrape_spec(repo_dir: Path, repo_short: str, spec_url: str, specs_dir: Path) -> bool:
    """Scrape spec PDF and commit into repo. Returns True if successful."""
    dest = repo_dir / "spec.pdf.bz2"
    if dest.exists():
        logger.info("  spec.pdf.bz2 already exists, skipping")
        return True

    # Try cached spec first
    cached = specs_dir / f"{repo_short}.pdf.bz2"
    if cached.exists():
        shutil.copy2(cached, dest)
        git(repo_dir, "add", "spec.pdf.bz2")
        git(repo_dir, "commit", "-m", f"Add spec PDF for {repo_short}")
        logger.info("  Used cached spec")
        return True

    if not spec_url:
        logger.info("  No spec URL, skipping")
        return False

    try:
        from tools.scrape_pdf import scrape_spec_sync
        logger.info("  Scraping spec from: %s", spec_url)
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
            logger.info("  Spec saved and committed")
            return True
        logger.warning("  Spec scraping returned no output")
        return False
    except ImportError:
        logger.warning("  scrape_pdf not available, skipping spec")
        return False
    except Exception as e:
        logger.warning("  Spec scraping failed: %s", e)
        return False


# ─── Push to Fork ─────────────────────────────────────────────────────────────




# ─── Dataset Entry ────────────────────────────────────────────────────────────


def _detect_java_version(repo_dir: Path) -> str:
    """Backward-compat wrapper around :func:`tools.java_version.detect`."""
    from commit0.harness.constants_java import (
        JAVA_VERSION_DEFAULT,
        SUPPORTED_JAVA_VERSIONS,
    )
    from tools.java_version import detect as _detect
    from tools._versioning import NoSignalsError, VersionConflictError

    try:
        return _detect(repo_dir, SUPPORTED_JAVA_VERSIONS, fallback=JAVA_VERSION_DEFAULT).version or JAVA_VERSION_DEFAULT
    except (VersionConflictError, NoSignalsError):
        return JAVA_VERSION_DEFAULT


def _detect_java_version_full(repo_dir: Path):
    """Return the full DetectionResult so callers can capture provenance."""
    from commit0.harness.constants_java import (
        JAVA_VERSION_DEFAULT,
        SUPPORTED_JAVA_VERSIONS,
    )
    from tools.java_version import detect as _detect
    from tools._versioning import NoSignalsError, VersionConflictError

    try:
        return _detect(repo_dir, SUPPORTED_JAVA_VERSIONS, fallback=JAVA_VERSION_DEFAULT)
    except VersionConflictError as exc:
        from tools._versioning import DetectionResult
        return DetectionResult(
            version=JAVA_VERSION_DEFAULT,
            source="conflict-fallback",
            conflicts=[f"{src}: {reason}" for src, reason in exc.rejecting_sources.items()],
            all_signals={},
        )
    except NoSignalsError:
        from tools._versioning import DetectionResult
        return DetectionResult(
            version=JAVA_VERSION_DEFAULT, source="default", conflicts=[], all_signals={},
        )


def create_dataset_entry(
    full_name: str,
    fork_name: str,
    base_commit: str,
    reference_commit: str,
    entry: dict,
    repo_dir: Path | None = None,
) -> dict:
    """Create a dataset entry with repo pointing to the fork."""
    repo_short = full_name.split("/")[-1]
    setup = dict(entry.get("setup") or {})
    if repo_dir is not None and "java_version" not in setup:
        det = _detect_java_version_full(repo_dir)
        setup["java_version"] = det.version
        setup["version_source"] = det.source
        setup["version_conflicts"] = det.conflicts


    return {
        "instance_id": f"commit-0/{repo_short}",
        "id": str(_uuid_mod.uuid4()),
        "repo": fork_name,
        "original_repo": full_name,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "setup": setup,
        "test": entry.get("test", {}),
        "src_dir": entry.get("src_dir", "src/main/java"),
    }


# ─── Main ─────────────────────────────────────────────────────────────────────


def prepare_java_repos(
    entries: list[dict],
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    dry_run: bool = False,
    specs_dir: str = "./specs",
    repo_filter: str | None = None,
) -> list[dict]:
    """Prepare Java repos for the dataset."""
    dataset_entries: list[dict] = []
    specs_path = Path(specs_dir)
    specs_path.mkdir(parents=True, exist_ok=True)

    for i, entry in enumerate(entries):
        full_name = entry["repo"]

        # Filter if specific repo requested
        if repo_filter and full_name != repo_filter:
            continue

        logger.info(
            "\n[%d/%d] Preparing %s...",
            i + 1, len(entries), full_name,
        )

        # Fork
        if dry_run:
            fork_name = f"{org}/{full_name.split('/')[-1]}"
            logger.info("  [DRY RUN] Would fork to %s", fork_name)
        else:
            try:
                fork_name = fork_repo(full_name, org)
            except Exception as e:
                logger.error("  Fork failed: %s", e)
                continue

        # Full clone at base_commit tag
        base_tag = entry["base_commit"]
        try:
            repo_dir = full_clone(full_name, clone_dir, tag=base_tag)
        except Exception as e:
            logger.error("  Clone failed: %s", e)
            continue

        # Create stubbed branch
        try:
            base_commit, reference_commit = create_stubbed_branch(
                repo_dir, full_name, entry,
            )
        except Exception as e:
            logger.error("  Stubbing failed: %s", e)
            continue

        # Scrape spec
        spec_url = entry.get("setup", {}).get("specification", "")
        scrape_spec(repo_dir, full_name.split("/")[-1], spec_url, specs_path)
        readme_spec_url = ""
        if not (repo_dir / "spec.pdf.bz2").exists() and not dry_run:
            repo_short = full_name.split("/")[-1]
            try:
                from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
                readme_spec_path, readme_spec_url = _scrape_readme_spec(repo_dir, specs_path, repo_short)
            except ImportError:
                readme_spec_path = None
            if readme_spec_path:
                if readme_spec_url:
                    entry.setdefault("setup", {})["specification"] = readme_spec_url
                try:
                    git(repo_dir, "checkout", REMOTE_BRANCH)
                    shutil.copy2(str(readme_spec_path), str(repo_dir / "spec.pdf.bz2"))
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add README-based spec for {repo_short}")
                    logger.info("  README spec committed")
                except Exception as e:
                    logger.warning("  README spec fallback failed: %s", e)

        base_commit = get_head_sha(repo_dir)

        # Push to fork
        if not dry_run:
            try:
                push_to_fork(repo_dir, fork_name, REMOTE_BRANCH)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                # Continue anyway — local clone still usable

        # Create dataset entry
        dataset_entry = create_dataset_entry(
            full_name=full_name,
            fork_name=fork_name,
            base_commit=base_commit,
            reference_commit=reference_commit,
            entry=entry,
            repo_dir=repo_dir,
        )
        logger.info("  Entry: repo=%s, base=%s", fork_name, base_commit[:12])
        dataset_entries.append(dataset_entry)

    return dataset_entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare Java repos for commit0 dataset"
    )
    parser.add_argument(
        "dataset_file",
        help="Input java_dataset.json with repo entries",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Prepare a single repo (e.g., apache/commons-io)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="java_entries.json",
        help="Output JSON file (default: java_entries.json)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default="./repos_staging/java",
        help="Directory to clone repos into (default: ./repos_staging/java)",
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
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory for spec PDFs (default: ./specs)",
    )

    args = parser.parse_args()

    # Load entries
    raw = json.loads(Path(args.dataset_file).read_text())
    if isinstance(raw, dict) and "entries" in raw:
        entries = raw["entries"]
    elif isinstance(raw, list):
        entries = raw
    else:
        parser.error("Unrecognized dataset format")
        return

    clone_dir = Path(args.clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=True)

    setup_git_credentials(dry_run=args.dry_run)

    dataset_entries = prepare_java_repos(
        entries,
        clone_dir=clone_dir,
        org=args.org,
        dry_run=args.dry_run,
        specs_dir=args.specs_dir,
        repo_filter=args.repo,
    )

    # Save entries
    output_path = Path(args.output)
    output_path.write_text(json.dumps(dataset_entries, indent=2))
    logger.info("Saved %d entries to %s", len(dataset_entries), output_path)

    # Summary
    print(f"\n{'=' * 80}")
    print(f"PREPARED ENTRIES: {len(dataset_entries)}")
    print(f"{'=' * 80}")
    for e in dataset_entries:
        print(f"  {e['instance_id']}: {e['repo']} (base={e['base_commit'][:12]})")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
