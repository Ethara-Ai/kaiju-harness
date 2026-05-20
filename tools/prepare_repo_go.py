"""Prepare Go repos for a commit0 dataset.

For each validated Go candidate:
1. Fork to target GitHub org
2. Create a 'commit0_all' branch
3. Apply Go AST stubbing via gostubber binary
4. Commit stubbed version as base_commit
5. Reset to original as reference_commit
6. Generate setup/test dict entries
7. Output dataset entries (GoRepoInstance-compatible)

Usage:
    python -m tools.prepare_repo_go validated.json --output dataset_entries.json
    python -m tools.prepare_repo_go --repo sourcegraph/conc --clone-dir ./repos_staging --output dataset_entries.json
    python -m tools.prepare_repo_go validated.json --dry-run --output dataset_entries.json

Requires:
    - GITHUB_TOKEN env var with repo/fork permissions
    - gh CLI installed (for forking)
    - gostubber binary (built automatically from tools/gostubber/)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import re

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_ORG = "Zahgon"
TOOLS_DIR = Path(__file__).parent


def _find_goimports() -> str:
    """Find goimports binary, checking PATH and common Go install locations."""
    path = shutil.which("goimports")
    if path:
        return path
    for candidate in [
        Path.home() / "go" / "bin" / "goimports",
        Path(os.environ.get("GOPATH", "")) / "bin" / "goimports"
        if os.environ.get("GOPATH")
        else None,
        Path(os.environ.get("GOROOT", "")) / "bin" / "goimports"
        if os.environ.get("GOROOT")
        else None,
    ]:
        if candidate and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "goimports not found. Install with: go install golang.org/x/tools/cmd/goimports@latest "
        "and ensure ~/go/bin is on PATH, or set GOPATH."
    )


sys.path.insert(0, str(TOOLS_DIR.parent))
from tools.stub_go import _ensure_gostubber, stub_go_repo

from tools._git_auth import (
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)

_scrape_spec_sync = None


def _get_scrape_func():
    """Lazy-load scrape_spec_sync to avoid importing optional deps at module level."""
    global _scrape_spec_sync
    if _scrape_spec_sync is None:
        from tools.scrape_pdf import scrape_spec_sync

        _scrape_spec_sync = scrape_spec_sync
    return _scrape_spec_sync




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




def full_clone(
    full_name: str, clone_dir: Path, branch: str | None = None, tag: str | None = None
) -> Path:
    repo_dir = clone_dir / full_name.replace("/", "__")
    if repo_dir.exists():
        shallow_file = repo_dir / ".git" / "shallow"
        if shallow_file.exists():
            logger.info("  Unshallowing existing clone...")
            git(repo_dir, "fetch", "--unshallow", check=False, timeout=300)
        if tag:
            git(repo_dir, "fetch", "--tags", timeout=120)
            git(repo_dir, "checkout", tag, check=False)
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


def detect_go_module(repo_dir: Path) -> dict:
    """Detect Go module info from go.mod."""
    go_mod = repo_dir / "go.mod"
    if not go_mod.exists():
        return {}

    info: dict = {}
    content = go_mod.read_text()
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("module "):
            info["module_path"] = line.split(None, 1)[1]
        elif line.startswith("go "):
            info["go_version"] = line.split(None, 1)[1]
    return info


def create_stubbed_branch(
    repo_dir: Path,
    full_name: str,
    branch_name: str | None = None,
) -> tuple[str, str]:
    """Create the commit0 branch with Go-stubbed code.

    Returns (base_commit_sha, reference_commit_sha).

    Workflow:
    1. Record the current HEAD as reference_commit
    2. Create branch 'commit0_all'
    3. Run gostubber on .go source files
    4. Commit stubbed version as base_commit
    """
    if branch_name is None:
        branch_name = "commit0_all"

    gostubber_bin = _ensure_gostubber()
    default_branch = get_default_branch(repo_dir)

    git(repo_dir, "checkout", default_branch)
    reference_commit = get_head_sha(repo_dir)
    logger.info("  Reference commit (original): %s", reference_commit[:12])

    try:
        git(repo_dir, "branch", "-D", branch_name, check=False)
    except Exception:
        pass
    git(repo_dir, "checkout", "-b", branch_name)

    logger.info("  Running gostubber on %s...", repo_dir.name)
    stubbed_count = 0
    for go_file in repo_dir.rglob("*.go"):
        rel = go_file.relative_to(repo_dir)
        if any(p in {"vendor", ".git", "testdata"} for p in rel.parts):
            continue
        if go_file.name.endswith("_test.go") or go_file.name == "doc.go":
            continue
        result = subprocess.run(
            [str(gostubber_bin), str(go_file)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            stubbed_count += 1
    logger.info("  Stubbed %d Go files", stubbed_count)

    logger.info("  Running goimports to clean unused imports...")
    goimports_bin = _find_goimports()
    subprocess.run(
        [goimports_bin, "-w", "."],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )

    for test_file in repo_dir.rglob("*_test.go"):
        subprocess.run(
            ["git", "checkout", "--", str(test_file.relative_to(repo_dir))],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

    git(repo_dir, "add", "-A")

    status = git(repo_dir, "status", "--porcelain")
    if not status:
        logger.warning("  No changes after stubbing — source may already be stubs?")
        base_commit = reference_commit
    else:
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
        if additions == 0 or deletions == 0:
            raise RuntimeError(
                f"Stubbing verification failed for {full_name}: "
                f"additions={additions}, deletions={deletions}. "
                f"Expected both >0."
            )

        git(repo_dir, "commit", "-m", "Commit 0")
        base_commit = get_head_sha(repo_dir)

    logger.info("  Base commit (stubbed): %s", base_commit[:12])
    return base_commit, reference_commit




def resolve_commits_from_remote(fork_name: str, branch: str) -> tuple[str, str] | None:
    """Resolve base/reference commits from remote branch via GitHub API."""
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


def build_setup_dict(repo_dir: Path, go_info: dict, full_name: str) -> dict:
    """Build the setup dict for a Go repo (mirrors Python's pip/packages setup)."""
    pre_install: list[str] = []

    apt_deps_file = repo_dir / ".apt-packages"
    if apt_deps_file.exists():
        pre_install = [
            line.strip()
            for line in apt_deps_file.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    spec_url = _find_docs_url(go_info.get("module_path", ""))

    return {
        "install": "go mod download && go build ./...",
        "packages": "",
        "pip_packages": "",
        "pre_install": pre_install,
        "go_version": go_info.get("go_version", "1.23"),
        "specification": spec_url,
    }


def _find_docs_url(module_path: str) -> str:
    """Construct the official pkg.go.dev documentation URL using the Go module path.

    Uses the module path from go.mod (e.g. 'github.com/go-chi/chi/v5',
    'go.uber.org/zap') to form the canonical pkg.go.dev URL. This correctly
    handles vanity import paths and v2+ major-version suffixes. If scraping
    it 404s, the caller falls back to generating a spec from the README.
    """
    return f"https://pkg.go.dev/{module_path}"




def build_test_dict(repo_dir: Path) -> dict:
    """Build the test dict for a Go repo."""
    return {
        "test_cmd": "go test -json -count=1 ./...",
        "test_dir": ".",
    }


def prepare_single_repo(
    full_name: str,
    clone_dir: Path,
    org: str = DEFAULT_ORG,
    dry_run: bool = False,
    tag: str | None = None,
    specs_dir: str = "./specs",
) -> dict | None:
    logger.info("\n=== Preparing %s ===", full_name)

    try:
        if dry_run:
            forked_name = f"{org}/{full_name.split('/')[-1]}"
            logger.info("  [DRY RUN] Would fork to %s", forked_name)
        else:
            forked_name = fork_repo(full_name, org)

        repo_dir = full_clone(full_name, clone_dir, tag=tag)
        go_info = detect_go_module(repo_dir)

        if not go_info.get("module_path"):
            logger.warning("  No go.mod found — skipping %s", full_name)
            return None

        base_commit, reference_commit = create_stubbed_branch(repo_dir, full_name)

        if not dry_run:
            branch_name = "commit0_all"
            try:
                git(repo_dir, "checkout", branch_name)
                push_to_fork(repo_dir, forked_name, branch=branch_name)
            except Exception as e:
                logger.error("  Push failed: %s", e)
                remote_commits = resolve_commits_from_remote(forked_name, branch_name)
                if remote_commits:
                    base_commit, reference_commit = remote_commits
                    logger.info(
                        "  Resolved commits from remote: base=%s, ref=%s",
                        base_commit[:12],
                        reference_commit[:12],
                    )
                else:
                    logger.warning(
                        "  No remote branch found — using local commits only"
                    )

        setup_dict = build_setup_dict(repo_dir, go_info, full_name)
        test_dict = build_test_dict(repo_dir)

        spec_path = None
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
                    branch_name = "commit0_all"
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
                                repo_dir, forked_name, branch=branch_name
                            )
                        except Exception as e:
                            logger.warning("  Spec push failed: %s", e)
                else:
                    logger.warning("  Spec scraping returned no output")
            except ImportError:
                logger.warning(
                    "  Skipping spec scrape — install: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests && playwright install chromium"
                )
            except Exception as e:
                logger.warning("  Spec scraping failed: %s", e)

        # Fallback: generate a README-based spec if URL scraping produced nothing
        if spec_path is None:
            _rname = full_name.split("/")[-1]
            try:
                from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
                readme_spec_path, readme_spec_url = _scrape_readme_spec(repo_dir, specs_dir, _rname)
            except ImportError:
                readme_spec_path, readme_spec_url = None, ""
            if readme_spec_path:
                if readme_spec_url:
                    setup_dict["specification"] = readme_spec_url
                try:
                    branch_name = "commit0_all"
                    git(repo_dir, "checkout", branch_name)
                    dest = repo_dir / "spec.pdf.bz2"
                    shutil.copy2(str(readme_spec_path), dest)
                    git(repo_dir, "add", "spec.pdf.bz2")
                    git(repo_dir, "commit", "-m", f"Add README-based spec for {_rname}")
                    base_commit = get_head_sha(repo_dir)
                    logger.info(
                        "  Updated base_commit with README spec: %s", base_commit[:12]
                    )
                    if not dry_run:
                        try:
                            push_to_fork(
                                repo_dir, forked_name, branch=branch_name
                            )
                        except Exception as push_err:
                            logger.warning("  README spec push failed: %s", push_err)
                    spec_path = str(readme_spec_path)
                except Exception as commit_err:
                    logger.warning("  README spec commit failed: %s", commit_err)

        repo_name = full_name.split("/")[-1]
        entry = {
            "instance_id": f"{full_name.replace('/', '_')}_go",
            "repo": forked_name,
            "original_repo": full_name,
            "base_commit": base_commit,
            "reference_commit": reference_commit,
            "setup": setup_dict,
            "test": test_dict,
            "src_dir": ".",
            "language": "go",
        }

        if dry_run:
            entry["base_commit"] = "DRY_RUN"
            entry["reference_commit"] = "DRY_RUN"

        logger.info("  Entry created for %s", full_name)
        return entry

    except Exception as e:
        logger.error("  FAILED to prepare %s: %s", full_name, e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare Go repos for a commit0 dataset"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Input validated.json from validate_go.py",
    )
    parser.add_argument("--repo", type=str, help="Single repo to prepare (owner/name)")
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("./repos_staging"),
        help="Directory for cloning repos (default: ./repos_staging)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="dataset_entries.json",
        help="Output JSON file (default: dataset_entries.json)",
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
        help="Skip GitHub fork and push operations",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Maximum number of repos to prepare",
    )
    parser.add_argument(
        "--tag",
        type=str,
        default=None,
        help="Git tag to checkout before stubbing",
    )
    parser.add_argument(
        "--specs-dir",
        type=str,
        default="./specs",
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )

    args = parser.parse_args()

    setup_git_credentials(dry_run=args.dry_run)

    args.clone_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []

    if args.repo:
        result = prepare_single_repo(
            args.repo,
            args.clone_dir,
            org=args.org,
            dry_run=args.dry_run,
            tag=args.tag,
            specs_dir=args.specs_dir,
        )
        if result:
            entries.append(result)
    elif args.input_file:
        candidates = json.loads(Path(args.input_file).read_text())
        if isinstance(candidates, dict) and "data" in candidates:
            candidates = candidates["data"]

        for i, candidate in enumerate(candidates):
            if args.max_repos and i >= args.max_repos:
                break

            full_name = candidate.get("full_name") or candidate.get("repo", "")
            if not full_name:
                logger.warning("  Skipping entry %d: no full_name or repo", i)
                continue

            result = prepare_single_repo(
                full_name,
                args.clone_dir,
                org=args.org,
                dry_run=args.dry_run,
                tag=candidate.get("tag"),
                specs_dir=args.specs_dir,
            )
            if result:
                entries.append(result)
    else:
        parser.error("Provide either input_file or --repo")

    output_path = Path(args.output)
    output_path.write_text(json.dumps(entries, indent=2))
    logger.info("\nSaved %d entries to %s", len(entries), output_path)


if __name__ == "__main__":
    main()
