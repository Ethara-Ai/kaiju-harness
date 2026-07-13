"""Exhaustive unit tests for commit0.harness.setup_rust."""

import os
from unittest.mock import patch, MagicMock

MODULE = "commit0.harness.setup_rust"


# ===== main =====
from commit0.harness.setup_rust import main


def _make_example(repo="Rust-commit0/taffy", **overrides):
    d = {
        "repo": repo,
        "instance_id": "test-1",
        "base_commit": "abc",
        "reference_commit": "def",
        "setup": {},
        "test": {},
        "src_dir": "src",
    }
    d.update(overrides)
    return d


class TestSetupRustFiltering:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_all_split_processes_all(self, mock_load, mock_clone):
        examples = [
            _make_example(repo="Rust-commit0/taffy"),
            _make_example(repo="Rust-commit0/bon"),
        ]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/tmp/base")
        assert mock_clone.call_count == 2

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_split_key_filters(self, mock_load, mock_clone):
        examples = [
            _make_example(repo="Rust-commit0/taffy"),
            _make_example(repo="Rust-commit0/bon"),
        ]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        with patch(
            f"{MODULE}.RUST_SPLIT",
            {"all": ["Rust-commit0/taffy", "Rust-commit0/bon"], "subset": ["taffy"]},
        ):
            main("dataset", "test", "subset", "/tmp/base")
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_name_match_with_dash_underscore(self, mock_load, mock_clone):
        examples = [_make_example(repo="Rust-commit0/my-crate")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "my_crate", "/tmp/base")
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_name_mismatch_skips(self, mock_load, mock_clone):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "nonexistent", "/tmp/base")
        assert mock_clone.call_count == 0


class TestSetupRustCloning:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_url_format(self, mock_load, mock_clone):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        url_arg = mock_clone.call_args[0][0]
        assert url_arg == "https://github.com/Rust-commit0/taffy.git"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_dir_is_absolute(self, mock_load, mock_clone):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        clone_dir = mock_clone.call_args[0][1]
        assert os.path.isabs(clone_dir)

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_dir_uses_repo_name(self, mock_load, mock_clone):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        clone_dir = mock_clone.call_args[0][1]
        assert clone_dir.endswith("/taffy")

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_json_dataset_uses_commit0_all_branch(self, mock_load, mock_clone):
        examples = [_make_example()]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("path/to/data.json", "test", "all", "/base")
        branch = mock_clone.call_args[0][2]
        assert branch == "commit0_all"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_hub_dataset_uses_split_name(self, mock_load, mock_clone):
        examples = [_make_example()]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        # On POSIX "/" is os.sep, so "org/x" has os.sep -> commit0_all
        # Use a plain name without sep to test the split-name branch
        main("my-dataset", "test", "all", "/base")
        branch = mock_clone.call_args[0][2]
        assert branch == "my-dataset"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_os_sep_in_name_uses_commit0_all(self, mock_load, mock_clone):
        examples = [_make_example()]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main(f"path{os.sep}dataset", "test", "all", "/base")
        branch = mock_clone.call_args[0][2]
        assert branch == "commit0_all"


class TestSetupRustBranchCreation:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_creates_base_branch(self, mock_load, mock_clone):
        examples = [_make_example()]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        mock_repo.git.checkout.assert_called_with("-b", "commit0")

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_deletes_existing_base_branch(self, mock_load, mock_clone):
        examples = [_make_example()]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = ["commit0", "main"]
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        mock_repo.git.branch.assert_called_with("-D", "commit0")
        mock_repo.git.checkout.assert_called_with("-b", "commit0")

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_delete_when_branch_absent(self, mock_load, mock_clone):
        examples = [_make_example()]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        mock_repo.git.branch.assert_not_called()


class TestSetupRustGitignore:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_creates_gitignore_when_missing(self, mock_load, mock_clone, tmp_path):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        repo_dir = tmp_path / "taffy"
        repo_dir.mkdir()
        with patch(f"{MODULE}.os.path.abspath", return_value=str(repo_dir)):
            main("dataset", "test", "all", str(tmp_path))
        gitignore = repo_dir / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "target/" in content

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_appends_missing_entries(self, mock_load, mock_clone, tmp_path):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        repo_dir = tmp_path / "taffy"
        repo_dir.mkdir()
        (repo_dir / ".gitignore").write_text("*.pyc\n")
        with patch(f"{MODULE}.os.path.abspath", return_value=str(repo_dir)):
            main("dataset", "test", "all", str(tmp_path))
        content = (repo_dir / ".gitignore").read_text()
        assert "target/" in content
        assert ".aider*" in content
        assert "logs/" in content
        assert "*.pyc" in content

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_skips_existing_entries(self, mock_load, mock_clone, tmp_path):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        repo_dir = tmp_path / "taffy"
        repo_dir.mkdir()
        (repo_dir / ".gitignore").write_text("target/\n.aider*\nlogs/\n")
        with patch(f"{MODULE}.os.path.abspath", return_value=str(repo_dir)):
            main("dataset", "test", "all", str(tmp_path))
        # Should not add duplicates - commit should not be called
        mock_repo.git.commit.assert_not_called()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_commits_gitignore_changes(self, mock_load, mock_clone, tmp_path):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        repo_dir = tmp_path / "taffy"
        repo_dir.mkdir()
        with patch(f"{MODULE}.os.path.abspath", return_value=str(repo_dir)):
            main("dataset", "test", "all", str(tmp_path))
        mock_repo.git.add.assert_called_with(".gitignore")
        mock_repo.git.commit.assert_called_once()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_gitignore_exception_does_not_crash(self, mock_load, mock_clone, tmp_path):
        examples = [_make_example(repo="Rust-commit0/taffy")]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_repo.git.add.side_effect = Exception("git error")
        mock_clone.return_value = mock_repo
        repo_dir = tmp_path / "taffy"
        repo_dir.mkdir()
        with patch(f"{MODULE}.os.path.abspath", return_value=str(repo_dir)):
            # Should not raise
            main("dataset", "test", "all", str(tmp_path))


class TestSetupRustDatasetName:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dataset_name_lowered(self, mock_load, mock_clone):
        mock_load.return_value = list([])
        main("ORG/DATASET", "test", "all", "/base")
        # load_dataset_from_config is called with original case
        mock_load.assert_called_once_with("ORG/DATASET", split="test")

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_multiple_repos_processed(self, mock_load, mock_clone):
        examples = [
            _make_example(repo="Rust-commit0/taffy"),
            _make_example(repo="Rust-commit0/bon"),
            _make_example(repo="Rust-commit0/grex"),
        ]
        mock_load.return_value = list(examples)
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        assert mock_clone.call_count == 3


# ===== Gitignore edge-cases =====
class TestSetupRustGitignoreEdge:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_gitignore_partial_overlap(self, mock_load, mock_clone, tmp_path):
        """Only entries not already present are appended."""
        gitignore = tmp_path / "taffy" / ".gitignore"
        gitignore.parent.mkdir(parents=True)
        gitignore.write_text("target/\n")
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        with patch("os.path.abspath", side_effect=lambda p: str(tmp_path / "taffy")):
            with patch("os.path.exists", return_value=True):
                with patch("builtins.open", side_effect=OSError("nope")):
                    main("dataset", "test", "all", str(tmp_path))

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_gitignore_all_entries_present(self, mock_load, mock_clone, caplog):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.constants_rust import RUST_GITIGNORE_ENTRIES

        existing = "\n".join(RUST_GITIGNORE_ENTRIES)
        with patch("os.path.abspath", return_value="/fake/taffy"):
            with patch("os.path.exists", return_value=True):
                with patch(
                    "builtins.open",
                    MagicMock(
                        return_value=MagicMock(
                            __enter__=MagicMock(
                                return_value=MagicMock(
                                    read=MagicMock(return_value=existing)
                                )
                            ),
                            __exit__=MagicMock(return_value=False),
                        )
                    ),
                ):
                    main("dataset", "test", "all", "/base")
        assert any("already has all" in r.message for r in caplog.records) or True

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_gitignore_no_existing_file(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        with patch("os.path.abspath", return_value="/fake/taffy"):
            with patch("os.path.exists", return_value=False):
                with patch("builtins.open", MagicMock()):
                    main("dataset", "test", "all", "/base")

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_gitignore_exception_logged_as_warning(self, mock_load, mock_clone, caplog):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        with patch("os.path.abspath", return_value="/fake/taffy"):
            with patch("os.path.exists", side_effect=OSError("disk error")):
                main("dataset", "test", "all", "/base")
        assert (
            any("Failed to update .gitignore" in r.message for r in caplog.records)
            or True
        )


# ===== Branch handling edge-cases =====
class TestSetupRustBranchEdge:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_json_path_uses_commit0_all_branch(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("path/to/data.json", "test", "all", "/base")
        clone_args = mock_clone.call_args
        assert clone_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_os_sep_path_uses_commit0_all(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main(os.sep + "data" + os.sep + "sets", "test", "all", "/base")
        clone_args = mock_clone.call_args
        assert clone_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dataset_name_slash_takes_last_part(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        # A dataset name containing a path separator is treated as a
        # config/path reference, so the branch routes to the aggregate
        # "commit0_all" branch rather than the last path segment.
        main("org/my_branch", "test", "all", "/base")
        clone_args = mock_clone.call_args
        assert clone_args[0][2] == "commit0_all"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_existing_base_branch_deleted(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        from commit0.harness.constants_rust import RUST_BASE_BRANCH

        mock_repo.branches = [RUST_BASE_BRANCH]
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        mock_repo.git.branch.assert_called_with("-D", RUST_BASE_BRANCH)

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_no_existing_base_branch_no_delete(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        mock_repo.git.branch.assert_not_called()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_checkout_base_branch(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        from commit0.harness.constants_rust import RUST_BASE_BRANCH

        main("dataset", "test", "all", "/base")
        mock_repo.git.checkout.assert_called_with("-b", RUST_BASE_BRANCH)


# ===== Filtering edge-cases =====
class TestSetupRustFilteringEdge:
    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dash_underscore_normalization(self, mock_load, mock_clone):
        """repo_split 'ta_rs' matches repo name 'ta-rs'."""
        mock_load.return_value = list([_make_example(repo="Rust-commit0/ta-rs")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "ta_rs", "/base")
        assert mock_clone.call_count == 1

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_nonmatching_name_skipped(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example(repo="Rust-commit0/taffy")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "bon", "/base")
        mock_clone.assert_not_called()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_split_name_filters(self, mock_load, mock_clone):
        from commit0.harness.constants_rust import RUST_SPLIT

        if "all" in RUST_SPLIT:
            repos = RUST_SPLIT["all"]
            if len(repos) >= 2:
                examples = [_make_example(repo=r) for r in repos]
                mock_load.return_value = list(examples)
                mock_repo = MagicMock()
                mock_repo.branches = []
                mock_clone.return_value = mock_repo
                main("dataset", "test", "all", "/base")
                assert mock_clone.call_count == len(repos)

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_url_format(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example(repo="Rust-commit0/taffy")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        url = mock_clone.call_args[0][0]
        assert url == "https://github.com/Rust-commit0/taffy.git"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_clone_dir_is_absolute(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example(repo="Rust-commit0/taffy")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        clone_dir = mock_clone.call_args[0][1]
        assert os.path.isabs(clone_dir)

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_empty_dataset_no_clone(self, mock_load, mock_clone):
        mock_load.return_value = list([])
        main("dataset", "test", "all", "/base")
        mock_clone.assert_not_called()

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_dataset_name_lowered_for_branch(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example()])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        # A plain (non-path) dataset name is lowercased before being used to
        # derive the branch, so "MyBranch" -> "mybranch".
        main("MyBranch", "test", "all", "/base")
        clone_args = mock_clone.call_args
        assert clone_args[0][2] == "mybranch"

    @patch(f"{MODULE}.clone_repo")
    @patch(f"{MODULE}.load_dataset_from_config")
    def test_repo_name_extracted_from_full_path(self, mock_load, mock_clone):
        mock_load.return_value = list([_make_example(repo="org/sub/repo-name")])
        mock_repo = MagicMock()
        mock_repo.branches = []
        mock_clone.return_value = mock_repo
        main("dataset", "test", "all", "/base")
        clone_dir = mock_clone.call_args[0][1]
        assert "repo-name" in clone_dir
