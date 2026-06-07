from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

MODULE = "commit0.cli_java"


def _dataset_entry(repo: str = "org/mylib", original_repo: str = "") -> dict:
    return {
        "repo": repo,
        "original_repo": original_repo or repo,
        "instance_id": repo,
        "base_commit": "abc",
        "reference_commit": "def",
        "setup": {},
        "test": {},
        "src_dir": "src",
    }


class TestLoadConfig:
    def test_success(self, tmp_path: Path) -> None:
        import os

        cfg_path = tmp_path / ".commit0.java.yaml"
        cfg_path.write_text(yaml.dump({"java_version": "17", "repos_dir": "/r"}))
        orig_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            from commit0.cli_java import _load_config

            result = _load_config()
            assert result["java_version"] == "17"
        finally:
            os.chdir(orig_cwd)

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        import os
        import typer

        orig_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            from commit0.cli_java import _load_config

            with pytest.raises(typer.BadParameter, match="not found"):
                _load_config()
        finally:
            os.chdir(orig_cwd)

    def test_empty_yaml_returns_empty_dict(self, tmp_path: Path) -> None:
        import os

        cfg_path = tmp_path / ".commit0.java.yaml"
        cfg_path.write_text("")
        orig_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            from commit0.cli_java import _load_config

            result = _load_config()
            assert result == {}
        finally:
            os.chdir(orig_cwd)


class TestPromoteSetupFields:
    def test_promotes_java_version_from_setup(self) -> None:
        from commit0.cli_java import _promote_setup_fields

        instance: dict = {"setup": {"java_version": "11"}}
        _promote_setup_fields(instance, {})
        assert instance["java_version"] == "11"

    def test_defaults_from_config(self) -> None:
        from commit0.cli_java import _promote_setup_fields

        instance: dict = {"setup": {}}
        _promote_setup_fields(instance, {"java_version": "21"})
        assert instance["java_version"] == "21"

    def test_hardcoded_defaults_when_no_config(self) -> None:
        from commit0.cli_java import _promote_setup_fields

        instance: dict = {"setup": {}}
        _promote_setup_fields(instance, {})
        assert instance["java_version"] == "17"
        assert instance["build_system"] == "maven"
        assert instance["test_framework"] == "junit5"

    def test_instance_field_takes_precedence(self) -> None:
        from commit0.cli_java import _promote_setup_fields

        instance: dict = {"java_version": "11", "setup": {"java_version": "21"}}
        _promote_setup_fields(instance, {"java_version": "17"})
        assert instance["java_version"] == "11"

    def test_setup_overrides_config_default(self) -> None:
        from commit0.cli_java import _promote_setup_fields

        instance: dict = {"setup": {"build_system": "gradle"}}
        _promote_setup_fields(instance, {"build_system": "maven"})
        assert instance["build_system"] == "gradle"


class TestLoadInstance:
    def _run(self, repo_arg: str | None, entries: list) -> dict:
        cfg = {"dataset_name": "ds", "repos_dir": "/r"}
        from commit0.cli_java import _load_instance

        with (
            patch(f"{MODULE}._load_config", return_value=cfg),
            patch(
                "commit0.harness.utils.load_dataset_from_config",
                return_value=entries,
            ),
        ):
            return _load_instance(repo_arg)

    def test_found_by_exact_name(self) -> None:
        result = self._run("org/mylib", [_dataset_entry("org/mylib")])
        assert result["repo"] == "org/mylib"

    def test_found_by_short_name(self) -> None:
        result = self._run("mylib", [_dataset_entry("org/mylib")])
        assert result["repo"] == "org/mylib"

    def test_found_by_original_repo(self) -> None:
        entry = _dataset_entry("fork/mylib", original_repo="orig/mylib")
        result = self._run("orig/mylib", [entry])
        assert result["repo"] == "fork/mylib"

    def test_not_found_raises(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter, match="not found"):
            self._run("org/missing", [_dataset_entry("org/other")])

    def test_multiple_repos_without_repo_raises(self) -> None:
        import typer

        entries = [_dataset_entry("org/a"), _dataset_entry("org/b")]
        with pytest.raises(typer.BadParameter, match="Multiple repos"):
            self._run(None, entries)

    def test_single_repo_without_repo_arg(self) -> None:
        result = self._run(None, [_dataset_entry("org/only")])
        assert result["repo"] == "org/only"


class TestLoadDatasetMap:
    def test_basic_mapping(self) -> None:
        cfg = {"dataset_name": "ds", "java_version": "17"}
        entries = [_dataset_entry("org/a"), _dataset_entry("org/b")]
        from commit0.cli_java import _load_dataset_map

        with (
            patch(f"{MODULE}._load_config", return_value=cfg),
            patch(
                "commit0.harness.utils.load_dataset_from_config",
                return_value=entries,
            ),
        ):
            result = _load_dataset_map()
        assert "org/a" in result
        assert "org/b" in result
        assert result["org/a"]["java_version"] == "17"
