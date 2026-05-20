from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

MODULE = "commit0.harness.setup_java"


def _entry(repo: str = "org/mylib") -> dict:
    return {"repo": repo, "original_repo": repo, "setup": {}}


class TestFindJavaSourceDirs:
    def test_standard_layout(self, tmp_path: Path) -> None:
        std = tmp_path / "src" / "main" / "java"
        std.mkdir(parents=True)
        from commit0.harness.setup_java import _find_java_source_dirs

        result = _find_java_source_dirs(tmp_path)
        assert len(result) == 1
        assert result[0] == std

    def test_rglob_fallback(self, tmp_path: Path) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="rglob_") as td:
            workspace = Path(td)
            sub = workspace / "mod" / "src" / "main" / "java"
            sub.mkdir(parents=True)
            (sub / "Foo.java").write_text("class Foo {}")
            from commit0.harness.setup_java import _find_java_source_dirs

            result = _find_java_source_dirs(workspace)
            assert len(result) >= 1

    def test_monorepo_layout(self, tmp_path: Path) -> None:
        mod_src = tmp_path / "guava" / "src"
        mod_src.mkdir(parents=True)
        (mod_src / "Foo.java").write_text("class Foo {}")
        from commit0.harness.setup_java import _find_java_source_dirs

        result = _find_java_source_dirs(tmp_path)
        assert len(result) >= 1

    def test_no_java_files(self, tmp_path: Path) -> None:
        from commit0.harness.setup_java import _find_java_source_dirs

        result = _find_java_source_dirs(tmp_path)
        assert result == []

    def test_skip_test_dirs(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "test-module" / "src" / "main" / "java"
        test_dir.mkdir(parents=True)
        main_dir = tmp_path / "core" / "src" / "main" / "java"
        main_dir.mkdir(parents=True)
        from commit0.harness.setup_java import _find_java_source_dirs

        result = _find_java_source_dirs(tmp_path)
        paths_str = [str(p) for p in result]
        for p in paths_str:
            assert "test" not in p.lower()


class TestSaveMain:
    @patch("subprocess.run")
    def test_generates_patch(self, mock_run: MagicMock, tmp_path: Path) -> None:
        cfg = {"repos_dir": str(tmp_path), "patches_dir": str(tmp_path / "patches")}
        repo_dir = tmp_path / "mylib"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        mock_run.return_value = MagicMock(stdout="diff content", returncode=0)
        with patch(f"{MODULE}.JAVA_CONFIG_FILE", str(tmp_path / ".commit0.java.yaml")):
            cfg_path = tmp_path / ".commit0.java.yaml"
            cfg_path.write_text(yaml.dump(cfg))
            from commit0.harness.setup_java import save_main

            save_main(repo="org/mylib")
        patch_file = tmp_path / "patches" / "mylib.patch"
        assert patch_file.exists() is True

    @patch("subprocess.run")
    def test_no_changes_empty_patch(self, mock_run: MagicMock, tmp_path: Path) -> None:
        cfg = {"repos_dir": str(tmp_path), "patches_dir": str(tmp_path / "patches")}
        repo_dir = tmp_path / "mylib"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        mock_run.return_value = MagicMock(stdout="   ", returncode=0)
        with patch(f"{MODULE}.JAVA_CONFIG_FILE", str(tmp_path / ".commit0.java.yaml")):
            (tmp_path / ".commit0.java.yaml").write_text(yaml.dump(cfg))
            from commit0.harness.setup_java import save_main

            save_main(repo="org/mylib")
        patch_file = tmp_path / "patches" / "mylib.patch"
        assert patch_file.exists() is False

    def test_reads_config(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / ".commit0.java.yaml"
        cfg_path.write_text(yaml.dump({"repos_dir": str(tmp_path)}))
        with patch(f"{MODULE}.JAVA_CONFIG_FILE", str(cfg_path)):
            from commit0.harness.setup_java import save_main

            save_main(repo="org/missing")


class TestScrapeSpecs:
    @patch(f"{MODULE}._commit_spec")
    def test_cached_spec_used(self, mock_commit: MagicMock, tmp_path: Path) -> None:
        repos_dir = tmp_path / "repos"
        specs_dir = repos_dir / "_specs"
        specs_dir.mkdir(parents=True)
        repo_dir = repos_dir / "mylib"
        repo_dir.mkdir()
        (specs_dir / "mylib.pdf.bz2").write_text("cached")
        dataset = [{"repo": "org/mylib"}]
        from commit0.harness.setup_java import _scrape_specs

        _scrape_specs(dataset, repos_dir, None)
        assert (repo_dir / "spec.pdf.bz2").exists() is True

    @patch(f"{MODULE}._commit_spec")
    def test_playwright_fallback(self, mock_commit: MagicMock, tmp_path: Path) -> None:
        repos_dir = tmp_path / "repos"
        specs_dir = repos_dir / "_specs"
        specs_dir.mkdir(parents=True)
        repo_dir = repos_dir / "mylib"
        repo_dir.mkdir()
        dataset = [{"repo": "org/mylib", "setup": {"specification": "https://x"}}]
        from commit0.harness.setup_java import _scrape_specs

        _scrape_specs(dataset, repos_dir, None)
        assert (repo_dir / "spec.pdf.bz2").exists() is False


class TestMainClone:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clones_each_repo(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry("org/a"), _entry("org/b")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))
        assert mock_clone.call_count == 2

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_creates_java_base_dir(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        base = tmp_path / "java_repos"
        mock_load.return_value = list([])
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(base))
        assert base.exists() is True

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.JAVA_SPLIT", {"lite": ["org/a"]})
    def test_respects_split_filter(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry("org/a"), _entry("org/b")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "lite", "17", str(tmp_path))
        assert mock_clone.call_count == 1


class TestMainSplitFilter:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    @patch(f"{MODULE}.JAVA_SPLIT", {"lite": ["org/a"]})
    def test_lite_split_filters(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry("org/a"), _entry("org/b")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "lite", "17", str(tmp_path))
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_all_split_includes_all(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry("org/a"), _entry("org/b")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))
        assert mock_clone.call_count == 2


class TestMainBaseBranch:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_creates_commit0_java_branch(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))
        mock_repo.git.checkout.assert_any_call("-b", "commit0_java")

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_deletes_existing_branch(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry()])
        mock_repo = MagicMock()
        branch_mock = MagicMock()
        branch_mock.name = "commit0_java"
        mock_repo.branches = [branch_mock]
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))
        mock_repo.git.branch.assert_any_call("-D", "commit0_java")


class TestMainGitignore:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_adds_aider_patterns(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry()])
        repo_dir = tmp_path / "mylib"
        repo_dir.mkdir()
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))
        mock_repo.git.add.assert_called()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_adds_logs_pattern(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([_entry()])
        repo_dir = tmp_path / "mylib"
        repo_dir.mkdir()
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))
        mock_repo.git.commit.assert_called()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_idempotent_update(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        repo_dir = tmp_path / "mylib"
        repo_dir.mkdir()
        gi = repo_dir / ".gitignore"
        gi.write_text(".aider*\nlogs/\n")
        mock_load.return_value = list([_entry()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.setup_java import main

        main("ds", "all", "17", str(tmp_path))


class TestMainYamlConfig:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_writes_commit0_java_yaml(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([])
        from commit0.harness.setup_java import main, JAVA_CONFIG_FILE

        main("ds", "all", "17", str(tmp_path))
        assert Path(JAVA_CONFIG_FILE).exists() is True

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_contains_expected_keys(
        self, mock_load: MagicMock, mock_clone: MagicMock, tmp_path: Path
    ) -> None:
        mock_load.return_value = list([])
        from commit0.harness.setup_java import main, JAVA_CONFIG_FILE

        main("ds", "all", "17", str(tmp_path))
        with open(JAVA_CONFIG_FILE) as f:
            cfg = yaml.safe_load(f)
        assert "java_version" in cfg
        assert "dataset_name" in cfg
        assert "base_dir" in cfg
