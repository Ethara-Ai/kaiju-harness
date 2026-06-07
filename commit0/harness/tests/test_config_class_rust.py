"""Tests for Commit0Config dataclass and config file handling."""

from dataclasses import fields, asdict
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Commit0Config dataclass
# ---------------------------------------------------------------------------
class TestCommit0ConfigDataclass:
    def test_all_fields_present(self):
        from commit0.configs.config_class import Commit0Config

        field_names = {f.name for f in fields(Commit0Config)}
        expected = {
            "dataset_name",
            "dataset_split",
            "base_dir",
            "repo_split",
            "num_workers",
            "backend",
            "timeout",
            "num_cpus",
            "github_token",
        }
        assert expected == field_names

    def test_create_minimal(self):
        from commit0.configs.config_class import Commit0Config

        cfg = Commit0Config(
            dataset_name="test/ds",
            dataset_split="train",
            base_dir="/tmp/repos",
            repo_split="all",
            num_workers=4,
            backend="local",
            timeout=300,
            num_cpus=2,
            github_token=None,
        )
        assert cfg.dataset_name == "test/ds"
        assert cfg.github_token is None

    def test_asdict(self):
        from commit0.configs.config_class import Commit0Config

        cfg = Commit0Config(
            dataset_name="ds",
            dataset_split="s",
            base_dir="/tmp",
            repo_split="all",
            num_workers=1,
            backend="local",
            timeout=60,
            num_cpus=1,
            github_token="tok",
        )
        d = asdict(cfg)
        assert isinstance(d, dict)
        assert d["dataset_name"] == "ds"
        assert d["github_token"] == "tok"

    def test_equality(self):
        from commit0.configs.config_class import Commit0Config

        kwargs = dict(
            dataset_name="ds",
            dataset_split="s",
            base_dir="/tmp",
            repo_split="all",
            num_workers=1,
            backend="local",
            timeout=60,
            num_cpus=1,
            github_token=None,
        )
        assert Commit0Config(**kwargs) == Commit0Config(**kwargs)

    def test_inequality(self):
        from commit0.configs.config_class import Commit0Config

        kwargs = dict(
            dataset_name="ds",
            dataset_split="s",
            base_dir="/tmp",
            repo_split="all",
            num_workers=1,
            backend="local",
            timeout=60,
            num_cpus=1,
            github_token=None,
        )
        cfg1 = Commit0Config(**kwargs)
        kwargs["timeout"] = 999
        cfg2 = Commit0Config(**kwargs)
        assert cfg1 != cfg2

    def test_no_default_values(self):
        """All fields are required (no defaults)."""
        from commit0.configs.config_class import Commit0Config

        with pytest.raises(TypeError):
            Commit0Config()

    def test_field_types_not_enforced_at_runtime(self):
        from commit0.configs.config_class import Commit0Config

        cfg = Commit0Config(
            dataset_name=42,
            dataset_split=True,
            base_dir=None,
            repo_split=[],
            num_workers="many",
            backend=3.14,
            timeout=False,
            num_cpus={},
            github_token=0,
        )
        assert cfg.dataset_name == 42


# ---------------------------------------------------------------------------
# Config file read/write round-trip (cli.py)
# ---------------------------------------------------------------------------
CLI_MODULE = "commit0.cli"


class TestConfigFileRoundTrip:
    def test_write_and_read_config(self, tmp_path):
        from commit0.cli import write_commit0_config_file, read_commit0_config_file

        base = tmp_path / "repos"
        base.mkdir()
        config = {
            "dataset_name": "org/dataset",
            "dataset_split": "train",
            "repo_split": "all",
            "base_dir": str(base),
        }
        config_path = str(tmp_path / ".commit0.yaml")
        write_commit0_config_file(config_path, config)
        loaded = read_commit0_config_file(config_path)
        assert loaded["dataset_name"] == "org/dataset"
        assert loaded["base_dir"] == str(base)

    def test_write_creates_file(self, tmp_path):
        from commit0.cli import write_commit0_config_file

        path = str(tmp_path / "new_config.yaml")
        write_commit0_config_file(path, {"key": "value"})
        assert Path(path).exists()

    def test_read_nonexistent_raises(self, tmp_path):
        from commit0.cli import read_commit0_config_file

        with pytest.raises(FileNotFoundError):
            read_commit0_config_file(str(tmp_path / "nope.yaml"))

    def test_read_non_dict_raises(self, tmp_path):
        from commit0.cli import read_commit0_config_file

        f = tmp_path / ".commit0.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError):
            read_commit0_config_file(str(f))

    def test_read_empty_file_raises(self, tmp_path):
        from commit0.cli import read_commit0_config_file

        f = tmp_path / ".commit0.yaml"
        f.write_text("")
        with pytest.raises(ValueError):
            read_commit0_config_file(str(f))
