"""Tests for type coercion and validation edge cases.

Covers: YAML type coercion, config validation, Pydantic model edges.
"""

import yaml

import pytest


# ---------------------------------------------------------------------------
# YAML type coercion — cli.py reads YAML configs via yaml.safe_load
# ---------------------------------------------------------------------------
class TestYamlTypeCoercion:
    def test_yes_no_as_strings_not_booleans(self):
        """YAML 1.1 treats yes/no as booleans. Verify safe_load behavior."""
        data = yaml.safe_load("value: yes")
        # In PyYAML safe_load, 'yes' becomes True
        assert data["value"] is True

    def test_quoted_yes_stays_string(self):
        data = yaml.safe_load('value: "yes"')
        assert data["value"] == "yes"

    def test_numeric_strings_become_numbers(self):
        data = yaml.safe_load("version: 1.0")
        assert isinstance(data["version"], float)

    def test_octal_string_interpretation(self):
        """YAML 1.1 octal: 0777 becomes 511."""
        data = yaml.safe_load("perms: 0777")
        assert data["perms"] == 511

    def test_null_values(self):
        data = yaml.safe_load("key: null")
        assert data["key"] is None

    def test_tilde_is_null(self):
        data = yaml.safe_load("key: ~")
        assert data["key"] is None

    def test_empty_value_is_none(self):
        data = yaml.safe_load("key: ")
        assert data["key"] is None

    def test_scientific_notation(self):
        """PyYAML safe_load treats 1e10 as a string, not float."""
        data = yaml.safe_load("val: 1e10")
        # In PyYAML, unquoted 1e10 is a string, not float
        assert isinstance(data["val"], str)


# ---------------------------------------------------------------------------
# validate_commit0_config — type checking
# ---------------------------------------------------------------------------
CLI_MODULE = "commit0.cli"


class TestConfigTypeValidation:
    def test_valid_config_passes(self, tmp_path):
        from commit0.cli import validate_commit0_config

        base = tmp_path / "repos"
        base.mkdir()
        config = {
            "dataset_name": "test/dataset",
            "dataset_split": "train",
            "repo_split": "all",
            "base_dir": str(base),
        }
        # Should not raise
        validate_commit0_config(config, ".commit0.yaml")

    def test_missing_key_raises_value_error(self, tmp_path):
        from commit0.cli import validate_commit0_config

        config = {
            "dataset_name": "test/dataset",
            # missing dataset_split, repo_split, base_dir
        }
        with pytest.raises(ValueError, match="missing required keys"):
            validate_commit0_config(config, ".commit0.yaml")

    def test_wrong_type_raises_type_error(self, tmp_path):
        from commit0.cli import validate_commit0_config

        base = tmp_path / "repos"
        base.mkdir()
        config = {
            "dataset_name": 12345,  # should be str
            "dataset_split": "train",
            "repo_split": "all",
            "base_dir": str(base),
        }
        with pytest.raises(TypeError):
            validate_commit0_config(config, ".commit0.yaml")

    def test_base_dir_not_exists_raises(self, tmp_path):
        from commit0.cli import validate_commit0_config

        config = {
            "dataset_name": "test/dataset",
            "dataset_split": "train",
            "repo_split": "all",
            "base_dir": str(tmp_path / "nonexistent"),
        }
        with pytest.raises(FileNotFoundError):
            validate_commit0_config(config, ".commit0.yaml")

    def test_none_value_for_required_key(self, tmp_path):
        from commit0.cli import validate_commit0_config

        config = {
            "dataset_name": None,
            "dataset_split": "train",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        with pytest.raises(TypeError):
            validate_commit0_config(config, ".commit0.yaml")

    def test_extra_keys_are_ok(self, tmp_path):
        from commit0.cli import validate_commit0_config

        base = tmp_path / "repos"
        base.mkdir()
        config = {
            "dataset_name": "test/dataset",
            "dataset_split": "train",
            "repo_split": "all",
            "base_dir": str(base),
            "extra_field": "should be fine",
        }
        # Should not raise
        validate_commit0_config(config, ".commit0.yaml")


# ---------------------------------------------------------------------------
# Commit0Config dataclass — type acceptance
# ---------------------------------------------------------------------------
class TestCommit0Config:
    def test_all_fields(self):
        from commit0.configs.config_class import Commit0Config

        cfg = Commit0Config(
            dataset_name="ds",
            dataset_split="train",
            base_dir="/tmp",
            repo_split="all",
            num_workers=4,
            backend="local",
            timeout=300,
            num_cpus=2,
            github_token="ghp_abc",
        )
        assert cfg.dataset_name == "ds"
        assert cfg.github_token == "ghp_abc"

    def test_github_token_optional(self):
        from commit0.configs.config_class import Commit0Config

        cfg = Commit0Config(
            dataset_name="ds",
            dataset_split="train",
            base_dir="/tmp",
            repo_split="all",
            num_workers=4,
            backend="local",
            timeout=300,
            num_cpus=2,
            github_token=None,
        )
        assert cfg.github_token is None

    def test_dataclass_accepts_wrong_types(self):
        """Dataclasses don't enforce types at runtime."""
        from commit0.configs.config_class import Commit0Config

        cfg = Commit0Config(
            dataset_name=123,
            dataset_split=456,
            base_dir=789,
            repo_split=None,
            num_workers="four",
            backend=True,
            timeout="long",
            num_cpus=[],
            github_token=42,
        )
        # No error — that is the current behavior (no validation)
        assert cfg.dataset_name == 123


# ---------------------------------------------------------------------------
# RepoInstance / SimpleInstance Pydantic models
# ---------------------------------------------------------------------------
class TestPydanticModels:
    def test_repo_instance_valid(self):
        from commit0.harness.constants import RepoInstance

        inst = RepoInstance(
            instance_id="test_1",
            repo="org/repo",
            base_commit="abc123",
            reference_commit="def456",
            setup={},
            test={},
            src_dir="src",
        )
        assert inst.repo == "org/repo"

    def test_repo_instance_missing_field(self):
        from commit0.harness.constants import RepoInstance
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            RepoInstance(instance_id="test_1")

    def test_simple_instance_valid(self):
        from commit0.harness.constants import SimpleInstance

        inst = SimpleInstance(
            instance_id="s1",
            prompt="Do something",
            canonical_solution="pass",
            test="assert True",
        )
        assert inst.prompt == "Do something"
