"""Regression tests for ``raise_if_transient_llm_error`` false-positive detection.

The httprouter Go run at outputs/d13b2b2f-.../.../httprouter/.../path/ regressed
because the module's stub docstring contained the prose ``return HTTP `500
Internal Server Error` `` (describing a panic handler). The old
``_INTERNAL_SERVER_ERR_RE`` matched that prose as if it were a real HTTP 500
error and re-ran the completed module. These tests lock in the fixed behaviour:
prose forms must NEVER trigger, while every real Python-SDK exception shape
MUST still trigger.
"""
from __future__ import annotations

import pytest

from commit0.harness._optional_dep_stubs import install_missing_optional_dep_stubs

install_missing_optional_dep_stubs()

from agent.agents import TransientLLMError, raise_if_transient_llm_error


def _fires(text: str) -> bool:
    try:
        raise_if_transient_llm_error(text, context="test")
    except TransientLLMError:
        return True
    return False


class TestInternalServerErrFalsePositives:
    """These prose forms all appear legitimately in source repos and MUST NOT fire."""

    def test_httprouter_docstring_regression(self):
        text = (
            "USER Handles panics recovered from HTTP handlers; intended to "
            "generate error page and return HTTP `500 Internal Server Error`, "
            "preventing server crash from unrecovered panics."
        )
        assert not _fires(text)

    def test_code_comment_variant(self):
        assert not _fires("// return 500 Internal Server Error on panic")

    def test_prose_with_http_version(self):
        assert not _fires("The server returned HTTP/1.1 500 Internal Server Error")

    def test_prose_with_backticks(self):
        assert not _fires("Returns HTTP `500 Internal Server Error` when panic occurs")

    def test_test_assertion_prose(self):
        assert not _fires('assert response.status == "500 Internal Server Error"')

    def test_go_source_symbol_reference(self):
        assert not _fires("http.StatusInternalServerError // = 500")


class TestInternalServerErrRealErrors:
    """These are the actual exception shapes real LLM SDKs emit; each MUST fire."""

    def test_openai_sdk_exception(self):
        assert _fires("raise openai.InternalServerError: 500 Internal server error")

    def test_litellm_exception(self):
        assert _fires("litellm.exceptions.InternalServerError: server error")

    def test_httpx_variant(self):
        assert _fires("httpx.InternalServerError")

    def test_provider_snake_case_variant(self):
        assert _fires("anthropic.internal_server_error")

    def test_bare_exception_format_at_line_start(self):
        assert _fires("Traceback:\nInternalServerError: bad thing happened")

    def test_structured_json_payload_double_quoted(self):
        assert _fires(
            '{"error": {"type": "internal_server_error", "message": "..."}}'
        )

    def test_structured_json_payload_single_quoted(self):
        assert _fires("{'error': {'type': 'internal_server_error'}}")

    def test_prose_error_type_marker(self):
        assert _fires("error type: internalservererror")

    def test_prose_error_type_snake_case(self):
        assert _fires("error type: internal_server_error")


class TestHTTPStatusCodePatterns:
    """Word-bounded HTTP status codes (502-529) require an HTTP context prefix.

    A stray ``504`` in source code (e.g. ``C2504`` from fmt/base.h) MUST NOT fire.
    """

    @pytest.mark.parametrize("code", ["502", "503", "504", "520", "524", "529"])
    def test_real_http_status_prefix_fires(self, code):
        assert _fires(f"http {code} bad gateway")

    @pytest.mark.parametrize("stray", ["C2504 warning", "issue #529 mentioned", "0x502"])
    def test_stray_number_in_prose_does_not_fire(self, stray):
        assert not _fires(stray)

    def test_500_prose_no_longer_matched_by_http_regex(self):
        """500 is intentionally excluded from _HTTP_TRANSIENT_CODE_RE because
        it appears too often in prose; _INTERNAL_SERVER_ERR_RE covers real
        InternalServerError shapes via class-name detection instead."""
        assert not _fires("Returns 500 when the server fails")


class TestOtherTransientSignals:
    """Spot-check the substring-list signals still work + docstring-safe."""

    def test_apiconnectionerror_class(self):
        assert _fires("openai.APIConnectionError: timed out")

    def test_read_timeout_in_stack_trace(self):
        assert _fires("httpx.ReadTimeout: read timeout after 30s")

    def test_overloaded_error_json_shape(self):
        assert _fires('{"error": {"type": "overloaded_error"}}')

    def test_bare_overloaded_word_does_not_fire(self):
        """C++ `operator overloaded` in source MUST NOT fire (b40 regression fix)."""
        assert not _fires("class Foo { void operator overloaded(); };")
