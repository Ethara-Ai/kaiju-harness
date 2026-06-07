from __future__ import annotations

from unittest.mock import MagicMock


from tools.scrape_pdf import (
    SOFT_404_FIRST_LINE_EXACT,
    SOFT_404_MARKERS,
    _is_soft_404_content,
    _is_soft_404_page,
)


class TestIsSoft404Content:
    def test_detects_title_404(self) -> None:
        html = (
            "<html><head><title>404 - Page not found</title></head><body></body></html>"
        )
        assert _is_soft_404_content(html) is True

    def test_detects_rtd_project_not_found(self) -> None:
        html = "<html><head><title>Project not found - Read the Docs Community</title></head><body></body></html>"
        assert _is_soft_404_content(html) is True

    def test_detects_h1_page_not_found(self) -> None:
        html = "<html><body><h1>Page not found</h1><p>Sorry, we couldn't find that.</p></body></html>"
        assert _is_soft_404_content(html) is True

    def test_detects_h1_404(self) -> None:
        html = "<html><body><h1>404</h1></body></html>"
        assert _is_soft_404_content(html) is True

    def test_does_not_flag_valid_docs_page(self) -> None:
        html = "<html><head><title>Welcome to requests</title></head><body><h1>Requests: HTTP for Humans</h1></body></html>"
        assert _is_soft_404_content(html) is False

    def test_does_not_flag_body_mention_of_404(self) -> None:
        html = "<html><head><title>Error Handling</title></head><body><p>When you get a 404 not found response, retry the request.</p></body></html>"
        assert _is_soft_404_content(html) is False

    def test_case_insensitive_title(self) -> None:
        html = "<html><head><title>PAGE NOT FOUND</title></head></html>"
        assert _is_soft_404_content(html) is True

    def test_empty_html(self) -> None:
        assert _is_soft_404_content("") is False

    def test_h1_with_attributes(self) -> None:
        html = '<html><body><h1 id="404-page-not-found" class="heading">Page Not Found</h1></body></html>'
        assert _is_soft_404_content(html) is True

    def test_no_false_positive_filenotfounderror_title(self) -> None:
        html = "<html><head><title>FileNotFoundError — Python 3.12 documentation</title></head></html>"
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_error_handling_guide(self) -> None:
        html = "<html><head><title>HTTP 404 Not Found - Error Handling Guide</title></head></html>"
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_django_404_tutorial(self) -> None:
        html = "<html><head><title>Django - Handling 404 Errors</title></head></html>"
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_kubernetes_docs(self) -> None:
        html = "<html><head><title>Why Resources Are Not Found in Kubernetes</title></head></html>"
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_troubleshooting_title(self) -> None:
        html = (
            "<html><head><title>Troubleshooting: Object Not Found</title></head></html>"
        )
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_h1_filenotfounderror(self) -> None:
        html = "<html><body><h1>FileNotFoundError</h1></body></html>"
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_h1_handling_404(self) -> None:
        html = "<html><body><h1>Handling 404 Not Found Responses</h1></body></html>"
        assert _is_soft_404_content(html) is False

    def test_no_false_positive_h1_object_not_found(self) -> None:
        html = "<html><body><h1>ObjectNotFoundError Reference</h1></body></html>"
        assert _is_soft_404_content(html) is False

    def test_title_not_found_with_site_separator(self) -> None:
        html = "<html><head><title>Page Not Found | My Site</title></head></html>"
        assert _is_soft_404_content(html) is True

    def test_title_404_with_dash_separator(self) -> None:
        html = "<html><head><title>404 — Error</title></head></html>"
        assert _is_soft_404_content(html) is True


class TestIsSoft404Page:
    def _make_page(self, text: str) -> MagicMock:
        page = MagicMock()
        page.get_text.return_value = text
        return page

    def test_detects_short_404_text(self) -> None:
        page = self._make_page(
            "404\nPage not found\nThe page you requested was not found."
        )
        assert _is_soft_404_page(page) is True

    def test_detects_project_not_found(self) -> None:
        page = self._make_page(
            "The project you requested does not exist or may have been removed."
        )
        assert _is_soft_404_page(page) is True

    def test_detects_first_line_404(self) -> None:
        page = self._make_page("404\nSorry, this page does not exist.")
        assert _is_soft_404_page(page) is True

    def test_detects_first_line_page_not_found(self) -> None:
        page = self._make_page("Page not found\nGo back to home.")
        assert _is_soft_404_page(page) is True

    def test_detects_first_line_not_found_with_period(self) -> None:
        page = self._make_page("Not found.\nPlease check the URL.")
        assert _is_soft_404_page(page) is True

    def test_does_not_flag_long_valid_page(self) -> None:
        text = "This is a valid documentation page. " * 50 + " page not found "
        assert len(text) > 500
        page = self._make_page(text)
        assert _is_soft_404_page(page) is False

    def test_does_not_flag_empty_page(self) -> None:
        page = self._make_page("")
        assert _is_soft_404_page(page) is False

    def test_does_not_flag_normal_short_page(self) -> None:
        page = self._make_page("Welcome to the documentation.\nGetting started guide.")
        assert _is_soft_404_page(page) is False

    def test_no_false_positive_page_not_found_in_cache(self) -> None:
        page = self._make_page("Page not found in cache\nRetry with fresh fetch.")
        assert _is_soft_404_page(page) is False

    def test_no_false_positive_404_error_codes(self) -> None:
        page = self._make_page("404 error codes\nHTTP status code reference.")
        assert _is_soft_404_page(page) is False

    def test_no_false_positive_filenotfounderror(self) -> None:
        page = self._make_page(
            "FileNotFoundError\nRaises this error when a file path is not found."
        )
        assert _is_soft_404_page(page) is False

    def test_no_false_positive_error_handling_content(self) -> None:
        page = self._make_page("Error Handling\nHow to handle 404 not found responses.")
        assert _is_soft_404_page(page) is False

    def test_all_long_markers_detected(self) -> None:
        for marker in SOFT_404_MARKERS:
            page = self._make_page(marker)
            assert _is_soft_404_page(page) is True, f"Failed to detect marker: {marker}"

    def test_all_first_line_exact_detected(self) -> None:
        for phrase in SOFT_404_FIRST_LINE_EXACT:
            page = self._make_page(phrase)
            assert _is_soft_404_page(page) is True, (
                f"Failed to detect first-line: {phrase}"
            )
