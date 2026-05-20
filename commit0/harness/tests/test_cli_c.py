"""Pure-logic tests for commit0.cli_c.

Covers the colour helper, split validation, and the config read/write
roundtrip. Typer command bodies (Docker-bound) are not exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from commit0.cli_c import (
    Colors,
    check_valid,
    highlight,
    read_commit0_c_config,
    validate_commit0_c_config,
    write_commit0_c_config,
)


class TestHighlight:
    def test_wraps_text_with_color_and_reset(self) -> None:
        out = highlight("hello", Colors.RED)
        assert "hello" in out
        assert out.startswith(Colors.RED)
        assert out.endswith(Colors.RESET)


class TestCheckValid:
    def test_valid_member_of_list_passes(self) -> None:
        check_valid("a", ["a", "b"])

    def test_valid_key_of_dict_passes(self) -> None:
        check_valid("c_lite", {"c_lite": ["cJSON"]})

    def test_invalid_raises_bad_parameter(self) -> None:
        with pytest.raises(typer.BadParameter):
            check_valid("missing", ["a", "b"])


class TestConfigValidation:
    def _valid(self, base_dir: str) -> dict:
        return {
            "dataset_name": "org/ds",
            "dataset_split": "test",
            "repo_split": "c_lite",
            "base_dir": base_dir,
        }

    def test_missing_key_raises_value_error(self, tmp_path: Path) -> None:
        cfg = self._valid(str(tmp_path))
        del cfg["repo_split"]
        with pytest.raises(ValueError):
            validate_commit0_c_config(cfg, "cfg.yaml")

    def test_wrong_type_raises_type_error(self, tmp_path: Path) -> None:
        cfg = self._valid(str(tmp_path))
        cfg["dataset_name"] = 123
        with pytest.raises(TypeError):
            validate_commit0_c_config(cfg, "cfg.yaml")

    def test_missing_base_dir_raises_file_not_found(self, tmp_path: Path) -> None:
        cfg = self._valid(str(tmp_path / "does-not-exist"))
        with pytest.raises(FileNotFoundError):
            validate_commit0_c_config(cfg, "cfg.yaml")

    def test_valid_config_passes(self, tmp_path: Path) -> None:
        validate_commit0_c_config(self._valid(str(tmp_path)), "cfg.yaml")


class TestConfigRoundtrip:
    def test_write_then_read_roundtrips(self, tmp_path: Path) -> None:
        cfg = {
            "dataset_name": "org/ds",
            "dataset_split": "test",
            "repo_split": "c_lite",
            "base_dir": str(tmp_path),
        }
        dot = tmp_path / ".commit0-c.yaml"
        write_commit0_c_config(str(dot), cfg)
        assert dot.exists()
        assert read_commit0_c_config(str(dot)) == cfg

    def test_read_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_commit0_c_config(str(tmp_path / "nope.yaml"))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
