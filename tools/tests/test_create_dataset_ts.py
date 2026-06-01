import pytest

from tools.create_dataset_ts import (
    validate_ts_entry,
    validate_ts_dataset,
    create_ts_hf_dataset_dict,
)


def _make_valid_ts_entry() -> dict:
    return {
        "instance_id": "commit-0/some-lib",
        "repo": "Zahgon/some-lib",
        "original_repo": "owner/some-lib",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node_version": "20",
            "install": "npm install",
            "packages": ["jest"],
            "pre_install": [],
            "specification": "",
        },
        "test": {
            "test_cmd": "npx jest",
            "test_dir": "__tests__",
        },
        "src_dir": "src",
        "language": "typescript",
        "test_framework": "jest",
    }


def test_validate_ts_entry_valid():
    entry = _make_valid_ts_entry()
    assert validate_ts_entry(entry, 0) == []


def test_validate_ts_entry_missing_language():
    entry = _make_valid_ts_entry()
    del entry["language"]
    issues = validate_ts_entry(entry, 0)
    assert len(issues) == 1
    assert "language" in issues[0]


def test_validate_ts_entry_wrong_language():
    entry = _make_valid_ts_entry()
    entry["language"] = "python"
    issues = validate_ts_entry(entry, 0)
    assert len(issues) == 1
    assert "typescript" in issues[0]


def test_validate_ts_entry_missing_setup():
    entry = _make_valid_ts_entry()
    del entry["setup"]
    issues = validate_ts_entry(entry, 0)
    assert len(issues) == 1
    assert "setup" in issues[0]


def test_validate_ts_entry_bad_node_version():
    entry = _make_valid_ts_entry()
    entry["setup"]["node_version"] = "14"
    issues = validate_ts_entry(entry, 0)
    assert len(issues) == 1
    assert "14" in issues[0]


@pytest.mark.parametrize("version", ["20", "22"])
def test_validate_ts_entry_valid_node_versions(version: str) -> None:
    entry = _make_valid_ts_entry()
    entry["setup"]["node_version"] = version
    assert validate_ts_entry(entry, 0) == []


def test_validate_ts_entry_short_commit():
    entry = _make_valid_ts_entry()
    entry["base_commit"] = "abc"
    issues = validate_ts_entry(entry, 0)
    assert len(issues) == 1
    assert "base_commit" in issues[0]


def test_validate_ts_entry_bad_framework():
    entry = _make_valid_ts_entry()
    entry["test_framework"] = "mocha"
    issues = validate_ts_entry(entry, 0)
    assert len(issues) == 1
    assert "mocha" in issues[0]


def test_validate_ts_entry_missing_test_fields():
    entry = _make_valid_ts_entry()
    entry["test"] = {}
    issues = validate_ts_entry(entry, 0)
    assert any("test_cmd" in i for i in issues)
    assert any("test_dir" in i for i in issues)


def test_validate_ts_dataset_mixed():
    good1 = _make_valid_ts_entry()
    good2 = _make_valid_ts_entry()
    good2["instance_id"] = "commit-0/other-lib"
    bad = _make_valid_ts_entry()
    del bad["language"]

    valid, all_issues = validate_ts_dataset([good1, bad, good2])
    assert len(valid) == 2
    assert len(all_issues) >= 1


def test_create_ts_hf_dataset_dict():
    entries = [_make_valid_ts_entry()]
    result = create_ts_hf_dataset_dict(entries)
    assert len(result) == 1
    assert set(result[0].keys()) == {
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
    }
    assert result[0]["language"] == "typescript"
    assert result[0]["test_framework"] == "jest"


def test_create_ts_hf_dataset_dict_preserves_all():
    entry = _make_valid_ts_entry()
    entry["test_framework"] = "vitest"
    result = create_ts_hf_dataset_dict([entry])
    assert result[0]["test_framework"] == "vitest"
    assert result[0]["instance_id"] == entry["instance_id"]
    assert result[0]["repo"] == entry["repo"]
    assert result[0]["original_repo"] == entry["original_repo"]
    assert result[0]["base_commit"] == entry["base_commit"]
    assert result[0]["reference_commit"] == entry["reference_commit"]
    assert result[0]["setup"] == entry["setup"]
    assert result[0]["test"] == entry["test"]
    assert result[0]["src_dir"] == entry["src_dir"]
    assert result[0]["language"] == entry["language"]


# ---------------------------------------------------------------------------
# Type mismatch validation
# ---------------------------------------------------------------------------


def test_validate_ts_entry_setup_wrong_type():
    entry = _make_valid_ts_entry()
    entry["setup"] = ["not", "a", "dict"]
    issues = validate_ts_entry(entry, 0)
    assert any("setup" in i and "dict" in i for i in issues)


def test_validate_ts_entry_test_wrong_type():
    entry = _make_valid_ts_entry()
    entry["test"] = "not-a-dict"
    issues = validate_ts_entry(entry, 0)
    assert any("test" in i and "dict" in i for i in issues)


def test_validate_ts_entry_base_commit_wrong_type():
    entry = _make_valid_ts_entry()
    entry["base_commit"] = 123
    with pytest.raises(TypeError):
        validate_ts_entry(entry, 0)


def test_validate_ts_entry_repo_wrong_type():
    entry = _make_valid_ts_entry()
    entry["repo"] = 42
    issues = validate_ts_entry(entry, 0)
    assert any("repo" in i and "str" in i for i in issues)


# ---------------------------------------------------------------------------
# Install cmd validation edge cases
# ---------------------------------------------------------------------------


def test_validate_ts_entry_empty_install_cmd():
    """Empty install cmd should not trigger package-manager error."""
    entry = _make_valid_ts_entry()
    entry["setup"]["install"] = ""
    assert validate_ts_entry(entry, 0) == []


def test_validate_ts_entry_install_cmd_whitespace_only():
    """Whitespace-only install cmd has no words → first_word is empty → triggers error."""
    entry = _make_valid_ts_entry()
    entry["setup"]["install"] = "   "
    issues = validate_ts_entry(entry, 0)
    assert any("Invalid package manager" in i for i in issues)


@pytest.mark.parametrize("mgr", ["npm", "yarn", "pnpm", "bun"])
def test_validate_ts_entry_valid_package_managers(mgr: str) -> None:
    entry = _make_valid_ts_entry()
    entry["setup"]["install"] = f"{mgr} install"
    assert validate_ts_entry(entry, 0) == []


def test_validate_ts_entry_invalid_package_manager():
    entry = _make_valid_ts_entry()
    entry["setup"]["install"] = "pip install something"
    issues = validate_ts_entry(entry, 0)
    assert any("Invalid package manager" in i for i in issues)
    assert any("pip" in i for i in issues)


# ---------------------------------------------------------------------------
# reference_commit too short
# ---------------------------------------------------------------------------


def test_validate_ts_entry_short_reference_commit():
    entry = _make_valid_ts_entry()
    entry["reference_commit"] = "abc"
    issues = validate_ts_entry(entry, 0)
    assert any("reference_commit" in i for i in issues)


# ---------------------------------------------------------------------------
# validate_ts_dataset edge cases
# ---------------------------------------------------------------------------


def test_validate_ts_dataset_all_valid():
    e1 = _make_valid_ts_entry()
    e2 = _make_valid_ts_entry()
    e2["instance_id"] = "commit-0/other-lib"
    valid, issues = validate_ts_dataset([e1, e2])
    assert len(valid) == 2
    assert issues == []


def test_validate_ts_dataset_all_invalid():
    bad1 = _make_valid_ts_entry()
    del bad1["language"]
    bad2 = _make_valid_ts_entry()
    bad2["language"] = "python"
    valid, issues = validate_ts_dataset([bad1, bad2])
    assert len(valid) == 0
    assert len(issues) >= 2


def test_validate_ts_dataset_empty():
    valid, issues = validate_ts_dataset([])
    assert valid == []
    assert issues == []


# ---------------------------------------------------------------------------
# create_ts_hf_dataset_dict: missing test_framework defaults to "jest"
# ---------------------------------------------------------------------------


def test_create_ts_hf_dataset_dict_missing_test_framework():
    entry = _make_valid_ts_entry()
    del entry["test_framework"]
    result = create_ts_hf_dataset_dict([entry])
    assert result[0]["test_framework"] == "jest"


# ---------------------------------------------------------------------------
# upload_ts_to_huggingface
# ---------------------------------------------------------------------------

import json
from unittest.mock import patch, MagicMock

from tools.create_dataset_ts import upload_ts_to_huggingface


def test_upload_ts_to_huggingface_import_error():
    """When 'datasets' is not importable, function logs error and returns."""
    entry = _make_valid_ts_entry()
    hf_entries = create_ts_hf_dataset_dict([entry])

    with patch.dict("sys.modules", {"datasets": None}):
        upload_ts_to_huggingface(hf_entries, "fake/repo", token="tok")


def test_upload_ts_to_huggingface_success():
    """Happy path: Dataset.from_list and push_to_hub are called correctly."""
    entry = _make_valid_ts_entry()
    hf_entries = create_ts_hf_dataset_dict([entry])

    mock_ds_instance = MagicMock()
    mock_dataset_cls = MagicMock()
    mock_dataset_cls.from_list.return_value = mock_ds_instance

    mock_datasets_module = MagicMock()
    mock_datasets_module.Dataset = mock_dataset_cls

    with patch.dict("sys.modules", {"datasets": mock_datasets_module}):
        upload_ts_to_huggingface(hf_entries, "my/repo", token="my-token")

    mock_dataset_cls.from_list.assert_called_once()
    call_arg = mock_dataset_cls.from_list.call_args[0][0]
    assert len(call_arg) == 1
    assert isinstance(call_arg[0]["setup"], str)
    assert isinstance(call_arg[0]["test"], str)
    parsed_setup = json.loads(call_arg[0]["setup"])
    assert parsed_setup == entry["setup"]

    mock_ds_instance.push_to_hub.assert_called_once_with(
        "my/repo", split="test", token="my-token"
    )


def test_upload_ts_to_huggingface_flattens_dicts():
    """Setup and test dicts are json.dumps'd, language defaults to typescript."""
    entry = _make_valid_ts_entry()
    del entry["test_framework"]
    hf_entries = create_ts_hf_dataset_dict([entry])

    mock_ds_instance = MagicMock()
    mock_dataset_cls = MagicMock()
    mock_dataset_cls.from_list.return_value = mock_ds_instance

    mock_datasets_module = MagicMock()
    mock_datasets_module.Dataset = mock_dataset_cls

    with patch.dict("sys.modules", {"datasets": mock_datasets_module}):
        upload_ts_to_huggingface(hf_entries, "repo/id")

    flat = mock_dataset_cls.from_list.call_args[0][0][0]
    assert flat["language"] == "typescript"
    assert flat["test_framework"] == "jest"


# ---------------------------------------------------------------------------
# main() CLI flow
# ---------------------------------------------------------------------------

from tools.create_dataset_ts import main


def test_main_happy_path(tmp_path):
    """main() reads input, validates, writes output JSON."""
    entry = _make_valid_ts_entry()
    input_file = tmp_path / "entries.json"
    input_file.write_text(json.dumps([entry]))
    output_file = tmp_path / "output.json"

    with patch(
        "sys.argv",
        ["prog", str(input_file), "--output", str(output_file)],
    ):
        main()

    result = json.loads(output_file.read_text())
    assert len(result) == 1
    assert result[0]["instance_id"] == entry["instance_id"]
    assert result[0]["test_framework"] == "jest"


def test_main_no_valid_entries_exits_early(tmp_path):
    """When all entries are invalid, main() logs error and returns without writing."""
    bad_entry = {"language": "python"}
    input_file = tmp_path / "entries.json"
    input_file.write_text(json.dumps([bad_entry]))
    output_file = tmp_path / "output.json"

    with patch(
        "sys.argv",
        ["prog", str(input_file), "--output", str(output_file)],
    ):
        main()

    assert not output_file.exists()


def test_main_upload_missing_token_raises(tmp_path):
    """--upload without HF_TOKEN or --hf-token raises EnvironmentError."""
    entry = _make_valid_ts_entry()
    input_file = tmp_path / "entries.json"
    input_file.write_text(json.dumps([entry]))
    output_file = tmp_path / "output.json"

    with (
        patch(
            "sys.argv",
            ["prog", str(input_file), "--output", str(output_file), "--upload"],
        ),
        patch.dict("os.environ", {}, clear=True),
    ):
        import os as _os

        _os.environ.pop("HF_TOKEN", None)
        with pytest.raises(EnvironmentError, match="HF_TOKEN"):
            main()


def test_main_upload_with_token(tmp_path):
    """--upload with --hf-token calls upload_ts_to_huggingface."""
    entry = _make_valid_ts_entry()
    input_file = tmp_path / "entries.json"
    input_file.write_text(json.dumps([entry]))
    output_file = tmp_path / "output.json"

    with (
        patch(
            "sys.argv",
            [
                "prog",
                str(input_file),
                "--output",
                str(output_file),
                "--upload",
                "--hf-token",
                "fake-token",
                "--hf-repo",
                "test/repo",
            ],
        ),
        patch("tools.create_dataset_ts.upload_ts_to_huggingface") as mock_upload,
    ):
        main()

    mock_upload.assert_called_once()
    call_args = mock_upload.call_args
    assert call_args[0][1] == "test/repo"
    assert call_args[1]["token"] == "fake-token"


def test_main_patch_constants(tmp_path):
    """--patch-constants generates a split constants file."""
    entry = _make_valid_ts_entry()
    input_file = tmp_path / "entries.json"
    input_file.write_text(json.dumps([entry]))
    output_file = tmp_path / "output.json"

    with (
        patch(
            "sys.argv",
            [
                "prog",
                str(input_file),
                "--output",
                str(output_file),
                "--patch-constants",
                "--split-name",
                "test_split",
            ],
        ),
        patch(
            "tools.create_dataset_ts.generate_split_constants",
            return_value="# generated code\n",
        ) as mock_gen,
    ):
        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            main()
        finally:
            os.chdir(old_cwd)

    mock_gen.assert_called_once()
    args = mock_gen.call_args[0]
    assert len(args[0]) == 1
    assert args[1] == "test_split"
    constants_file = tmp_path / "split_test_split.py"
    assert constants_file.exists()


def test_main_generate_yaml(tmp_path):
    """--generate-yaml generates a .commit0 yaml config file."""
    entry = _make_valid_ts_entry()
    input_file = tmp_path / "entries.json"
    input_file.write_text(json.dumps([entry]))
    output_file = tmp_path / "output.json"

    with (
        patch(
            "sys.argv",
            [
                "prog",
                str(input_file),
                "--output",
                str(output_file),
                "--generate-yaml",
                "--split-name",
                "my_ts",
                "--hf-repo",
                "org/repo",
            ],
        ),
        patch(
            "tools.create_dataset_ts.generate_commit0_yaml",
            return_value="dataset: test\n",
        ) as mock_yaml,
    ):
        import os

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            main()
        finally:
            os.chdir(old_cwd)

    mock_yaml.assert_called_once()
    args = mock_yaml.call_args[0]
    assert len(args[0]) == 1
    assert args[1] == "my_ts"
    assert args[2] == "org/repo"
    yaml_file = tmp_path / ".commit0.my_ts.ts.yaml"
    assert yaml_file.exists()
