"""Tests that TS batch mode honors per-candidate src_dir / src_dir_override.

Covers fix C for the graphql-editor/graphql-zeus monorepo regression: the
TS batch loop in tools/prepare_repo_ts.py previously hard-coded
src_dir_override=None, ignoring per-candidate hints. Now it threads
candidate['src_dir_override'] or candidate['src_dir'] through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

MODULE = "tools.prepare_repo_ts"


def _entry_result(name: str = "my-lib") -> dict:
    """Minimal valid prepare_ts_repo return shape."""
    return {
        "instance_id": f"commit-0/{name}",
        "repo": f"Org/{name}",
        "original_repo": f"owner/{name}",
        "base_commit": "abc",
        "reference_commit": "def",
        "src_dir": "src",
        "language": "typescript",
        "test_framework": "jest",
        "setup": {},
        "test": {},
    }


def _build_batch_argv(input_file: Path, clone_dir: Path) -> list[str]:
    """Build the sys.argv the batch test invocation needs."""
    return [
        "prepare_repo_ts.py",
        str(input_file),
        "--dry-run",
        "--clone-dir",
        str(clone_dir),
    ]


def _invoke_main_with_candidates(
    tmp_path: Path, candidates: list[dict]
) -> Mapping[str, Any]:
    """Drive main() in batch mode and return prepare_ts_repo's kwargs."""
    from tools.prepare_repo_ts import main

    input_file = tmp_path / "candidates.json"
    input_file.write_text(json.dumps(candidates))

    test_args = _build_batch_argv(input_file, tmp_path)

    with patch("sys.argv", test_args):
        with patch(f"{MODULE}._validate_stubber_deps"):
            with patch(
                f"{MODULE}.prepare_ts_repo", return_value=_entry_result()
            ) as mock_prep:
                main()

    assert mock_prep.call_count == 1, (
        f"expected exactly one prepare_ts_repo call, got "
        f"{mock_prep.call_count}"
    )
    return mock_prep.call_args.kwargs


class TestBatchSrcDirOverride:
    """Candidate-level src_dir hints flow through the batch loop."""

    def test_candidate_src_dir_override_used(self, tmp_path: Path) -> None:
        kwargs = _invoke_main_with_candidates(
            tmp_path,
            [
                {
                    "full_name": "owner/my-lib",
                    "src_dir_override": "packages/foo/src",
                }
            ],
        )
        assert kwargs["full_name"] == "owner/my-lib"
        assert kwargs["src_dir_override"] == "packages/foo/src"

    def test_candidate_src_dir_used_when_no_override(
        self, tmp_path: Path
    ) -> None:
        """src_dir falls through when src_dir_override is absent."""
        kwargs = _invoke_main_with_candidates(
            tmp_path,
            [{"full_name": "owner/my-lib", "src_dir": "packages/bar/src"}],
        )
        assert kwargs["src_dir_override"] == "packages/bar/src"

    def test_src_dir_override_wins_over_src_dir(self, tmp_path: Path) -> None:
        """If both are present, src_dir_override takes precedence."""
        kwargs = _invoke_main_with_candidates(
            tmp_path,
            [
                {
                    "full_name": "owner/my-lib",
                    "src_dir_override": "explicit/override",
                    "src_dir": "should/be/ignored",
                }
            ],
        )
        assert kwargs["src_dir_override"] == "explicit/override"

    def test_neither_present_passes_none(self, tmp_path: Path) -> None:
        kwargs = _invoke_main_with_candidates(
            tmp_path, [{"full_name": "owner/my-lib"}]
        )
        assert kwargs["src_dir_override"] is None

    def test_repo_key_works_instead_of_full_name(self, tmp_path: Path) -> None:
        """Candidates may use 'repo' as the name field."""
        kwargs = _invoke_main_with_candidates(
            tmp_path,
            [{"repo": "owner/my-lib", "src_dir": "packages/baz/src"}],
        )
        assert kwargs["full_name"] == "owner/my-lib"
        assert kwargs["src_dir_override"] == "packages/baz/src"
