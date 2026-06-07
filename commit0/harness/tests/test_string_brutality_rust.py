"""Tests for string brutality — special characters in names, paths, IDs.

Covers: repo names, test IDs, branch names, docker paths with unicode,
        injection patterns, and encoding edge cases.
"""

import os
from pathlib import Path



# ---------------------------------------------------------------------------
# Repo name handling — used everywhere (split lookups, path construction)
# ---------------------------------------------------------------------------
class TestRepoNameHandling:
    def test_repo_name_with_hyphen(self):
        """Standard pattern: org/my-repo -> my-repo."""
        repo = "org/my-repo"
        name = repo.split("/")[-1]
        assert name == "my-repo"

    def test_repo_name_with_dots(self):
        repo = "org/my.repo.v2"
        name = repo.split("/")[-1]
        assert name == "my.repo.v2"

    def test_repo_name_with_underscore_normalization(self):
        """setup_rust.py normalizes hyphens to underscores for matching."""
        repo_name = "my-cool-repo"
        split_name = "my_cool_repo"
        assert repo_name.replace("-", "_") == split_name.replace("-", "_")

    def test_repo_name_empty_after_split(self):
        repo = "org/"
        name = repo.split("/")[-1]
        assert name == ""

    def test_repo_name_no_slash(self):
        repo = "standalone"
        name = repo.split("/")[-1]
        assert name == "standalone"

    def test_repo_name_multiple_slashes(self):
        repo = "github.com/org/suborg/repo"
        name = repo.split("/")[-1]
        assert name == "repo"


# ---------------------------------------------------------------------------
# Test IDs — passed to pytest, potential injection vector
# ---------------------------------------------------------------------------
class TestTestIdHandling:
    def test_simple_test_id(self):
        test_id = "test_module.py::TestClass::test_method"
        assert "::" in test_id

    def test_parametrized_test_id(self):
        test_id = "test_mod.py::test_func[param1-param2]"
        assert "[" in test_id and "]" in test_id

    def test_test_id_with_spaces(self):
        """Test IDs should not have spaces, but what if they do?"""
        test_id = "test mod.py::test func"
        # Path construction would break
        parts = test_id.split("::")
        assert len(parts) == 2

    def test_test_id_with_special_chars(self):
        test_id = "test_mod.py::test_func[key=va!ue&other]"
        # Should not crash when hashed
        from commit0.harness.utils import get_hash_string

        h = get_hash_string(test_id)
        assert isinstance(h, str)
        assert len(h) > 0

    def test_test_id_with_unicode(self):
        test_id = "test_mod.py::test_\u00fcnicode_\u00e4bc"
        from commit0.harness.utils import get_hash_string

        h = get_hash_string(test_id)
        assert isinstance(h, str)

    def test_empty_test_id(self):
        from commit0.harness.utils import get_hash_string

        h = get_hash_string("")
        assert isinstance(h, str)

    def test_very_long_test_id(self):
        from commit0.harness.utils import get_hash_string

        test_id = "a" * 10000
        h = get_hash_string(test_id)
        assert isinstance(h, str)


# ---------------------------------------------------------------------------
# Branch names — used in git operations and path construction
# ---------------------------------------------------------------------------
class TestBranchNameHandling:
    def test_branch_with_slash(self):
        """feature/my-branch is valid in git."""
        branch = "feature/my-branch"
        # Path construction must handle this
        p = Path("/logs") / "repo" / branch / "hash"
        assert str(p) == "/logs/repo/feature/my-branch/hash"

    def test_branch_with_dots(self):
        branch = "v1.2.3"
        p = Path("/logs") / "repo" / branch
        assert branch in str(p)

    def test_branch_with_at_sign(self):
        branch = "user@fix"
        p = Path("/logs") / "repo" / branch
        assert "@" in str(p)

    def test_branch_empty_string(self):
        branch = ""
        p = Path("/logs") / "repo" / branch
        assert str(p) == "/logs/repo"


# ---------------------------------------------------------------------------
# Docker path construction — used in docker_utils.py
# ---------------------------------------------------------------------------
class TestDockerPathHandling:
    def test_path_with_spaces(self):
        """docker_utils uses f-strings for paths — spaces can break shell commands."""
        path = "/workspace/my project/src"
        cmd = f"mkdir -p {path}"
        # This would break in shell without quoting
        assert " " in cmd
        # The safe version:
        safe_cmd = f'mkdir -p "{path}"'
        assert '"' in safe_cmd

    def test_path_with_parentheses(self):
        path = "/workspace/src (copy)/file.py"
        cmd = f"tar -xf {path}.tar"
        # Parentheses break shell too
        assert "(" in cmd

    def test_path_with_newline(self):
        """Newlines in paths could inject shell commands."""
        path = "/workspace/src\n/bin/rm -rf /"
        assert "\n" in path

    def test_unicode_path(self):
        path = "/workspace/\u65e5\u672c\u8a9e/src"
        p = Path(path)
        assert "\u65e5\u672c\u8a9e" in str(p)


# ---------------------------------------------------------------------------
# YAML config values — special string handling
# ---------------------------------------------------------------------------
class TestYamlStringValues:
    def test_dataset_name_with_colon(self):
        """HuggingFace dataset names contain slashes, colons could break YAML."""
        import yaml

        config = yaml.safe_load('dataset_name: "org/dataset:split"')
        assert config["dataset_name"] == "org/dataset:split"

    def test_base_dir_with_tilde(self):
        """Tilde expansion is common in paths but YAML won't expand it."""
        import yaml

        config = yaml.safe_load("base_dir: ~/repos")
        assert config["base_dir"] == "~/repos"
        # Code must expand: os.path.expanduser
        expanded = os.path.expanduser(config["base_dir"])
        assert "~" not in expanded or expanded == "~/repos"

    def test_github_token_with_special_chars(self):
        """Tokens can have mixed case and underscores."""
        import yaml

        config = yaml.safe_load('github_token: "ghp_ABCdef123_XYZ"')
        assert config["github_token"] == "ghp_ABCdef123_XYZ"
