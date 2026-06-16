from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest
import yaml

import commit0.cli_js as cli_js
from commit0.cli_js import (
    check_valid_js,
    read_commit0_js_config_file,
    write_commit0_js_config_file,
)


CLI_JS_SOURCE = Path(cli_js.__file__).read_text(encoding="utf-8")
CLI_JS_TREE = ast.parse(CLI_JS_SOURCE)


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in cli_js.py")


class TestSafeYamlLoad:
    def test_no_yaml_load_only_safe_load(self) -> None:
        for node in ast.walk(CLI_JS_TREE):
            if isinstance(node, ast.Attribute):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "yaml"
                ):
                    assert node.attr != "load", (
                        f"cli_js must use yaml.safe_load, not yaml.load (line {node.lineno})"
                    )

    def test_safe_load_rejects_python_object_payload(self, tmp_path) -> None:
        cfg = tmp_path / "evil.yaml"
        cfg.write_text(
            "dataset_name: !!python/object/apply:os.system ['echo pwned']\n",
            encoding="utf-8",
        )
        with pytest.raises((yaml.YAMLError, ValueError, TypeError)):
            read_commit0_js_config_file(str(cfg))

    def test_safe_load_round_trips_normal_mapping(self, tmp_path) -> None:
        cfg = tmp_path / "good.yaml"
        cfg.write_text(
            "dataset_name: x.json\n"
            "dataset_split: test\n"
            "repo_split: all\n"
            "base_dir: repos\n",
            encoding="utf-8",
        )
        data = read_commit0_js_config_file(str(cfg))
        assert data["dataset_name"] == "x.json"
        assert data["repo_split"] == "all"


class TestReadConfigErrorPaths:
    def test_missing_file_raises_filenotfound(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            read_commit0_js_config_file(str(tmp_path / "nope.yaml"))

    def test_empty_file_raises_valueerror(self, tmp_path) -> None:
        cfg = tmp_path / "empty.yaml"
        cfg.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty or invalid"):
            read_commit0_js_config_file(str(cfg))

    def test_array_root_raises_valueerror(self, tmp_path) -> None:
        cfg = tmp_path / "list.yaml"
        cfg.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Expected a YAML mapping"):
            read_commit0_js_config_file(str(cfg))

    def test_missing_required_key_raises(self, tmp_path) -> None:
        cfg = tmp_path / "partial.yaml"
        cfg.write_text("dataset_name: x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required keys"):
            read_commit0_js_config_file(str(cfg))

    def test_unknown_key_raises(self, tmp_path) -> None:
        cfg = tmp_path / "extra.yaml"
        cfg.write_text(
            "dataset_name: x.json\n"
            "dataset_split: test\n"
            "repo_split: all\n"
            "base_dir: repos\n"
            "repository_split: typo\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown commit0.js.yaml keys"):
            read_commit0_js_config_file(str(cfg))


class TestWriteConfigErrorPaths:
    def test_writes_yaml_mapping(self, tmp_path) -> None:
        out = tmp_path / "out.yaml"
        write_commit0_js_config_file(
            str(out),
            {
                "dataset_name": "x.json",
                "dataset_split": "test",
                "repo_split": "all",
                "base_dir": "repos",
            },
        )
        loaded = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert loaded["dataset_name"] == "x.json"


class TestCheckValidJsHelper:
    def test_all_keyword_passes(self) -> None:
        check_valid_js("all", {"foo": ["a/b"]})

    def test_known_key_passes(self) -> None:
        check_valid_js("foo", {"foo": ["a/b"]})

    def test_unknown_raises_badparameter(self) -> None:
        import typer

        with pytest.raises(typer.BadParameter):
            check_valid_js("typo", {"foo": ["a/b"]})


class TestSetupDoesNotValidateRepoSplit:
    def test_setup_does_not_call_check_valid_js(self) -> None:
        setup_fn = _find_function(CLI_JS_TREE, "setup")
        for node in ast.walk(setup_fn):
            if isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "check_valid_js"
                ):
                    pytest.fail(
                        "setup command unexpectedly calls check_valid_js; "
                        "if this is intended, update CL-G7 documentation"
                    )

    def test_check_valid_js_is_exported_helper(self) -> None:
        assert callable(check_valid_js)
        sig = inspect.signature(check_valid_js)
        assert list(sig.parameters) == ["one", "total"]


class TestTestCommandPathNormalization:
    def _write_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / ".commit0.js.yaml"
        cfg.write_text(
            "dataset_name: x.json\n"
            "dataset_split: test\n"
            "repo_split: all\n"
            f"base_dir: {tmp_path / 'repos'}\n",
            encoding="utf-8",
        )
        return cfg

    def test_relative_traversal_path_is_resolved(
        self, tmp_path, monkeypatch
    ) -> None:
        cfg = self._write_config(tmp_path)
        captured: dict[str, str] = {}

        def fake_run(**kwargs: object) -> None:
            captured["repo_or_repo_dir"] = str(kwargs["repo_or_repo_dir"])

        monkeypatch.setattr(
            "commit0.harness.run_js_tests.main", fake_run, raising=False
        )

        cli_js.test(
            repo_or_repo_path="../../../etc/passwd",
            test_ids="",
            branch="",
            backend="local",
            timeout=1,
            num_cpus=1,
            rebuild=False,
            commit0_config_file=str(cfg),
            verbose=1,
        )

        assert ".." not in captured["repo_or_repo_dir"]
        assert os.path.isabs(captured["repo_or_repo_dir"])

    def test_bare_repo_name_unchanged(self, tmp_path, monkeypatch) -> None:
        cfg = self._write_config(tmp_path)
        captured: dict[str, str] = {}

        def fake_run(**kwargs: object) -> None:
            captured["repo_or_repo_dir"] = str(kwargs["repo_or_repo_dir"])

        monkeypatch.setattr(
            "commit0.harness.run_js_tests.main", fake_run, raising=False
        )

        cli_js.test(
            repo_or_repo_path="lodash",
            test_ids="",
            branch="",
            backend="local",
            timeout=1,
            num_cpus=1,
            rebuild=False,
            commit0_config_file=str(cfg),
            verbose=1,
        )

        assert captured["repo_or_repo_dir"] == "lodash"

    def test_absolute_path_is_normalized(self, tmp_path, monkeypatch) -> None:
        cfg = self._write_config(tmp_path)
        real_repo = tmp_path / "some_repo"
        real_repo.mkdir()
        captured: dict[str, str] = {}

        def fake_run(**kwargs: object) -> None:
            captured["repo_or_repo_dir"] = str(kwargs["repo_or_repo_dir"])

        monkeypatch.setattr(
            "commit0.harness.run_js_tests.main", fake_run, raising=False
        )

        weird = f"{tmp_path}/./some_repo/../some_repo"
        cli_js.test(
            repo_or_repo_path=weird,
            test_ids="",
            branch="",
            backend="local",
            timeout=1,
            num_cpus=1,
            rebuild=False,
            commit0_config_file=str(cfg),
            verbose=1,
        )

        assert captured["repo_or_repo_dir"] == str(real_repo)
