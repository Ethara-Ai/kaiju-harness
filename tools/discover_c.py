"""Discover candidate C repos for a commit0 C dataset.

Searches GitHub for popular C repos that look like libraries (CMake +
permissive licence + tests). Filters out repos already in the C candidate
set.

Usage:
    python -m tools.discover_c [--min-stars 1000] [--max-results 200] \\
        [--output c_candidates.json]
    GITHUB_TOKEN=ghp_... python -m tools.discover_c
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


EXISTING_C_REPOS: set[str] = set()

GITHUB_API = "https://api.github.com"


def _gh_request(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:  # nosec B310 - URL is hardcoded https://api.github.com base (see GITHUB_API); no file:// or other-scheme reachable
        return json.loads(resp.read())


def _search_c_repos(
    min_stars: int,
    max_results: int,
    token: str | None,
) -> list[dict]:
    candidates = []
    page = 1
    per_page = min(100, max_results)

    while len(candidates) < max_results:
        params = urlencode(
            {
                "q": (
                    f"language:C stars:>={min_stars} archived:false fork:false "
                    f"license:mit license:apache-2.0 license:bsd-3-clause "
                    f"license:bsd-2-clause license:isc"
                ),
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            }
        )
        url = f"{GITHUB_API}/search/repositories?{params}"
        logger.info("  Fetching page %d ...", page)

        try:
            data = _gh_request(url, token)
        except HTTPError as e:
            if e.code == 403:
                logger.warning("Rate limited. Waiting 60s...")
                time.sleep(60)
                continue
            raise

        items = data.get("items", [])
        if not items:
            break

        for repo in items:
            full_name = repo["full_name"]
            if full_name in EXISTING_C_REPOS:
                continue

            candidates.append(
                {
                    "full_name": full_name,
                    "stars": repo["stargazers_count"],
                    "description": (repo.get("description") or "")[:200],
                    "default_branch": repo.get("default_branch", "main"),
                    "license": (repo.get("license") or {}).get("spdx_id"),
                    "topics": repo.get("topics", []),
                    "size_kb": repo.get("size", 0),
                }
            )
            if len(candidates) >= max_results:
                break

        if len(items) < per_page:
            break
        page += 1

    return candidates


_LIBRARY_TOPICS = frozenset(
    {
        "library",
        "c-library",
        "single-header",
        "header-only",
        "embedded",
        "json",
        "yaml",
        "toml",
        "parser",
        "hash",
        "crypto",
        "compression",
    }
)


def score_candidate(candidate: dict) -> float:
    """Heuristic score: higher = better library candidate.

    Rewards: small repos, permissive licences, library-ish topics.
    Penalises: huge repos (>30MB), missing licence.
    """
    score = 0.0
    score += min(candidate.get("stars", 0) / 1000.0, 30.0)

    size_kb = candidate.get("size_kb", 0)
    if size_kb < 5000:
        score += 10
    elif size_kb < 30000:
        score += 5
    else:
        score -= 5

    licence = candidate.get("license") or ""
    if licence.upper() in {"MIT", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "APACHE-2.0", "ISC"}:
        score += 8
    elif licence:
        score += 2
    else:
        score -= 10

    topics = set(candidate.get("topics", []))
    score += 3 * len(topics & _LIBRARY_TOPICS)

    desc = candidate.get("description", "").lower()
    if any(kw in desc for kw in ("library", "json", "yaml", "toml", "parser")):
        score += 3
    if any(kw in desc for kw in ("kernel", "driver", "firmware", "bootloader")):
        score -= 8

    return score


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover C library candidates")
    parser.add_argument("--min-stars", type=int, default=1000)
    parser.add_argument("--max-results", type=int, default=200)
    parser.add_argument(
        "--output", type=Path, default=Path("c_candidates.json")
    )
    parser.add_argument(
        "--token",
        type=str,
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub API token (or set GITHUB_TOKEN env var)",
    )
    args = parser.parse_args()

    logger.info(
        "Searching GitHub: language=C, min_stars=%d, max=%d",
        args.min_stars,
        args.max_results,
    )
    candidates = _search_c_repos(args.min_stars, args.max_results, args.token)
    logger.info("Got %d candidates from GitHub", len(candidates))

    for c in candidates:
        c["score"] = round(score_candidate(c), 2)

    candidates.sort(key=lambda c: c["score"], reverse=True)

    args.output.write_text(json.dumps(candidates, indent=2))
    logger.info("Wrote %s", args.output)

    print(f"\nTop 20 C candidates:\n{'=' * 80}")
    for i, c in enumerate(candidates[:20], 1):
        print(
            f"  {i:>3}. {c['full_name']:<40} "
            f"stars={c['stars']:<6} score={c['score']:<6.2f} "
            f"lic={c.get('license') or '?':<14}"
        )


if __name__ == "__main__":
    main()
