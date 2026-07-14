"""Unit tests for tools.stub_go.

Includes XFAIL anchors for M5 (path-segment matching for vendor/testdata so
vendor_old/ and testdata-v2/ are NOT falsely skipped — mirroring the Java
"/src/test/" bug fix from the auto-memory).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

MODULE = "tools.stub_go"


from tools.stub_go import SKIP_DIRS, stub_go_repo


class TestSkipDirs:
    def test_is_set(self) -> None:
        """Test that SKIP_DIRS is a set (fast membership check, deduped)."""
        assert isinstance(SKIP_DIRS, set)

    def test_contains_known_skip_dirs(self) -> None:
        """Test that vendor, .git, testdata, node_modules are all present."""
        assert {"vendor", ".git", "testdata", "node_modules"}.issubset(SKIP_DIRS)


class TestStubGoRepo:
    def _make_repo(self, tmp_path, *files: tuple[str, str]) -> None:
        for rel, body in files:
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)

    def test_copies_source_to_output(self, tmp_path) -> None:
        """Test that stub_go_repo copies the source tree to out_dir."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(src, ("main.go", "package main\n"))
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            stub_go_repo(src, out)
        assert (out / "main.go").exists()

    def test_skips_test_files(self, tmp_path) -> None:
        """Test that *_test.go files are NOT passed to gostubber."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(
            src,
            ("main.go", "package main\n"),
            ("main_test.go", "package main\n"),
        )
        run_mock = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(f"{MODULE}.subprocess.run", run_mock):
            stub_go_repo(src, out)
        # subprocess.run must have been called for main.go but NOT main_test.go
        called_files = [call.args[0][-1] for call in run_mock.call_args_list]
        assert any("main.go" in f for f in called_files)
        assert not any("main_test.go" in f for f in called_files)

    def test_skips_doc_go(self, tmp_path) -> None:
        """Test that doc.go is not stubbed (it's the package documentation stub)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(
            src,
            ("main.go", "package main\n"),
            ("doc.go", "// Package foo docs\npackage foo\n"),
        )
        run_mock = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(f"{MODULE}.subprocess.run", run_mock):
            stub_go_repo(src, out)
        called_files = [call.args[0][-1] for call in run_mock.call_args_list]
        assert not any("doc.go" in f for f in called_files)

    def test_skips_vendor_dir(self, tmp_path) -> None:
        """Test that files under vendor/ are excluded via SKIP_DIRS copytree ignore."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(
            src,
            ("main.go", "package main\n"),
            ("vendor/dep/dep.go", "package dep\n"),
        )
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            stub_go_repo(src, out)
        assert not (out / "vendor").exists()

    def test_skips_testdata_dir(self, tmp_path) -> None:
        """Test that files under testdata/ are excluded via SKIP_DIRS copytree ignore."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(
            src,
            ("main.go", "package main\n"),
            ("testdata/fixture.json", "{}\n"),
        )
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            stub_go_repo(src, out)
        assert not (out / "testdata").exists()

    def test_dry_run_does_not_invoke_gostubber(self, tmp_path) -> None:
        """Test that dry_run=True skips subprocess.run and still counts files."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(src, ("main.go", "package main\n"))
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(f"{MODULE}.subprocess.run") as run_mock:
            count = stub_go_repo(src, out, dry_run=True)
        assert count == 1
        run_mock.assert_not_called()

    def test_returns_zero_on_gostubber_failure(self, tmp_path) -> None:
        """Test that a non-zero gostubber rc doesn't crash and returns 0 stubs."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(src, ("main.go", "package main\n"))
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="parse error"),
        ):
            count = stub_go_repo(src, out)
        assert count == 0

    def test_timeout_does_not_crash(self, tmp_path) -> None:
        """Test that a gostubber timeout is caught (logged, not raised)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(src, ("main.go", "package main\n"))
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gostubber"], timeout=30),
        ):
            # Should not raise
            count = stub_go_repo(src, out)
        assert count == 0

    def test_overwrites_existing_output(self, tmp_path) -> None:
        """Test that an existing out_dir is removed and recreated (no dirty merge)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        self._make_repo(src, ("main.go", "package main\n"))
        # Pre-populate out_dir with stale content
        out.mkdir()
        (out / "stale.go").write_text("stale\n")
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            stub_go_repo(src, out)
        assert not (out / "stale.go").exists()
        assert (out / "main.go").exists()


# ===== Path-segment matching regression guards =====
# stub_go currently uses exact set-membership on path segments (SKIP_DIRS is a
# set; matches use `p in SKIP_DIRS`) AND shutil.ignore_patterns with the bare
# names, both of which are exact-segment. These guards lock that in so a future
# refactor to substring matching (as in the Java /src/test/ bug from auto-memory)
# does not silently drop legit dirs. The M5 critique's substring-match concern
# does NOT apply to the current stub_go source — the tests below PASS today
# and act as regression barriers.

class TestPathSegmentMatching:
    def test_vendor_old_is_not_skipped(self, tmp_path) -> None:
        """Test that a directory literally named vendor_old/ is preserved (not skipped)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        (src / "vendor_old").mkdir(parents=True)
        (src / "vendor_old" / "code.go").write_text("package legit\n")
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            stub_go_repo(src, out)
        assert (out / "vendor_old" / "code.go").exists()

    def test_testdata_v2_is_not_skipped(self, tmp_path) -> None:
        """Test that a directory literally named testdata-v2/ is preserved (not skipped)."""
        src = tmp_path / "src"
        out = tmp_path / "out"
        (src / "testdata-v2").mkdir(parents=True)
        (src / "testdata-v2" / "code.go").write_text("package legit\n")
        with patch(
            f"{MODULE}._ensure_gostubber",
            return_value=tmp_path / "gostubber",
        ), patch(
            f"{MODULE}.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            stub_go_repo(src, out)
        assert (out / "testdata-v2" / "code.go").exists()
