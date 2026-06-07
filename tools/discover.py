"""
Discover candidate Python repos for a custom commit0 dataset.

Searches GitHub for large, popular Python repos with good test suites.
Filters out repos already in commit0's existing 54.

Usage:
    python -m tools.discover [--min-stars 5000] [--max-results 200] [--output candidates.json]

    # With GitHub token for higher rate limits (5000/hr vs 60/hr):
    GITHUB_TOKEN=ghp_... python -m tools.discover
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# All 54 original_repo values from wentingzhao/commit0_combined
EXISTING_COMMIT0_REPOS: set[str] = {
    "minitorch/minitorch",
    "wenting-zhao/simpy",
    "scott-griffiths/bitstring",
    "msiemens/tinydb",
    "marshmallow-code/marshmallow",
    "prompt-toolkit/python-prompt-toolkit",
    "scrapy/parsel",
    "jpadilla/pyjwt",
    "networkx/networkx",
    "graphql-python/graphene",
    "tlsfuzzer/tlslite-ng",
    "jquast/wcwidth",
    "chardet/chardet",
    "rthalley/dnspython",
    "mjs/imapclient",
    "pypa/virtualenv",
    "pexpect/pexpect",
    "ethereum/web3.py",
    "python-babel/babel",
    "geopandas/geopandas",
    "jelmer/dulwich",
    "pallets/flask",
    "alecthomas/voluptuous",
    "pallets/jinja",
    "mwaskom/seaborn",
    "psf/requests",
    "scrapy/scrapy",
    "fastapi/fastapi",
    "pallets/click",
    "sybrenstuvel/python-rsa",
    "statsmodels/statsmodels",
    "more-itertools/more-itertools",
    "Zulko/moviepy",
    "laurent-laporte-pro/deprecated",
    "pydantic/pydantic",
    "Delgan/loguru",
    "py-pdf/pypdf",
    "python-attrs/attrs",
    "lk-geimfari/mimesis",
    "cookiecutter/cookiecutter",
    "tornadoweb/tornado",
    "scikit-learn-contrib/imbalanced-learn",
    "wolph/python-progressbar",
    "pylint-dev/pylint",
    "sphinx-doc/sphinx",
    "joblib/joblib",
    "pydata/xarray",
    "tkem/cachetools",
    "paramiko/paramiko",
    "fabric/fabric",
    "fsspec/filesystem_spec",
    "davidhalter/jedi",
    "andialbrecht/sqlparse",
    "wolph/portalocker",
}

# Repos that are known to be poor commit0 candidates (wrappers, ML-heavy, GUI, etc.)
EXCLUDED_REPOS: set[str] = {
    # ML/DL frameworks (require GPU, C extensions)
    "tensorflow/tensorflow",
    "pytorch/pytorch",
    "keras-team/keras",
    "scikit-learn/scikit-learn",
    "huggingface/transformers",
    "Lightning-AI/pytorch-lightning",
    "openai/openai-python",
    # DevOps/infra tools (not libraries)
    "ansible/ansible",
    "saltstack/salt",
    "docker/compose",
    # CLI/GUI apps (not libraries)
    "httpie/cli",
    "ytdl-org/youtube-dl",
    "yt-dlp/yt-dlp",
    "sherlock-project/sherlock",
    "certbot/certbot",
    "locustio/locust",
    "mitmproxy/mitmproxy",
    "psf/black",
    "home-assistant/core",
    "commaai/openpilot",
    "3b1b/manim",
    "chubin/wttr.in",
    # Wrappers around C/Rust (not native Python)
    "python-pillow/Pillow",
    "numpy/numpy",
    "scipy/scipy",
    "pandas-dev/pandas",
    "matplotlib/matplotlib",
    "sympy/sympy",
    # Web frameworks (too large/complex for stub-based benchmark)
    "django/django",
    "pallets/werkzeug",
}


def github_api(
    endpoint: str,
    params: dict | None = None,
    token: str | None = None,
    retries: int = 3,
) -> dict | list:
    """Make a GitHub API request with retry and rate limit handling."""
    base_url = "https://api.github.com"
    url = f"{base_url}/{endpoint.lstrip('/')}"
    if params:
        url += "?" + urlencode(params)

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=30) as resp:  # nosec B310 - URL is hardcoded https://api.github.com base; no file:// or other-scheme reachable
                remaining = resp.headers.get("X-RateLimit-Remaining", "?")
                if remaining != "?" and int(remaining) < 5:
                    reset_time = int(resp.headers.get("X-RateLimit-Reset", "0"))
                    wait = max(0, reset_time - int(time.time())) + 1
                    logger.warning(
                        "Rate limit nearly exhausted (%s remaining). Waiting %ds...",
                        remaining,
                        wait,
                    )
                    time.sleep(wait)
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 403:
                # Rate limited
                reset_time = int(e.headers.get("X-RateLimit-Reset", "0"))
                wait = max(0, reset_time - int(time.time())) + 2
                logger.warning("Rate limited. Waiting %ds...", wait)
                time.sleep(wait)
            elif e.code == 422:
                logger.error("GitHub API validation error: %s", e.read().decode())
                raise
            else:
                if attempt < retries - 1:
                    time.sleep(2**attempt)
                else:
                    raise
        except Exception:
            logger.debug(
                "GitHub API request failed (attempt %d/%d)",
                attempt + 1,
                retries,
                exc_info=True,
            )
            if attempt < retries - 1:
                time.sleep(2**attempt)
            else:
                raise

    msg = f"Failed after {retries} retries: {url}"
    raise RuntimeError(msg)


def search_python_repos(
    min_stars: int = 5000,
    max_results: int = 300,
    token: str | None = None,
) -> list[dict]:
    """Search GitHub for Python repos with >min_stars stars."""
    repos: list[dict] = []
    per_page = 100
    pages = (max_results + per_page - 1) // per_page

    # GitHub search returns max 1000 results. Split into star ranges if needed.
    star_ranges = _compute_star_ranges(min_stars, max_results)

    for star_min, star_max in star_ranges:
        if star_max:
            query = f"language:python stars:{star_min}..{star_max}"
        else:
            query = f"language:python stars:>={star_min}"

        for page in range(1, pages + 1):
            if len(repos) >= max_results:
                break

            logger.info(
                "Searching: %s (page %d, found %d so far)",
                query,
                page,
                len(repos),
            )

            data = github_api(
                "/search/repositories",
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": per_page,
                    "page": page,
                },
                token=token,
            )

            items = data.get("items", [])
            if not items:
                break

            for item in items:
                full_name = item["full_name"]

                # Skip existing commit0 repos and excluded repos
                if full_name in EXISTING_COMMIT0_REPOS:
                    logger.debug("Skipping (existing commit0): %s", full_name)
                    continue
                if full_name in EXCLUDED_REPOS:
                    logger.debug("Skipping (excluded): %s", full_name)
                    continue

                # Skip forks, archived, disabled repos
                if item.get("fork") or item.get("archived") or item.get("disabled"):
                    continue

                repos.append(
                    {
                        "full_name": full_name,
                        "name": item["name"],
                        "owner": item["owner"]["login"],
                        "stars": item["stargazers_count"],
                        "forks": item["forks_count"],
                        "size_kb": item["size"],
                        "description": item.get("description", ""),
                        "homepage": item.get("homepage", ""),
                        "topics": item.get("topics", []),
                        "license": (item.get("license") or {}).get("spdx_id", ""),
                        "default_branch": item.get("default_branch", "main"),
                        "open_issues": item.get("open_issues_count", 0),
                        "created_at": item.get("created_at", ""),
                        "updated_at": item.get("updated_at", ""),
                        "html_url": item.get("html_url", ""),
                    }
                )

            # Respect rate limits
            time.sleep(1)

    # Deduplicate by full_name
    seen = set()
    unique: list[dict] = []
    for r in repos:
        if r["full_name"] not in seen:
            seen.add(r["full_name"])
            unique.append(r)

    return sorted(unique, key=lambda x: x["stars"], reverse=True)[:max_results]


def _compute_star_ranges(
    min_stars: int, max_results: int
) -> list[tuple[int, int | None]]:
    """Split star range into chunks to work around GitHub's 1000-result limit."""
    if max_results <= 1000:
        return [(min_stars, None)]

    # Use logarithmic ranges for better distribution
    ranges: list[tuple[int, int | None]] = []
    boundaries = [min_stars, 10000, 20000, 50000, 100000]
    for i in range(len(boundaries) - 1):
        ranges.append((boundaries[i], boundaries[i + 1] - 1))
    ranges.append((boundaries[-1], None))
    return list(reversed(ranges))  # Start from highest stars


def get_language_breakdown(full_name: str, token: str | None = None) -> dict[str, int]:
    """Get language byte counts for a repo."""
    return github_api(f"/repos/{full_name}/languages", token=token)


def get_latest_release_tag(full_name: str, token: str | None = None) -> str | None:
    """Get the latest release tag name, falling back to the most recent tag."""
    try:
        release = github_api(f"/repos/{full_name}/releases/latest", token=token)
        return release.get("tag_name")
    except Exception:
        logger.debug("No release found for %s, trying tags", full_name)
    try:
        tags = github_api(f"/repos/{full_name}/tags?per_page=1", token=token)
        if tags and isinstance(tags, list) and len(tags) > 0:
            return tags[0].get("name")
    except Exception:
        logger.debug("No tags found for %s", full_name)
    return None


def compute_python_percentage(languages: dict[str, int]) -> float:
    """Compute Python % of total code bytes."""
    total = sum(languages.values())
    if total == 0:
        return 0.0
    return languages.get("Python", 0) / total * 100


def check_has_pytest(
    full_name: str,
    default_branch: str = "main",
    token: str | None = None,
) -> bool:
    """Check if repo likely uses pytest by searching for it in config files."""
    # Check common dependency files for "pytest"
    files_to_check = [
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "requirements-tests.txt",
        "tox.ini",
    ]

    for filename in files_to_check:
        try:
            data = github_api(
                f"/repos/{full_name}/contents/{filename}",
                params={"ref": default_branch},
                token=token,
            )
            if data.get("encoding") == "base64":
                import base64

                content = base64.b64decode(data["content"]).decode(errors="replace")
                if "pytest" in content.lower():
                    return True
        except Exception:
            logger.debug("Failed to check %s/%s for pytest", full_name, filename)
            continue

    return False


def enrich_candidates(
    repos: list[dict],
    token: str | None = None,
    check_pytest: bool = True,
    min_python_pct: float = 80.0,
) -> list[dict]:
    """Enrich repos with language breakdown and pytest detection."""
    enriched: list[dict] = []

    for i, repo in enumerate(repos):
        full_name = repo["full_name"]
        logger.info(
            "[%d/%d] Enriching %s (%d stars)...",
            i + 1,
            len(repos),
            full_name,
            repo["stars"],
        )

        # Get language breakdown
        try:
            languages = get_language_breakdown(full_name, token=token)
            python_pct = compute_python_percentage(languages)
            repo["languages"] = languages
            repo["python_pct"] = round(python_pct, 1)
        except Exception as e:
            logger.warning("Failed to get languages for %s: %s", full_name, e)
            repo["languages"] = {}
            repo["python_pct"] = 0.0

        # Filter by Python percentage
        if repo["python_pct"] < min_python_pct:
            logger.info(
                "  Skipping %s: %.1f%% Python (need %.0f%%)",
                full_name,
                repo["python_pct"],
                min_python_pct,
            )
            continue

        # Check for pytest
        if check_pytest:
            try:
                has_pytest = check_has_pytest(
                    full_name, repo.get("default_branch", "main"), token=token
                )
                repo["has_pytest"] = has_pytest
            except Exception:
                logger.debug("Failed to check pytest for %s", full_name)
                repo["has_pytest"] = False

            if not has_pytest:
                logger.info("  Skipping %s: no pytest found", full_name)
                continue

        # Check for documentation (homepage or docs topic)
        has_docs = bool(repo.get("homepage")) or any(
            t in repo.get("topics", []) for t in ["documentation", "docs"]
        )
        repo["has_docs"] = has_docs

        # Get latest release tag for commit pinning
        try:
            release_tag = get_latest_release_tag(full_name, token=token)
            repo["release_tag"] = release_tag
        except Exception:
            logger.debug("Failed to get release tag for %s", full_name)
            repo["release_tag"] = None

        logger.info(
            "  CANDIDATE: %s — %.1f%% Python, %d stars, pytest=%s, docs=%s",
            full_name,
            repo["python_pct"],
            repo["stars"],
            repo.get("has_pytest"),
            has_docs,
        )
        enriched.append(repo)

        # Rate limit courtesy
        time.sleep(0.5)

    return enriched


def print_summary(candidates: list[dict]) -> None:
    """Print a human-readable summary of candidates."""
    print(f"\n{'=' * 80}")
    print(f"CANDIDATES: {len(candidates)} repos found")
    print(f"{'=' * 80}\n")

    # Table header
    print(
        f"{'#':>3}  {'Repository':<45} {'Stars':>7} {'Python%':>8} "
        f"{'Size(MB)':>9} {'Pytest':>6} {'Docs':>5}"
    )
    print("-" * 90)

    for i, r in enumerate(candidates, 1):
        size_mb = r.get("size_kb", 0) / 1024
        py_pct = r.get("python_pct")
        py_str = f"{py_pct:>7.1f}%" if py_pct is not None else "     N/A"
        print(
            f"{i:>3}  {r['full_name']:<45} {r['stars']:>7,} {py_str} "
            f"{size_mb:>8.1f}  {'yes' if r.get('has_pytest') else 'no':>6} "
            f"{'yes' if r.get('has_docs') else 'no':>5}"
        )

    print(f"\n{'=' * 80}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover candidate Python repos for commit0 dataset"
    )
    parser.add_argument(
        "--min-stars",
        type=int,
        default=5000,
        help="Minimum GitHub stars (default: 5000)",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=200,
        help="Max repos to search (before filtering) (default: 200)",
    )
    parser.add_argument(
        "--min-python-pct",
        type=float,
        default=80.0,
        help="Minimum Python code percentage (default: 80.0)",
    )
    parser.add_argument(
        "--skip-pytest-check",
        action="store_true",
        help="Skip checking for pytest in dependencies",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="candidates.json",
        help="Output JSON file (default: candidates.json)",
    )
    parser.add_argument(
        "--search-only",
        action="store_true",
        help="Only search (skip enrichment/filtering)",
    )

    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")

    if not token:
        logger.warning(
            "No GITHUB_TOKEN set. Rate limited to 60 requests/hour. "
            "Set GITHUB_TOKEN for 5000/hour."
        )

    # Phase 1: Search
    logger.info("Searching GitHub for Python repos with >%d stars...", args.min_stars)
    repos = search_python_repos(
        min_stars=args.min_stars,
        max_results=args.max_results,
        token=token,
    )
    logger.info(
        "Found %d repos (after excluding existing commit0 + blocklist)", len(repos)
    )

    if args.search_only:
        output_path = Path(args.output)
        output_path.write_text(json.dumps(repos, indent=2), encoding="utf-8")
        print(f"Saved {len(repos)} repos to {output_path}")
        print_summary(repos)
        return

    # Phase 2: Enrich with language breakdown + pytest detection
    logger.info("Enriching %d repos with language data + pytest check...", len(repos))
    candidates = enrich_candidates(
        repos,
        token=token,
        check_pytest=not args.skip_pytest_check,
        min_python_pct=args.min_python_pct,
    )

    # Phase 3: Output
    output_path = Path(args.output)
    output_path.write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    logger.info("Saved %d candidates to %s", len(candidates), output_path)

    print_summary(candidates)


if __name__ == "__main__":
    main()
