"""Scrape library documentation websites into PDF specifications.

For each library, crawls its documentation website using a headless browser,
generates a PDF per page, removes blank pages, merges into a single PDF,
and optionally bz2-compresses the result.

Usage:
    python -m tools.scrape_pdf --url https://docs.python-requests.org/ --name requests
    python -m tools.scrape_pdf --input validated.json --output-dir ./specs
    python -m tools.scrape_pdf --url https://rich.readthedocs.io/ --name rich --compress

Requires:
    pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests
    playwright install chromium
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import hashlib
import re
import shutil
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    import fitz
    import requests as requests_lib
    from bs4 import BeautifulSoup
    from PyPDF2 import PdfMerger, PdfReader

try:
    import fitz  # type: ignore[no-redef]
    import requests as requests_lib  # type: ignore[no-redef]
    from bs4 import BeautifulSoup  # type: ignore[no-redef]
    from PyPDF2 import PdfMerger, PdfReader  # type: ignore[no-redef]
    from playwright.sync_api import sync_playwright  # type: ignore[no-redef]

    _MISSING_DEPS = False
    _MISSING_DEP_MSG = ""
except ImportError as _e:
    _MISSING_DEPS = True
    _MISSING_DEP_MSG = f"scrape_pdf requires: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests && playwright install chromium ({_e})"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SKIP_URL_PATTERNS: dict[str, list[str]] = {
    "pydantic": ["changelog", "people", "integrations", "migration", "why"],
    "fastapi": ["changelog", "people"],
    "seaborn": [".png"],
}

CAPTCHA_MARKERS = [
    "This website uses a security service to protect against malicious bots",
    "This page is displayed while the website verifies you are not a bot",
    "Checking if the site connection is secure",
    "Enable JavaScript and cookies to continue",
    "Verify you are human",
    "Please verify you are a human",
]

SOFT_404_MARKERS = [
    "the page you requested was not found",
    "this page doesn't exist",
    "nothing to see here",
    "the project you requested does not exist",
    "the page you are looking for could not be found",
    "this page could not be found",
    "we couldn't find the page",
    "the requested page was not found",
]

SOFT_404_FIRST_LINE_EXACT = frozenset(
    [
        "404",
        "page not found",
        "not found",
        "project not found",
        "404 not found",
        "404 error",
        "404 page not found",
    ]
)

_SOFT_404_TITLE_RE = re.compile(
    r"<title[^>]*>\s*"
    r"(404|page\s+not\s+found|project\s+not\s+found|not\s+found)"
    r"\s*(?:[|\-\u2014:]|</title>)",
    re.IGNORECASE,
)
_SOFT_404_H1_RE = re.compile(
    r"<h1[^>]*>\s*"
    r"(404|page\s+not\s+found|project\s+not\s+found|not\s+found)"
    r"\s*</h1>",
    re.IGNORECASE,
)

FASTAPI_NON_ENGLISH_PREFIXES = frozenset(
    [
        "az",
        "bn",
        "de",
        "es",
        "fa",
        "fr",
        "he",
        "hu",
        "id",
        "it",
        "ja",
        "ko",
        "pl",
        "pt",
        "ru",
        "tr",
        "uk",
        "ur",
        "vi",
        "yo",
        "zh",
        "zh-hant",
        "em",
    ]
)


def _is_page_blank(page: Any) -> bool:
    text = page.get_text("text")
    return not text.strip()


def _is_captcha_page(page: Any) -> bool:
    """Check if a PDF page contains bot-verification / CAPTCHA content."""
    text = page.get_text("text")
    text_lower = text.lower()
    return any(marker.lower() in text_lower for marker in CAPTCHA_MARKERS)


def _is_soft_404_page(page: Any) -> bool:
    """Check if a PDF page contains soft-404 content (short page with 404 markers)."""
    text = page.get_text("text")
    text_lower = text.lower().strip()
    if len(text_lower) > 500:
        return False
    if any(marker in text_lower for marker in SOFT_404_MARKERS):
        return True
    first_line = text_lower.split("\n")[0].strip().rstrip(".!:;, ")
    return first_line in SOFT_404_FIRST_LINE_EXACT


def _is_soft_404_content(html: str) -> bool:
    """Check if raw HTML content indicates a soft-404 (HTTP 200 with not-found body)."""
    if _SOFT_404_TITLE_RE.search(html):
        return True
    if _SOFT_404_H1_RE.search(html):
        return True
    return False


_CLOUDFLARE_MARKERS = (
    "cdn-cgi/challenge-platform",
    "cf-browser-verification",
    "cf_chl_opt",
    "Checking your browser",
    "Attention Required! | Cloudflare",
    "Just a moment...",
    "_cf_chl_",
)


def _is_cloudflare_challenge(html: str) -> bool:
    return any(marker in html for marker in _CLOUDFLARE_MARKERS)


def _remove_blank_pages(pdf_path: str) -> None:
    document = fitz.open(pdf_path)

    output_document = fitz.open()
    removed_captcha = 0
    removed_soft_404 = 0
    try:
        for i in range(document.page_count):
            page = document.load_page(i)
            if _is_page_blank(page):
                continue
            if _is_captcha_page(page):
                removed_captcha += 1
                continue
            if _is_soft_404_page(page):
                removed_soft_404 += 1
                continue
            output_document.insert_pdf(document, from_page=i, to_page=i)

        if removed_captcha:
            logger.info(
                "  Removed %d captcha/bot-check page(s) from %s", removed_captcha, pdf_path
            )
        if removed_soft_404:
            logger.info("  Removed %d soft-404 page(s) from %s", removed_soft_404, pdf_path)

        document.close()
        output_document.save(pdf_path)
    finally:
        output_document.close()
        if not document.is_closed:
            document.close()


def _clean_pdf_directory(docs: list[str]) -> None:
    for doc in docs:
        if os.path.exists(doc):
            _remove_blank_pages(doc)


def _is_valid_link(link: str, base_url: str) -> str | None:
    parsed_url = urlparse(link)
    if parsed_url.fragment:
        return None
    if not parsed_url.scheme:
        return urljoin(base_url, link)
    if parsed_url.netloc == urlparse(base_url).netloc:
        return link
    return None


# Generic URL path segments that indicate auth/login pages (never useful for docs).
_AUTH_PATH_SEGMENTS = frozenset(
    [
        "login",
        "logout",
        "signin",
        "signout",
        "sign-in",
        "sign-out",
        "signup",
        "sign-up",
        "register",
        "auth",
        "oauth",
        "sso",
        "callback",
        "reset-password",
        "forgot-password",
        "verify-email",
    ]
)


def _should_skip_url(current_url: str, base_url: str) -> bool:
    # Per-site pattern filtering
    for site_key, patterns in SKIP_URL_PATTERNS.items():
        if site_key in base_url:
            if any(p in current_url for p in patterns):
                return True

    if "fastapi" in base_url:
        stripped = current_url.replace("https://", "")
        parts = [x for x in stripped.split("/") if x]
        if len(parts) > 1 and parts[1] in FASTAPI_NON_ENGLISH_PREFIXES:
            return True

    # Generic: skip auth/login pages on ANY site
    parsed_path = urlparse(current_url).path.lower().strip("/")
    path_segments = parsed_path.split("/")
    if any(seg in _AUTH_PATH_SEGMENTS for seg in path_segments):
        logger.debug("  Skipping auth/login URL: %s", current_url)
        return True

    # Skip URLs with login-related query parameters (e.g., ?redirect_uri=...)
    query = urlparse(current_url).query.lower()
    if "redirect_uri=" in query or "return_to=" in query or "next=" in query:
        logger.debug("  Skipping redirect URL: %s", current_url)
        return True

    return False


def _generate_pdf(page: Any, url: str, output_dir: str) -> str:
    pdf_path = ""
    try:
        try:
            response = page.goto(url, wait_until="networkidle", timeout=_PLAYWRIGHT_NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            logger.debug(
                "  networkidle timeout for %s, retrying with domcontentloaded", url
            )
            response = page.goto(url, wait_until="domcontentloaded", timeout=_PLAYWRIGHT_DOMCONTENT_TIMEOUT_MS)

        if response and response.status >= 400:
            logger.debug(
                "  HTTP %d generating PDF for %s, skipping", response.status, url
            )
            return pdf_path

        _url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        out_name = f"{urlparse(url).path.replace('/', '_').strip('_')}_{_url_hash}.pdf"
        if out_name == f"_{_url_hash}.pdf":
            out_name = f"base_{_url_hash}.pdf"
        pdf_path = os.path.join(output_dir, out_name)

        page.pdf(
            path=pdf_path,
            print_background=True,
            format="A4",
            margin={"top": "0px", "bottom": "0px", "left": "0px", "right": "0px"},
        )
        logger.debug("  Saved PDF: %s", pdf_path)
    except Exception as e:
        logger.warning("  Error creating PDF for %s: %s", url, e)
    return pdf_path


# F1/F5 audit: shared transient-retry policy. `_is_transient_scrape_error` runs
# BEFORE any README fallback so a single-attempt HTTP 429 / 5xx / connect-timeout
# on a large batch doesn't silently degrade the spec (measured ~30-40% degradation
# at 1-2% transient rate). Parse errors (PDF corruption, HTML decode, exhausted
# generator) are NOT retryable and short-circuit to fallback immediately. Tunable
# via env for old-repo backends with slow docs mirrors.
_SCRAPE_MAX_RETRIES = int(os.environ.get("KAIJU_SCRAPE_MAX_RETRIES", "3"))
_SCRAPE_INITIAL_BACKOFF_SEC = float(os.environ.get("KAIJU_SCRAPE_INITIAL_BACKOFF_SEC", "2.0"))
_SCRAPE_MAX_BACKOFF_SEC = float(os.environ.get("KAIJU_SCRAPE_MAX_BACKOFF_SEC", "30.0"))
_SCRAPE_TRANSIENT_HTTP_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 528, 529})
# Substrings that identify TRANSIENT network conditions in exception messages.
# Kept narrow so ordinary parse-error text ("invalid", "malformed", etc.) does not
# false-positive into an infinite retry loop.
_SCRAPE_TRANSIENT_MSG_MARKERS: tuple[str, ...] = (
    "timeout", "timed out", "timeouterror",
    "connection reset", "connection refused", "connection aborted",
    "remote end closed", "read timed out", "dial tcp",
    "temporary failure in name resolution", "getaddrinfo",
    "maxretryerror", "newconnectionerror", "connecterror",
    "broken pipe", "eof occurred", "ssleoferror",
    "cloudflare", "rate limit", "too many requests",
)
# Substrings that identify NON-transient parse/format errors. Explicit list so
# short-circuiting is a positive assertion, not a fallthrough.
_SCRAPE_PARSE_ERR_MARKERS: tuple[str, ...] = (
    "invalid pdf", "pdf parse", "pdfreaderror", "pdfreadwarning", "pdfmergererror",
    "cannot read", "invalid document", "corrupt", "malformed",
    "encoding", "decode", "decoding", "unicodeerror",
    "invalid start byte", "invalid continuation byte",
    "empty file", "not a pdf",
)


def _is_transient_scrape_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient network/upstream condition worth retrying.

    Order of checks:
      1. HTTP status codes on requests exceptions (positive assertion).
      2. Playwright TimeoutError class name (positive assertion).
      3. Parse-error markers in message (positive REJECT — never retry these).
      4. Transient markers in message (positive assertion).
      5. Default False."""
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int) and status_code in _SCRAPE_TRANSIENT_HTTP_CODES:
        return True
    cls_name = type(exc).__name__.lower()
    if "timeout" in cls_name and "parse" not in cls_name:
        return True
    msg = str(exc).lower()
    if any(m in msg for m in _SCRAPE_PARSE_ERR_MARKERS):
        return False
    return any(m in msg for m in _SCRAPE_TRANSIENT_MSG_MARKERS)


def _scrape_backoff_delay(attempt: int) -> float:
    """Exponential backoff with cap. attempt is 0-indexed (attempt 0 = first retry)."""
    delay = _SCRAPE_INITIAL_BACKOFF_SEC * (2 ** attempt)
    return min(delay, _SCRAPE_MAX_BACKOFF_SEC)


# F4 audit: minimum-content threshold for merged specs. Partial crawls (a
# handful of pages out of the site's real total) used to sail through as long
# as ANY page was scraped. Reject specs with fewer than
# `KAIJU_SCRAPE_MIN_PAGES` pages so an operator investigates before a broken
# spec propagates into a dataset entry. Kept modest (10) to avoid rejecting
# genuinely-small libs; tune via env for stricter batches.
_SCRAPE_MIN_PAGES = int(os.environ.get("KAIJU_SCRAPE_MIN_PAGES", "10"))


# F8 audit: Playwright page.goto() timeouts. Previously hardcoded 30000/15000ms
# in _generate_pdf; slow-mirror docs sites (older docs.rs, JS libs with heavy
# first-paint) can legitimately exceed 30s on first request. Bump via env when
# a batch shows systematic goto timeouts.
_PLAYWRIGHT_NETWORKIDLE_TIMEOUT_MS = int(os.environ.get("KAIJU_SCRAPE_PLAYWRIGHT_NETWORKIDLE_MS", "30000"))
_PLAYWRIGHT_DOMCONTENT_TIMEOUT_MS = int(os.environ.get("KAIJU_SCRAPE_PLAYWRIGHT_DOMCONTENT_MS", "15000"))

# F7 audit: minimum decompressed size for compressed specs. bz2.open(...,"wb")
# does NOT verify the round-trip; a truncated or corrupt-on-write blob compresses
# without error but fails at agent read time with an opaque bz2.OSError. We
# re-open the compressed file and stream-read it fully to catch this at prep
# time. Threshold intentionally modest (5KB) — anything smaller is almost
# certainly a failed compression artifact, not a legitimate spec.
_MIN_DECOMPRESSED_SPEC_BYTES = int(os.environ.get("KAIJU_SPEC_MIN_DECOMPRESSED_BYTES", "5120"))


# Issue-2 audit: silent-failure accumulation. When spec_source becomes "none"
# (crawl failed AND README fallback failed), current behavior logs WARNING and
# continues — lets an entire batch build a dataset with no-spec entries. With
# KAIJU_STRICT_SPEC=1, elevate the WARNING into a hard RuntimeError so operators
# discover the missing-doc regression at prep time (not at agent-run time).
# Default off for backward compatibility.
def enforce_strict_spec_mode(spec_source: str, repo_name: str) -> None:
    """Raise RuntimeError when strict-spec mode is on and spec is missing.

    Call from each prepare_repo_*.py AFTER the scrape+README fallback chain
    finishes. Non-strict (default) logs a WARNING and returns — preserves the
    current forgiving behavior. Strict raises so the batch stops before
    writing a degraded dataset entry.
    """
    if spec_source not in ("none", ""):
        return
    strict = os.environ.get("KAIJU_STRICT_SPEC", "").lower() in ("1", "true", "yes")
    if strict:
        raise RuntimeError(
            f"Spec scraping failed for {repo_name} (spec_source={spec_source!r}) "
            f"and KAIJU_STRICT_SPEC is enabled. Unset KAIJU_STRICT_SPEC to continue "
            f"with the README/no-spec fallback (dataset quality will degrade)."
        )
    logger.warning(
        "Spec missing for %s (spec_source=%s). Enable KAIJU_STRICT_SPEC=1 to fail-fast "
        "instead of silently proceeding with a no-spec dataset entry.",
        repo_name, spec_source,
    )


def _crawl_website(
    browser: Any, base_url: str, output_dir: str, max_pages: int = 500
) -> list[str]:
    page = browser.new_page()
    visited: set[str] = set()
    to_visit = deque([base_url])
    queued: set[str] = {base_url}
    sequence: list[str] = []
    pages_scraped = 0

    while to_visit and pages_scraped < max_pages:
        current_url = to_visit.popleft()

        if _should_skip_url(current_url, base_url):
            continue

        if current_url in visited:
            continue

        logger.info("  Crawling: %s", current_url)
        visited.add(current_url)

        _attempt = 0
        _fetched = False
        while _attempt <= _SCRAPE_MAX_RETRIES:
            try:
                response = page.goto(
                    current_url, wait_until="domcontentloaded", timeout=30000
                )
                if response and response.status >= 400:
                    if response.status in _SCRAPE_TRANSIENT_HTTP_CODES and _attempt < _SCRAPE_MAX_RETRIES:
                        _delay = _scrape_backoff_delay(_attempt)
                        logger.info(
                            "  HTTP %d on %s (attempt %d/%d) — retrying in %.1fs",
                            response.status, current_url, _attempt + 1, _SCRAPE_MAX_RETRIES + 1, _delay,
                        )
                        time.sleep(_delay)
                        _attempt += 1
                        continue
                    logger.debug("  HTTP %d: %s", response.status, current_url)
                    break

                content = page.content()
                _fetched = True
                break
            except Exception as e:
                if _is_transient_scrape_error(e) and _attempt < _SCRAPE_MAX_RETRIES:
                    _delay = _scrape_backoff_delay(_attempt)
                    logger.info(
                        "  Transient error on %s (attempt %d/%d): %s — retrying in %.1fs",
                        current_url, _attempt + 1, _SCRAPE_MAX_RETRIES + 1, e, _delay,
                    )
                    time.sleep(_delay)
                    _attempt += 1
                    continue
                logger.warning("  Error crawling %s (non-transient or retries exhausted): %s", current_url, e)
                break
        if not _fetched:
            continue

        try:
            if _is_cloudflare_challenge(content):
                logger.warning("  Cloudflare challenge detected, aborting crawl: %s", current_url)
                break

            if _is_soft_404_content(content):
                logger.info("  Soft-404 detected, skipping: %s", current_url)
                continue

            soup = BeautifulSoup(content, "html.parser")

            for link in soup.find_all("a", href=True):
                full_url = _is_valid_link(link["href"], base_url)
                if (
                    full_url
                    and full_url not in visited
                    and full_url not in queued
                    and (full_url == base_url or full_url.startswith(base_url.rstrip("/") + "/") or full_url.startswith(base_url.rstrip("/") + "@"))
                ):
                    to_visit.append(full_url)
                    queued.add(full_url)

            pdf = _generate_pdf(page, current_url, output_dir)
            if pdf:
                sequence.append(pdf)
            pages_scraped += 1
        except Exception as e:
            logger.warning("  Parse/render error on %s (non-transient): %s", current_url, e)

    page.close()
    return sequence


def _merge_pdfs(docs: list[str], output_filename: str) -> None:
    merger = PdfMerger()
    try:
        for pdf in docs:
            if os.path.exists(pdf):
                try:
                    merger.append(pdf)
                except Exception as e:
                    logger.warning("  Skipping corrupt PDF %s: %s", pdf, e)
        merger.write(output_filename)
    finally:
        merger.close()


def _bz2_integrity_check(path: str) -> None:
    """Re-open compressed file, stream-decompress, verify size threshold.

    Raises RuntimeError if the file cannot be decompressed or decompresses to
    less than KAIJU_SPEC_MIN_DECOMPRESSED_BYTES bytes — catches corrupt or
    empty-crawl artifacts at prep time so they never ship in a dataset.
    Chunk-reads to avoid loading giant PDFs into memory."""
    try:
        _size = 0
        with bz2.open(path, "rb") as f_check:
            while True:
                _chunk = f_check.read(1 << 16)
                if not _chunk:
                    break
                _size += len(_chunk)
    except OSError as e:
        raise RuntimeError(
            f"bz2 integrity check failed for {path}: decompression raised {e!r}. "
            f"File is corrupt — remove it and re-scrape."
        ) from e
    if _size < _MIN_DECOMPRESSED_SPEC_BYTES:
        raise RuntimeError(
            f"bz2 integrity check failed for {path}: decompresses to {_size} bytes "
            f"(threshold={_MIN_DECOMPRESSED_SPEC_BYTES}, override via "
            f"KAIJU_SPEC_MIN_DECOMPRESSED_BYTES). Likely a failed compression run "
            f"or an empty-crawl artifact."
        )


def _compress_bz2(input_path: str, output_path: str) -> None:
    with open(input_path, "rb") as f_in:
        with bz2.open(output_path, "wb") as f_out:
            f_out.writelines(f_in)
    _bz2_integrity_check(output_path)


def scrape_spec(
    base_url: str,
    name: str,
    output_dir: str = "specs",
    compress: bool = True,
) -> str | None:
    if _MISSING_DEPS:
        raise ImportError(_MISSING_DEP_MSG)

    blocked = {"github.com", "gitlab.com", "bitbucket.org", "pypi.org", "wikipedia.org", "raw.githubusercontent.com", "go.googlesource.com"}
    domain = urlparse(base_url).netloc.lower()
    if any(domain == b or domain.endswith("." + b) for b in blocked):
        logger.warning("  Blocked domain %s — skipping spec scrape for %s", domain, name)
        return None

    os.makedirs(output_dir, exist_ok=True)
    pages_dir = os.path.join(output_dir, f"{name}_{os.getpid()}_pages")
    final_pdf = os.path.join(output_dir, f"{name}.pdf")

    url_parts = [x for x in base_url.split("/") if x]
    if url_parts and url_parts[-1] == "pdf":
        logger.info("  Direct PDF download: %s", base_url)
        _attempt = 0
        _downloaded = False
        while _attempt <= _SCRAPE_MAX_RETRIES:
            try:
                response = requests_lib.get(base_url, timeout=60)
                response.raise_for_status()
                with open(final_pdf, "wb") as f:
                    f.write(response.content)
                _downloaded = True
                break
            except Exception as e:
                if _is_transient_scrape_error(e) and _attempt < _SCRAPE_MAX_RETRIES:
                    _delay = _scrape_backoff_delay(_attempt)
                    logger.info(
                        "  Transient error downloading %s (attempt %d/%d): %s — retrying in %.1fs",
                        base_url, _attempt + 1, _SCRAPE_MAX_RETRIES + 1, e, _delay,
                    )
                    time.sleep(_delay)
                    _attempt += 1
                    continue
                logger.error("  Failed to download PDF (non-transient or retries exhausted): %s", e)
                return None
        if not _downloaded:
            return None
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                os.makedirs(pages_dir, exist_ok=True)
                pdfs = _crawl_website(browser, base_url, pages_dir)
                if not pdfs:
                    logger.warning("  No pages crawled for %s", name)
                    return None

                _clean_pdf_directory(pdfs)
                _merge_pdfs(pdfs, final_pdf)
            finally:
                browser.close()
                if os.path.isdir(pages_dir):
                    shutil.rmtree(pages_dir, ignore_errors=True)

    if not os.path.exists(final_pdf):
        return None
    try:
        _rdr = PdfReader(final_pdf)
        _page_count = len(_rdr.pages)
        if _page_count == 0:
            os.remove(final_pdf)
            logger.warning("  All pages filtered out — no valid content for %s", name)
            return None
        if _page_count < _SCRAPE_MIN_PAGES:
            os.remove(final_pdf)
            logger.warning(
                "  F4 min-content reject: only %d page(s) for %s (threshold=%d, override via KAIJU_SCRAPE_MIN_PAGES). "
                "Likely a partial crawl — falling back to README so dataset entry is honest.",
                _page_count, name, _SCRAPE_MIN_PAGES,
            )
            return None
    except Exception as _pdf_e:
        logger.warning("  Cannot validate merged PDF for %s: %s", name, _pdf_e)
        os.remove(final_pdf)
        return None

    if compress:
        compressed_path = f"{final_pdf}.bz2"
        _compress_bz2(final_pdf, compressed_path)
        os.remove(final_pdf)
        logger.info("  Spec saved: %s", compressed_path)
        return compressed_path

    logger.info("  Spec saved: %s", final_pdf)
    return final_pdf


# Alias for backward compatibility (was async, now sync)
scrape_spec_sync = scrape_spec


_README_NOISE_DOMAINS: frozenset[str] = frozenset([
    "shields.io", "img.shields.io", "badge.fury.io",
    "travis-ci.org", "travis-ci.com", "app.travis-ci.com", "circleci.com",
    "codecov.io", "coveralls.io", "goreportcard.com", "snyk.io",
    "pkg-size.dev", "hits.sh", "buymeacoffee.com", "ko-fi.com",
    "patreon.com", "twitter.com", "x.com", "linkedin.com",
    "discord.gg", "discord.com", "slack.com", "t.me",
])

_DOC_HINTS: tuple[str, ...] = (
    "docs.", "/docs", "documentation", "api.", "/api",
    "readthedocs", "godoc", "pkg.go.dev", "docs.rs",
    "/doc", "manual", "guide", "reference", "javadoc", "rustdoc",
    "github.io",
)


def _score_doc_url(url: str, identity_tokens: frozenset[str] = frozenset()) -> int:
    lower = url.lower()
    score = sum(5 for hint in _DOC_HINTS if hint in lower)
    # Strongly prefer the project's OWN docs over a dependency's docs that the
    # README also links to (e.g. memo's README links joblib.readthedocs.io).
    if identity_tokens and any(tok in lower for tok in identity_tokens):
        score += 100
    return score


def _render_readme_text_pdf(
    readme_content: str,
    readme_name: str,
    repo_name: str,
    specs_dir: str | Path,
) -> str | None:
    """Render README text + extracted links to a bz2-compressed PDF. Returns path or None."""
    try:
        import fitz as _fitz
    except ImportError:
        logger.warning("  README text fallback requires PyMuPDF: pip install PyMuPDF")
        return None
    import bz2 as _bz2

    _raw_urls = re.findall(r"https?://[^\s<>\"'\)\]]+", readme_content)
    all_urls = list(dict.fromkeys(u.rstrip(".,;:!?") for u in _raw_urls))
    header = f"{repo_name} \u2014 Specification (generated from {readme_name})"
    sep = "=" * len(header)
    doc_lines: list[str] = [header, sep, "", readme_content.strip()]
    if all_urls:
        doc_lines += ["", sep, "Referenced Links", sep, ""]
        doc_lines.extend(f"  {u}" for u in all_urls)
    full_text = "\n".join(doc_lines)

    page_w, page_h = 595, 842
    margin = 40
    font_size = 9
    line_h = font_size * 1.35
    max_chars = int((page_w - 2 * margin) / (font_size * 0.52))

    def _wrap(line: str) -> list[str]:
        if len(line) <= max_chars:
            return [line]
        wrapped: list[str] = []
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            if cut < max_chars // 2:
                cut = max_chars
            wrapped.append(line[:cut])
            line = line[cut:].lstrip()
        if line:
            wrapped.append(line)
        return wrapped

    fitz_doc = _fitz.open()
    try:
        def _new_page():
            p = fitz_doc.new_page(width=page_w, height=page_h)
            return p, margin + font_size

        cur_page, y = _new_page()
        for raw_line in full_text.split("\n"):
            for sub in _wrap(raw_line):
                if y + line_h > page_h - margin:
                    cur_page, y = _new_page()
                if sub.strip():
                    cur_page.insert_text((margin, y), sub, fontsize=font_size, color=(0, 0, 0))
                y += line_h

        pdf_bytes = fitz_doc.tobytes()
    finally:
        fitz_doc.close()
    specs_path = Path(specs_dir)
    specs_path.mkdir(parents=True, exist_ok=True)
    out_path = specs_path / f"{repo_name}_readme_spec.pdf.bz2"
    with _bz2.open(out_path, "wb") as fh:
        fh.write(pdf_bytes)
    logger.info("  README text spec written: %s", out_path)
    return str(out_path)


def _md_to_html(md: str) -> str:
    """Lightweight markdown-to-HTML for README rendering (no external deps)."""
    import html as _html

    def _inline(text: str) -> str:
        text = _html.escape(text)
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        text = re.sub(r'\*\*\*([^*]+)\*\*\*', r'<strong><em>\1</em></strong>', text)
        text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'\*([^*\n]+)\*', r'<em>\1</em>', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
        return text

    parts: list[str] = []
    in_code = False
    code_lang = ""
    code_buf: list[str] = []
    in_list = ""

    def _flush_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append(f"</{in_list}>")
            in_list = ""

    for line in md.splitlines():
        if line.startswith("```"):
            if in_code:
                escaped = _html.escape("\n".join(code_buf))
                parts.append(f'<pre><code class="language-{code_lang}">{escaped}</code></pre>')
                code_buf = []
                code_lang = ""
                in_code = False
            else:
                _flush_list()
                in_code = True
                code_lang = line[3:].strip()
            continue
        if in_code:
            code_buf.append(line)
            continue
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            _flush_list()
            lvl = len(m.group(1))
            parts.append(f'<h{lvl}>{_inline(m.group(2))}</h{lvl}>')
            continue
        if re.match(r'^[-*_]{3,}\s*$', line):
            _flush_list()
            parts.append('<hr>')
            continue
        m = re.match(r'^>\s?(.*)', line)
        if m:
            _flush_list()
            parts.append(f'<blockquote><p>{_inline(m.group(1))}</p></blockquote>')
            continue
        m = re.match(r'^[-*+]\s+(.*)', line)
        if m:
            if in_list != 'ul':
                _flush_list()
                parts.append('<ul>')
                in_list = 'ul'
            parts.append(f'<li>{_inline(m.group(1))}</li>')
            continue
        m = re.match(r'^\d+\.\s+(.*)', line)
        if m:
            if in_list != 'ol':
                _flush_list()
                parts.append('<ol>')
                in_list = 'ol'
            parts.append(f'<li>{_inline(m.group(1))}</li>')
            continue
        if not line.strip():
            _flush_list()
            parts.append('')
            continue
        _flush_list()
        parts.append(f'<p>{_inline(line)}</p>')

    if in_code and code_buf:
        parts.append(f'<pre><code>{_html.escape(chr(10).join(code_buf))}</code></pre>')
    _flush_list()
    return '\n'.join(parts)


def _render_readme_html_pdf(
    readme_content: str,
    readme_name: str,
    repo_name: str,
    specs_dir: str | Path,
) -> str | None:
    """Render README as styled HTML via Playwright → bz2-compressed PDF."""
    if _MISSING_DEPS:
        return _render_readme_text_pdf(readme_content, readme_name, repo_name, specs_dir)

    import bz2 as _bz2
    import tempfile

    body_html = _md_to_html(readme_content)
    safe_repo = re.sub(r'[<>&"\']', '', repo_name)
    safe_readme = re.sub(r'[<>&"\']', '', readme_name)

    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 13px; line-height: 1.65; color: #24292e; background: #ffffff;
  }}
  .page-header {{
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
    padding: 28px 48px 24px;
  }}
  .page-header h1 {{ font-size: 22px; font-weight: 700; color: #e8f4fd; border: none; padding: 0; margin: 0; }}
  .page-header .sub {{ font-size: 11px; color: #8ab4d4; margin-top: 6px; }}
  .content {{ padding: 32px 48px 48px; max-width: 860px; }}
  h1 {{ font-size: 20px; color: #0f3460; border-bottom: 2px solid #dde4ed; padding-bottom: 8px; margin: 28px 0 14px; }}
  h2 {{ font-size: 17px; color: #1a5276; border-bottom: 1px solid #dde4ed; padding-bottom: 6px; margin: 22px 0 10px; }}
  h3 {{ font-size: 14px; color: #1f618d; margin: 18px 0 8px; font-weight: 600; }}
  h4, h5, h6 {{ font-size: 13px; color: #2874a6; margin: 14px 0 6px; font-weight: 600; }}
  p {{ margin: 6px 0 10px; }}
  a {{ color: #0366d6; text-decoration: none; }}
  code {{
    background: #f0f4f8; border: 1px solid #d1d9e0; border-radius: 3px;
    padding: 1px 5px;
    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;
    font-size: 11.5px; color: #d63384;
  }}
  pre {{
    background: #1e2a38; border-radius: 6px; padding: 16px 20px; margin: 14px 0;
    border-left: 4px solid #0366d6; page-break-inside: avoid;
  }}
  pre code {{ background: none; border: none; padding: 0; color: #cdd9e5; font-size: 11px; white-space: pre-wrap; word-break: break-all; }}
  ul, ol {{ padding-left: 24px; margin: 8px 0 10px; }}
  li {{ margin: 3px 0; }}
  blockquote {{
    border-left: 4px solid #0366d6; background: #f0f7ff;
    margin: 12px 0; padding: 10px 16px; color: #586069; border-radius: 0 4px 4px 0;
  }}
  blockquote p {{ margin: 0; }}
  hr {{ border: none; border-top: 1px solid #dde4ed; margin: 22px 0; }}
  strong {{ color: #24292e; font-weight: 600; }}
  em {{ color: #586069; }}
  .page-footer {{
    background: #f6f8fa; border-top: 1px solid #dde4ed;
    padding: 12px 48px; font-size: 10px; color: #6a737d; margin-top: 12px;
  }}
</style>
</head>
<body>
<div class=\"page-header\">
  <h1>{safe_repo} &mdash; Specification</h1>
  <div class=\"sub\">Generated from {safe_readme} &middot; Kaiju Commit-0 Dataset</div>
</div>
<div class=\"content\">
{body_html}
</div>
<div class=\"page-footer\">
  Kaiju prepare_repo pipeline &middot; Source: {safe_readme} &middot; {safe_repo}
</div>
</body>
</html>"""

    specs_path = Path(specs_dir)
    specs_path.mkdir(parents=True, exist_ok=True)
    out_path = specs_path / f"{repo_name}_readme_spec.pdf.bz2"
    tmp_path = ""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[no-redef]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                pg = browser.new_page()
                pg.set_content(html, wait_until="domcontentloaded")
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name
                pg.pdf(
                    path=tmp_path,
                    print_background=True,
                    format="A4",
                    margin={"top": "0px", "bottom": "0px", "left": "0px", "right": "0px"},
                )
                pg.close()
            finally:
                browser.close()
        with open(tmp_path, "rb") as f_in, _bz2.open(out_path, "wb") as f_out:
            f_out.writelines(f_in)
        logger.info("  README HTML spec written: %s", out_path)
        return str(out_path)
    except Exception as exc:
        logger.warning("  Playwright HTML render failed (%s), falling back to text PDF", exc)
        return _render_readme_text_pdf(readme_content, readme_name, repo_name, specs_dir)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _github_slug_from_repo(repo_dir: Path) -> str:
    """Extract 'org/repo' from a local clone's git remotes. Returns '' if unavailable."""
    import subprocess
    for remote in ("origin", "upstream", "fork"):
        try:
            res = subprocess.run(
                ["git", "-C", str(repo_dir), "config", "--get", f"remote.{remote}.url"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            continue
        url = (res.stdout or "").strip()
        if not url:
            continue
        m = re.search(r'github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$', url)
        if m:
            return m.group(1)
    return ""


def _render_github_readme_pdf(
    slug: str,
    repo_name: str,
    specs_dir: str | Path,
) -> str | None:
    """Render a repo's README straight from its GitHub page to a bz2-compressed PDF.

    GitHub natively renders Markdown, reStructuredText, and embedded raw HTML, so this
    produces a faithful, fully-styled spec regardless of README format.
    """
    if _MISSING_DEPS or not slug:
        return None
    import bz2 as _bz2
    import tempfile

    url = f"https://github.com/{slug}"
    specs_path = Path(specs_dir)
    specs_path.mkdir(parents=True, exist_ok=True)
    out_path = specs_path / f"{repo_name}_readme_spec.pdf.bz2"
    isolate_js = """() => {
        const art = document.querySelector('article.markdown-body')
            || document.querySelector('#readme article')
            || document.querySelector('[data-testid=readme]');
        if (!art) return false;
        const clone = art.cloneNode(true);
        clone.querySelectorAll('img').forEach(i => {
            i.loading = 'eager';
            const s = i.getAttribute('src');
            if (s) { i.setAttribute('src', ''); i.setAttribute('src', s); }
        });
        document.body.innerHTML = '';
        document.body.style.cssText = 'margin:0;padding:36px 44px;background:#ffffff;';
        clone.style.maxWidth = '100%';
        document.body.appendChild(clone);
        return true;
    }"""
    tmp_path = ""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[no-redef]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                pg = browser.new_page(viewport={"width": 1024, "height": 1400})
                pg.emulate_media(color_scheme="light")
                resp = pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                if not resp or resp.status >= 400:
                    logger.debug("  GitHub page HTTP %s for %s", resp.status if resp else "?", url)
                    return None
                pg.wait_for_selector(
                    "article.markdown-body, [data-testid=readme]", timeout=20000
                )
                if not pg.evaluate(isolate_js):
                    logger.debug("  Could not isolate README article on %s", url)
                    return None
                pg.wait_for_timeout(1800)
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name
                pg.pdf(
                    path=tmp_path,
                    print_background=True,
                    format="A4",
                    margin={"top": "0px", "bottom": "0px", "left": "0px", "right": "0px"},
                )
                pg.close()
            finally:
                browser.close()
        with open(tmp_path, "rb") as f_in, _bz2.open(out_path, "wb") as f_out:
            f_out.writelines(f_in)
        logger.info("  GitHub README spec rendered: %s (from %s)", out_path, url)
        return str(out_path)
    except Exception as exc:
        logger.warning("  GitHub README render failed (%s)", exc)
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def scrape_readme_spec(
    repo_dir: str | Path,
    specs_dir: str | Path = "specs",
    repo_name: str = "",
    compress: bool = True,
) -> tuple[str | None, str]:
    """Generate a spec PDF for a repo by crawling README links via Playwright.

    Strategy:
      1. Find repo README and extract all HTTP(S) links.
      2. Remove badge/noise domains; score by documentation relevance.
      3. Try scrape_spec() (Playwright BFS crawl) on the top 3 candidates.
      4. Fall back to plain-text PDF rendering of the README itself.

    Returns
    -------
        (spec_path, crawled_url) -- crawled_url is empty string for the text fallback.

    """
    repo_dir = Path(repo_dir)
    if not repo_name:
        repo_name = repo_dir.name
    _readme_names = ["README.md", "README.rst", "README.txt", "README", "readme.md"]
    readme_content = ""
    readme_name = "README"
    for _n in _readme_names:
        _candidate = repo_dir / _n
        if _candidate.exists():
            readme_content = _candidate.read_text(errors="replace")
            readme_name = _n
            break
    if not readme_content.strip():
        logger.info("  No README for %s \u2014 skipping README spec fallback", repo_name)
        return None, ""
    _raw_urls = re.findall(r"https?://[^\s<>\"'\)\]]+", readme_content)
    all_urls = list(dict.fromkeys(u.rstrip(".,;:!?") for u in _raw_urls))
    _blocked_all = {
        "github.com", "gitlab.com", "bitbucket.org", "pypi.org",
        "wikipedia.org", "raw.githubusercontent.com", "go.googlesource.com",
    } | _README_NOISE_DOMAINS
    candidate_urls = [
        u for u in all_urls
        if not any(
            urlparse(u).netloc.lower() == b or urlparse(u).netloc.lower().endswith("." + b)
            for b in _blocked_all
        )
    ]
    _identity_tokens = frozenset(
        t for t in re.split(r"[^a-z0-9]+", f"{repo_dir.name} {repo_name}".lower())
        if len(t) >= 3
    )
    candidate_urls.sort(
        key=lambda u: _score_doc_url(u, _identity_tokens), reverse=True
    )
    identity_matched = [
        u for u in candidate_urls
        if _identity_tokens and _score_doc_url(u, _identity_tokens) >= 100
    ]
    if not _MISSING_DEPS and identity_matched:
        for url in identity_matched[:3]:
            try:
                logger.info("  Trying README link via Playwright: %s", url)
                path = scrape_spec(url, repo_name, output_dir=str(specs_dir), compress=compress)
                if path:
                    logger.info("  README Playwright spec scraped: %s (from %s)", path, url)
                    return path, url
            except Exception as exc:
                logger.debug("  README link Playwright failed for %s: %s", url, exc)
    elif not identity_matched:
        logger.info(
            "  No identity-matched doc URLs in README for %s; skipping generic candidates",
            repo_name,
        )
    logger.info("  Falling back to README rendering for %s", repo_name)
    _slug = _github_slug_from_repo(repo_dir)
    if _slug:
        logger.info("  Rendering README from GitHub page: %s", _slug)
        gh_path = _render_github_readme_pdf(_slug, repo_name, specs_dir)
        if gh_path:
            return gh_path, f"https://github.com/{_slug}"
    spec_path = _render_readme_html_pdf(readme_content, readme_name, repo_name, specs_dir)
    return spec_path, ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape library docs into PDF specs")
    parser.add_argument("--url", type=str, help="Documentation URL to scrape")
    parser.add_argument(
        "--name", type=str, help="Library name (used for output filename)"
    )
    parser.add_argument(
        "--input",
        type=str,
        help="Input JSON (validated.json or dataset_entries.json) with specification URLs",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./specs",
        help="Output directory for PDFs (default: ./specs)",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Skip bz2 compression of output PDFs",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to scrape specs for",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help="Path to a local clone; when no --url is available, generate the "
        "spec from the repo's README/docs links via scrape_readme_spec() "
        "(README fallback). Use with --name.",
    )

    args = parser.parse_args()

    if args.url and args.name:
        result = scrape_spec(args.url, args.name, args.output_dir, not args.no_compress)
        if result:
            print(f"Done: {result}")
        else:
            print("Failed to scrape spec")
            exit(1)

    elif args.repo_dir:
        # README-fallback path: no documentation URL, scrape from the local clone.
        # NOTE: scrape_readme_spec(repo_dir, specs_dir, repo_name, compress) — the
        # 2nd positional is the OUTPUT dir, the 3rd is the repo name. Pass by
        # keyword so the output dir is never mistaken for the name.
        result, _crawled = scrape_readme_spec(
            repo_dir=args.repo_dir,
            specs_dir=args.output_dir,
            repo_name=args.name or "",
            compress=not args.no_compress,
        )
        if result:
            print(f"Done: {result}")
        else:
            print("Failed to scrape README spec")
            exit(1)

    elif args.input:
        entries = json.loads(Path(args.input).read_text(encoding="utf-8"))

        if isinstance(entries, dict) and "data" in entries:
            entries = entries["data"]

        count = 0
        for entry in entries:
            if args.max_repos and count >= args.max_repos:
                break

            spec_url = None
            name = None

            if isinstance(entry, dict):
                name = (
                    entry.get("instance_id", "").split("/")[-1]
                    or entry.get("name", "").split("/")[-1]
                )

                if "setup" in entry and isinstance(entry["setup"], dict):
                    spec_url = entry["setup"].get("specification")
                elif "analysis" in entry and isinstance(entry.get("analysis"), dict):
                    spec_url = entry["analysis"].get("docs_url")
                elif "specification" in entry:
                    spec_url = entry["specification"]

            if not spec_url or not name:
                logger.warning(
                    "  Skipping entry — no spec URL or name: %s",
                    entry.get("instance_id", "?"),
                )
                continue

            logger.info("\nScraping spec for %s: %s", name, spec_url)
            result = scrape_spec(spec_url, name, args.output_dir, not args.no_compress)
            if result:
                count += 1
                logger.info("  [%d] Done: %s", count, result)
            else:
                logger.warning("  Failed: %s", name)

        print(f"\nScraped {count} specs to {args.output_dir}")

    else:
        parser.error("Provide either --url/--name or --input")


if __name__ == "__main__":
    main()
