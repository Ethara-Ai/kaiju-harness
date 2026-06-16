from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness import setup_js
from commit0.harness.constants_js import JS_BASE_BRANCH, JS_GITIGNORE_ENTRIES


SETUP_JS_SOURCE = Path(setup_js.__file__).read_text(encoding="utf-8")


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instance_id": "commit-0/p-queue",
        "repo": "sindresorhus/p-queue",
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
    }
    base.update(overrides)
    return base


def _make_mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.branches = []
    repo.git = MagicMock()
    return repo


class TestMissingRepoFieldRaisesKeyError:
    def test_keyerror_on_missing_repo_inconsistent_with_build_js(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad = _entry()
        del bad["repo"]
        monkeypatch.setattr(
            setup_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([bad]),
        )
        monkeypatch.setattr(
            setup_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        with pytest.raises(KeyError):
            setup_js.main(
                dataset_name="ds.json",
                dataset_split="test",
                repo_split="all",
                base_dir=str(tmp_path),
            )


class TestCloneUrlConstruction:
    def test_clone_url_format_uses_github_https(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        captured: dict[str, Any] = {}

        def _capture(url, clone_dir, branch, _logger):
            captured["url"] = url
            captured["clone_dir"] = clone_dir
            captured["branch"] = branch
            return _make_mock_repo()

        monkeypatch.setattr(
            setup_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry(repo="sindresorhus/p-queue")]),
        )
        monkeypatch.setattr(
            setup_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(setup_js, "clone_repo", _capture)
        setup_js.main(
            dataset_name="ds.json",
            dataset_split="test",
            repo_split="all",
            base_dir=str(tmp_path),
        )
        assert captured["url"] == "https://github.com/sindresorhus/p-queue.git"

    @pytest.mark.parametrize(
        "evil_repo",
        [
            "a/b#evilbranch",
            "a/b?q=evil",
            "a/b ;rm -rf /",
            "a/b\nrm",
            "a/b%2Fevil",
        ],
    )
    def test_evil_repo_value_is_rejected_before_clone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        evil_repo: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        captured: dict[str, Any] = {}

        def _capture(url, clone_dir, branch, _logger):
            captured["url"] = url
            return _make_mock_repo()

        monkeypatch.setattr(
            setup_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry(repo=evil_repo)]),
        )
        monkeypatch.setattr(
            setup_js,
            "resolve_split",
            lambda *_a, **_kw: [evil_repo.split("/")[-1]],
        )
        monkeypatch.setattr(setup_js, "clone_repo", _capture)
        with caplog.at_level("WARNING", logger="commit0.harness.setup_js"):
            setup_js.main(
                dataset_name="ds.json",
                dataset_split="test",
                repo_split="all",
                base_dir=str(tmp_path),
            )
        assert "url" not in captured
        assert any(
            "Skipping repo with invalid name" in r.message for r in caplog.records
        )


class TestGitignoreUpdateSwallowsExceptions:
    def test_oserror_during_exclude_logged_as_warning_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake_repo = _make_mock_repo()
        clone_dir = tmp_path / "p-queue"
        clone_dir.mkdir()
        exclude_path = clone_dir / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True)
        exclude_path.write_text("# existing\n", encoding="utf-8")

        original_open = open

        def _broken_open(path, mode="r", *args, **kwargs):
            if str(path) == str(exclude_path) and "a" in mode:
                raise OSError("disk full")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(
            setup_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry()]),
        )
        monkeypatch.setattr(
            setup_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            setup_js, "clone_repo", lambda *_a, **_kw: fake_repo
        )
        with (
            patch("builtins.open", side_effect=_broken_open),
            caplog.at_level(logging.WARNING, logger="commit0.harness.setup_js"),
        ):
            setup_js.main(
                dataset_name="ds.json",
                dataset_split="test",
                repo_split="all",
                base_dir=str(tmp_path),
            )
        assert any(
            "Failed to update .git/info/exclude" in rec.message
            for rec in caplog.records
        )

    def test_branch_creation_runs_even_when_exclude_block_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_repo = _make_mock_repo()
        monkeypatch.setattr(
            setup_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry()]),
        )
        monkeypatch.setattr(
            setup_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            setup_js, "clone_repo", lambda *_a, **_kw: fake_repo
        )
        monkeypatch.setattr(
            setup_js.os.path,
            "exists",
            MagicMock(side_effect=OSError("fs error")),
        )
        setup_js.main(
            dataset_name="ds.json",
            dataset_split="test",
            repo_split="all",
            base_dir=str(tmp_path),
        )
        fake_repo.git.checkout.assert_called_with("-b", JS_BASE_BRANCH)


class TestF011GitignoreNoCommit:
    def test_exclude_used_instead_of_gitignore_no_repo_git_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        fake_repo = _make_mock_repo()
        clone_dir = tmp_path / "p-queue"
        clone_dir.mkdir()
        monkeypatch.setattr(
            setup_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry()]),
        )
        monkeypatch.setattr(
            setup_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            setup_js, "clone_repo", lambda *_a, **_kw: fake_repo
        )
        setup_js.main(
            dataset_name="ds.json",
            dataset_split="test",
            repo_split="all",
            base_dir=str(tmp_path),
        )

        fake_repo.git.add.assert_not_called()
        fake_repo.git.commit.assert_not_called()

        exclude_path = clone_dir / ".git" / "info" / "exclude"
        assert exclude_path.exists(), "F-011: exclude file must be created"
        body = exclude_path.read_text(encoding="utf-8")
        for entry in JS_GITIGNORE_ENTRIES:
            assert entry in body

        gitignore_path = clone_dir / ".gitignore"
        assert not gitignore_path.exists(), (
            "F-011: .gitignore must NOT be touched (would shift HEAD off base_commit)"
        )


class TestGitignoreEntriesUsed:
    def test_constants_are_imported(self) -> None:
        tree = ast.parse(SETUP_JS_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "JS_GITIGNORE_ENTRIES":
                        assert len(JS_GITIGNORE_ENTRIES) > 0
                        return
        pytest.fail("setup_js.py must import JS_GITIGNORE_ENTRIES")
