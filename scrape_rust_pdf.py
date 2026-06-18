"""Scrape Rust library documentation into PDF specifications.

Supports three documentation sources:
  1. docs.rs  -- Uses the all.html index to discover every item page
  2. mdBook   -- Detects mdBook sites and renders print.html as a single PDF
  3. Fallback -- BFS crawl for custom sites (tokio.rs, etc.)

Can also resolve a crate name via the crates.io API to find docs automatically.

Usage:
    python -m tools.scrape_rust_pdf --crate serde
    python -m tools.scrape_rust_pdf --url https://docs.rs/tokio/latest/tokio/ --name tokio
    python -m tools.scrape_rust_pdf --url https://serde.rs/ --name serde
    python -m tools.scrape_rust_pdf --input validated.json --output-dir ./specs
    python -m tools.scrape_rust_pdf --crate serde --no-compress --max-pages 200

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
import re
import shutil
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import requests as requests_lib

if TYPE_CHECKING:
    import fitz
    from bs4 import BeautifulSoup
    from PyPDF2 import PdfMerger

try:
    import fitz  # type: ignore[no-redef]
    from bs4 import BeautifulSoup  # type: ignore[no-redef]
    from PyPDF2 import PdfMerger  # type: ignore[no-redef]
    from playwright.sync_api import sync_playwright  # type: ignore[no-redef]

    _MISSING_DEPS = False
    _MISSING_DEP_MSG = ""
except ImportError as _e:
    _MISSING_DEPS = True
    _MISSING_DEP_MSG = (
        "scrape_rust_pdf requires: "
        "pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests "
        f"&& playwright install chromium ({_e})"
    )

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

_DOCSRS_SOURCE_PATTERN = re.compile(r"/src/[^/]+/")

_DOCSRS_HIDE_CHROME_CSS = """
    .nav-container { display: none !important; }
    nav.sidebar { display: none !important; }
    div#rustdoc-modnav { display: none !important; }
    section#rustdoc-toc { display: none !important; }
    div.sidebar-resizer { display: none !important; }
    .sub-heading { display: none !important; }
"""

_MDBOOK_PRINT_CSS = """
    nav#sidebar { display: none !important; }
    .nav-wrapper { display: none !important; }
    #menu-bar { display: none !important; }
    .mobile-nav-chapters { display: none !important; }
"""

CAPTCHA_MARKERS = [
    "This website uses a security service to protect against malicious bots",
    "This page is displayed while the website verifies you are not a bot",
    "Checking if the site connection is secure",
    "Enable JavaScript and cookies to continue",
    "Verify you are human",
    "Please verify you are a human",
]

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


class SiteType:
    DOCSRS = "docs.rs"
    MDBOOK = "mdbook"
    GENERIC = "generic"


def _detect_site_type(page: Any, url: str) -> str:
    parsed = urlparse(url)

    if parsed.netloc == "docs.rs":
        return SiteType.DOCSRS

    try:
        content = page.content()
        if 'name="generator" content="mdBook"' in content:
            return SiteType.MDBOOK
        if "book.js" in content or "book.css" in content:
            return SiteType.MDBOOK
    except Exception:
        pass

    return SiteType.GENERIC


def resolve_crate_docs_url(crate_name: str) -> str:
    api_url = f"https://crates.io/api/v1/crates/{crate_name}"
    headers = {"User-Agent": "scrape_rust_pdf/1.0 (spec-generation tool)"}
    resp = requests_lib.get(api_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    doc_url = data.get("crate", {}).get("documentation")
    if doc_url and doc_url.strip():
        return doc_url.strip()

    return f"https://docs.rs/{crate_name}/latest/{crate_name}/"


def _is_page_blank(page: Any) -> bool:
    text = page.get_text("text")
    return not text.strip()


def _is_captcha_page(page: Any) -> bool:
    text = page.get_text("text")
    text_lower = text.lower()
    return any(marker.lower() in text_lower for marker in CAPTCHA_MARKERS)


def _remove_blank_pages(pdf_path: str) -> None:
    document = fitz.open(pdf_path)
    if document.page_count < 2:
        document.close()
        return

    output_document = fitz.open()
    removed_captcha = 0
    for i in range(document.page_count):
        page = document.load_page(i)
        if _is_page_blank(page):
            continue
        if _is_captcha_page(page):
            removed_captcha += 1
            continue
        output_document.insert_pdf(document, from_page=i, to_page=i)

    if removed_captcha:
        logger.info(
            "  Removed %d captcha/bot-check page(s) from %s",
            removed_captcha,
            pdf_path,
        )

    output_document.save(pdf_path)
    output_document.close()
    document.close()


def _clean_pdf_directory(docs: list[str]) -> None:
    for doc in docs:
        if os.path.exists(doc):
            _remove_blank_pages(doc)


def _merge_pdfs(docs: list[str], output_filename: str) -> None:
    merger = PdfMerger()
    for pdf in docs:
        if os.path.exists(pdf):
            merger.append(pdf)
    merger.write(output_filename)
    merger.close()


def _compress_bz2(input_path: str, output_path: str) -> None:
    with open(input_path, "rb") as f_in:
        with bz2.open(output_path, "wb") as f_out:
            f_out.writelines(f_in)


def _generate_pdf(
    page: Any,
    url: str,
    output_dir: str,
    extra_css: str = "",
) -> str:
    pdf_path = ""
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            logger.debug("  domcontentloaded timeout for %s, retrying with commit", url)
            page.goto(url, wait_until="commit", timeout=15000)

        if extra_css:
            page.add_style_tag(content=extra_css)

        out_name = f"{urlparse(url).path.replace('/', '_').strip('_')}.pdf"
        if out_name == ".pdf":
            out_name = "base.pdf"
        pdf_path = os.path.join(output_dir, out_name)

        page.pdf(
            path=pdf_path,
            print_background=True,
            format="A4",
            margin={"top": "10px", "bottom": "10px", "left": "10px", "right": "10px"},
        )
        logger.debug("  Saved PDF: %s", pdf_path)
    except Exception as e:
        logger.warning("  Error creating PDF for %s: %s", url, e)
    return pdf_path


def _is_docsrs_source_url(url: str) -> bool:
    return bool(_DOCSRS_SOURCE_PATTERN.search(urlparse(url).path))


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/index.html"):
        path = path[: -len("/index.html")]
    elif path == "index.html":
        path = ""
    return f"{parsed.scheme}://{parsed.netloc}{path}/"


def _discover_docsrs_urls(page: Any, base_url: str) -> list[str]:
    if not base_url.endswith("/"):
        base_url += "/"

    all_url = urljoin(base_url, "all.html")
    seen: set[str] = {_normalize_url(base_url)}
    urls: list[str] = [base_url]

    try:
        page.goto(all_url, wait_until="domcontentloaded", timeout=30000)
        content = page.content()
        soup = BeautifulSoup(content, "html.parser")

        main_content = soup.select_one("section#main-content") or soup
        for a_tag in main_content.find_all("a", href=True):
            href = a_tag["href"]
            full_url = urljoin(all_url, href)

            if not full_url.startswith(base_url):
                continue
            if _is_docsrs_source_url(full_url):
                continue
            if "#" in full_url:
                full_url = full_url.split("#")[0]
            if "?" in full_url:
                full_url = full_url.split("?")[0]

            normalized = _normalize_url(full_url)
            if full_url and normalized not in seen:
                urls.append(full_url)
                seen.add(normalized)

        logger.info("  Discovered %d docs.rs pages via all.html", len(urls))
    except Exception as e:
        logger.warning("  Failed to load all.html (%s), falling back to BFS", e)

    return urls


def _crawl_docsrs(
    browser: Any,
    base_url: str,
    output_dir: str,
    max_pages: int = 500,
) -> list[str]:
    page = browser.new_page()
    sequence: list[str] = []

    urls = _discover_docsrs_urls(page, base_url)

    seen = {_normalize_url(u) for u in urls}
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        content = page.content()
        soup = BeautifulSoup(content, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            full_url = urljoin(base_url, a_tag["href"])
            if "#" in full_url:
                full_url = full_url.split("#")[0]
            if "?" in full_url:
                full_url = full_url.split("?")[0]
            normalized = _normalize_url(full_url)
            if (
                full_url.startswith(base_url)
                and normalized not in seen
                and not _is_docsrs_source_url(full_url)
            ):
                urls.append(full_url)
                seen.add(normalized)
    except Exception:
        pass

    if len(urls) > max_pages:
        logger.info("  Capping from %d to %d pages", len(urls), max_pages)
        urls = urls[:max_pages]

    for i, url in enumerate(urls):
        if _is_docsrs_source_url(url):
            continue
        logger.info("  [%d/%d] %s", i + 1, len(urls), url)
        pdf = _generate_pdf(page, url, output_dir, extra_css=_DOCSRS_HIDE_CHROME_CSS)
        if pdf:
            sequence.append(pdf)

    page.close()
    return sequence


def _crawl_mdbook(
    browser: Any,
    base_url: str,
    output_dir: str,
) -> list[str]:
    page = browser.new_page()
    sequence: list[str] = []

    if not base_url.endswith("/"):
        base_url += "/"
    print_url = urljoin(base_url, "print.html")

    logger.info("  mdBook detected, rendering print.html: %s", print_url)

    try:
        page.goto(print_url, wait_until="domcontentloaded", timeout=60000)
        page.add_style_tag(content=_MDBOOK_PRINT_CSS)

        pdf_path = os.path.join(output_dir, "print.pdf")
        page.pdf(
            path=pdf_path,
            print_background=True,
            format="A4",
            margin={"top": "10px", "bottom": "10px", "left": "10px", "right": "10px"},
        )
        sequence.append(pdf_path)
        logger.info("  Saved mdBook print PDF: %s", pdf_path)
    except Exception as e:
        logger.warning("  Failed to render print.html: %s, falling back to BFS", e)
        page.close()
        return _crawl_generic(browser, base_url, output_dir)

    page.close()
    return sequence


def _should_skip_url(current_url: str, base_url: str) -> bool:
    parsed_path = urlparse(current_url).path.lower().strip("/")
    path_segments = parsed_path.split("/")

    if any(seg in _AUTH_PATH_SEGMENTS for seg in path_segments):
        logger.debug("  Skipping auth/login URL: %s", current_url)
        return True

    query = urlparse(current_url).query.lower()
    if "redirect_uri=" in query or "return_to=" in query or "next=" in query:
        logger.debug("  Skipping redirect URL: %s", current_url)
        return True

    if "docs.rs" in current_url and _is_docsrs_source_url(current_url):
        return True

    return False


def _is_valid_link(link: str, base_url: str) -> str | None:
    parsed_url = urlparse(link)
    if parsed_url.fragment:
        return None
    if not parsed_url.scheme:
        return urljoin(base_url, link)
    if parsed_url.netloc == urlparse(base_url).netloc:
        return link
    return None


def _crawl_generic(
    browser: Any,
    base_url: str,
    output_dir: str,
    max_pages: int = 500,
) -> list[str]:
    page = browser.new_page()
    visited: set[str] = set()
    to_visit: deque[str] = deque([base_url])
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

        try:
            response = page.goto(
                current_url, wait_until="domcontentloaded", timeout=30000
            )
            if response and response.status == 404:
                logger.debug("  404: %s", current_url)
                continue

            content = page.content()
            soup = BeautifulSoup(content, "html.parser")

            for link in soup.find_all("a", href=True):
                full_url = _is_valid_link(link["href"], base_url)
                if (
                    full_url
                    and full_url not in visited
                    and full_url.startswith(base_url)
                ):
                    to_visit.append(full_url)

            pdf = _generate_pdf(page, current_url, output_dir)
            if pdf:
                sequence.append(pdf)
            pages_scraped += 1
        except Exception as e:
            logger.warning("  Error crawling %s: %s", current_url, e)

    page.close()
    return sequence


def scrape_rust_spec(
    base_url: str,
    name: str,
    output_dir: str = "specs",
    compress: bool = True,
    max_pages: int = 500,
) -> str | None:
    """Scrape Rust documentation into a single PDF spec.

    Auto-detects site type (docs.rs, mdBook, or generic) and picks the
    optimal crawling strategy. For docs.rs, uses all.html index discovery.
    For mdBook, renders the single-page print.html. Falls back to BFS crawl.

    Returns path to the output file (PDF or .pdf.bz2), or None on failure.
    """
    if _MISSING_DEPS:
        raise ImportError(_MISSING_DEP_MSG)

    os.makedirs(output_dir, exist_ok=True)
    pages_dir = os.path.join(output_dir, f"{name}_pages")
    final_pdf = os.path.join(output_dir, f"{name}.pdf")

    url_parts = [x for x in base_url.split("/") if x]
    if url_parts and url_parts[-1].endswith(".pdf"):
        logger.info("  Direct PDF download: %s", base_url)
        try:
            response = requests_lib.get(base_url, timeout=60)
            response.raise_for_status()
            with open(final_pdf, "wb") as f:
                f.write(response.content)
        except Exception as e:
            logger.error("  Failed to download PDF: %s", e)
            return None
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                os.makedirs(pages_dir, exist_ok=True)

                detection_page = browser.new_page()
                try:
                    detection_page.goto(
                        base_url, wait_until="domcontentloaded", timeout=30000
                    )
                    site_type = _detect_site_type(detection_page, base_url)
                finally:
                    detection_page.close()

                logger.info("  Detected site type: %s for %s", site_type, base_url)

                if site_type == SiteType.DOCSRS:
                    pdfs = _crawl_docsrs(browser, base_url, pages_dir, max_pages)
                elif site_type == SiteType.MDBOOK:
                    pdfs = _crawl_mdbook(browser, base_url, pages_dir)
                else:
                    pdfs = _crawl_generic(browser, base_url, pages_dir, max_pages)

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

    if compress:
        compressed_path = f"{final_pdf}.bz2"
        _compress_bz2(final_pdf, compressed_path)
        os.remove(final_pdf)
        logger.info("  Spec saved: %s", compressed_path)
        return compressed_path

    logger.info("  Spec saved: %s", final_pdf)
    return final_pdf


scrape_rust_spec_sync = scrape_rust_spec


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Rust library documentation into PDF specs"
    )
    parser.add_argument(
        "--crate",
        type=str,
        help="Crate name (auto-resolves docs URL via crates.io API)",
    )
    parser.add_argument("--url", type=str, help="Documentation URL to scrape directly")
    parser.add_argument(
        "--name",
        type=str,
        help="Library name for output filename (defaults to --crate value)",
    )
    parser.add_argument(
        "--input",
        type=str,
        help="Input JSON with crate entries (must have 'name' or 'crate' and optional 'docs_url')",
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
        "--max-pages",
        type=int,
        default=500,
        help="Maximum number of pages to crawl per crate (default: 500)",
    )
    parser.add_argument(
        "--max-crates",
        type=int,
        default=None,
        help="Maximum number of crates to process from --input",
    )

    args = parser.parse_args()

    if args.crate:
        name = args.name or args.crate
        if args.url:
            url = args.url
        else:
            logger.info("Resolving docs URL for crate: %s", args.crate)
            url = resolve_crate_docs_url(args.crate)
            logger.info("  Resolved: %s", url)

        result = scrape_rust_spec(
            url, name, args.output_dir, not args.no_compress, args.max_pages
        )
        if result:
            print(f"Done: {result}")
        else:
            print("Failed to scrape spec")
            exit(1)

    elif args.url and args.name:
        result = scrape_rust_spec(
            args.url, args.name, args.output_dir, not args.no_compress, args.max_pages
        )
        if result:
            print(f"Done: {result}")
        else:
            print("Failed to scrape spec")
            exit(1)

    elif args.input:
        entries = json.loads(Path(args.input).read_text())

        if isinstance(entries, dict) and "data" in entries:
            entries = entries["data"]

        count = 0
        for entry in entries:
            if args.max_crates and count >= args.max_crates:
                break

            crate_name = None
            docs_url = None

            if isinstance(entry, dict):
                crate_name = (
                    entry.get("crate")
                    or entry.get("name", "").split("/")[-1]
                    or entry.get("instance_id", "").split("/")[-1]
                )

                if "setup" in entry and isinstance(entry["setup"], dict):
                    docs_url = entry["setup"].get("specification")
                elif "docs_url" in entry:
                    docs_url = entry["docs_url"]
                elif "documentation" in entry:
                    docs_url = entry["documentation"]

            elif isinstance(entry, str):
                crate_name = entry

            if not crate_name:
                logger.warning("  Skipping entry, no crate name: %s", entry)
                continue

            if not docs_url:
                try:
                    docs_url = resolve_crate_docs_url(crate_name)
                except Exception as e:
                    logger.warning("  Failed to resolve docs for %s: %s", crate_name, e)
                    continue

            logger.info("\nScraping spec for %s: %s", crate_name, docs_url)
            result = scrape_rust_spec(
                docs_url,
                crate_name,
                args.output_dir,
                not args.no_compress,
                args.max_pages,
            )
            if result:
                count += 1
                logger.info("  [%d] Done: %s", count, result)
            else:
                logger.warning("  Failed: %s", crate_name)

        print(f"\nScraped {count} specs to {args.output_dir}")

    else:
        parser.error("Provide --crate, --url/--name, or --input")


if __name__ == "__main__":
    main()
