"""Tests for commit0.harness.lint_filter module."""

from __future__ import annotations

import pytest

from commit0.harness.lint_filter import (
    ErrorCategory,
    FilterResult,
    classify_pyright_line,
    filter_lint_output,
)


class TestClassifyPyrightLine:
    def test_env_rule_reportMissingImports(self):
        line = '/path/to/file.py:10:1 - error: Import "requests" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "mypackage", {"requests"})
        assert result.category == ErrorCategory.ENVIRONMENT
        assert "requests" in result.reason

    def test_env_rule_own_package_classified_as_code(self):
        line = '/path/to/file.py:10:1 - error: Import "mypackage" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "mypackage", set())
        assert result.category == ErrorCategory.CODE
        assert "own package" in result.reason

    def test_env_rule_known_dep_classified_as_env(self):
        line = '/path/to/file.py:10:1 - error: Import "numpy" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "mypackage", {"numpy"})
        assert result.category == ErrorCategory.ENVIRONMENT

    def test_env_rule_unknown_import(self):
        line = '/path/to/file.py:10:1 - error: Import "unknown" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "mypackage", set())
        assert result.category == ErrorCategory.ENVIRONMENT

    def test_env_rule_no_import_match(self):
        line = "/path/to/file.py:10:1 - error: Some other issue [reportMissingModuleSource]"
        result = classify_pyright_line(line, "mypackage", set())
        assert result.category == ErrorCategory.ENVIRONMENT

    def test_code_rule(self):
        line = '/path/to/file.py:10:1 - error: Variable "x" is not defined [reportUndefinedVariable]'
        result = classify_pyright_line(line, "mypackage", set())
        assert result.category == ErrorCategory.CODE

    @pytest.mark.parametrize(
        "rule",
        [
            "reportGeneralClassIssue",
            "reportAttributeAccessIssue",
            "reportIndexIssue",
            "reportCallIssue",
            "reportReturnType",
            "reportAssignmentType",
            "reportArgumentType",
            "reportOptionalMemberAccess",
            "reportOptionalSubscript",
            "reportOptionalCall",
        ],
    )
    def test_all_code_rules(self, rule):
        line = f"/path/to/file.py:10:1 - error: Some issue [{rule}]"
        result = classify_pyright_line(line, "pkg", set())
        assert result.category == ErrorCategory.CODE

    def test_unknown_rule(self):
        line = "/path/to/file.py:10:1 - error: Something [reportSomethingNew]"
        result = classify_pyright_line(line, "mypackage", set())
        assert result.category == ErrorCategory.UNKNOWN

    def test_no_rule_code(self):
        line = "some random line without a rule code"
        result = classify_pyright_line(line, "mypackage", set())
        assert result.category == ErrorCategory.UNKNOWN
        assert result.reason is None

    def test_empty_string(self):
        result = classify_pyright_line("", "pkg", set())
        assert result.category == ErrorCategory.UNKNOWN

    def test_case_sensitivity_of_import_match(self):
        line = '/path/to/file.py:10:1 - error: Import "MyPackage" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "MyPackage", set())
        assert result.category == ErrorCategory.CODE


class TestFilterLintOutput:
    def test_suppresses_env_errors(self):
        raw = (
            'file.py:1:1 - error: Import "requests" not resolved [reportMissingImports]\n'
            'file.py:2:1 - error: Variable "x" undefined [reportUndefinedVariable]'
        )
        result = filter_lint_output(raw, "mypackage", {"requests"})
        assert result.suppressed_count == 1
        assert result.code_error_count == 1
        assert "reportMissingImports" not in result.output
        assert "reportUndefinedVariable" in result.output

    def test_keeps_unknown_by_default(self):
        raw = "file.py:1:1 - error: Something [reportNewRule]"
        result = filter_lint_output(raw, "pkg", set(), keep_unknown=True)
        assert result.suppressed_count == 0
        assert "reportNewRule" in result.output

    def test_suppresses_unknown_when_flagged(self):
        raw = "file.py:1:1 - error: Something [reportNewRule]"
        result = filter_lint_output(raw, "pkg", set(), keep_unknown=False)
        assert result.suppressed_count == 1
        assert "reportNewRule" not in result.output

    def test_non_error_lines_always_kept(self):
        raw = "Some info line\nAnother info line"
        result = filter_lint_output(raw, "pkg", set())
        assert "Some info line" in result.output
        assert "Another info line" in result.output
        assert result.suppressed_count == 0

    def test_suppression_message_appended(self):
        raw = 'file.py:1:1 - error: Import "x" not resolved [reportMissingImports]'
        result = filter_lint_output(raw, "pkg", set())
        assert "Suppressed 1" in result.output

    def test_no_suppression_message_when_zero(self):
        raw = 'file.py:1:1 - error: Variable "x" undefined [reportUndefinedVariable]'
        result = filter_lint_output(raw, "pkg", set())
        assert "Suppressed" not in result.output

    def test_empty_input(self):
        result = filter_lint_output("", "pkg", set())
        assert result.output == ""
        assert result.suppressed_count == 0
        assert result.code_error_count == 0

    def test_warning_and_information_lines_also_filtered(self):
        raw = (
            'file.py:1:1 - warning: Import "x" not found [reportMissingImports]\n'
            'file.py:2:1 - information: Import "y" not found [reportMissingModuleSource]'
        )
        result = filter_lint_output(raw, "pkg", set())
        assert result.suppressed_count == 2

    def test_mixed_output(self):
        raw = "\n".join(
            [
                "Starting lint...",
                'file.py:1:1 - error: Import "x" not resolved [reportMissingImports]',
                'file.py:2:1 - error: Undefined "y" [reportUndefinedVariable]',
                "Done.",
            ]
        )
        result = filter_lint_output(raw, "pkg", set())
        assert result.suppressed_count == 1
        assert result.code_error_count == 1
        assert "Starting lint..." in result.output
        assert "Done." in result.output


class TestClassifyPyrightLineExpanded:
    def test_all_three_env_rules(self):
        for rule in (
            "reportMissingImports",
            "reportMissingModuleSource",
            "reportMissingTypeStubs",
        ):
            line = f"/p.py:1:1 - error: Issue [{rule}]"
            result = classify_pyright_line(line, "pkg", set())
            assert result.category == ErrorCategory.ENVIRONMENT, f"Failed for {rule}"

    def test_import_match_with_known_dep_in_set(self):
        line = '/f.py:1:1 - error: Import "flask" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "myapp", {"flask", "sqlalchemy"})
        assert result.category == ErrorCategory.ENVIRONMENT
        assert "flask" in result.reason

    def test_import_match_unknown_dep_still_env(self):
        line = '/f.py:1:1 - error: Import "obscure_lib" could not be resolved [reportMissingImports]'
        result = classify_pyright_line(line, "myapp", {"flask"})
        assert result.category == ErrorCategory.ENVIRONMENT

    def test_rule_at_end_with_trailing_whitespace(self):
        line = "/f.py:1:1 - error: Issue [reportUndefinedVariable]   "
        result = classify_pyright_line(line, "pkg", set())
        assert result.category == ErrorCategory.CODE

    def test_multiple_brackets_uses_last(self):
        line = "/f.py:1:1 - error: List[int] issue [reportReturnType]"
        result = classify_pyright_line(line, "pkg", set())
        assert result.category == ErrorCategory.CODE

    def test_classified_error_stores_original_line(self):
        line = "/f.py:1:1 - error: Issue [reportUndefinedVariable]"
        result = classify_pyright_line(line, "pkg", set())
        assert result.line == line

    def test_reason_includes_rule_name_for_code(self):
        line = "/f.py:1:1 - error: Issue [reportCallIssue]"
        result = classify_pyright_line(line, "pkg", set())
        assert "reportCallIssue" in result.reason

    def test_reason_includes_rule_name_for_unknown(self):
        line = "/f.py:1:1 - error: Issue [reportSomethingNew]"
        result = classify_pyright_line(line, "pkg", set())
        assert "reportSomethingNew" in result.reason


class TestFilterLintOutputExpanded:
    def test_only_env_errors_all_suppressed(self):
        raw = "\n".join(
            [
                'f.py:1:1 - error: Import "a" [reportMissingImports]',
                "f.py:2:1 - error: Missing [reportMissingModuleSource]",
                "f.py:3:1 - error: Stub [reportMissingTypeStubs]",
            ]
        )
        result = filter_lint_output(raw, "pkg", set())
        assert result.suppressed_count == 3
        assert result.code_error_count == 0

    def test_only_code_errors_none_suppressed(self):
        raw = "\n".join(
            [
                "f.py:1:1 - error: Undef [reportUndefinedVariable]",
                "f.py:2:1 - error: Return [reportReturnType]",
            ]
        )
        result = filter_lint_output(raw, "pkg", set())
        assert result.suppressed_count == 0
        assert result.code_error_count == 2

    def test_suppression_message_text(self):
        raw = 'f.py:1:1 - error: Import "x" [reportMissingImports]'
        result = filter_lint_output(raw, "pkg", set())
        assert "Suppressed 1 environment-related" in result.output

    def test_multiple_code_errors_counted(self):
        raw = "\n".join(
            [
                "f.py:1:1 - error: A [reportUndefinedVariable]",
                "f.py:2:1 - error: B [reportCallIssue]",
                "f.py:3:1 - error: C [reportReturnType]",
            ]
        )
        result = filter_lint_output(raw, "pkg", set())
        assert result.code_error_count == 3

    def test_known_deps_cause_env_suppression(self):
        raw = 'f.py:1:1 - error: Import "numpy" could not be resolved [reportMissingImports]'
        result = filter_lint_output(raw, "myapp", {"numpy"})
        assert result.suppressed_count == 1

    def test_own_package_import_is_code_error(self):
        raw = 'f.py:1:1 - error: Import "myapp" could not be resolved [reportMissingImports]'
        result = filter_lint_output(raw, "myapp", set())
        assert result.suppressed_count == 0
        assert result.code_error_count == 1

    def test_large_output_with_many_lines(self):
        lines = []
        for i in range(100):
            lines.append(f"f.py:{i}:1 - error: Undef [reportUndefinedVariable]")
        raw = "\n".join(lines)
        result = filter_lint_output(raw, "pkg", set())
        assert result.code_error_count == 100
        assert result.suppressed_count == 0

    def test_filter_result_dataclass_fields(self):
        result = FilterResult(output="hello", suppressed_count=5, code_error_count=3)
        assert result.output == "hello"
        assert result.suppressed_count == 5
        assert result.code_error_count == 3
