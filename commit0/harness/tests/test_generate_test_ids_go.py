"""Unit tests for tools.generate_test_ids_go.

Locks in:
- B2 fix: _ensure_go_modules RAISES RuntimeError on failure (was silent no-op).
- B3 fix: _parse_go_test_list_plain sequencing (names FIRST then package summary).
- H1 fix: _find_docker_image catches DockerException/OSError (narrowed from bare
  Exception).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import docker.errors
import pytest

MODULE = "tools.generate_test_ids_go"


from tools.generate_test_ids_go import (
    _ensure_go_modules,
    _find_docker_image,
    _get_module_path,
    _parse_go_test_list_json,
    _parse_go_test_list_plain,
    collect_test_ids_local,
    save_test_ids,
)


class TestParseGoTestListJson:
    def test_extracts_test_names(self) -> None:
        """Test that JSON output events with Test-shaped Output become test IDs."""
        events = [
            {"Action": "output", "Package": "example.com/pkg", "Output": "TestFoo\n"},
            {"Action": "output", "Package": "example.com/pkg", "Output": "TestBar\n"},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        ids = _parse_go_test_list_json(stdout)
        assert ids == ["example.com/pkg/TestFoo", "example.com/pkg/TestBar"]

    def test_ignores_malformed_json_lines(self) -> None:
        """Test that non-JSON lines are silently skipped, not raised."""
        stdout = "not-json\n" + json.dumps(
            {"Action": "output", "Package": "p", "Output": "TestA\n"}
        )
        ids = _parse_go_test_list_json(stdout)
        assert ids == ["p/TestA"]

    def test_ignores_non_output_actions(self) -> None:
        """Test that Action != 'output' events are dropped."""
        events = [
            {"Action": "start", "Package": "p"},
            {"Action": "run", "Package": "p", "Test": "TestA"},
            {"Action": "pass", "Package": "p", "Test": "TestA"},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        assert _parse_go_test_list_json(stdout) == []

    def test_extracts_example_and_fuzz(self) -> None:
        """Test that ExampleX and FuzzX are recognised alongside TestX."""
        events = [
            {"Action": "output", "Package": "p", "Output": "ExampleFoo\n"},
            {"Action": "output", "Package": "p", "Output": "FuzzBar\n"},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        assert _parse_go_test_list_json(stdout) == ["p/ExampleFoo", "p/FuzzBar"]

    def test_skips_benchmark(self) -> None:
        """Test that BenchmarkX is NOT extracted (eval doesn't run -bench)."""
        events = [
            {"Action": "output", "Package": "p", "Output": "BenchmarkFoo\n"},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        assert _parse_go_test_list_json(stdout) == []

    def test_empty_input(self) -> None:
        """Test that empty input returns an empty list."""
        assert _parse_go_test_list_json("") == []

    def test_skips_events_missing_package(self) -> None:
        """Test that an output event missing Package is dropped."""
        events = [{"Action": "output", "Output": "TestA\n"}]
        stdout = "\n".join(json.dumps(e) for e in events)
        assert _parse_go_test_list_json(stdout) == []


class TestParseGoTestListPlain:
    def test_names_attached_to_following_summary(self) -> None:
        """Test that test names are grouped under the NEXT package summary line."""
        stdout = (
            "TestFoo\nTestBar\n"
            "ok  \texample.com/pkg\t0.100s\n"
        )
        ids = _parse_go_test_list_plain(stdout)
        assert ids == ["example.com/pkg/TestFoo", "example.com/pkg/TestBar"]

    def test_sequencing_across_multiple_packages(self) -> None:
        """Test that pending names flush only when THEIR package summary arrives."""
        stdout = (
            "TestA1\nTestA2\n"
            "ok  \texample.com/a\t0.1s\n"
            "TestB1\n"
            "FAIL\texample.com/b\t0.2s\n"
        )
        ids = _parse_go_test_list_plain(stdout)
        assert ids == [
            "example.com/a/TestA1",
            "example.com/a/TestA2",
            "example.com/b/TestB1",
        ]

    def test_leftover_pending_uses_module_path(self) -> None:
        """Test that names with no trailing summary fall back to module_path."""
        stdout = "TestOrphan\n"
        ids = _parse_go_test_list_plain(stdout, module_path="example.com/mod")
        assert ids == ["example.com/mod/TestOrphan"]

    def test_leftover_pending_dropped_without_module_path(self) -> None:
        """Test that orphan names are dropped when module_path is empty."""
        stdout = "TestOrphan\n"
        assert _parse_go_test_list_plain(stdout, module_path="") == []

    def test_no_test_files_summary_still_flushes(self) -> None:
        """Test that '?  pkg [no test files]' also acts as a package boundary."""
        stdout = (
            "TestA\n"
            "?   \texample.com/empty\t[no test files]\n"
        )
        ids = _parse_go_test_list_plain(stdout)
        assert ids == ["example.com/empty/TestA"]

    def test_skips_error_and_separator_lines(self) -> None:
        """Test that lines like --- FAIL:, # pkg, and 'FAIL\\tpkg' are handled."""
        stdout = (
            "--- FAIL: TestShouldNotAppear\n"
            "# example.com/pkg [build failed]\n"
            "FAIL\texample.com/pkg\t[build failed]\n"
            "TestReal\n"
            "ok  \texample.com/other\t0.001s\n"
        )
        ids = _parse_go_test_list_plain(stdout)
        assert ids == ["example.com/other/TestReal"]

    def test_empty_input_returns_empty(self) -> None:
        """Test that empty stdout returns an empty list."""
        assert _parse_go_test_list_plain("") == []


class TestGetModulePath:
    def test_reads_go_mod(self, tmp_path) -> None:
        """Test that _get_module_path returns the declared module path."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
        assert _get_module_path(tmp_path) == "example.com/foo"

    def test_no_go_mod_returns_empty(self, tmp_path) -> None:
        """Test that a directory without go.mod returns ''."""
        assert _get_module_path(tmp_path) == ""

    def test_no_module_line_returns_empty(self, tmp_path) -> None:
        """Test that a go.mod without a module line returns ''."""
        (tmp_path / "go.mod").write_text("go 1.21\n")
        assert _get_module_path(tmp_path) == ""

    def test_ignores_leading_whitespace(self, tmp_path) -> None:
        """Test that a module line with leading whitespace is still parsed."""
        (tmp_path / "go.mod").write_text("   module example.com/bar\n")
        assert _get_module_path(tmp_path) == "example.com/bar"


class TestEnsureGoModules:
    def test_skips_when_vendored(self, tmp_path) -> None:
        """Test that a vendor/modules.txt short-circuits (no download)."""
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "modules.txt").write_text("# modules\n")
        with patch(f"{MODULE}.subprocess.run") as mock_run:
            _ensure_go_modules(tmp_path)
        mock_run.assert_not_called()

    def test_skips_when_no_go_mod(self, tmp_path) -> None:
        """Test that a directory without go.mod short-circuits."""
        with patch(f"{MODULE}.subprocess.run") as mock_run:
            _ensure_go_modules(tmp_path)
        mock_run.assert_not_called()

    def test_success_returns_none(self, tmp_path) -> None:
        """Test that a successful `go mod download` returns None (side-effect only)."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=ok):
            assert _ensure_go_modules(tmp_path) is None

    def test_nonzero_exit_raises_runtimeerror(self, tmp_path) -> None:
        """Test B2 fix: non-zero exit raises RuntimeError with rc + stderr."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        fail = MagicMock(returncode=1, stdout="", stderr="module not found")
        with patch(f"{MODULE}.subprocess.run", return_value=fail):
            with pytest.raises(RuntimeError, match=r"go mod download failed .*rc=1"):
                _ensure_go_modules(tmp_path)

    def test_timeout_raises_runtimeerror(self, tmp_path) -> None:
        """Test B2 fix: TimeoutExpired is wrapped into a RuntimeError."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        with patch(
            f"{MODULE}.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["go"], timeout=30),
        ):
            with pytest.raises(RuntimeError, match="go mod download timed out"):
                _ensure_go_modules(tmp_path, timeout=30)

    def test_missing_go_binary_raises_runtimeerror(self, tmp_path) -> None:
        """Test B2 fix: OSError (missing go binary) is wrapped into RuntimeError."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        with patch(f"{MODULE}.subprocess.run", side_effect=OSError("go: not found")):
            with pytest.raises(RuntimeError, match="could not be executed"):
                _ensure_go_modules(tmp_path)


class TestCollectTestIdsLocal:
    def test_json_success_no_fallback(self, tmp_path) -> None:
        """Test that a successful JSON parse skips the plain-text fallback."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        json_stdout = json.dumps(
            {"Action": "output", "Package": "example.com/foo/pkg", "Output": "TestA\n"}
        )
        mock_ok = MagicMock(returncode=0, stdout=json_stdout, stderr="")
        with patch(f"{MODULE}.subprocess.run", return_value=mock_ok) as mock_run:
            ids = collect_test_ids_local(tmp_path)
        assert ids == ["example.com/foo/pkg/TestA"]
        # 1 call for _ensure_go_modules + 1 call for `go test -list -json` = 2
        assert mock_run.call_count == 2

    def test_json_empty_triggers_plain_fallback(self, tmp_path) -> None:
        """Test that when the JSON parse yields 0 IDs, the plain-text fallback runs."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")
        call_index = [0]

        def side_effect(*args, **kwargs):
            call_index[0] += 1
            # 1: _ensure_go_modules (go mod download)  → success
            # 2: go test -list -json                    → empty
            # 3: go test -list (plain fallback)         → "TestPlain" + summary
            if call_index[0] == 1:
                return MagicMock(returncode=0, stdout="", stderr="")
            if call_index[0] == 2:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(
                returncode=0,
                stdout="TestPlain\nok  \texample.com/foo\t0.01s\n",
                stderr="",
            )

        with patch(f"{MODULE}.subprocess.run", side_effect=side_effect):
            ids = collect_test_ids_local(tmp_path)
        assert ids == ["example.com/foo/TestPlain"]
        assert call_index[0] == 3

    def test_json_timeout_returns_empty(self, tmp_path) -> None:
        """Test that the primary go test -list timeout returns []."""
        (tmp_path / "go.mod").write_text("module example.com/foo\n")

        calls = [0]

        def side_effect(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise subprocess.TimeoutExpired(cmd=["go"], timeout=30)

        with patch(f"{MODULE}.subprocess.run", side_effect=side_effect):
            assert collect_test_ids_local(tmp_path, timeout=30) == []


class TestFindDockerImage:
    def test_returns_matching_tag(self) -> None:
        """Test that a matching commit0.repo.<short>. tag is returned."""
        mock_img = MagicMock()
        mock_img.tags = ["commit0.repo.foo.v0", "other:tag"]
        mock_client = MagicMock()
        mock_client.images.list.return_value = [mock_img]
        with patch(f"{MODULE}.docker.from_env", return_value=mock_client):
            assert _find_docker_image("acme__foo-x") == "commit0.repo.foo.v0"

    def test_returns_none_when_no_match(self) -> None:
        """Test that a repo without a matching tag returns None."""
        mock_img = MagicMock()
        mock_img.tags = ["commit0.repo.other.v0"]
        mock_client = MagicMock()
        mock_client.images.list.return_value = [mock_img]
        with patch(f"{MODULE}.docker.from_env", return_value=mock_client):
            assert _find_docker_image("acme__foo-x") is None

    def test_docker_exception_returns_none(self) -> None:
        """Test H1 fix: docker.errors.DockerException is caught → None."""
        with patch(
            f"{MODULE}.docker.from_env",
            side_effect=docker.errors.DockerException("daemon down"),
        ):
            assert _find_docker_image("acme__foo") is None

    def test_os_error_returns_none(self) -> None:
        """Test H1 fix: OSError (socket missing) is caught → None."""
        with patch(f"{MODULE}.docker.from_env", side_effect=OSError("socket")):
            assert _find_docker_image("acme__foo") is None

    def test_unexpected_exception_propagates(self) -> None:
        """Test H1 fix: unrelated exceptions (e.g. RuntimeError) are NOT swallowed."""
        with patch(
            f"{MODULE}.docker.from_env",
            side_effect=RuntimeError("bug"),
        ):
            with pytest.raises(RuntimeError, match="bug"):
                _find_docker_image("acme__foo")


class TestSaveTestIds:
    def test_writes_bz2_file(self, tmp_path) -> None:
        """Test that save_test_ids writes an on-disk .bz2 file with the joined IDs."""
        import bz2

        path = save_test_ids(["p/TestA", "p/TestB"], "acme.thing", tmp_path)
        assert path.exists()
        assert path.suffix == ".bz2"
        assert path.name == "acme-thing.bz2"  # dots replaced with dashes
        with bz2.open(path, "rt") as f:
            assert f.read() == "p/TestA\np/TestB"

    def test_creates_output_dir(self, tmp_path) -> None:
        """Test that save_test_ids creates the output directory if missing."""
        out = tmp_path / "nested" / "out"
        save_test_ids(["p/TestA"], "foo", out)
        assert out.is_dir()
