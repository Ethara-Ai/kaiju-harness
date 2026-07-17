import base64
import json
import logging
import re
import subprocess
from typing import List, Dict
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

MAVEN_SEARCH_URL = "https://search.maven.org/solrsearch/select"

# SSRF / arg-injection defense (parity with discover_{c,go,js}.py, QC-C8-001).
# Java reaches the network via `requests` (Maven Central) and the `gh` CLI
# (GitHub), so instead of the shared _safe_request it validates the Maven host
# up front and validates every owner/repo interpolated into a `gh api` path.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_MAVEN_HOSTS: frozenset[str] = frozenset({"search.maven.org"})
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _validate_host(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Refusing non-http(s) URL scheme: {parsed.scheme!r}")
    if parsed.port is not None:
        raise ValueError(f"Refusing URL with explicit port: {parsed.port!r}")
    hostname = (parsed.hostname or "").lower()
    if hostname not in allowed_hosts:
        raise ValueError(f"Refusing unexpected host: {hostname!r}")


def _owner_repo_from_url(repo_url: str) -> str | None:
    """Extract and validate an ``owner/repo`` from a GitHub URL.

    Returns None (caller falls back to its safe default) if the string is not a
    clean ``owner/repo`` slug, so no attacker-controlled value can be spliced
    into a ``gh api`` path or CLI argument.
    """
    owner_repo = repo_url.replace("https://github.com/", "").strip("/")
    if not _OWNER_REPO_RE.match(owner_repo):
        logger.warning("Refusing gh call for malformed owner/repo: %r", owner_repo)
        return None
    return owner_repo


@dataclass
class JavaRepoCandidate:
    name: str
    url: str
    stars: int
    test_count: int
    build_system: str
    java_version: str
    license: str
    last_commit: str


def search_maven_central(
    query: str,
    rows: int = 20,
) -> List[Dict]:

    params = {
        "q": query,
        "rows": rows,
        "wt": "json",
    }
    _validate_host(MAVEN_SEARCH_URL, _MAVEN_HOSTS)
    resp = requests.get(MAVEN_SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("response", {}).get("docs", [])


def search_github_java(
    min_stars: int = 500,
    min_test_files: int = 50,
    max_results: int = 100,
) -> List[JavaRepoCandidate]:
    query = f"language:Java stars:>={min_stars} archived:false"
    cmd = [
        "gh", "search", "repos",
        query,
        "--json", "name,url,stargazersCount,license,updatedAt",
        "--limit", str(max_results),
        "--sort", "stars",
        "--order", "desc",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
    except FileNotFoundError:
        logger.error("gh CLI not found. Install: https://cli.github.com/")
        return []
    except subprocess.CalledProcessError as e:
        logger.error("gh search failed: %s", e.stderr)
        return []

    repos = json.loads(result.stdout)
    candidates = []
    for repo in repos:
        license_name = ""
        license_info = repo.get("license")
        if isinstance(license_info, dict):
            license_name = license_info.get("key", license_info.get("name", ""))
        elif isinstance(license_info, str):
            license_name = license_info

        repo_url = repo.get("url", "")
        build_system = _detect_build_system_from_gh(repo_url)
        test_count = _estimate_test_count_from_gh(repo_url)

        candidate = JavaRepoCandidate(
            name=repo.get("name", ""),
            url=repo_url,
            stars=repo.get("stargazersCount", 0),
            test_count=test_count,
            build_system=build_system,
            java_version=_detect_java_version_from_gh(repo_url, build_system),
            license=license_name,
            last_commit=repo.get("updatedAt", ""),
        )
        candidates.append(candidate)

    return candidates


def _detect_build_system_from_gh(repo_url: str) -> str:
    if not repo_url:
        return "unknown"

    owner_repo = _owner_repo_from_url(repo_url)
    if owner_repo is None:
        return "unknown"
    for filename, system in [("pom.xml", "maven"), ("build.gradle", "gradle"), ("build.gradle.kts", "gradle")]:
        cmd = ["gh", "api", f"repos/{owner_repo}/contents/{filename}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return system
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
    return "unknown"


def _estimate_test_count_from_gh(repo_url: str) -> int:
    if not repo_url:
        return 0
    owner_repo = _owner_repo_from_url(repo_url)
    if owner_repo is None:
        return 0
    cmd = [
        "gh", "api",
        f"search/code?q=repo:{owner_repo}+filename:Test.java+path:src/test",
        "--jq", ".total_count",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 0


def _detect_java_version_from_gh(repo_url: str, build_system: str) -> str:
    if not repo_url or build_system not in ("maven", "gradle"):
        return ""
    owner_repo = _owner_repo_from_url(repo_url)
    if owner_repo is None:
        return ""
    if build_system == "maven":
        cmd = ["gh", "api", f"repos/{owner_repo}/contents/pom.xml", "--jq", ".content"]
    else:
        cmd = ["gh", "api", f"repos/{owner_repo}/contents/build.gradle", "--jq", ".content"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return ""
        content = base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
        if build_system == "maven":
            match = re.search(r"<maven\.compiler\.source>(\d+)</maven\.compiler\.source>", content)
            if not match:
                match = re.search(r"<release>(\d+)</release>", content)
        else:
            match = re.search(r"sourceCompatibility\s*=\s*['\"]?(\d+)", content)
        if match:
            return match.group(1)
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass
    return ""


def validate_candidate(candidate: JavaRepoCandidate) -> Dict[str, bool]:
    has_tests = candidate.test_count >= 50
    has_build = candidate.build_system in ("maven", "gradle")
    has_jdk = candidate.java_version in ("11", "17", "21") if candidate.java_version else False
    return {
        "has_tests": has_tests,
        "has_build_system": has_build,
        "compatible_jdk": has_jdk,
        "has_license": bool(candidate.license),
        "is_active": True,
    }
