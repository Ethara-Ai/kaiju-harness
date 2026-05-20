"""Prepare C repos for a commit0 dataset.

For each validated C candidate:
1. Clone to a working directory.
2. Run ``validate_c.validate_c_candidate`` and reject if it fails.
3. Record ``reference_commit`` (HEAD before stubbing).
4. Run ``cstubber`` (libclang) over the source tree.
5. Commit the stubbed version on a ``commit0`` branch as ``base_commit``.
6. Emit a dataset entry compatible with ``CRepoInstance`` /
   ``create_dataset_c.py``.

Optional behaviour gated behind flags:
* ``--fork-org <org>`` — fork to a GitHub org via ``gh repo fork``.
* ``--push`` — push the ``commit0`` branch to the fork.
* ``--scrape-spec`` — scrape a PDF spec from ``--spec-url`` (best-effort),
  compress to ``spec.pdf.bz2``, and commit it into the stubbed branch so it
  becomes part of ``base_commit`` (mirrors the Python ``prepare_repo.py``).

Usage:
    python -m tools.prepare_repo_c --repo DaveGamble/cJSON \\
        --clone-dir ./repos_staging --output entries.json
    python -m tools.prepare_repo_c --repo DaveGamble/cJSON \\
        --scrape-spec --spec-url https://docs.example.org/cjson
    python -m tools.prepare_repo_c candidates.json --output entries.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR.parent))

from tools.validate_c import validate_c_candidate  # noqa: E402
from tools.cstubber.cstubber import (  # noqa: E402
    DEFAULT_FALLBACK_ARGS,
    stub_directory,
)

from tools._git_auth import (  # noqa: E402
    git,
    fork_repo,
    push_to_fork,
    setup_git_credentials,
)

# GitHub org to fork repos into (matches Rust/C++/Java/TS pipelines).
DEFAULT_ORG = "Zahgon"

# Lazy handle for the optional Playwright-backed spec scraper. Importing
# ``tools.scrape_pdf`` eagerly would pull in optional heavy deps, so defer it.
_scrape_spec = None


def _get_scrape_func():
    """Lazy-load ``scrape_spec`` to avoid importing optional deps at import time."""
    global _scrape_spec
    if _scrape_spec is None:
        from tools.scrape_pdf import scrape_spec

        _scrape_spec = scrape_spec
    return _scrape_spec




def clone_repo(slug: str, dest: Path) -> Path:
    """Clone https://github.com/<slug> into ``dest/<repo_name>``."""
    name = slug.split("/")[-1]
    target = dest / name
    if target.exists():
        logger.info("Reusing existing clone at %s", target)
        return target
    dest.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning %s -> %s", slug, target)
    subprocess.run(
        ["git", "clone", "--depth", "1", f"https://github.com/{slug}.git", str(target)],
        check=True,
    )
    # Unshallow so we can checkout history-tracking branches.
    subprocess.run(
        ["git", "-C", str(target), "fetch", "--unshallow"], check=False
    )
    return target


def _ensure_compile_commands(repo_dir: Path) -> Path | None:
    """Run cmake configure to generate compile_commands.json. Best-effort."""
    build_dir = repo_dir / "build"
    try:
        subprocess.run(
            [
                "cmake",
                "-S",
                str(repo_dir),
                "-B",
                str(build_dir),
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                "-DBUILD_TESTING=ON",
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning(
            "cmake configure failed for %s — stubber will use fallback args: %s",
            repo_dir,
            exc,
        )
        return None
    cc = build_dir / "compile_commands.json"
    return cc if cc.exists() else None


def _maybe_clang_format(repo_dir: Path) -> None:
    """Run clang-format if a .clang-format config is present."""
    if not (repo_dir / ".clang-format").exists():
        return
    clang_format = shutil.which("clang-format")
    if not clang_format:
        return
    c_files = list(repo_dir.rglob("*.c"))
    if not c_files:
        return
    logger.info("Running clang-format on %d .c files", len(c_files))
    try:
        subprocess.run(
            [clang_format, "-i", *[str(p) for p in c_files]],
            check=False,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("clang-format timed out — continuing")


def _restore_test_files(repo_dir: Path, reference_commit: str) -> int:
    """Reset test/ and tests/ trees back to the reference commit.

    The stubber's skip-dir regex already excludes them, but in case any test
    file slipped through (e.g. via odd directory naming), this gives us a
    second line of defence.
    """
    restored = 0
    for sub in ("tests", "test"):
        d = repo_dir / sub
        if not d.exists():
            continue
        try:
            git(repo_dir, "checkout", reference_commit, "--", sub, timeout=30)
            restored += 1
        except subprocess.CalledProcessError:
            pass
    return restored


def _diff_stats(repo_dir: Path, ref: str) -> tuple[int, int]:
    """Return ``(additions, deletions)`` of working-tree changes vs ``ref``."""
    try:
        out = git(repo_dir, "diff", "--numstat", ref)
    except subprocess.CalledProcessError:
        return 0, 0
    adds = dels = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            adds += int(parts[0])
            dels += int(parts[1])
        except ValueError:
            continue
    return adds, dels


def _scrape_and_commit_spec(
    repo_dir: Path,
    repo_name: str,
    spec_url: str,
    specs_dir: Path,
) -> Optional[str]:
    """Scrape a spec PDF and commit it into the current branch.

    Mirrors the prepare_repo_cpp.py / prepare_repo_rust.py spec step:
    1. If ``spec_url`` is provided, scrape it via ``tools.scrape_pdf.scrape_spec``.
    2. If that fails OR ``spec_url`` is empty, fall back to a README-based PDF
       generated by ``tools.scrape_pdf.scrape_readme_spec`` (the same fallback
       Rust and Go pipelines now use).
    3. Compress to ``spec.pdf.bz2`` and commit it so it becomes part of the
       stubbed ``base_commit``. Best-effort: any failure returns ``None`` and
       the caller keeps the existing ``base_commit``.

    Returns the new ``base_commit`` SHA when a spec was committed, else ``None``.
    """
    spec_path: Optional[str] = None

    if spec_url:
        logger.info("  Scraping spec for %s from: %s", repo_name, spec_url)
        try:
            scrape_fn = _get_scrape_func()
            spec_path = scrape_fn(
                base_url=spec_url,
                name=repo_name,
                output_dir=str(specs_dir),
                compress=True,
            )
        except ImportError as exc:
            logger.warning(
                "  Spec scrape unavailable (%s) — will try README fallback", exc,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
            logger.warning("  Spec URL scrape failed for %s: %s", repo_name, exc)

    if not spec_path or not Path(spec_path).exists():
        try:
            from tools.scrape_pdf import scrape_readme_spec as _scrape_readme_spec
            readme_path, _readme_url = _scrape_readme_spec(
                repo_dir, specs_dir, repo_name
            )
            if readme_path and Path(readme_path).exists():
                spec_path = str(readme_path)
                logger.info("  README-based spec used as fallback")
        except ImportError:
            logger.warning(
                "  README spec fallback unavailable (install scrape deps)"
            )
        except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
            logger.warning("  README spec fallback failed: %s", exc)

    if not spec_path or not Path(spec_path).exists():
        logger.warning("  No spec PDF produced for %s — skipping", repo_name)
        return None

    dest = repo_dir / "spec.pdf.bz2"
    shutil.copy2(spec_path, dest)
    git(repo_dir, "add", "spec.pdf.bz2")
    git(
        repo_dir,
        "commit",
        "-m",
        f"commit0: add spec PDF for {repo_name}",
        timeout=30,
    )
    new_commit = git(repo_dir, "rev-parse", "HEAD")
    logger.info("  Spec committed; base_commit now %s", new_commit[:12])
    return new_commit


def _detect_c_standard(repo_dir: Path) -> str:
    """Detect the C language standard from CMakeLists.txt.

    Looks for ``set(CMAKE_C_STANDARD <N>)`` and ``-std=cNN`` patterns.
    Defaults to ``"11"`` (widely supported) when no standard is declared.
    """
    import re

    cmake = repo_dir / "CMakeLists.txt"
    if cmake.exists():
        text = cmake.read_text(errors="replace")
        m = re.search(
            r"set\s*\(\s*CMAKE_C_STANDARD\s+(\d+)\s*\)", text, re.IGNORECASE
        )
        if m:
            return m.group(1)
        m = re.search(r"-std=c(\d+)", text, re.IGNORECASE)
        if m:
            return m.group(1)
    return "11"


def prepare_one(
    slug: str,
    clone_dir: Path,
    fork_org: str = DEFAULT_ORG,
    push: bool = False,
    branch: str = "commit0_all",
    cmake_flags: str = "",
    skip_spec: bool = False,
    spec_url: str = "",
    specs_dir: Path = Path("specs"),
) -> dict | None:
    """Prepare a single C repo. Returns a dataset entry or None if rejected."""
    repo_path = clone_repo(slug, clone_dir)

    ok, reason = validate_c_candidate(repo_path)
    if not ok:
        logger.warning("%s rejected at validation: %s", slug, reason)
        return None

    reference_commit = git(repo_path, "rev-parse", "HEAD")
    logger.info("%s: reference_commit=%s", slug, reference_commit[:12])

    # Configure a fresh build dir for compile_commands.json.
    _ensure_compile_commands(repo_path)

    # Create the commit0 branch.
    try:
        git(repo_path, "branch", "-D", branch, check=False)
    except subprocess.CalledProcessError:
        pass
    git(repo_path, "checkout", "-b", branch)

    # Run cstubber over the repo.
    report = stub_directory(
        repo_path,
        compile_db_path=repo_path / "build" / "compile_commands.json",
        fallback_args=DEFAULT_FALLBACK_ARGS,
        write_header=True,
    )

    if report.functions_stubbed == 0:
        logger.warning(
            "%s: cstubber stubbed 0 functions — refusing to commit empty stub. "
            "function_decl_count=%d, used_fallback=%s",
            slug,
            report.function_decl_count,
            report.used_fallback_args,
        )
        return None

    if (
        report.function_decl_count > 0
        and report.functions_stubbed / report.function_decl_count < 0.30
    ):
        logger.warning(
            "%s: stubbed only %d / %d functions (<30%%) — likely macro-heavy. "
            "Skipping.",
            slug,
            report.functions_stubbed,
            report.function_decl_count,
        )
        return None

    _maybe_clang_format(repo_path)

    restored = _restore_test_files(repo_path, reference_commit)
    if restored:
        logger.info("Restored %d test directories from reference", restored)

    git(repo_path, "add", "-A")
    additions, deletions = _diff_stats(repo_path, reference_commit)
    if additions == 0 and deletions == 0:
        logger.warning("%s: stub produced empty diff — skipping commit", slug)
        return None
    if additions == 0 or deletions == 0:
        logger.warning(
            "%s: one-sided diff (+%d / -%d) — likely something is off; skipping",
            slug,
            additions,
            deletions,
        )
        return None

    git(
        repo_path,
        "commit",
        "-m",
        "commit0: stub function bodies (cstubber, libclang)",
        timeout=30,
    )
    base_commit = git(repo_path, "rev-parse", "HEAD")
    logger.info("%s: base_commit=%s (+%d/-%d)", slug, base_commit[:12], additions, deletions)

    # Scrape a spec PDF and fold it into base_commit (default-on,
    # best-effort, with README fallback when spec_url is missing or fails).
    if not skip_spec:
        new_base = _scrape_and_commit_spec(
            repo_path, slug.split("/")[-1], spec_url, specs_dir
        )
        if new_base:
            base_commit = new_base

    target_repo_slug = f"{fork_org}/{slug.split('/')[-1]}" if fork_org else slug

    if push and fork_org:
        try:
            fork_repo(slug, fork_org)
            push_to_fork(repo_path, target_repo_slug, branch, remote_name="origin")
        except Exception as exc:
            logger.warning("Fork/push failed for %s: %s", slug, exc)

    entry = {
        "instance_id": f"{slug.split('/')[-1]}_c",
        "repo": target_repo_slug,
        "original_repo": slug,
        "base_commit": base_commit,
        "reference_commit": reference_commit,
        "language": "c",
        "src_dir": ".",
        "setup": {
            "build_system": "cmake",
            "c_standard": _detect_c_standard(repo_path),
            "packages": "",
            "cmake_flags": cmake_flags,
            "pre_install": [],
            "specification": spec_url,
            "install": "cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DBUILD_TESTING=ON && cmake --build build -j$(nproc)",
        },
        "test": {
            "framework": "ctest",
            "test_cmd": (
                "ctest --test-dir build --output-on-failure "
                "--output-junit /testbed/test_report.xml"
            ),
        },
        "stub_report": report.to_dict(),
    }
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare C repos for commit0")
    parser.add_argument(
        "candidates_file",
        nargs="?",
        help="JSON file of candidates (output of discover_c.py)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Process a single repo by slug, e.g. DaveGamble/cJSON",
    )
    parser.add_argument(
        "--upstream",
        type=str,
        default=None,
        help="Alias for --repo (kept for cross-language consistency)",
    )
    parser.add_argument(
        "--clone-dir",
        type=Path,
        default=Path("repos_staging"),
        help="Where to clone repos for preparation",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("c_entries.json"),
        help="Where to write the dataset entries JSON",
    )
    parser.add_argument(
        "--fork-org",
        type=str,
        default=DEFAULT_ORG,
        help=f"GitHub org to fork into (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push commit0 branch to the fork (requires --fork-org and gh CLI)",
    )
    parser.add_argument(
        "--branch",
        type=str,
        default="commit0_all",
        help="Branch name for the stubbed commit",
    )
    parser.add_argument(
        "--cmake-flags",
        type=str,
        default="",
        help="Extra CMake flags to record in setup.cmake_flags",
    )
    parser.add_argument(
        "--skip-spec",
        action="store_true",
        help="Skip spec PDF generation (default: scrape via --spec-url with README fallback)",
    )
    parser.add_argument(
        "--spec-url",
        type=str,
        default="",
        help="Documentation URL to scrape into spec.pdf.bz2 (used with --scrape-spec)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=Path("specs"),
        help="Directory to save scraped spec PDFs (default: ./specs)",
    )
    args = parser.parse_args()

    args.repo = args.repo or args.upstream

    setup_git_credentials(dry_run=not args.push)

    candidates: list[str] = []
    if args.repo:
        candidates.append(args.repo)
    if args.candidates_file:
        data = json.loads(Path(args.candidates_file).read_text())
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    candidates.append(item)
                elif isinstance(item, dict):
                    full = item.get("full_name") or item.get("repo")
                    if full:
                        candidates.append(full)
    if not candidates:
        parser.error("Provide --repo or a candidates_file with entries")

    entries: list[dict] = []
    rejected: list[dict] = []
    for slug in candidates:
        try:
            entry = prepare_one(
                slug,
                args.clone_dir,
                fork_org=args.fork_org,
                push=args.push,
                branch=args.branch,
                cmake_flags=args.cmake_flags,
                skip_spec=args.skip_spec,
                spec_url=args.spec_url,
                specs_dir=args.specs_dir,
            )
        except Exception:
            logger.exception("Failed to prepare %s", slug)
            rejected.append({"repo": slug, "reason": "exception"})
            continue
        if entry is None:
            rejected.append({"repo": slug, "reason": "validation_or_empty_stub"})
            continue
        entries.append(entry)

    args.output.write_text(json.dumps(entries, indent=2))
    logger.info("Wrote %d entries to %s", len(entries), args.output)

    if rejected:
        rej_path = args.output.with_name("candidates_c_rejected.json")
        rej_path.write_text(json.dumps(rejected, indent=2))
        logger.info("Wrote %d rejections to %s", len(rejected), rej_path)


if __name__ == "__main__":
    main()
