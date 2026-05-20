"""Pure-logic tests for tools/generate_test_ids_c.py.

Loaded by file path because ``tools`` is not an importable package.
Covers ctest output parsing (json + plain) and the bz2 writer.
"""

from __future__ import annotations

import bz2
import importlib.util
import json
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "generate_test_ids_c", _TOOLS / "generate_test_ids_c.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gti = _load_module()


class TestParseJson:
    def test_extracts_test_names(self) -> None:
        stdout = json.dumps(
            {"tests": [{"name": "t_a"}, {"name": "t_b"}, {"name": ""}]}
        )
        assert gti._parse_ctest_show_only(stdout) == ["t_a", "t_b"]

    def test_non_json_falls_back_to_plain(self) -> None:
        stdout = "  Test #1: t_plain\n  Test #2: t_other\n"
        assert gti._parse_ctest_show_only(stdout) == ["t_plain", "t_other"]


class TestParsePlain:
    def test_parses_test_hash_lines(self) -> None:
        stdout = "Total Tests: 2\n  Test #1: t_one\n  Test #2: t_two\n"
        assert gti._parse_ctest_show_only_plain(stdout) == ["t_one", "t_two"]

    def test_ignores_unrelated_lines(self) -> None:
        assert gti._parse_ctest_show_only_plain("no tests here\n") == []


class TestWriteBz2:
    def test_roundtrip(self, tmp_path: Path) -> None:
        ids = ["t_a", "t_b", "t_c"]
        target = tmp_path / "nested" / "ids.bz2"
        gti.write_bz2(ids, target)
        assert target.exists()
        with bz2.open(target, "rb") as fh:
            payload = fh.read().decode("utf-8")
        assert payload.split("\n") == ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
