"""Tests for generate_test_ids module."""

from tools.generate_test_ids import _parse_collect_output


class TestParseCollectOutput:
    def test_quiet_format(self):
        """Standard quiet output with path::test lines."""
        output = (
            "tests/test_foo.py::test_bar\n"
            "tests/test_foo.py::TestClass::test_method\n"
            "\n"
            "== 2 tests collected in 0.01s ==\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/test_foo.py::test_bar",
            "tests/test_foo.py::TestClass::test_method",
        ]

    def test_verbose_format(self):
        """Legacy verbose output with inline <Module>::<Class>::<Function> tags."""
        output = (
            "<Module tests/test_foo.py>::<Function test_bar>\n"
            "<Module tests/test_foo.py>::<Class TestBaz>::<Function test_qux>\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/test_foo.py::test_bar",
            "tests/test_foo.py::TestBaz::test_qux",
        ]

    def test_modern_indented_tree_format(self):
        """Modern pytest (7/8/9) prints an INDENTED tree — each node on its own
        line, NOT the legacy single-line ``<Module>::<Function>`` form. Each
        Dir/Package/Module contributes a ``/`` path segment; Class/Function join
        with ``::``. The rootdir container node is dropped (ids are rootdir-
        relative). Regression for the BlackSheep ``tests/Test:`` degenerate
        inventory (verbose tree yielded zero real ids)."""
        output = (
            "============================= test session starts ==============================\n"
            "collected 3 items\n"
            "\n"
            "<Dir testbed>\n"
            "  <Package tests>\n"
            "    <Package client>\n"
            "      <Module test_client.py>\n"
            "        <Function test_get_url_value[-/]>\n"
            "        <Coroutine test_client_session_add_middlewares>\n"
            "    <Module test_utils.py>\n"
            "      <UnitTestCase TestUtils>\n"
            "        <TestCaseFunction test_thing>\n"
            "\n"
            "======================== 3 tests collected in 3.58s ========================\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/client/test_client.py::test_get_url_value[-/]",
            "tests/client/test_client.py::test_client_session_add_middlewares",
            "tests/test_utils.py::TestUtils::test_thing",
        ]

    def test_warnings_line_with_colons_not_parsed_as_id(self):
        """A deprecation note in the warnings summary
        (``Test: tests/x.py::y, argvalues type: generator``) contains ``::`` but
        its first token is ``Test:`` — it must NOT be saved as a node id. This is
        the exact BlackSheep bug that produced an 11-byte ``tests/Test:``
        inventory. Verifies BOTH the tree path and the quiet-line fallback."""
        # Quiet output followed by the poisoning warnings section.
        quiet = (
            "tests/test_responses.py::test_ok\n"
            "tests/test_responses.py::test_redirect\n"
            "\n"
            "=============================== warnings summary ===============================\n"
            "../usr/local/lib/python3.10/site-packages/_pytest/python.py:124\n"
            "  PytestRemovedIn10Warning: Passing a non-Collection iterable is deprecated.\n"
            "  Test: tests/test_responses.py::test_redirect_method_raises, argvalues type: generator\n"
            "  Please convert to a list or tuple.\n"
            "2 tests collected in 1.82s\n"
        )
        result = _parse_collect_output(quiet)
        assert result == [
            "tests/test_responses.py::test_ok",
            "tests/test_responses.py::test_redirect",
        ]
        assert not any("Test:" in r for r in result)

        # Same warnings poison, but after an indented TREE (verbose default).
        tree = (
            "collected 1 items\n"
            "<Dir testbed>\n"
            "  <Package tests>\n"
            "    <Module test_responses.py>\n"
            "      <Function test_ok>\n"
            "=============================== warnings summary ===============================\n"
            "  Test: tests/test_responses.py::test_redirect_method_raises, argvalues type: generator\n"
            "1 tests collected in 0.5s\n"
        )
        result = _parse_collect_output(tree)
        assert result == ["tests/test_responses.py::test_ok"]
        assert not any("Test:" in r for r in result)

    def test_empty_output(self):
        result = _parse_collect_output("")
        assert result == []

    def test_only_summary_lines(self):
        """When output only contains summary (no individual IDs)."""
        output = "== 42 tests collected in 1.23s ==\n"
        result = _parse_collect_output(output)
        assert result == []

    def test_mixed_with_errors(self):
        """Error lines should be filtered out."""
        output = (
            "ERROR: could not collect tests\n"
            "tests/test_a.py::test_one\n"
            "== 1 test collected, 3 errors ==\n"
        )
        result = _parse_collect_output(output)
        assert result == ["tests/test_a.py::test_one"]

    def test_separator_lines_filtered(self):
        """Lines starting with = or - are separator/summary lines."""
        output = (
            "======= test session starts =======\n"
            "tests/test_a.py::test_one\n"
            "-------\n"
            "== 1 test collected ==\n"
        )
        result = _parse_collect_output(output)
        assert result == ["tests/test_a.py::test_one"]

    def test_parametrized_ids(self):
        """Parametrized test IDs with brackets."""
        output = "tests/test_foo.py::test_bar[param1-param2]\n"
        result = _parse_collect_output(output)
        assert result == ["tests/test_foo.py::test_bar[param1-param2]"]

    def test_per_file_summary_fallback(self):
        """Case A (``adaptive-classifier`` / ``bake``): pytest emits a
        ``path: count`` summary instead of individual node IDs.

        Falls back to file-level IDs (which pytest accepts as run targets).
        """
        output = (
            "tests/test_classifier.py: 11\n"
            "tests/test_enterprise_classifiers_integration.py: 104\n"
            "tests/test_memory.py: 13\n"
            "\n"
            "== 175 tests collected in 2.34s ==\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/test_classifier.py",
            "tests/test_enterprise_classifiers_integration.py",
            "tests/test_memory.py",
        ]

    def test_per_file_summary_with_parametrize_suffix(self):
        """Summary lines may include a parametrize suffix in brackets."""
        output = (
            "tests/test_foo.py[fast]: 5\n"
            "tests/test_bar.py: 7\n"
        )
        result = _parse_collect_output(output)
        assert result == ["tests/test_foo.py", "tests/test_bar.py"]

    def test_per_test_ids_preferred_over_summary(self):
        """When both per-test IDs and summary lines appear, per-test IDs win.

        Per-test IDs preserve harness ``fail_to_pass`` / ``pass_to_pass``
        granularity; the summary fallback would lose it.
        """
        output = (
            "tests/test_a.py::test_one\n"
            "tests/test_a.py::test_two\n"
            "tests/test_b.py: 5\n"
        )
        result = _parse_collect_output(output)
        assert result == [
            "tests/test_a.py::test_one",
            "tests/test_a.py::test_two",
        ]

    def test_summary_does_not_match_module_docstrings(self):
        """Lines like ``description: foo`` (no .py, no integer) are ignored."""
        output = (
            "description: This is a docstring\n"
            "author: someone\n"
            "file.py: not_a_count\n"
        )
        result = _parse_collect_output(output)
        assert result == []

    def test_summary_requires_path_ends_in_py(self):
        """Only ``.py`` paths should be picked up as summary entries."""
        output = (
            "some/path/data.txt: 11\n"
            "tests/test_real.py: 11\n"
        )
        result = _parse_collect_output(output)
        assert result == ["tests/test_real.py"]

    def test_summary_ignores_lines_without_whitespace_after_colon(self):
        """``path:11`` (no space) should NOT match — reserved for IDs like ``ns::class``."""
        output = "tests/test_foo.py:11\n"
        result = _parse_collect_output(output)
        assert result == []
