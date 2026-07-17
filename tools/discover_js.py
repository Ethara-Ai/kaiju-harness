"""Discover candidate JavaScript repos for a commit0 JS dataset.

Searches GitHub for popular JS repos with good test suites, with an optional
npm-registry quality enrichment pass (latest version, weekly downloads).

Usage:
    python -m tools.discover_js [--min-stars 5000] [--max-results 200] [--output js_candidates.json]
    GITHUB_TOKEN=ghp_... python -m tools.discover_js
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


EXISTING_JS_REPOS: set[str] = set()

SKIP_REPO_TOPICS: set[str] = {
    "awesome",
    "awesome-list",
    "tutorial",
    "book",
    "cheatsheet",
    "interview-questions",
}

GITHUB_API = "https://api.github.com"
NPM_REGISTRY = "https://registry.npmjs.org"
NPM_DOWNLOADS = "https://api.npmjs.org/downloads"

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_GITHUB_HOSTS: frozenset[str] = frozenset({"api.github.com"})
_NPM_HOSTS: frozenset[str] = frozenset({"registry.npmjs.org", "api.npmjs.org"})


def _safe_request(url: str, headers: dict[str, str], timeout: int = 30) -> bytes:
    # RATIONALE: No TLS certificate pinning. This function only contacts the
    # three well-known public hosts in `_GITHUB_HOSTS | _NPM_HOSTS`, so we rely
    # on the system trust store. Pinning would require shipping and rotating
    # roots across CI/dev environments, which is heavyweight for a discovery
    # tool that handles no secrets beyond a short-lived GitHub token. Accepted
    # risk: MITM via system trust compromise is in scope of the host OS, not
    # this script. (Per REPORT_CODE_REVIEW.md F-013.)
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Refusing non-http(s) URL scheme: {parsed.scheme!r}")
    if parsed.port is not None:
        raise ValueError(f"Refusing URL with explicit port: {parsed.port!r}")
    hostname = (parsed.hostname or "").lower()
    if hostname not in (_GITHUB_HOSTS | _NPM_HOSTS):
        raise ValueError(f"Refusing unexpected host: {hostname!r}")
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:  # nosec B310 - scheme + host both validated via _ALLOWED_SCHEMES and _GITHUB_HOSTS|_NPM_HOSTS allowlists above (lines 56-63); raises ValueError on any non-http(s)/non-allowed-host before this call is reached
        return resp.read()


def _gh_request(url: str, token: str | None = None, retries: int = 5) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(retries):
        try:
            body = _safe_request(url, headers)
            return json.loads(body)
        except HTTPError as e:
            if e.code == 403:
                reset_time = int(e.headers.get("X-RateLimit-Reset", "0"))
                wait = max(0, reset_time - int(time.time())) + 2
                logger.warning(
                    "GitHub rate limited (403); waiting %ds until X-RateLimit-Reset...",
                    wait,
                )
                time.sleep(wait)
            elif e.code == 422:
                logger.error("GitHub API validation error: %s", e.read().decode())
                raise
            else:
                if attempt < retries - 1:
                    time.sleep(2**attempt)
                else:
                    raise
    raise RuntimeError(f"_gh_request: exhausted {retries} retries for {url}")


def _npm_request(url: str) -> dict:
    body = _safe_request(url, headers={"Accept": "application/json"})
    return json.loads(body)


def _search_js_repos(
    min_stars: int,
    max_results: int,
    token: str | None,
) -> list[dict]:
    candidates: list[dict] = []
    page = 1
    per_page = min(100, max_results)

    while len(candidates) < max_results:
        params = urlencode(
            {
                "q": f"language:javascript stars:>={min_stars} archived:false fork:false",
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            }
        )
        url = f"{GITHUB_API}/search/repositories?{params}"
        logger.info("  Fetching page %d ...", page)

        # _gh_request bounds 403/rate-limit handling internally (waits until
        # X-RateLimit-Reset, capped at `retries`, then raises RuntimeError) — the
        # old fixed-60s outer retry loop was dead code, removed for parity with
        # discover_c.py / discover_go.py (QC-C8-001).
        data = _gh_request(url, token)

        items = data.get("items", [])
        if not items:
            break

        for repo in items:
            full_name = repo["full_name"]
            if full_name in EXISTING_JS_REPOS:
                logger.debug("  Skipping existing: %s", full_name)
                continue
            topics = repo.get("topics") or []
            if SKIP_REPO_TOPICS.intersection(topics):
                logger.debug("  Skipping non-library topic: %s", full_name)
                continue

            candidates.append(
                {
                    "full_name": full_name,
                    "stars": repo["stargazers_count"],
                    "description": (repo.get("description") or "")[:200],
                    "default_branch": repo.get("default_branch", "main"),
                    "topics": topics,
                    "license": (repo.get("license") or {}).get("spdx_id", "Unknown"),
                    "language": "JavaScript",
                    "npm_name": None,
                    "npm_weekly_downloads": None,
                }
            )

        page += 1
        if len(items) < per_page:
            break
        time.sleep(2)

    return candidates[:max_results]


def _check_js_test_files(full_name: str, token: str | None) -> bool:
    params = urlencode({"q": f"repo:{full_name} filename:.test.js"})
    url = f"{GITHUB_API}/search/code?{params}"
    try:
        data = _gh_request(url, token)
        return data.get("total_count", 0) >= 3
    except HTTPError:
        return False


def _fetch_package_json(full_name: str, branch: str, token: str | None) -> dict | None:
    url = f"{GITHUB_API}/repos/{full_name}/contents/package.json?ref={branch}"
    try:
        data = _gh_request(url, token)
        import base64

        content = base64.b64decode(data["content"]).decode("utf-8")
        pkg = json.loads(content)
        return pkg if isinstance(pkg, dict) else None
    except (HTTPError, KeyError, json.JSONDecodeError):
        return None


def _enrich_npm(candidate: dict, full_name: str, branch: str, token: str | None) -> None:
    pkg = _fetch_package_json(full_name, branch, token)
    if pkg is None:
        return
    npm_name = pkg.get("name")
    if not isinstance(npm_name, str) or not npm_name:
        return
    candidate["npm_name"] = npm_name
    encoded = npm_name.replace("/", "%2F") if npm_name.startswith("@") else npm_name
    try:
        meta = _npm_request(f"{NPM_REGISTRY}/{encoded}")
        latest = (meta.get("dist-tags") or {}).get("latest")
        if latest:
            candidate["npm_latest_version"] = latest
    except (HTTPError, URLError):
        pass
    try:
        downloads = _npm_request(f"{NPM_DOWNLOADS}/point/last-week/{encoded}")
        candidate["npm_weekly_downloads"] = downloads.get("downloads")
    except (HTTPError, URLError):
        pass


def main() -> None:
    """CLI entry: search GitHub for JS repos and write a candidate list."""
    parser = argparse.ArgumentParser(description="Discover JS repos for commit0")
    parser.add_argument("--min-stars", type=int, default=5000)
    parser.add_argument("--max-results", type=int, default=200)
    parser.add_argument("--output", type=str, default="js_candidates.json")
    parser.add_argument("--check-tests", action="store_true", default=False)
    parser.add_argument("--enrich-npm", action="store_true", default=False)
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.warning("No GITHUB_TOKEN set. Rate limits will be strict (60/hr).")

    logger.info("Searching GitHub for JS repos (stars >= %d)...", args.min_stars)
    candidates = _search_js_repos(args.min_stars, args.max_results, token)
    logger.info("Found %d candidates", len(candidates))

    if args.check_tests:
        logger.info("Checking for .test.js files...")
        filtered = []
        for c in candidates:
            if _check_js_test_files(c["full_name"], token):
                filtered.append(c)
            else:
                logger.info("  Skipping %s (insufficient tests)", c["full_name"])
            time.sleep(3)
        candidates = filtered
        logger.info("After test filter: %d candidates", len(candidates))

    if args.enrich_npm:
        logger.info("Enriching with npm registry metadata...")
        for c in candidates:
            _enrich_npm(c, c["full_name"], c["default_branch"], token)
            time.sleep(1)

    output_path = Path(args.output)
    output_path.write_text(json.dumps(candidates, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %d candidates to %s", len(candidates), output_path)


if __name__ == "__main__":
    main()
