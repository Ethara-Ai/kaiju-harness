"""Discover candidate Go repos for a commit0 Go dataset.

Searches GitHub for popular Go repos with good test suites.
Filters out repos already in the Go candidate set.

Usage:
    python -m tools.discover_go [--min-stars 5000] [--max-results 200] [--output go_candidates.json]
    GITHUB_TOKEN=ghp_... python -m tools.discover_go
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


EXISTING_GO_REPOS: set[str] = set()

SKIP_DIRS: set[str] = {"vendor", ".git", "testdata", "internal"}

GITHUB_API = "https://api.github.com"


def _gh_request(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:  # nosec B310 - URL is hardcoded https://api.github.com base (see GITHUB_API); no file:// or other-scheme reachable
        return json.loads(resp.read())


def _search_go_repos(
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
                "q": f"language:go stars:>={min_stars} archived:false fork:false",
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
            if full_name in EXISTING_GO_REPOS:
                logger.debug("  Skipping existing: %s", full_name)
                continue

            candidates.append(
                {
                    "full_name": full_name,
                    "stars": repo["stargazers_count"],
                    "description": (repo.get("description") or "")[:200],
                    "default_branch": repo.get("default_branch", "main"),
                    "topics": repo.get("topics", []),
                    "license": (repo.get("license") or {}).get("spdx_id", "Unknown"),
                    "language": "Go",
                    "go_version": None,
                }
            )

        page += 1
        if len(items) < per_page:
            break
        time.sleep(2)

    return candidates[:max_results]


def _check_go_test_files(full_name: str, token: str | None) -> bool:
    url = f"{GITHUB_API}/search/code?{urlencode({'q': f'repo:{full_name} filename:_test.go'})}"
    try:
        data = _gh_request(url, token)
        count = data.get("total_count", 0)
        return count >= 5
    except HTTPError:
        return False


def _get_go_version(full_name: str, branch: str, token: str | None) -> str | None:
    url = f"{GITHUB_API}/repos/{full_name}/contents/go.mod?ref={branch}"
    try:
        data = _gh_request(url, token)
        import base64

        content = base64.b64decode(data["content"]).decode("utf-8")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("go ") and not line.startswith("go."):
                return line.split()[1]
    except (HTTPError, KeyError):
        pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Go repos for commit0")
    parser.add_argument("--min-stars", type=int, default=5000)
    parser.add_argument("--max-results", type=int, default=200)
    parser.add_argument("--output", type=str, default="go_candidates.json")
    parser.add_argument("--check-tests", action="store_true", default=False)
    parser.add_argument("--check-go-version", action="store_true", default=False)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning("No GITHUB_TOKEN set. Rate limits will be strict (60/hr).")

    logger.info("Searching GitHub for Go repos (stars >= %d)...", args.min_stars)
    candidates = _search_go_repos(args.min_stars, args.max_results, token)
    logger.info("Found %d candidates", len(candidates))

    if args.check_tests:
        logger.info("Checking for _test.go files...")
        filtered = []
        for c in candidates:
            if _check_go_test_files(c["full_name"], token):
                filtered.append(c)
            else:
                logger.info("  Skipping %s (insufficient tests)", c["full_name"])
            time.sleep(3)
        candidates = filtered
        logger.info("After test filter: %d candidates", len(candidates))

    if args.check_go_version:
        logger.info("Detecting go.mod versions...")
        for c in candidates:
            c["go_version"] = _get_go_version(
                c["full_name"], c["default_branch"], token
            )
            time.sleep(1)

    output_path = Path(args.output)
    output_path.write_text(json.dumps(candidates, indent=2) + "\n")
    logger.info("Wrote %d candidates to %s", len(candidates), output_path)


if __name__ == "__main__":
    main()
