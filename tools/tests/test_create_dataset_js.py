from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.create_dataset_js import (
    DEFAULT_HF_REPO,
    create_js_hf_dataset_dict,
    generate_commit0_js_yaml,
    generate_js_split_constants,
    upload_js_to_huggingface,
    validate_js_dataset,
    validate_js_entry,
)


def _valid_entry() -> dict:
    return {
        "instance_id": "commit-0/p-queue",
        "repo": "Zahgon/p-queue",
        "original_repo": "sindresorhus/p-queue",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node_version": 20,
            "install": "npm install",
            "packages": [],
            "pre_install": [],
            "specification": "",
        },
        "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
        "src_dir": "src",
        "language": "javascript",
        "test_framework": "jest",
        "package_manager": "npm",
    }


class TestValidateJsEntry:
    def test_valid_entry(self) -> None:
        assert validate_js_entry(_valid_entry(), 0) == []

    def test_missing_language(self) -> None:
        entry = _valid_entry()
        del entry["language"]
        issues = validate_js_entry(entry, 0)
        assert any("language" in i for i in issues)

    def test_wrong_language(self) -> None:
        entry = _valid_entry()
        entry["language"] = "python"
        issues = validate_js_entry(entry, 0)
        assert any("javascript" in i or "js" in i for i in issues)

    @pytest.mark.parametrize("lang", ["javascript", "js"])
    def test_accepts_both_js_aliases(self, lang: str) -> None:
        entry = _valid_entry()
        entry["language"] = lang
        assert validate_js_entry(entry, 0) == []

    def test_missing_setup(self) -> None:
        entry = _valid_entry()
        del entry["setup"]
        issues = validate_js_entry(entry, 0)
        assert any("setup" in i for i in issues)

    def test_bad_node_version(self) -> None:
        entry = _valid_entry()
        entry["setup"]["node_version"] = 18
        issues = validate_js_entry(entry, 0)
        assert any("18" in i for i in issues)

    @pytest.mark.parametrize("v", [20, 22])
    def test_valid_node_versions(self, v: int) -> None:
        entry = _valid_entry()
        entry["setup"]["node_version"] = v
        assert validate_js_entry(entry, 0) == []

    def test_short_base_commit(self) -> None:
        entry = _valid_entry()
        entry["base_commit"] = "abc"
        issues = validate_js_entry(entry, 0)
        assert any("base_commit" in i for i in issues)

    def test_unsupported_framework(self) -> None:
        entry = _valid_entry()
        entry["test_framework"] = "qunit"
        issues = validate_js_entry(entry, 0)
        assert any("qunit" in i for i in issues)

    @pytest.mark.parametrize("fw", ["jest", "mocha", "vitest", "node_test"])
    def test_valid_frameworks(self, fw: str) -> None:
        entry = _valid_entry()
        entry["test_framework"] = fw
        if fw == "node_test":
            entry["test"]["test_cmd"] = "node --test"
        elif fw == "mocha":
            entry["test"]["test_cmd"] = "npx mocha"
        elif fw == "vitest":
            entry["test"]["test_cmd"] = "npx vitest"
        assert validate_js_entry(entry, 0) == []

    @pytest.mark.parametrize("pm", ["npm", "pnpm", "yarn", "bun"])
    def test_valid_package_managers(self, pm: str) -> None:
        entry = _valid_entry()
        entry["package_manager"] = pm
        entry["setup"]["install"] = f"{pm} install"
        assert validate_js_entry(entry, 0) == []

    def test_unsupported_package_manager(self) -> None:
        entry = _valid_entry()
        entry["package_manager"] = "deno"
        issues = validate_js_entry(entry, 0)
        assert any("deno" in i for i in issues)

    def test_npm_test_command_rejected(self) -> None:
        entry = _valid_entry()
        entry["test"]["test_cmd"] = "npm test"
        issues = validate_js_entry(entry, 0)
        assert any("test_cmd prefix" in i and "npm" in i for i in issues), (
            f"'npm test' should be rejected because 'npm' is not in _TEST_CMD_PREFIXES; "
            f"use 'npx', 'pnpm', 'yarn', 'bunx', 'node', 'jest', 'vitest', or 'mocha'. "
            f"issues={issues}"
        )

    @pytest.mark.parametrize(
        "test_cmd",
        [
            "npx jest",
            "pnpm exec jest",
            "yarn jest",
            "bunx jest",
            "node --test",
            "jest",
            "vitest",
            "mocha",
        ],
    )
    def test_allowed_test_cmd_prefixes(self, test_cmd: str) -> None:
        entry = _valid_entry()
        entry["test"]["test_cmd"] = test_cmd
        issues = validate_js_entry(entry, 0)
        assert not any("test_cmd prefix" in i for i in issues), (
            f"{test_cmd!r} should be accepted; got issues={issues}"
        )

    @pytest.mark.parametrize(
        "test_cmd",
        [
            "npm test",
            "npm run test",
            "deno test",
            "rake test",
            "make test",
            "./run-tests.sh",
            "$(echo evil)",
        ],
    )
    def test_disallowed_test_cmd_prefixes(self, test_cmd: str) -> None:
        entry = _valid_entry()
        entry["test"]["test_cmd"] = test_cmd
        issues = validate_js_entry(entry, 0)
        assert any("test_cmd prefix" in i for i in issues), (
            f"{test_cmd!r} should be rejected; got issues={issues}"
        )

    def test_shell_metacharacters_in_install(self) -> None:
        entry = _valid_entry()
        entry["setup"]["install"] = "npm install; rm -rf /"
        issues = validate_js_entry(entry, 0)
        assert any("shell metacharacters" in i for i in issues)

    @pytest.mark.parametrize(
        "payload",
        [
            "npm install\n rm -rf /",
            "npm install\r evil",
            "npm install \\ evil",
            'npm install "evil"',
            "npm install 'evil'",
            "npm install\nrm -rf /",
            "npm install \\$(evil)",
            "npm install\trm -rf /",
        ],
    )
    def test_extended_shell_metacharacters_in_install(self, payload: str) -> None:
        entry = _valid_entry()
        entry["setup"]["install"] = payload
        issues = validate_js_entry(entry, 0)
        assert any("shell metacharacters" in i for i in issues), (
            f"payload {payload!r} did not trip the danger filter; issues={issues}"
        )


class TestValidateJsDataset:
    def test_all_valid(self) -> None:
        e1 = _valid_entry()
        e2 = _valid_entry()
        e2["instance_id"] = "commit-0/other"
        valid, issues = validate_js_dataset([e1, e2])
        assert len(valid) == 2
        assert issues == []

    def test_mixed(self) -> None:
        good = _valid_entry()
        bad = _valid_entry()
        del bad["language"]
        valid, issues = validate_js_dataset([good, bad])
        assert len(valid) == 1
        assert len(issues) >= 1

    def test_empty(self) -> None:
        valid, issues = validate_js_dataset([])
        assert valid == []
        assert issues == []


class TestCreateJsHfDatasetDict:
    def test_schema_keys(self) -> None:
        result = create_js_hf_dataset_dict([_valid_entry()])
        assert len(result) == 1
        row = result[0]
        for k in (
            "instance_id",
            "repo",
            "original_repo",
            "base_commit",
            "reference_commit",
            "setup",
            "test",
            "src_dir",
            "language",
            "test_framework",
            "package_manager",
        ):
            assert k in row, f"missing key {k!r}"

    def test_test_framework_default_jest(self) -> None:
        entry = _valid_entry()
        del entry["test_framework"]
        result = create_js_hf_dataset_dict([entry])
        assert result[0]["test_framework"] == "jest"

    def test_package_manager_default_npm(self) -> None:
        entry = _valid_entry()
        del entry["package_manager"]
        result = create_js_hf_dataset_dict([entry])
        assert result[0]["package_manager"] == "npm"

    def test_instance_id_hyphen_format_preserved(self) -> None:
        result = create_js_hf_dataset_dict([_valid_entry()])
        assert result[0]["instance_id"].startswith("commit-0/")


class TestGenerateJsSplitConstants:
    def test_includes_split_name(self) -> None:
        code = generate_js_split_constants([_valid_entry()], split_name="tier1_js")
        assert 'JS_SPLIT["tier1_js"]' in code

    def test_individual_repo_split(self) -> None:
        code = generate_js_split_constants([_valid_entry()], split_name="tier1_js")
        assert 'JS_SPLIT["p-queue"]' in code


class TestGenerateCommit0JsYaml:
    def test_contains_dataset_name(self) -> None:
        yaml_text = generate_commit0_js_yaml(
            [_valid_entry()], split_name="tier1_js", dataset_name="org/ds"
        )
        assert "dataset_name: org/ds" in yaml_text

    def test_contains_language(self) -> None:
        yaml_text = generate_commit0_js_yaml(
            [_valid_entry()], split_name="tier1_js", dataset_name="org/ds"
        )
        assert "language: javascript" in yaml_text


class TestUploadHuggingface:
    def test_import_error_returns_silently(self) -> None:
        with patch.dict("sys.modules", {"datasets": None}):
            upload_js_to_huggingface([_valid_entry()], "fake/repo")

    def test_happy_path_flattens_dicts(self) -> None:
        mock_dataset = MagicMock()
        mock_cls = MagicMock()
        mock_cls.from_list.return_value = mock_dataset
        mock_datasets = MagicMock()
        mock_datasets.Dataset = mock_cls

        entry = _valid_entry()
        with patch.dict("sys.modules", {"datasets": mock_datasets}):
            upload_js_to_huggingface([entry], "my/repo", token="t")

        mock_cls.from_list.assert_called_once()
        row = mock_cls.from_list.call_args[0][0][0]
        assert isinstance(row["setup"], str)
        assert isinstance(row["test"], str)
        assert json.loads(row["setup"]) == entry["setup"]
        assert json.loads(row["test"]) == entry["test"]
        mock_dataset.push_to_hub.assert_called_once_with(
            "my/repo", split="test", token="t"
        )


class TestDefaultHfRepo:
    def test_default_repo_constant(self) -> None:
        assert DEFAULT_HF_REPO == "wentingzhao/commit0_js"


class TestMainCli:
    def test_main_writes_output(self, tmp_path: Path) -> None:
        from tools.create_dataset_js import main as create_main

        entries_file = tmp_path / "entries.json"
        entries_file.write_text(
            json.dumps([_valid_entry()]), encoding="utf-8"
        )
        output_file = tmp_path / "out.json"
        with patch(
            "sys.argv",
            ["prog", str(entries_file), "--output", str(output_file)],
        ):
            create_main()
        rows = json.loads(output_file.read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["instance_id"] == "commit-0/p-queue"

    def test_main_upload_missing_token_raises(self, tmp_path: Path) -> None:
        from tools.create_dataset_js import main as create_main

        entries_file = tmp_path / "entries.json"
        entries_file.write_text(
            json.dumps([_valid_entry()]), encoding="utf-8"
        )
        output_file = tmp_path / "out.json"
        with (
            patch(
                "sys.argv",
                [
                    "prog",
                    str(entries_file),
                    "--output",
                    str(output_file),
                    "--upload",
                ],
            ),
            patch.dict("os.environ", {}, clear=True),
        ):
            with pytest.raises(OSError, match="HF_TOKEN"):
                create_main()
