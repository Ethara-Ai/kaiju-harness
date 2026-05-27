"""
Prepare Rust repos for a commit0 dataset.

For each repo:
1. Fork to Zahgon GitHub org
2. Clone locally, record reference_commit (HEAD)
3. Create 'commit0_all' branch
4. Run ruststubber on source files
5. Commit stubbed version as base_commit
6. Push commit0_all branch to fork
7. Collect test IDs via cargo test --list
8. Save test IDs as .bz2
9. Append entry to rust_dataset.json
10. Generate per-repo YAML config in commit0/data/

Usage:
    python3 -m tools.prepare_repo_rust \
        --repo open-telemetry/opentelemetry-rust \
        --crate opentelemetry-http \
        --src-dir opentelemetry-http/src \
        --test-cmd "cargo test -p opentelemetry-http"

    # Dry run (no fork, no push):
    python3 -m tools.prepare_repo_rust \
        --repo serde-rs/serde \
        --crate serde \
        --src-dir serde/src \
        --test-cmd "cargo test -p serde" \
        --dry-run

Requires:
    - gh CLI installed (for forking)
    - ruststubber binary built at tools/ruststubber/target/release/ruststubber
    - cargo installed (for cargo test --list)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Paths
TOOLS_DIR = Path(__file__).parent
PROJECT_ROOT = TOOLS_DIR.parent
RUSTSTUBBER = TOOLS_DIR / "ruststubber" / "target" / "release" / "ruststubber"
SPECS_DIR = PROJECT_ROOT / "specs"

DEFAULT_ORG = "Zahgon"


# ─── Git Helpers ──────────────────────────────────────────────────────────────
# git(), fork_repo(), push_to_fork() are imported from tools._git_auth
# (the single source of truth for all prepare_repo_* pipelines).


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


# ─── Fork & Clone ────────────────────────────────────────────────────────────


def clone_repo(full_name: str, clone_dir: Path) -> Path:
    """Full clone of a repo. Returns repo dir."""
    repo_name = full_name.split("/")[-1]
    repo_dir = clone_dir / repo_name

    if repo_dir.exists():
        logger.info("Clone already exists: %s", repo_dir)
        return repo_dir

    url = f"https://github.com/{full_name}.git"
    logger.info("Cloning %s...", full_name)
    result = subprocess.run(
        ["git", "clone", url, str(repo_dir)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git clone failed (exit {result.returncode}) for {full_name}:\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return repo_dir


# ─── Stubbing ────────────────────────────────────────────────────────────────


def stub_source_dir(repo_dir: Path, src_dir_relative: str) -> tuple[int, int]:
    """Stub all .rs files in src_dir using ruststubber --in-place.

    The ruststubber binary walks the directory, skips target/ directories,
    stubs .rs files, and copies non-.rs files unchanged. Returns (success_count, fail_count).
    """
    src_dir = repo_dir / src_dir_relative
    if not src_dir.is_dir():
        logger.error("Source directory not found: %s", src_dir)
        return 0, 0

    logger.info("Running ruststubber --in-place on %s", src_dir_relative)

    try:
        result = subprocess.run(
            [str(RUSTSTUBBER), "--input-dir", str(src_dir), "--in-place"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.error("ruststubber timed out on %s", src_dir_relative)
        return 0, 1

    ok, fail = 0, 0
    for line in result.stderr.splitlines():
        if line.startswith("ruststubber:"):
            m_ok = re.search(r"(\d+)\s+files?\s+stubbed", line)
            m_err = re.search(r"(\d+)\s+errors?", line)
            if m_ok:
                ok = int(m_ok.group(1))
            if m_err:
                fail = int(m_err.group(1))

    if result.returncode != 0:
        logger.warning(
            "ruststubber exited with code %d: %s",
            result.returncode,
            result.stderr.strip(),
        )

    logger.info("Stubbed %d files (%d errors)", ok, fail)
    return ok, fail


# ─── Spec Scraping ───────────────────────────────────────────────────────────


def scrape_spec(crate: str, repo_dir: Path) -> Path | None:
    """Scrape docs.rs documentation for a crate into a compressed PDF.

    Places <crate>.pdf.bz2 at the repo root. Returns the path on success, None on failure.
    """
    try:
        from scrape_rust_pdf import scrape_rust_spec
    except ImportError:
        logger.warning(
            "scrape_rust_pdf not available (missing deps: playwright, PyMuPDF, PyPDF2, beautifulsoup4). "
            "Skipping spec generation."
        )
        return None

    tmp_specs = repo_dir / "_spec_tmp"
    try:
        result = scrape_rust_spec(
            base_url=f"https://docs.rs/{crate}/latest/{crate}/",
            name=crate,
            output_dir=str(tmp_specs),
            compress=True,
            max_pages=500,
        )
        if not result:
            logger.warning("Spec scraping produced no output for %s", crate)
            return None

        src_path = Path(result)
        dest_path = repo_dir / src_path.name
        shutil.move(str(src_path), str(dest_path))
        logger.info("Spec placed at repo root: %s", dest_path.name)
        return dest_path
    except Exception as e:
        logger.warning("Spec scraping failed for %s: %s", crate, e)
        return None
    finally:
        if tmp_specs.exists():
            shutil.rmtree(tmp_specs, ignore_errors=True)


# ─── Dataset Entry ───────────────────────────────────────────────────────────


def create_dataset_entry(
    upstream: str,
    fork_name: str,
    crate: str,
    src_dir: str,
    test_cmd: str,
    base_commit: str,
    reference_commit: str,
    rust_version: str = "stable",
    edition: str = "2021",
    packages: str = "pkg-config libssl-dev",
    specification: str = "",
    version_source: str = "default",
    version_conflicts: list[str] | None = None,
) -> dict:
    """Create a dataset entry compatible with RustRepoInstance."""
    # Determine test_dir from src_dir (parent of /src usually)
    test_dir = src_dir.rsplit("/src", 1)[0] if "/src" in src_dir else crate

    return {
        "instance_id": f"commit-0/{crate}",
        "repo": fork_name,
        "original_repo": upstream,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "setup": {
            "rust": rust_version,
            "edition": edition,
            "packages": packages,
            "pre_install": [],
            "install": "cargo fetch",
            "specification": specification,
            "version_source": version_source,
            "version_conflicts": version_conflicts or [],
        },
        "test": {
            "test_cmd": test_cmd,
            "test_dir": test_dir,
        },
        "src_dir": src_dir,
        "language": "rust",
    }


def get_dataset_path(repo_name: str) -> Path:
    """Return the per-repo dataset file path: PROJECT_ROOT/<reponame>_rust_dataset.json."""
    return PROJECT_ROOT / f"{repo_name}_dataset.json"


def append_to_dataset(entry: dict, repo_name: str) -> Path:
    """Write entry to <reponame>_dataset.json in project root.

    Returns the dataset file path.
    """
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

    # Remove existing entry with same instance_id (update in place)
    existing = [e for e in existing if e.get("instance_id") != entry["instance_id"]]
    existing.append(entry)

    content = json.dumps(existing, indent=2) + "\n"
    dataset_file.write_text(content)
    logger.info("Updated %s (%d entries)", dataset_file, len(existing))

    entries_file = PROJECT_ROOT / f"{repo_name}_entries.json"
    entries_file.write_text(content)
    logger.info("Updated %s", entries_file)

    return dataset_file


# ─── Per-Repo YAML Config ───────────────────────────────────────────────────


def generate_commit0_yaml(crate: str, repo_name: str, entry: dict) -> Path:
    """Generate .commit0_rust.yaml in project root (single config file, overwritten each run)."""
    yaml_path = PROJECT_ROOT / ".commit0_rust.yaml"
    dataset_file = f"./{repo_name}_dataset.json"

    content = f"""# commit0 Rust config for {crate}
dataset_name: {dataset_file}
dataset_split: test
repo_split: all
base_dir: repos

# Repo details
# upstream: {entry["original_repo"]}
# fork: {entry["repo"]}
# crate: {crate}
# language: rust
# test_cmd: {entry["test"]["test_cmd"]}
# src_dir: {entry["src_dir"]}
"""
    yaml_path.write_text(content)
    logger.info("Generated config: %s", yaml_path)
    return yaml_path


# ─── Main Pipeline ───────────────────────────────────────────────────────────


def prepare_rust_repo(
    upstream: str,
    crate: str,
    src_dir: str,
    test_cmd: str,
    org: str = DEFAULT_ORG,
    clone_dir: Path | None = None,
    dry_run: bool = False,
    rust_version: str = "stable",
    edition: str = "2021",
    packages: str = "pkg-config libssl-dev",
    skip_spec: bool = False,
    specs_dir: Path = SPECS_DIR,
) -> dict | None:
    """
    Run the full preparation pipeline for a single Rust repo/crate.

    Returns the dataset entry dict on success, None on failure.
    """
    repo_name = upstream.split("/")[-1]

    if clone_dir is None:
        clone_dir = Path("repos_staging")

    logger.info("=" * 60)
    logger.info("Preparing: %s (crate: %s)", upstream, crate)
    logger.info("=" * 60)

    # Step 1: Fork
    if dry_run:
        fork_name = f"{org}/{repo_name}"
        logger.info("[DRY RUN] Would fork %s to %s", upstream, org)
    else:
        fork_name = fork_repo(upstream, org)

    # Step 2: Clone (from fork so we can push)
    repo_dir = clone_repo(fork_name, clone_dir)

    # Detect rust toolchain + edition from the cloned repo. CLI kwargs win
    # only when the user explicitly overrode the defaults; otherwise the
    # detected values flow through.
    from tools.rust_version import detect as _detect_rust

    det = _detect_rust(repo_dir)
    detected_version = det.version or rust_version
    detected_edition = det.edition
    version_source = det.source
    version_conflicts = det.conflicts
    if rust_version == "stable":  # only override on default
        rust_version = detected_version
    if edition == "2021":  # only override on default
        edition = detected_edition
    logger.info(
        "Rust detection: version=%s (src=%s) edition=%s (src=%s) conflicts=%s",
        rust_version,
        version_source,
        edition,
        det.edition_source,
        det.conflicts or "(none)",
    )

    # Step 3: Record reference commit
    reference_commit = get_head_sha(repo_dir)
    logger.info("Reference commit: %s", reference_commit[:12])

    # Step 4: Create commit0_all branch
    default_branch = get_default_branch(repo_dir)
    try:
        git(repo_dir, "checkout", "-b", "commit0_all")
    except subprocess.CalledProcessError:
        # Branch may already exist
        git(repo_dir, "checkout", "commit0_all")
        git(repo_dir, "reset", "--hard", default_branch)

    # Step 5: Stub source files
    ok, fail = stub_source_dir(repo_dir, src_dir)
    if ok == 0:
        logger.error("No files were stubbed. Aborting.")
        return None

    # Step 7: Commit
    git(repo_dir, "add", "-A")
    git(repo_dir, "commit", "-m", f"Commit 0: stub {crate} source")
    base_commit = get_head_sha(repo_dir)
    logger.info("Base commit (stubbed): %s", base_commit[:12])

    # Step 7.5: Scrape spec PDF
    spec_filename = ""
    readme_spec_url = ""
    if not skip_spec:
        spec_path = scrape_spec(crate, repo_dir)
        if spec_path:
            spec_filename = spec_path.name
            git(repo_dir, "add", spec_filename)
            git(repo_dir, "commit", "-m", f"Add {crate} API spec (docs.rs PDF)")
            # Save a local copy to specs_rust/
            specs_dir.mkdir(parents=True, exist_ok=True)
            local_spec = specs_dir / spec_filename
            shutil.copy2(str(spec_path), str(local_spec))
            logger.info("Local spec copy: %s", local_spec)
        else:
            if not dry_run:
                try:
                    from tools.scrape_pdf import (
                        scrape_readme_spec as _scrape_readme_spec,
                    )

                    readme_spec_path, readme_spec_url = _scrape_readme_spec(
                        repo_dir, specs_dir, crate
                    )
                except ImportError:
                    readme_spec_path = None
                if readme_spec_path:
                    try:
                        git(repo_dir, "checkout", "commit0_all")
                        shutil.copy2(
                            str(readme_spec_path), str(repo_dir / "spec.pdf.bz2")
                        )
                        git(repo_dir, "add", "spec.pdf.bz2")
                        git(
                            repo_dir,
                            "commit",
                            "-m",
                            f"Add README-based spec for {crate}",
                        )
                        base_commit = get_head_sha(repo_dir)
                        spec_filename = "spec.pdf.bz2"
                        logger.info("  README spec committed")
                    except Exception as e:
                        logger.warning("  README spec fallback failed: %s", e)
    else:
        logger.info("Skipping spec generation (--skip-spec)")

    # Step 8: Push
    if dry_run:
        logger.info("[DRY RUN] Would push commit0_all to %s", fork_name)
    else:
        try:
            push_to_fork(repo_dir, fork_name, "commit0_all", remote_name="origin")
        except Exception as e:
            logger.error("Push failed for %s: %s", fork_name, e)

    # Step 9: Test ID collection removed — use tools/generate_test_ids_rust.py separately

    # Step 10: Create dataset entry
    entry = create_dataset_entry(
        upstream=upstream,
        fork_name=fork_name,
        crate=crate,
        src_dir=src_dir,
        test_cmd=test_cmd,
        base_commit=base_commit,
        reference_commit=reference_commit,
        rust_version=rust_version,
        edition=edition,
        version_source=version_source,
        version_conflicts=version_conflicts,
        packages=packages,
        specification=readme_spec_url or f"https://docs.rs/{crate}",
    )

    if not dry_run:
        append_to_dataset(entry, repo_name)
    else:
        logger.info("[DRY RUN] Dataset entry:\n%s", json.dumps(entry, indent=2))

    # Step 11: Generate .commit0.yaml
    if not dry_run:
        generate_commit0_yaml(crate, repo_name, entry)
    else:
        logger.info("[DRY RUN] Would generate .commit0.yaml")

    logger.info("=" * 60)
    logger.info("SUCCESS: %s prepared", crate)
    logger.info("  fork:       %s", fork_name)
    logger.info("  reference:  %s", reference_commit[:12])
    logger.info("  base:       %s", base_commit[:12])
    logger.info("  stubbed:    %d files", ok)
    logger.info("=" * 60)

    return entry


def _fetch_cargo_toml(upstream: str, sub_path: str = "") -> str | None:
    import urllib.request

    suffix = f"/{sub_path.strip('/')}" if sub_path else ""
    for branch in ("main", "master"):
        url = (
            f"https://raw.githubusercontent.com/{upstream}/{branch}{suffix}/Cargo.toml"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kaiju-prepare"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return r.read().decode("utf-8")
        except Exception:
            continue
    return None


def _derive_rust_defaults(upstream: str) -> dict:
    short = upstream.split("/")[-1]
    fallback = {"crate": short, "src_dir": "src", "test_cmd": "cargo test"}
    raw = _fetch_cargo_toml(upstream)
    if not raw:
        return fallback
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        data = tomllib.loads(raw)
    except Exception:
        return fallback

    if "package" in data and "name" in data["package"]:
        return {
            "crate": data["package"]["name"],
            "src_dir": "src",
            "test_cmd": "cargo test",
        }

    if "workspace" in data:
        members = data["workspace"].get("members") or []
        for member in members:
            if "*" in member:
                continue
            sub_raw = _fetch_cargo_toml(upstream, sub_path=member)
            if not sub_raw:
                continue
            try:
                sub_data = tomllib.loads(sub_raw)
            except Exception:
                continue
            if "package" in sub_data and "name" in sub_data["package"]:
                crate = sub_data["package"]["name"]
                return {
                    "crate": crate,
                    "src_dir": f"{member.rstrip('/')}/src",
                    "test_cmd": f"cargo test -p {crate}",
                }
    return fallback


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a Rust repo for commit0 dataset"
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Single repo to prepare (owner/name, e.g. dtolnay/syn).",
    )
    parser.add_argument(
        "--upstream",
        default=None,
        help="Alias for --repo (kept for backwards compatibility).",
    )
    parser.add_argument(
        "--output",
        default="dataset_entries.json",
        help="Output JSON file (default: dataset_entries.json)",
    )
    parser.add_argument(
        "--crate",
        default=None,
        help="Crate name to stub. Auto-detected from Cargo.toml if omitted.",
    )
    parser.add_argument(
        "--src-dir",
        default=None,
        help="Source dir relative to repo root. Defaults to 'src' (or '<member>/src' for workspaces).",
    )
    parser.add_argument(
        "--test-cmd",
        default=None,
        help="Test command. Defaults to 'cargo test' (or 'cargo test -p <crate>' for workspaces).",
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("repos_staging"),
        help="Directory for local clones (default: ./repos_staging)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path("specs"),
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip fork, push, and dataset writes",
    )
    parser.add_argument(
        "--rust-version",
        default="stable",
        help="Rust version for setup (default: stable)",
    )
    parser.add_argument(
        "--edition",
        default="2021",
        help="Rust edition (default: 2021)",
    )
    parser.add_argument(
        "--packages",
        default="pkg-config libssl-dev",
        help="System packages needed (default: pkg-config libssl-dev)",
    )
    parser.add_argument(
        "--skip-spec",
        action="store_true",
        help="Skip scraping docs.rs spec PDF",
    )

    args = parser.parse_args()

    if args.repo is None:
        args.repo = args.upstream
    if not args.repo:
        parser.error("--repo is required")

    setup_git_credentials(dry_run=args.dry_run)

    if not all([args.crate, args.src_dir, args.test_cmd]):
        derived = _derive_rust_defaults(args.repo)
        if not args.crate:
            args.crate = derived["crate"]
        if not args.src_dir:
            args.src_dir = derived["src_dir"]
        if not args.test_cmd:
            args.test_cmd = derived["test_cmd"]
        logger.info(
            "Resolved Rust args — crate=%s src_dir=%s test_cmd=%r",
            args.crate, args.src_dir, args.test_cmd,
        )

    if not RUSTSTUBBER.exists():
        logger.error(
            "ruststubber binary not found at %s\n"
            "Build it first: cd tools/ruststubber && cargo build --release",
            RUSTSTUBBER,
        )
        sys.exit(1)

    entry = prepare_rust_repo(
        upstream=args.repo,
        crate=args.crate,
        src_dir=args.src_dir,
        test_cmd=args.test_cmd,
        org=args.org,
        clone_dir=args.clone_dir,
        specs_dir=args.specs_dir,
        dry_run=args.dry_run,
        rust_version=args.rust_version,
        edition=args.edition,
        packages=args.packages,
        skip_spec=args.skip_spec,
    )

    if entry is None:
        sys.exit(1)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps([entry], indent=2))
    logger.info("Wrote dataset entry to %s", out_path)


if __name__ == "__main__":
    main()
