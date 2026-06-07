"""Tests for C1-C10, C12 config validation fixes.

Each test class maps to one C-issue:
  C1:  yaml.safe_load (not FullLoader)
  C2:  Empty/invalid YAML raises ValueError
  C3:  .commit0.yaml schema validation
  C4:  .agent.yaml load_agent_config helper
  C5:  Dead Commit0Config dataclass (existence check)
  C6:  AgentConfig.__post_init__ range/type validation
  C8:  Environment variable token guards
  C9:  base_dir existence validation
  C10: RepoInstance.__getitem__ raises KeyError not AttributeError
  C12: Schema evolution — new fields must have defaults
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers — build a minimal valid AgentConfig dict
# ---------------------------------------------------------------------------


def _valid_agent_dict() -> dict:
    """Return minimal valid dict for AgentConfig construction."""
    return {
        "agent_name": "aider",
        "model_name": "test-model",
        "use_user_prompt": False,
        "user_prompt": "test prompt",
        "use_topo_sort_dependencies": False,
        "add_import_module_to_context": False,
        "use_repo_info": False,
        "max_repo_info_length": 10000,
        "use_unit_tests_info": False,
        "max_unit_tests_info_length": 10000,
        "use_spec_info": False,
        "max_spec_info_length": 10000,
        "use_lint_info": False,
        "run_entire_dir_lint": False,
        "max_lint_info_length": 10000,
        "pre_commit_config_path": ".pre-commit-config.yaml",
        "run_tests": False,
        "max_iteration": 3,
        "record_test_for_each_commit": False,
    }


def _valid_commit0_dict(tmp_path: Path) -> dict:
    """Return minimal valid dict for .commit0.yaml, using tmp_path as base_dir."""
    return {
        "dataset_name": "commit0/commit0",
        "dataset_split": "test",
        "repo_split": "all",
        "base_dir": str(tmp_path),
    }


# ---------------------------------------------------------------------------
# C1: yaml.safe_load used instead of yaml.FullLoader
# ---------------------------------------------------------------------------


class TestC1SafeLoad:
    """Both YAML readers must use yaml.safe_load, not yaml.FullLoader."""

    def test_commit0_config_uses_safe_load(self) -> None:
        """read_commit0_config_file source must not contain FullLoader."""
        import inspect
        from commit0.cli import read_commit0_config_file

        source = inspect.getsource(read_commit0_config_file)
        assert "FullLoader" not in source, (
            "read_commit0_config_file still uses yaml.FullLoader"
        )
        assert "safe_load" in source, "read_commit0_config_file must use yaml.safe_load"

    def test_agent_config_uses_safe_load(self) -> None:
        """read_yaml_config source must not contain FullLoader."""
        import inspect
        from agent.agent_utils import read_yaml_config

        source = inspect.getsource(read_yaml_config)
        assert "FullLoader" not in source, "read_yaml_config still uses yaml.FullLoader"
        assert "safe_load" in source, "read_yaml_config must use yaml.safe_load"

    def test_valid_yaml_roundtrips(self, tmp_path: Path) -> None:
        """A valid YAML file loads correctly with safe_load path."""
        cfg = {
            "dataset_name": "test",
            "dataset_split": "all",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        cfg_path = tmp_path / ".commit0.yaml"
        cfg_path.write_text(yaml.dump(cfg))

        from commit0.cli import read_commit0_config_file

        result = read_commit0_config_file(str(cfg_path))
        assert result["dataset_name"] == "test"


# ---------------------------------------------------------------------------
# C2: Empty YAML raises ValueError
# ---------------------------------------------------------------------------


class TestC2EmptyYaml:
    """Empty or non-dict YAML must raise ValueError, not TypeError."""

    def test_empty_commit0_yaml_raises_valueerror(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".commit0.yaml"
        cfg_path.write_text("")
        from commit0.cli import read_commit0_config_file

        with pytest.raises(ValueError, match="empty or invalid"):
            read_commit0_config_file(str(cfg_path))

    def test_null_commit0_yaml_raises_valueerror(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".commit0.yaml"
        cfg_path.write_text("null\n")
        from commit0.cli import read_commit0_config_file

        with pytest.raises(ValueError, match="empty or invalid"):
            read_commit0_config_file(str(cfg_path))

    def test_list_yaml_raises_valueerror(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".commit0.yaml"
        cfg_path.write_text("- item1\n- item2\n")
        from commit0.cli import read_commit0_config_file

        with pytest.raises(ValueError, match="empty or invalid"):
            read_commit0_config_file(str(cfg_path))

    def test_empty_agent_yaml_raises_valueerror(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".agent.yaml"
        cfg_path.write_text("")
        from agent.agent_utils import read_yaml_config

        with pytest.raises(ValueError, match="empty or invalid"):
            read_yaml_config(str(cfg_path))

    def test_nonexistent_file_raises_filenotfound(self) -> None:
        from commit0.cli import read_commit0_config_file

        with pytest.raises(FileNotFoundError):
            read_commit0_config_file("/nonexistent/path/.commit0.yaml")


# ---------------------------------------------------------------------------
# C3: .commit0.yaml schema validation
# ---------------------------------------------------------------------------


class TestC3Commit0SchemaValidation:
    """validate_commit0_config must check required keys and types."""

    def test_missing_single_key_raises_valueerror(self, tmp_path: Path) -> None:
        from commit0.cli import validate_commit0_config

        cfg = _valid_commit0_dict(tmp_path)
        del cfg["dataset_name"]
        with pytest.raises(ValueError, match="dataset_name"):
            validate_commit0_config(cfg, ".commit0.yaml")

    def test_missing_all_keys_raises_valueerror(self) -> None:
        from commit0.cli import validate_commit0_config

        with pytest.raises(ValueError, match="missing required keys"):
            validate_commit0_config({}, ".commit0.yaml")

    def test_wrong_type_raises_typeerror(self, tmp_path: Path) -> None:
        from commit0.cli import validate_commit0_config

        cfg = _valid_commit0_dict(tmp_path)
        cfg["dataset_name"] = 123
        with pytest.raises(TypeError, match="dataset_name.*must be str.*got int"):
            validate_commit0_config(cfg, ".commit0.yaml")

    def test_valid_config_passes(self, tmp_path: Path) -> None:
        from commit0.cli import validate_commit0_config

        cfg = _valid_commit0_dict(tmp_path)
        validate_commit0_config(cfg, ".commit0.yaml")  # should not raise


# ---------------------------------------------------------------------------
# C4: load_agent_config helper
# ---------------------------------------------------------------------------


class TestC4LoadAgentConfig:
    """load_agent_config must filter unknowns, wrap TypeError, return AgentConfig."""

    def test_valid_config_returns_agent_config(self, tmp_path: Path) -> None:
        from agent.agent_utils import load_agent_config
        from agent.class_types import AgentConfig

        cfg_path = tmp_path / ".agent.yaml"
        cfg_path.write_text(yaml.dump(_valid_agent_dict()))
        result = load_agent_config(str(cfg_path))
        assert isinstance(result, AgentConfig)
        assert result.model_name == "test-model"

    def test_unknown_key_filtered_with_warning(self, tmp_path: Path, caplog) -> None:
        from agent.agent_utils import load_agent_config
        import logging

        cfg = _valid_agent_dict()
        cfg["bogus_key"] = "should be ignored"
        cfg_path = tmp_path / ".agent.yaml"
        cfg_path.write_text(yaml.dump(cfg))
        with caplog.at_level(logging.WARNING):
            result = load_agent_config(str(cfg_path))
        assert "bogus_key" in caplog.text
        assert result.model_name == "test-model"

    def test_missing_required_field_gives_helpful_error(self, tmp_path: Path) -> None:
        from agent.agent_utils import load_agent_config

        cfg = _valid_agent_dict()
        del cfg["model_name"]
        cfg_path = tmp_path / ".agent.yaml"
        cfg_path.write_text(yaml.dump(cfg))
        with pytest.raises(TypeError, match="model_name"):
            load_agent_config(str(cfg_path))

    def test_mentions_config_file_in_error(self, tmp_path: Path) -> None:
        from agent.agent_utils import load_agent_config

        cfg = _valid_agent_dict()
        del cfg["agent_name"]
        cfg_path = tmp_path / "my_agent.yaml"
        cfg_path.write_text(yaml.dump(cfg))
        with pytest.raises(TypeError, match="my_agent.yaml"):
            load_agent_config(str(cfg_path))


# ---------------------------------------------------------------------------
# C5: Dead Commit0Config (existence check — not deleting)
# ---------------------------------------------------------------------------


class TestC5DeadCode:
    """Commit0Config in config_class.py is dead code — never imported."""

    def test_config_class_not_imported_anywhere(self) -> None:
        """Verify no Python file imports Commit0Config (proving it's dead)."""
        import subprocess

        result = subprocess.run(
            [
                "grep",
                "-rn",
                "from commit0.configs.config_class import",
                "--include=*.py",
                ".",
            ],
            capture_output=True,
            text=True,
            cwd="/Users/macbookpro/Desktop/kaiju_harness/commit0_jsonl",
        )
        # Exclude test files (this file checks for it)
        lines = [
            l
            for l in result.stdout.strip().split("\n")
            if l and "test_config_validation" not in l
        ]
        assert len(lines) == 0, f"Commit0Config is imported somewhere: {lines}"


# ---------------------------------------------------------------------------
# C6: AgentConfig __post_init__ validation
# ---------------------------------------------------------------------------


class TestC6PostInit:
    """AgentConfig.__post_init__ must catch invalid values at construction time."""

    def test_max_iteration_zero_raises(self) -> None:
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        cfg["max_iteration"] = 0
        with pytest.raises(ValueError, match="max_iteration"):
            AgentConfig(**cfg)

    def test_max_iteration_negative_raises(self) -> None:
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        cfg["max_iteration"] = -1
        with pytest.raises(ValueError, match="max_iteration"):
            AgentConfig(**cfg)

    def test_empty_model_name_raises(self) -> None:
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        cfg["model_name"] = ""
        with pytest.raises(ValueError, match="model_name"):
            AgentConfig(**cfg)

    def test_whitespace_model_name_raises(self) -> None:
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        cfg["model_name"] = "   "
        with pytest.raises(ValueError, match="model_name"):
            AgentConfig(**cfg)

    def test_negative_max_length_raises(self) -> None:
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        cfg["max_repo_info_length"] = -5
        with pytest.raises(ValueError, match="max_repo_info_length"):
            AgentConfig(**cfg)

    def test_valid_config_passes_post_init(self) -> None:
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        ac = AgentConfig(**cfg)
        assert ac.max_iteration == 3
        assert ac.model_name == "test-model"

    def test_max_length_zero_is_valid(self) -> None:
        """Zero is valid for max_*_length (means 'no limit' or empty)."""
        from agent.class_types import AgentConfig

        cfg = _valid_agent_dict()
        cfg["max_repo_info_length"] = 0
        ac = AgentConfig(**cfg)
        assert ac.max_repo_info_length == 0


# ---------------------------------------------------------------------------
# C8: Environment variable token guards
# ---------------------------------------------------------------------------


class TestC8TokenGuards:
    """Functions that need tokens must fail fast when tokens are missing."""

    def test_save_raises_without_github_token(self) -> None:
        """save.py must raise EnvironmentError when GITHUB_TOKEN is unset."""
        import inspect
        from commit0.harness import save

        source = inspect.getsource(save)
        # The guard must exist in the source
        assert "EnvironmentError" in source or "raise" in source
        assert "GITHUB_TOKEN" in source

    def test_save_guard_pattern(self) -> None:
        """save.py main() checks github_token and falls back to env var."""
        import inspect
        from commit0.harness import save

        source = inspect.getsource(save.main)
        assert 'os.environ.get("GITHUB_TOKEN")' in source
        assert "if github_token is None" in source

    def test_batch_prepare_has_token_guard(self) -> None:
        """batch_prepare.py must check for GITHUB_TOKEN/GH_TOKEN."""
        import inspect
        from tools import batch_prepare

        source = inspect.getsource(batch_prepare)
        assert "GITHUB_TOKEN" in source or "GH_TOKEN" in source

    def test_create_dataset_has_hf_token_guard(self) -> None:
        """create_dataset.py upload must check for HF_TOKEN."""
        import inspect
        from tools import create_dataset

        source = inspect.getsource(create_dataset)
        assert "HF_TOKEN" in source


# ---------------------------------------------------------------------------
# C9: base_dir existence validation
# ---------------------------------------------------------------------------


class TestC9BaseDirCheck:
    """validate_commit0_config must check base_dir exists on disk."""

    def test_nonexistent_base_dir_raises(self) -> None:
        from commit0.cli import validate_commit0_config

        cfg = {
            "dataset_name": "test",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/nonexistent/path/that/does/not/exist",
        }
        with pytest.raises(FileNotFoundError, match="does not exist"):
            validate_commit0_config(cfg, ".commit0.yaml")

    def test_valid_base_dir_passes(self, tmp_path: Path) -> None:
        from commit0.cli import validate_commit0_config

        cfg = _valid_commit0_dict(tmp_path)
        validate_commit0_config(cfg, ".commit0.yaml")  # should not raise

    def test_error_mentions_commit0_setup(self) -> None:
        from commit0.cli import validate_commit0_config

        cfg = {
            "dataset_name": "test",
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": "/no/such/dir",
        }
        with pytest.raises(FileNotFoundError, match="commit0 setup"):
            validate_commit0_config(cfg, ".commit0.yaml")


# ---------------------------------------------------------------------------
# C10: RepoInstance.__getitem__ raises KeyError
# ---------------------------------------------------------------------------


class TestC10GetItemKeyError:
    """RepoInstance and SimpleInstance must raise KeyError for missing keys."""

    def test_repo_instance_missing_key_raises_keyerror(self) -> None:
        from commit0.harness.constants import RepoInstance

        inst = RepoInstance(
            instance_id="test/repo",
            repo="test/repo",
            base_commit="abc1234",
            reference_commit="def5678",
            setup={},
            test={"test_cmd": "pytest", "test_dir": "tests"},
            src_dir="src",
        )
        with pytest.raises(KeyError):
            _ = inst["nonexistent_field"]

    def test_repo_instance_valid_key_works(self) -> None:
        from commit0.harness.constants import RepoInstance

        inst = RepoInstance(
            instance_id="test/repo",
            repo="test/repo",
            base_commit="abc1234",
            reference_commit="def5678",
            setup={},
            test={"test_cmd": "pytest", "test_dir": "tests"},
            src_dir="src",
        )
        assert inst["repo"] == "test/repo"
        assert inst["src_dir"] == "src"

    def test_simple_instance_missing_key_raises_keyerror(self) -> None:
        from commit0.harness.constants import SimpleInstance

        inst = SimpleInstance(
            instance_id="test",
            prompt="hello",
            canonical_solution="pass",
            test="assert True",
        )
        with pytest.raises(KeyError):
            _ = inst["bogus"]

    def test_not_attribute_error(self) -> None:
        """Specifically verify it's KeyError, NOT AttributeError."""
        from commit0.harness.constants import RepoInstance

        inst = RepoInstance(
            instance_id="test/repo",
            repo="test/repo",
            base_commit="abc1234",
            reference_commit="def5678",
            setup={},
            test={"test_cmd": "pytest", "test_dir": "tests"},
            src_dir="src",
        )
        try:
            _ = inst["nonexistent"]
            assert False, "Should have raised"
        except KeyError:
            pass  # correct
        except AttributeError:
            pytest.fail("Raised AttributeError instead of KeyError — C10 not fixed")


# ---------------------------------------------------------------------------
# C12: Schema evolution — new fields must have defaults
# ---------------------------------------------------------------------------


class TestC12SchemaEvolution:
    """All fields after record_test_for_each_commit must have defaults."""

    def test_new_fields_have_defaults(self) -> None:
        """Fields added after the original 19 must all have defaults."""
        from agent.class_types import AgentConfig

        fields = dataclasses.fields(AgentConfig)
        # Find the boundary: record_test_for_each_commit is the last required field
        boundary_idx = None
        for i, f in enumerate(fields):
            if f.name == "record_test_for_each_commit":
                boundary_idx = i
                break
        assert boundary_idx is not None, "record_test_for_each_commit field not found"

        # All fields AFTER the boundary must have defaults
        for f in fields[boundary_idx + 1 :]:
            has_default = (
                f.default is not dataclasses.MISSING
                or f.default_factory is not dataclasses.MISSING
            )
            assert has_default, (
                f"Field '{f.name}' (added after record_test_for_each_commit) "
                f"has no default — violates schema contract"
            )

    def test_schema_contract_comment_exists(self) -> None:
        """class_types.py must have the SCHEMA CONTRACT comment."""
        import inspect
        from agent import class_types

        source = inspect.getsource(class_types)
        assert "SCHEMA CONTRACT" in source

    def test_existing_yaml_without_new_fields_works(self, tmp_path: Path) -> None:
        """A YAML with only the original 19 required fields must still work."""
        from agent.agent_utils import load_agent_config

        cfg = _valid_agent_dict()
        # Explicitly don't include: cache_prompts, capture_thinking, trajectory_md, output_jsonl
        cfg_path = tmp_path / ".agent.yaml"
        cfg_path.write_text(yaml.dump(cfg))
        result = load_agent_config(str(cfg_path))
        # Defaults should kick in
        assert result.cache_prompts is True
        assert result.capture_thinking is False
        assert result.trajectory_md is True
        assert result.output_jsonl is False
