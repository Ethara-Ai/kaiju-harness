"""Non-Docker wiring test for the C harness.

Asserts that spec generation, split resolution, and ctest-result parsing
compose correctly end-to-end without touching Docker or the network.
"""

from __future__ import annotations

import pytest

from commit0.harness.c_test_parser import (
    parse_ctest_junit,
    summarize_ctest_results,
)
from commit0.harness.constants import TestStatus
from commit0.harness.constants_c import C_SPLIT, resolve_c_split
from commit0.harness.spec_c import make_c_spec


def _instance(**overrides) -> dict:
    inst = {
        "instance_id": "cjson-1",
        "repo": "commit0/cjson",
        "base_commit": "abcdef1234567",
        "reference_commit": "1234567abcdef",
        "setup": {
            "build_system": "cmake",
            "apt": [],
            "cmake_flags": "-DENABLE_X=1 -DENABLE_Y=0",
            "pre_install": [],
        },
        "test": {"framework": "ctest", "test_cmd": "ctest"},
        "src_dir": ".",
    }
    inst.update(overrides)
    return inst


class TestSpecWiring:
    def test_setup_script_orders_clone_reset_build(self) -> None:
        spec = make_c_spec(_instance(), absolute=True)
        script = spec.setup_script
        assert "git clone" in script
        assert "git reset --hard" in script
        assert "rm -rf build" in script
        # reset must precede the cmake build invocation.
        assert script.index("git reset --hard") < script.index("cmake")

    def test_eval_script_short_circuits_on_compile_failure(self) -> None:
        spec = make_c_spec(_instance(), absolute=True)
        eval_script = spec.eval_script
        assert "COMPILE_FAILED" in eval_script
        assert "test_exit_code.txt" in eval_script
        assert "ctest" in eval_script

    def test_cmake_flags_are_individually_quoted(self) -> None:
        spec = make_c_spec(_instance(), absolute=True)
        # Per-token shlex.quote keeps the two flags as separate tokens
        # rather than collapsing them into one quoted string.
        assert "-DENABLE_X=1 -DENABLE_Y=0" in spec.setup_script

    def test_absolute_vs_relative_repo_directory(self) -> None:
        assert make_c_spec(_instance(), absolute=True).repo_directory == "/testbed"
        assert make_c_spec(_instance(), absolute=False).repo_directory == "testbed"


class TestSplitResolution:
    def test_c_lite_resolves_to_cjson(self) -> None:
        assert C_SPLIT["c_lite"] == ["cJSON"]
        resolved = resolve_c_split("dummy_dataset")
        assert resolved["c_lite"] == ["cJSON"]


class TestParserComposition:
    def test_realistic_ctest_xml_roundtrips(self) -> None:
        xml = """<?xml version="1.0"?>
        <testsuite name="ctest" tests="3">
          <testcase name="parse_object"/>
          <testcase name="parse_array"><failure message="x"/></testcase>
          <testcase name="parse_null"><skipped/></testcase>
        </testsuite>"""
        results = parse_ctest_junit(xml)
        assert results["parse_object"] == TestStatus.PASSED
        summary = summarize_ctest_results(results)
        assert summary["total"] == 3
        assert summary["passed"] == 1
        assert summary["failed"] == 1
        assert summary["skipped"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
