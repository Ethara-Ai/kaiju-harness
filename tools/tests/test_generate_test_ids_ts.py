"""Tests for tools/generate_test_ids_ts.py — TypeScript test ID collection."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.generate_test_ids_ts import (
    _build_collect_command,
    _detect_framework_from_entry,
    _dispatch_parse,
    _normalize_ts_test_ids,
    _parse_jest_list_output,
    _parse_vitest_list_output,
    collect_ts_test_ids_docker,
    collect_ts_test_ids_local,
    generate_for_ts_dataset,
    validate_ts_base_commit_docker,
)

MODULE = "tools.generate_test_ids_ts"


# ---------------------------------------------------------------------------
# _parse_vitest_list_output
# ---------------------------------------------------------------------------


class TestParseVitestListOutput:
    def test_valid_json_array(self):
        stdout = json.dumps(
            [
                {
                    "name": "adds numbers",
                    "file": "/testbed/__tests__/math.test.ts",
                    "projectName": "default",
                },
                {"name": "subtracts", "file": "/testbed/__tests__/math.test.ts"},
            ]
        )
        result = _parse_vitest_list_output(stdout)
        assert result == [
            "__tests__/math.test.ts > adds numbers",
            "__tests__/math.test.ts > subtracts",
        ]

    def test_preamble_before_json(self):
        preamble = "Loading vitest config...\nDEPRECATION WARNING: something\n"
        payload = json.dumps(
            [
                {"name": "test1", "file": "/testbed/src/a.test.ts"},
            ]
        )
        stdout = preamble + payload
        result = _parse_vitest_list_output(stdout)
        assert result == ["src/a.test.ts > test1"]

    def test_empty_stdout(self):
        assert _parse_vitest_list_output("") == []

    def test_whitespace_only(self):
        assert _parse_vitest_list_output("   \n  ") == []

    def test_no_json_array(self):
        assert _parse_vitest_list_output("Error: Cannot find module 'vitest'") == []

    def test_malformed_json(self):
        assert _parse_vitest_list_output("[{name: invalid json}]") == []

    def test_missing_name_field(self):
        stdout = json.dumps(
            [
                {"file": "/testbed/a.test.ts"},
                {"name": "good", "file": "/testbed/b.test.ts"},
            ]
        )
        result = _parse_vitest_list_output(stdout)
        assert result == ["b.test.ts > good"]

    def test_missing_file_field(self):
        stdout = json.dumps(
            [
                {"name": "orphan"},
                {"name": "ok", "file": "/testbed/x.test.ts"},
            ]
        )
        result = _parse_vitest_list_output(stdout)
        assert result == ["x.test.ts > ok"]

    def test_custom_repo_root(self):
        stdout = json.dumps(
            [
                {"name": "t1", "file": "/home/user/project/src/a.test.ts"},
            ]
        )
        result = _parse_vitest_list_output(stdout, repo_root="/home/user/project")
        assert result == ["src/a.test.ts > t1"]

    def test_non_dict_entries_skipped(self):
        stdout = json.dumps(
            ["string_entry", {"name": "ok", "file": "/testbed/a.test.ts"}]
        )
        result = _parse_vitest_list_output(stdout)
        assert result == ["a.test.ts > ok"]

    def test_absolute_path_different_root(self):
        stdout = json.dumps([{"name": "t", "file": "/other/root/a.test.ts"}])
        result = _parse_vitest_list_output(stdout, repo_root="/testbed")
        assert result == ["other/root/a.test.ts > t"]

    def test_relative_file_path(self):
        stdout = json.dumps([{"name": "t", "file": "src/a.test.ts"}])
        result = _parse_vitest_list_output(stdout)
        assert result == ["src/a.test.ts > t"]


# ---------------------------------------------------------------------------
# _parse_jest_list_output
# ---------------------------------------------------------------------------


class TestParseJestListOutput:
    def test_standard_line_output(self):
        stdout = "/testbed/__tests__/foo.test.ts\n/testbed/__tests__/bar.test.ts\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["__tests__/foo.test.ts", "__tests__/bar.test.ts"]

    def test_json_array_output(self):
        stdout = json.dumps(["/testbed/src/a.test.ts", "/testbed/src/b.spec.js"])
        result = _parse_jest_list_output(stdout)
        assert result == ["src/a.test.ts", "src/b.spec.js"]

    def test_empty_stdout(self):
        assert _parse_jest_list_output("") == []

    def test_whitespace_only(self):
        assert _parse_jest_list_output("  \n  ") == []

    def test_noise_lines_filtered(self):
        stdout = (
            "Determining test suites to run...\n"
            "/testbed/__tests__/ok.test.ts\n"
            "PASS some/other/thing\n"
            "FAIL another/thing\n"
        )
        result = _parse_jest_list_output(stdout)
        assert result == ["__tests__/ok.test.ts"]

    def test_mixed_extensions(self):
        stdout = (
            "/testbed/a.test.ts\n"
            "/testbed/b.spec.js\n"
            "/testbed/c.test.tsx\n"
            "/testbed/d.spec.jsx\n"
        )
        result = _parse_jest_list_output(stdout)
        assert len(result) == 4
        assert "a.test.ts" in result
        assert "b.spec.js" in result
        assert "c.test.tsx" in result
        assert "d.spec.jsx" in result

    def test_relative_paths(self):
        stdout = "__tests__/foo.test.ts\n__tests__/bar.test.ts\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["__tests__/foo.test.ts", "__tests__/bar.test.ts"]

    def test_custom_repo_root(self):
        stdout = "/workspace/src/a.test.ts\n/workspace/src/b.test.ts\n"
        result = _parse_jest_list_output(stdout, repo_root="/workspace")
        assert result == ["src/a.test.ts", "src/b.test.ts"]

    def test_path_heuristic_no_extension(self):
        stdout = "/testbed/some/path/without/ext\nnot a path at all\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["some/path/without/ext"]

    def test_json_decode_falls_through(self):
        stdout = "[invalid json array\n/testbed/a.test.ts\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["a.test.ts"]


# ---------------------------------------------------------------------------
# _detect_framework_from_entry
# ---------------------------------------------------------------------------


class TestDetectFrameworkFromEntry:
    def test_explicit_vitest(self):
        assert _detect_framework_from_entry({"test_framework": "vitest"}) == "vitest"

    def test_explicit_jest(self):
        assert _detect_framework_from_entry({"test_framework": "jest"}) == "jest"

    def test_explicit_case_insensitive(self):
        assert _detect_framework_from_entry({"test_framework": "Vitest"}) == "vitest"
        assert _detect_framework_from_entry({"test_framework": " JEST "}) == "jest"

    def test_infer_from_test_cmd_vitest(self):
        entry = {"test": {"test_cmd": "npx vitest run"}}
        assert _detect_framework_from_entry(entry) == "vitest"

    def test_infer_from_test_cmd_jest(self):
        entry = {"test": {"test_cmd": "npx jest --coverage"}}
        assert _detect_framework_from_entry(entry) == "jest"

    def test_fallback_to_jest(self):
        entry = {"repo": "some-repo", "test": {"test_dir": "tests"}}
        assert _detect_framework_from_entry(entry) == "jest"

    def test_empty_entry(self):
        assert _detect_framework_from_entry({}) == "jest"

    def test_non_dict_test_field(self):
        entry = {"test": "not_a_dict"}
        assert _detect_framework_from_entry(entry) == "jest"


# ---------------------------------------------------------------------------
# _build_collect_command
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _parse_jest_json_results
# ---------------------------------------------------------------------------


class TestParseJestJsonResults:
    def test_full_json_report(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "testFilePath": "/testbed/__tests__/math.test.ts",
                    "assertionResults": [
                        {"fullName": "adds numbers", "status": "passed"},
                        {"fullName": "subtracts", "status": "failed"},
                    ],
                }
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == [
            "__tests__/math.test.ts > adds numbers",
            "__tests__/math.test.ts > subtracts",
        ]

    def test_missing_fullname_uses_title_ancestors(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "testFilePath": "/testbed/a.test.ts",
                    "assertionResults": [
                        {
                            "ancestorTitles": ["Math", "add"],
                            "title": "returns sum",
                            "status": "passed",
                        },
                    ],
                }
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == ["a.test.ts > Math > add > returns sum"]

    def test_preamble_before_json(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        preamble = "Determining test suites...\n"
        report = {
            "testResults": [
                {
                    "testFilePath": "/testbed/x.test.ts",
                    "assertionResults": [{"fullName": "works", "status": "passed"}],
                }
            ]
        }
        result = _parse_jest_json_results(preamble + json.dumps(report))
        assert len(result) == 1

    def test_empty_report_falls_to_list_parser(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        result = _parse_jest_json_results("/testbed/a.test.ts\n")
        assert result == ["a.test.ts"]

    def test_empty_input(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        assert _parse_jest_json_results("") == []

    def test_no_test_results_falls_to_list_parser(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        result = _parse_jest_json_results(json.dumps({"success": True}))
        assert result == []

    def test_empty_assertions_falls_to_list_parser(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {"testFilePath": "/testbed/a.test.ts", "assertionResults": []}
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == []

    def test_uses_name_field_when_no_testFilePath(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "name": "/testbed/b.test.ts",
                    "assertionResults": [{"fullName": "test1", "status": "passed"}],
                }
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == ["b.test.ts > test1"]


class TestBuildCollectCommand:
    def test_vitest_command(self):
        assert _build_collect_command("vitest", "__tests__") == [
            "npx",
            "vitest",
            "list",
            "--json",
            "__tests__",
        ]

    def test_jest_command(self):
        assert _build_collect_command("jest", "src") == [
            "npx",
            "jest",
            "--json",
            "--forceExit",
            "src",
        ]

    def test_custom_test_dir(self):
        cmd = _build_collect_command("vitest", "packages/core/tests")
        assert "packages/core/tests" in cmd

    def test_unknown_framework_raises(self):
        with pytest.raises(ValueError, match="Unknown framework"):
            _build_collect_command("mocha", "__tests__")


# ---------------------------------------------------------------------------
# _dispatch_parse
# ---------------------------------------------------------------------------


class TestDispatchParse:
    def test_dispatches_vitest(self):
        stdout = json.dumps([{"name": "t1", "file": "/testbed/a.test.ts"}])
        result = _dispatch_parse(stdout, "vitest")
        assert result == ["a.test.ts > t1"]

    def test_dispatches_jest(self):
        stdout = "/testbed/a.test.ts\n"
        result = _dispatch_parse(stdout, "jest")
        assert result == ["a.test.ts"]

    def test_unknown_falls_back_to_jest(self):
        stdout = "/testbed/a.test.ts\n"
        result = _dispatch_parse(stdout, "mocha")
        assert result == ["a.test.ts"]


# ---------------------------------------------------------------------------
# _normalize_ts_test_ids
# ---------------------------------------------------------------------------


class TestNormalizeTsTestIds:
    def test_already_prefixed(self):
        ids = ["__tests__/foo.test.ts > bar"]
        assert _normalize_ts_test_ids(ids, "__tests__") == ids

    def test_needs_prefix_vitest(self):
        ids = ["foo.test.ts > bar"]
        result = _normalize_ts_test_ids(ids, "__tests__")
        assert result == ["__tests__/foo.test.ts > bar"]

    def test_jest_file_paths(self):
        ids = ["math.test.ts"]
        result = _normalize_ts_test_ids(ids, "test")
        assert result == ["test/math.test.ts"]

    def test_root_test_dir(self):
        ids = ["foo.test.ts > bar"]
        assert _normalize_ts_test_ids(ids, ".") == ids

    def test_empty_test_dir(self):
        ids = ["foo.test.ts"]
        assert _normalize_ts_test_ids(ids, "") == ids

    def test_empty_lines_filtered(self):
        ids = ["", "  ", "foo.test.ts > bar"]
        result = _normalize_ts_test_ids(ids, "__tests__")
        assert result == ["__tests__/foo.test.ts > bar"]

    def test_absolute_path_not_prefixed(self):
        ids = ["/some/abs/path.test.ts > baz"]
        result = _normalize_ts_test_ids(ids, "__tests__")
        assert result == ["/some/abs/path.test.ts > baz"]

    def test_trailing_slash_on_test_dir(self):
        ids = ["math.test.ts"]
        result = _normalize_ts_test_ids(ids, "test/")
        assert result == ["test/math.test.ts"]


# ---------------------------------------------------------------------------
# collect_ts_test_ids_local
# ---------------------------------------------------------------------------


class TestCollectTsTestIdsLocal:
    @patch(f"{MODULE}.subprocess.run")
    def test_vitest_local_success(self, mock_run: MagicMock):
        vitest_output = json.dumps(
            [
                {"name": "adds", "file": "/repo/__tests__/math.test.ts"},
            ]
        )
        mock_run.return_value = MagicMock(stdout=vitest_output, stderr="", returncode=0)
        result = collect_ts_test_ids_local(
            repo_dir=Path("/repo"), test_dir="__tests__", framework="vitest"
        )
        assert result == ["__tests__/math.test.ts > adds"]
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["npx", "vitest", "list", "--json", "__tests__"]

    @patch(f"{MODULE}.subprocess.run")
    def test_jest_local_success(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(
            stdout="/cwd/src/a.test.ts\n/cwd/src/b.test.ts\n", stderr="", returncode=0
        )
        result = collect_ts_test_ids_local(
            repo_dir=Path("/cwd"), test_dir="src", framework="jest"
        )
        assert result == ["src/a.test.ts", "src/b.test.ts"]

    @patch(f"{MODULE}.subprocess.run")
    def test_timeout_returns_empty(self, mock_run: MagicMock):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="npx", timeout=300)
        result = collect_ts_test_ids_local(repo_dir=Path("/repo"), framework="jest")
        assert result == []

    @patch(f"{MODULE}.subprocess.run")
    def test_vitest_fallback_on_empty(self, mock_run: MagicMock):
        fallback_output = json.dumps(
            [
                {"name": "from_fallback", "file": "/repo/__tests__/x.test.ts"},
            ]
        )
        primary_result = MagicMock(stdout="", stderr="", returncode=1)
        fallback_result = MagicMock(stdout=fallback_output, stderr="", returncode=0)
        mock_run.side_effect = [primary_result, fallback_result]

        result = collect_ts_test_ids_local(
            repo_dir=Path("/repo"), test_dir="__tests__", framework="vitest"
        )
        assert mock_run.call_count == 2
        assert result == ["__tests__/x.test.ts > from_fallback"]

    @patch(f"{MODULE}.subprocess.run")
    def test_file_not_found(self, mock_run: MagicMock):
        mock_run.side_effect = FileNotFoundError("npx not found")
        result = collect_ts_test_ids_local(repo_dir=Path("/repo"), framework="jest")
        assert result == []


# ---------------------------------------------------------------------------
# collect_ts_test_ids_docker
# ---------------------------------------------------------------------------


class TestCollectTsTestIdsDocker:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_vitest_docker_success(self, mock_docker: MagicMock, mock_plat: MagicMock):
        vitest_json = json.dumps(
            [
                {"name": "test1", "file": "/testbed/__tests__/a.test.ts"},
            ]
        )
        mock_client = MagicMock()
        mock_client.containers.run.return_value = vitest_json.encode()
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="mylib",
            test_dir="__tests__",
            framework="vitest",
            image_name="commit0.repo.mylib:v0",
            reference_commit="abc1234",
        )
        assert result == ["__tests__/a.test.ts > test1"]
        call_args = mock_client.containers.run.call_args
        bash_cmd = (
            call_args[1]["command"] if "command" in call_args[1] else call_args[0][1]
        )
        if isinstance(bash_cmd, list):
            bash_cmd = bash_cmd[2]
        assert "git checkout abc1234" in bash_cmd
        assert "vitest list --json" in bash_cmd

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_jest_docker_success(self, mock_docker: MagicMock, mock_plat: MagicMock):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/src/a.test.ts\n"
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="mylib",
            framework="jest",
            image_name="img:v0",
        )
        assert result == ["src/a.test.ts"]

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_no_reference_commit_skips_checkout(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="img:v0", reference_commit=None
        )
        call_args = mock_client.containers.run.call_args
        bash_cmd = (
            call_args[1]["command"] if "command" in call_args[1] else call_args[0][1]
        )
        assert "git checkout" not in bash_cmd

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_image_not_found(self, mock_docker: MagicMock, mock_plat: MagicMock):
        import docker.errors as de

        mock_client = MagicMock()
        mock_client.containers.run.side_effect = de.ImageNotFound("not found")
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="bad:v0"
        )
        assert result == []

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_container_error_parses_stderr(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        import docker.errors as de

        error = de.ContainerError(
            container="c",
            exit_status=1,
            command="cmd",
            image="img",
            stderr=b"/testbed/x.test.ts\n",
        )
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = error
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert result == ["x.test.ts"]

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_read_timeout(self, mock_docker: MagicMock, mock_plat: MagicMock):
        import requests.exceptions

        mock_client = MagicMock()
        mock_client.containers.run.side_effect = requests.exceptions.ReadTimeout()
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert result == []


# ---------------------------------------------------------------------------
# validate_ts_base_commit_docker
# ---------------------------------------------------------------------------


class TestValidateTsBaseCommitDocker:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_successful_validation(self, mock_docker: MagicMock, mock_plat: MagicMock):
        vitest_json = json.dumps(
            [
                {"name": f"test{i}", "file": f"/testbed/__tests__/t{i}.test.ts"}
                for i in range(10)
            ]
        )
        mock_client = MagicMock()
        mock_client.containers.run.return_value = vitest_json.encode()
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="mylib", framework="vitest", image_name="img:v0"
        )
        assert count == 10
        assert isinstance(snippet, str)

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_zero_tests_at_base(self, mock_docker: MagicMock, mock_plat: MagicMock):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"Error: cannot find module\n"
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert count == 0
        assert "cannot find module" in snippet

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_timeout_returns_zero(self, mock_docker: MagicMock, mock_plat: MagicMock):
        import requests.exceptions

        mock_client = MagicMock()
        mock_client.containers.run.side_effect = requests.exceptions.ReadTimeout()
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert count == 0
        assert snippet == "timeout"

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_no_checkout_in_command(self, mock_docker: MagicMock, mock_plat: MagicMock):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b""
        mock_docker.return_value = mock_client

        validate_ts_base_commit_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        call_args = mock_client.containers.run.call_args
        bash_cmd = (
            call_args[1]["command"] if "command" in call_args[1] else call_args[0][1]
        )
        assert "git checkout" not in bash_cmd


# ---------------------------------------------------------------------------
# generate_for_ts_dataset
# ---------------------------------------------------------------------------


class TestGenerateForTsDataset:
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_docker")
    def test_docker_mode_full_pipeline(
        self, mock_collect: MagicMock, mock_save: MagicMock, tmp_path: Path
    ):
        dataset = [
            {
                "repo": "org/mathlib",
                "test_framework": "vitest",
                "test": {"test_dir": "__tests__"},
                "reference_commit": "abc123",
            },
            {
                "repo": "org/utils",
                "test_framework": "jest",
                "test": {"test_dir": "src"},
                "reference_commit": "def456",
            },
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        mock_collect.side_effect = [
            ["__tests__/a.test.ts > t1", "__tests__/a.test.ts > t2"],
            ["src/b.test.ts"],
        ]
        mock_save.return_value = tmp_path / "fake.bz2"

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=True,
        )
        assert results["mathlib"] == 2
        assert results["utils"] == 1
        assert mock_save.call_count == 2

    @patch(f"{MODULE}._find_docker_image", return_value=None)
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    @patch(f"{MODULE}._find_repo_dir")
    def test_local_mode_repo_not_found(
        self,
        mock_find: MagicMock,
        mock_collect: MagicMock,
        mock_save: MagicMock,
        mock_img: MagicMock,
        tmp_path: Path,
    ):
        dataset = [{"repo": "org/missing", "test": {"test_dir": "__tests__"}}]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))
        mock_find.return_value = None

        results = generate_for_ts_dataset(
            dataset_path=ds_file, output_dir=tmp_path / "out"
        )
        assert results["missing"] == 0
        mock_collect.assert_not_called()

    @patch(f"{MODULE}.validate_ts_base_commit_docker")
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_docker")
    def test_validate_base_failure(
        self,
        mock_collect: MagicMock,
        mock_save: MagicMock,
        mock_validate: MagicMock,
        tmp_path: Path,
    ):
        dataset = [
            {
                "repo": "org/lib",
                "test_framework": "vitest",
                "test": {"test_dir": "__tests__"},
                "reference_commit": "ref1",
            },
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        mock_collect.return_value = [
            f"__tests__/t.test.ts > test{i}" for i in range(10)
        ]
        mock_save.return_value = tmp_path / "fake.bz2"
        mock_validate.return_value = (0, "Error: module not found")

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=True,
            validate_base=True,
        )
        assert results["lib"] == -10

    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_docker")
    def test_framework_override(
        self, mock_collect: MagicMock, mock_save: MagicMock, tmp_path: Path
    ):
        dataset = [
            {
                "repo": "org/repo1",
                "test_framework": "jest",
                "test": {"test_dir": "__tests__"},
            },
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        mock_collect.return_value = ["__tests__/a.test.ts > t1"]
        mock_save.return_value = tmp_path / "fake.bz2"

        generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=True,
            framework_override="vitest",
        )
        call_kwargs = mock_collect.call_args[1]
        assert call_kwargs["framework"] == "vitest"

    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_docker")
    def test_max_repos_limit(
        self, mock_collect: MagicMock, mock_save: MagicMock, tmp_path: Path
    ):
        dataset = [
            {"repo": f"org/repo{i}", "test": {"test_dir": "__tests__"}}
            for i in range(5)
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        mock_collect.return_value = ["__tests__/a.test.ts"]
        mock_save.return_value = tmp_path / "fake.bz2"

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=True,
            max_repos=2,
        )
        assert len(results) == 2

    def test_dict_format_with_data_key(self, tmp_path: Path):
        dataset = {
            "data": [
                {
                    "repo": "org/r",
                    "test_framework": "jest",
                    "test": {"test_dir": "__tests__"},
                },
            ]
        }
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        with (
            patch(f"{MODULE}.collect_ts_test_ids_docker") as mock_collect,
            patch(f"{MODULE}.save_test_ids") as mock_save,
        ):
            mock_collect.return_value = ["__tests__/a.test.ts"]
            mock_save.return_value = tmp_path / "fake.bz2"
            results = generate_for_ts_dataset(
                dataset_path=ds_file, output_dir=tmp_path / "out", use_docker=True
            )
            assert results["r"] == 1

    def test_invalid_dataset_format_raises(self, tmp_path: Path):
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps({"wrong_key": "value"}))
        with pytest.raises(ValueError, match="Unknown dataset format"):
            generate_for_ts_dataset(dataset_path=ds_file, output_dir=tmp_path / "out")


# ---------------------------------------------------------------------------
# Additional _parse_vitest_list_output tests
# ---------------------------------------------------------------------------


class TestParseVitestListOutputExtended:
    def test_malformed_json_no_closing_bracket(self):
        stdout = '[{"name": "t1", "file": "/testbed/a.test.ts"}'
        result = _parse_vitest_list_output(stdout)
        assert result == []

    def test_empty_array(self):
        result = _parse_vitest_list_output("[]")
        assert result == []

    def test_nested_describe_blocks(self):
        stdout = json.dumps(
            [
                {
                    "name": "Math > add > returns sum of two numbers",
                    "file": "/testbed/__tests__/math.test.ts",
                },
                {
                    "name": "Math > subtract > returns difference",
                    "file": "/testbed/__tests__/math.test.ts",
                },
            ]
        )
        result = _parse_vitest_list_output(stdout)
        assert result == [
            "__tests__/math.test.ts > Math > add > returns sum of two numbers",
            "__tests__/math.test.ts > Math > subtract > returns difference",
        ]


# ---------------------------------------------------------------------------
# Additional _parse_jest_list_output tests
# ---------------------------------------------------------------------------


class TestParseJestListOutputExtended:
    def test_json_with_test_results_empty_assertion_results(self):
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "testFilePath": "/testbed/__tests__/empty.test.ts",
                    "assertionResults": [],
                }
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == []

    def test_plain_text_paths(self):
        stdout = (
            "/testbed/src/utils/helpers.test.ts\n" "/testbed/src/core/engine.spec.js\n"
        )
        result = _parse_jest_list_output(stdout)
        assert result == ["src/utils/helpers.test.ts", "src/core/engine.spec.js"]


# ---------------------------------------------------------------------------
# Additional _build_collect_command tests
# ---------------------------------------------------------------------------


class TestBuildCollectCommandExtended:
    def test_vitest_command_structure(self):
        cmd = _build_collect_command("vitest", "tests")
        assert cmd[0] == "npx"
        assert cmd[1] == "vitest"
        assert "list" in cmd
        assert "--json" in cmd
        assert cmd[-1] == "tests"

    def test_jest_command_structure(self):
        cmd = _build_collect_command("jest", "tests")
        assert cmd[0] == "npx"
        assert cmd[1] == "jest"
        assert "--json" in cmd
        assert "--forceExit" in cmd
        assert cmd[-1] == "tests"

    def test_empty_test_dir(self):
        cmd = _build_collect_command("vitest", "")
        assert cmd[-1] == ""

        cmd = _build_collect_command("jest", "")
        assert cmd[-1] == ""


# ---------------------------------------------------------------------------
# Additional _normalize_ts_test_ids tests
# ---------------------------------------------------------------------------


class TestNormalizeTsTestIdsExtended:
    def test_deduplication_not_built_in(self):
        ids = ["foo.test.ts > bar", "foo.test.ts > bar"]
        result = _normalize_ts_test_ids(ids, "__tests__")
        assert len(result) == 2
        assert result == ["__tests__/foo.test.ts > bar", "__tests__/foo.test.ts > bar"]

    def test_strips_empty_strings(self):
        ids = ["", "", "a.test.ts > x", "  "]
        result = _normalize_ts_test_ids(ids, "src")
        assert result == ["src/a.test.ts > x"]

    def test_ids_with_special_characters(self):
        ids = ['utils.test.ts > handles "quotes" properly', "math.test.ts > adds 1+1=2"]
        result = _normalize_ts_test_ids(ids, "__tests__")
        assert result == [
            '__tests__/utils.test.ts > handles "quotes" properly',
            "__tests__/math.test.ts > adds 1+1=2",
        ]


# ---------------------------------------------------------------------------
# Additional collect_ts_test_ids_docker tests
# ---------------------------------------------------------------------------


class TestCollectTsTestIdsDockerExtended:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_shlex_quote_applied_to_cmd_parts(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        collect_ts_test_ids_docker(
            repo_name="mylib",
            test_dir="tests; rm -rf /",
            framework="jest",
            image_name="img:v0",
        )

        call_args = mock_client.containers.run.call_args
        cmd_arg = call_args[1].get("command") or call_args[0][1]
        # Production passes command as ["bash", "-c", bash_cmd]; flatten for substring check
        bash_cmd = cmd_arg[-1] if isinstance(cmd_arg, list) else cmd_arg
        assert "'tests; rm -rf /'" in bash_cmd

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_invalid_reference_commit_non_hex(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        with pytest.raises(ValueError, match="Invalid reference_commit"):
            collect_ts_test_ids_docker(
                repo_name="mylib",
                framework="jest",
                image_name="img:v0",
                reference_commit="ZZZNOTAHEX",
            )

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_invalid_reference_commit_too_short(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        with pytest.raises(ValueError, match="Invalid reference_commit"):
            collect_ts_test_ids_docker(
                repo_name="mylib",
                framework="jest",
                image_name="img:v0",
                reference_commit="abc",
            )

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_valid_reference_commit_sha(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="mylib",
            framework="jest",
            image_name="img:v0",
            reference_commit="abcdef1234567890",
        )
        assert result == ["a.test.ts"]


# ---------------------------------------------------------------------------
# Additional validate_ts_base_commit_docker tests
# ---------------------------------------------------------------------------


class TestValidateTsBaseCommitDockerExtended:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_container_error_still_returns_tuple(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        import docker.errors as de

        error = de.ContainerError(
            container="c",
            exit_status=1,
            command="cmd",
            image="img",
            stderr=b"/testbed/x.test.ts\n",
        )
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = error
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert count == 1
        assert isinstance(snippet, str)


class TestDockerVitestFallbackQuoting:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_vitest_fallback_quotes_test_dir(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = [
            b"[]",
            b'[{"name": "t1", "file": "/testbed/a.test.ts"}]',
        ]
        mock_docker.return_value = mock_client

        collect_ts_test_ids_docker(
            repo_name="r",
            framework="vitest",
            test_dir="src/tests dir",
            image_name="img:v0",
        )

        fallback_call = mock_client.containers.run.call_args_list[1]
        cmd_arg = fallback_call.kwargs.get(
            "command", fallback_call[1].get("command", "")
        )
        # Production passes command as ["bash", "-c", bash_cmd]; flatten for substring check
        bash_cmd = cmd_arg[-1] if isinstance(cmd_arg, list) else cmd_arg
        assert "'src/tests dir'" in bash_cmd or '"src/tests dir"' in bash_cmd

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_validate_docker_quotes_cmd_parts(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        mock_client = MagicMock()
        mock_client.containers.run.return_value = (
            b'[{"name":"t","file":"/testbed/a.test.ts"}]'
        )
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="r",
            framework="vitest",
            test_dir="my tests",
            image_name="img:v0",
        )

        call_args = mock_client.containers.run.call_args
        cmd_arg = call_args.kwargs.get("command", call_args[1].get("command", ""))
        # Production passes command as ["bash", "-c", bash_cmd]; flatten for substring check
        bash_cmd = cmd_arg[-1] if isinstance(cmd_arg, list) else cmd_arg
        assert "'my tests'" in bash_cmd or '"my tests"' in bash_cmd


# ---------------------------------------------------------------------------
# Lines 108-109: _parse_vitest_list_output — JSON that is not a list
# ---------------------------------------------------------------------------


class TestParseVitestNotList:
    def test_json_loads_returns_non_list(self):
        with patch(
            "tools.generate_test_ids_ts.json.loads", return_value={"not": "a list"}
        ):
            result = _parse_vitest_list_output('[{"fake": "data"}]')
            assert result == []


# ---------------------------------------------------------------------------
# Lines 176-179: _parse_jest_list_output JSON mode — path stripping branches
# ---------------------------------------------------------------------------


class TestParseJestListOutputJsonPaths:
    def test_json_array_absolute_different_root(self):
        """Line 176-177: absolute path not matching root → strip leading /."""
        stdout = json.dumps(["/other/root/a.test.ts", "/other/root/b.test.ts"])
        result = _parse_jest_list_output(stdout, repo_root="/testbed")
        assert result == ["other/root/a.test.ts", "other/root/b.test.ts"]

    def test_json_array_relative_paths(self):
        """Lines 178-179: paths without leading / → used as-is."""
        stdout = json.dumps(["src/a.test.ts", "lib/b.spec.js"])
        result = _parse_jest_list_output(stdout, repo_root="/testbed")
        assert result == ["src/a.test.ts", "lib/b.spec.js"]

    def test_json_array_matching_root(self):
        """Line 174-175: path starts with root_prefix → stripped."""
        stdout = json.dumps(["/testbed/x.test.ts"])
        result = _parse_jest_list_output(stdout, repo_root="/testbed")
        assert result == ["x.test.ts"]

    def test_json_array_mixed_paths(self):
        """All three branches exercised in one call."""
        stdout = json.dumps(
            [
                "/testbed/__tests__/a.test.ts",  # matches root
                "/other/root/b.test.ts",  # abs, different root
                "relative/c.test.ts",  # relative
            ]
        )
        result = _parse_jest_list_output(stdout, repo_root="/testbed")
        assert result == [
            "__tests__/a.test.ts",
            "other/root/b.test.ts",
            "relative/c.test.ts",
        ]


# ---------------------------------------------------------------------------
# Lines 190, 210: _parse_jest_list_output line-by-line noise & abs path
# ---------------------------------------------------------------------------


class TestParseJestListOutputLineByLine:
    def test_blank_lines_skipped(self):
        """Line 190: blank lines in the middle are skipped."""
        stdout = "/testbed/a.test.ts\n\n\n/testbed/b.test.ts\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["a.test.ts", "b.test.ts"]

    def test_pass_fail_tests_prefix_lines_skipped(self):
        """Lines 195-196: PASS and FAIL prefix lines are noise."""
        stdout = "PASS src/a.test.ts\n" "FAIL src/b.test.ts\n" "/testbed/c.test.ts\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["c.test.ts"]

    def test_absolute_path_different_root_line_by_line(self):
        """Line 210: absolute path not matching repo_root → strip leading /."""
        stdout = "/other/place/a.test.ts\n"
        result = _parse_jest_list_output(stdout, repo_root="/testbed")
        assert result == ["other/place/a.test.ts"]

    def test_line_with_space_and_no_ext_skipped(self):
        """Line 203: line with spaces and no test extension → skipped."""
        stdout = "Tests: 5 passed, 5 total\n/testbed/a.test.ts\n"
        result = _parse_jest_list_output(stdout)
        assert result == ["a.test.ts"]


# ---------------------------------------------------------------------------
# Lines 286-287, 299-302: _parse_jest_json_results edge cases
# ---------------------------------------------------------------------------


class TestParseJestJsonResultsEdgeCases:
    def test_malformed_json_falls_back_to_list_parser(self):
        """Lines 286-287: JSONDecodeError → falls back to _parse_jest_list_output."""
        from tools.generate_test_ids_ts import _parse_jest_json_results

        stdout = "{invalid json}\n/testbed/a.test.ts"
        result = _parse_jest_json_results(stdout)
        # The { and } are found, but json.loads fails → falls back to list parser
        # The list parser should find a.test.ts
        assert "a.test.ts" in result

    def test_suite_without_assertionResults_key(self):
        """Lines 304: suite.get('assertionResults', []) → empty list when key missing."""
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "testFilePath": "/testbed/a.test.ts",
                    # no assertionResults key at all
                }
            ]
        }
        # No assertions found → test_ids empty → falls back to list parser
        result = _parse_jest_json_results(json.dumps(report))
        assert result == []

    def test_relative_file_path_in_suite(self):
        """Lines 301-302: file path that is relative (no leading /)."""
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "testFilePath": "src/a.test.ts",
                    "assertionResults": [
                        {"fullName": "test1", "status": "passed"},
                    ],
                }
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == ["src/a.test.ts > test1"]

    def test_absolute_path_different_root_in_suite(self):
        """Lines 299-300: absolute path not matching repo_root → strip leading /."""
        from tools.generate_test_ids_ts import _parse_jest_json_results

        report = {
            "testResults": [
                {
                    "testFilePath": "/other/root/a.test.ts",
                    "assertionResults": [
                        {"fullName": "works", "status": "passed"},
                    ],
                }
            ]
        }
        result = _parse_jest_json_results(json.dumps(report))
        assert result == ["other/root/a.test.ts > works"]

    def test_no_braces_at_all_falls_back(self):
        """Line 282: no { or } found → falls back to _parse_jest_list_output."""
        from tools.generate_test_ids_ts import _parse_jest_json_results

        stdout = "/testbed/a.test.ts\n/testbed/b.test.ts\n"
        result = _parse_jest_json_results(stdout)
        assert result == ["a.test.ts", "b.test.ts"]


# ---------------------------------------------------------------------------
# Lines 485-488, 492: vitest fallback TimeoutExpired / FileNotFoundError
# ---------------------------------------------------------------------------


class TestCollectLocalVitestFallbackErrors:
    @patch(f"{MODULE}.subprocess.run")
    def test_vitest_fallback_timeout(self, mock_run: MagicMock):
        """Lines 485-486: vitest fallback raises TimeoutExpired."""
        primary_result = MagicMock(stdout="", stderr="some err", returncode=1)
        mock_run.side_effect = [
            primary_result,
            subprocess.TimeoutExpired(cmd="npx", timeout=300),
        ]
        result = collect_ts_test_ids_local(
            repo_dir=Path("/repo"), test_dir="__tests__", framework="vitest"
        )
        assert result == []
        assert mock_run.call_count == 2

    @patch(f"{MODULE}.subprocess.run")
    def test_vitest_fallback_file_not_found(self, mock_run: MagicMock):
        """Lines 487-488: vitest fallback raises FileNotFoundError."""
        primary_result = MagicMock(stdout="", stderr="", returncode=1)
        mock_run.side_effect = [
            primary_result,
            FileNotFoundError("npx not found"),
        ]
        result = collect_ts_test_ids_local(
            repo_dir=Path("/repo"), test_dir="__tests__", framework="vitest"
        )
        assert result == []
        assert mock_run.call_count == 2

    @patch(f"{MODULE}.subprocess.run")
    def test_vitest_logs_stderr_when_empty(self, mock_run: MagicMock):
        """Line 491-492: logs stderr when collection returns nothing."""
        primary_result = MagicMock(
            stdout="", stderr="Error: vitest not found", returncode=1
        )
        mock_run.return_value = primary_result
        # jest framework → no vitest fallback, but stderr logged
        result = collect_ts_test_ids_local(
            repo_dir=Path("/repo"), test_dir="__tests__", framework="jest"
        )
        assert result == []


# ---------------------------------------------------------------------------
# Lines 535-537: Docker client setup with auto image_name
# ---------------------------------------------------------------------------


class TestCollectDockerAutoImage:
    @patch(f"{MODULE}._find_docker_image", return_value=None)
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_auto_image_name_when_none(
        self, mock_docker: MagicMock, mock_plat: MagicMock, mock_find: MagicMock
    ):
        """Lines 534-537: image_name=None → _find_docker_image → fallback name."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        collect_ts_test_ids_docker(
            repo_name="My/Lib",
            framework="jest",
            image_name=None,
        )
        # Verify the auto-generated image name was used
        call_args = mock_client.containers.run.call_args
        assert call_args[0][0] == "commit0.repo.my_lib:v0"

    @patch(f"{MODULE}._find_docker_image", return_value="found-image:v1")
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_auto_image_name_found(
        self, mock_docker: MagicMock, mock_plat: MagicMock, mock_find: MagicMock
    ):
        """Line 535: _find_docker_image returns a match → use it."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        collect_ts_test_ids_docker(
            repo_name="mylib",
            framework="jest",
            image_name=None,
        )
        call_args = mock_client.containers.run.call_args
        assert call_args[0][0] == "found-image:v1"


# ---------------------------------------------------------------------------
# Lines 608-609: vitest Docker fallback ContainerError/ReadTimeout
# ---------------------------------------------------------------------------


class TestDockerVitestFallbackErrors:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_vitest_docker_fallback_container_error(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Lines 608-609: vitest Docker fallback raises ContainerError."""
        import docker.errors as de

        mock_client = MagicMock()
        # First call: empty vitest list output (triggers fallback)
        # Second call: ContainerError in fallback
        mock_client.containers.run.side_effect = [
            b"[]",  # empty vitest list
            de.ContainerError(
                container="c",
                exit_status=1,
                command="cmd",
                image="img",
                stderr=b"error",
            ),
        ]
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r",
            framework="vitest",
            image_name="img:v0",
        )
        assert result == []
        assert mock_client.containers.run.call_count == 2

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_vitest_docker_fallback_read_timeout(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Lines 608-609: vitest Docker fallback raises ReadTimeout."""
        import requests.exceptions

        mock_client = MagicMock()
        mock_client.containers.run.side_effect = [
            b"[]",  # empty vitest list
            requests.exceptions.ReadTimeout(),
        ]
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r",
            framework="vitest",
            image_name="img:v0",
        )
        assert result == []
        assert mock_client.containers.run.call_count == 2


# ---------------------------------------------------------------------------
# Lines 644-646: validate_ts_base_commit_docker auto image_name
# ---------------------------------------------------------------------------


class TestValidateDockerAutoImage:
    @patch(f"{MODULE}._find_docker_image", return_value=None)
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_auto_image_name_fallback(
        self, mock_docker: MagicMock, mock_plat: MagicMock, mock_find: MagicMock
    ):
        """Lines 644-646: image_name=None + _find_docker_image=None → auto name."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        count, _ = validate_ts_base_commit_docker(
            repo_name="Org/MyLib",
            framework="jest",
            image_name=None,
        )
        call_args = mock_client.containers.run.call_args
        assert call_args[0][0] == "commit0.repo.org_mylib:v0"

    @patch(f"{MODULE}._find_docker_image", return_value="found:v1")
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_auto_image_name_found(
        self, mock_docker: MagicMock, mock_plat: MagicMock, mock_find: MagicMock
    ):
        """Line 644: _find_docker_image returns a match."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = b"/testbed/a.test.ts\n"
        mock_docker.return_value = mock_client

        validate_ts_base_commit_docker(
            repo_name="mylib",
            framework="jest",
            image_name=None,
        )
        call_args = mock_client.containers.run.call_args
        assert call_args[0][0] == "found:v1"


# ---------------------------------------------------------------------------
# Lines 773-811, 836-842: generate_for_ts_dataset local mode
# ---------------------------------------------------------------------------


class TestGenerateForTsDatasetLocalMode:
    @patch(f"{MODULE}._find_docker_image", return_value=None)
    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    @patch(f"{MODULE}._find_repo_dir")
    def test_local_mode_with_reference_commit(
        self,
        mock_find_repo: MagicMock,
        mock_collect: MagicMock,
        mock_save: MagicMock,
        mock_subproc: MagicMock,
        mock_find_img: MagicMock,
        tmp_path: Path,
    ):
        """Lines 773-793: local mode with reference_commit → git checkout called."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        mock_find_repo.return_value = repo_dir
        mock_collect.return_value = ["__tests__/a.test.ts > t1"]
        mock_save.return_value = tmp_path / "out" / "fake.bz2"
        mock_subproc.return_value = MagicMock(returncode=0)

        dataset = [
            {
                "repo": "org/mylib",
                "test": {"test_dir": "__tests__"},
                "reference_commit": "abc1234567",
            }
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=False,
            clone_dir=tmp_path,
        )
        assert results["mylib"] == 1
        # git checkout was called
        mock_subproc.assert_called_once()
        checkout_cmd = mock_subproc.call_args[0][0]
        assert checkout_cmd == ["git", "checkout", "abc1234567"]

    @patch(f"{MODULE}.collect_ts_test_ids_docker")
    @patch(f"{MODULE}._find_docker_image", return_value="myimg:v0")
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    @patch(f"{MODULE}._find_repo_dir")
    def test_local_to_docker_fallback(
        self,
        mock_find_repo: MagicMock,
        mock_collect_local: MagicMock,
        mock_save: MagicMock,
        mock_find_img: MagicMock,
        mock_collect_docker: MagicMock,
        tmp_path: Path,
    ):
        """Lines 796-811: local returns empty → Docker fallback with found image."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        mock_find_repo.return_value = repo_dir
        mock_collect_local.return_value = []  # local fails
        mock_collect_docker.return_value = ["__tests__/a.test.ts > t1"]
        mock_save.return_value = tmp_path / "out" / "fake.bz2"

        dataset = [
            {
                "repo": "org/mylib",
                "test": {"test_dir": "__tests__"},
            }
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=False,
            clone_dir=tmp_path,
        )
        assert results["mylib"] == 1
        mock_collect_docker.assert_called_once()
        assert mock_collect_docker.call_args[1]["image_name"] == "myimg:v0"

    @patch(f"{MODULE}._find_docker_image", return_value=None)
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    @patch(f"{MODULE}._find_repo_dir")
    def test_local_no_docker_fallback_when_no_image(
        self,
        mock_find_repo: MagicMock,
        mock_collect_local: MagicMock,
        mock_save: MagicMock,
        mock_find_img: MagicMock,
        tmp_path: Path,
    ):
        """Lines 797-798: local empty + no Docker image → result is 0."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        mock_find_repo.return_value = repo_dir
        mock_collect_local.return_value = []

        dataset = [{"repo": "org/mylib", "test": {"test_dir": "__tests__"}}]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=False,
            clone_dir=tmp_path,
        )
        assert results["mylib"] == 0

    @patch(f"{MODULE}.validate_ts_base_commit_docker")
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_docker")
    def test_validate_base_success(
        self,
        mock_collect: MagicMock,
        mock_save: MagicMock,
        mock_validate: MagicMock,
        tmp_path: Path,
    ):
        """Lines 836-839: base validation succeeds (base_collected > 0)."""
        dataset = [
            {
                "repo": "org/lib",
                "test_framework": "vitest",
                "test": {"test_dir": "__tests__"},
                "reference_commit": "ref1",
            }
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        mock_collect.return_value = ["__tests__/t.test.ts > test1"]
        mock_save.return_value = tmp_path / "fake.bz2"
        mock_validate.return_value = (5, "all good")

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=True,
            validate_base=True,
        )
        assert results["lib"] == 1  # positive = OK

    @patch(f"{MODULE}._find_docker_image", return_value=None)
    @patch(f"{MODULE}.subprocess.run")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    @patch(f"{MODULE}._find_repo_dir")
    def test_git_checkout_failure_still_collects(
        self,
        mock_find_repo: MagicMock,
        mock_collect: MagicMock,
        mock_subproc: MagicMock,
        mock_find_img: MagicMock,
        tmp_path: Path,
    ):
        """Git checkout fails → warns and skips repo (safer than collecting
        against the wrong commit).
        """
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        mock_find_repo.return_value = repo_dir
        mock_subproc.side_effect = subprocess.CalledProcessError(1, "git checkout")
        mock_collect.return_value = []

        dataset = [
            {
                "repo": "org/lib",
                "test": {"test_dir": "__tests__"},
                "reference_commit": "abc1234567",
            }
        ]
        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps(dataset))

        results = generate_for_ts_dataset(
            dataset_path=ds_file,
            output_dir=tmp_path / "out",
            use_docker=False,
            clone_dir=tmp_path,
        )
        # On checkout failure the repo is skipped entirely — collection is
        # never attempted because test IDs gathered against the wrong tree
        # would silently corrupt downstream pass-rate metrics.
        assert "lib" not in results
        assert mock_collect.call_count == 0


# ---------------------------------------------------------------------------
# Lines 853-992: main() CLI
# ---------------------------------------------------------------------------


class TestMainCli:
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    def test_single_repo_mode_success(
        self, mock_collect: MagicMock, mock_save: MagicMock, tmp_path: Path
    ):
        """Lines 931-949: --repo-dir + --name → single-repo mode."""
        from tools.generate_test_ids_ts import main

        mock_collect.return_value = ["__tests__/a.test.ts > t1"]
        mock_save.return_value = tmp_path / "out" / "fake.bz2"

        with patch(
            "sys.argv",
            [
                "prog",
                "--repo-dir",
                str(tmp_path),
                "--name",
                "mylib",
                "--output-dir",
                str(tmp_path / "out"),
                "--test-dir",
                "__tests__",
                "--framework",
                "vitest",
                "--timeout",
                "60",
            ],
        ):
            main()

        mock_collect.assert_called_once()
        assert mock_collect.call_args[1]["framework"] == "vitest"
        mock_save.assert_called_once()

    @patch(f"{MODULE}.collect_ts_test_ids_local")
    def test_single_repo_mode_no_tests_exits(
        self, mock_collect: MagicMock, tmp_path: Path
    ):
        """Lines 950-952: no test IDs collected → sys.exit(1)."""
        from tools.generate_test_ids_ts import main

        mock_collect.return_value = []

        with patch(
            "sys.argv",
            [
                "prog",
                "--repo-dir",
                str(tmp_path),
                "--name",
                "mylib",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_single_repo_mode_missing_name_errors(self, tmp_path: Path):
        """Lines 932-933: --repo-dir without --name → parser.error."""
        from tools.generate_test_ids_ts import main

        with patch(
            "sys.argv",
            [
                "prog",
                "--repo-dir",
                str(tmp_path),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2  # argparse error exit code

    @patch(f"{MODULE}.generate_for_ts_dataset")
    def test_dataset_mode(self, mock_gen: MagicMock, tmp_path: Path):
        """Lines 955-984: dataset_file arg → dataset mode."""
        from tools.generate_test_ids_ts import main

        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps([{"repo": "org/r", "test": {"test_dir": "t"}}]))
        mock_gen.return_value = {"r": 5}

        with patch(
            "sys.argv",
            [
                "prog",
                str(ds_file),
                "--output-dir",
                str(tmp_path / "out"),
                "--docker",
                "--timeout",
                "120",
                "--max-repos",
                "10",
                "--validate-base",
                "--framework",
                "jest",
            ],
        ):
            main()

        mock_gen.assert_called_once()
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["use_docker"] is True
        assert call_kwargs["timeout"] == 120
        assert call_kwargs["max_repos"] == 10
        assert call_kwargs["validate_base"] is True
        assert call_kwargs["framework_override"] == "jest"

    @patch(f"{MODULE}.generate_for_ts_dataset")
    def test_dataset_mode_auto_framework(self, mock_gen: MagicMock, tmp_path: Path):
        """Line 961: framework=auto → framework_override=None."""
        from tools.generate_test_ids_ts import main

        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps([{"repo": "org/r", "test": {"test_dir": "t"}}]))
        mock_gen.return_value = {"r": 1}

        with patch(
            "sys.argv",
            [
                "prog",
                str(ds_file),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            main()

        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["framework_override"] is None

    @patch(f"{MODULE}.generate_for_ts_dataset")
    def test_dataset_mode_with_clone_dir(self, mock_gen: MagicMock, tmp_path: Path):
        """Line 960: --clone-dir provided."""
        from tools.generate_test_ids_ts import main

        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps([{"repo": "org/r", "test": {"test_dir": "t"}}]))
        mock_gen.return_value = {"r": 1}

        with patch(
            "sys.argv",
            [
                "prog",
                str(ds_file),
                "--output-dir",
                str(tmp_path / "out"),
                "--clone-dir",
                str(tmp_path / "clones"),
            ],
        ):
            main()

        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["clone_dir"] == Path(str(tmp_path / "clones"))

    def test_dataset_mode_file_not_found(self, tmp_path: Path):
        """Lines 957-958: dataset file doesn't exist → error."""
        from tools.generate_test_ids_ts import main

        with patch(
            "sys.argv",
            [
                "prog",
                str(tmp_path / "nonexistent.json"),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2

    def test_no_args_errors(self):
        """Lines 985-987: no dataset_file and no --repo-dir → error."""
        from tools.generate_test_ids_ts import main

        with patch("sys.argv", ["prog"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2

    @patch(f"{MODULE}.install_test_ids")
    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    def test_install_step(
        self,
        mock_collect: MagicMock,
        mock_save: MagicMock,
        mock_install: MagicMock,
        tmp_path: Path,
    ):
        """Lines 990-992: --install flag triggers install_test_ids."""
        from tools.generate_test_ids_ts import main

        mock_collect.return_value = ["__tests__/a.test.ts > t1"]
        mock_save.return_value = tmp_path / "out" / "fake.bz2"
        mock_install.return_value = 1

        with patch(
            "sys.argv",
            [
                "prog",
                "--repo-dir",
                str(tmp_path),
                "--name",
                "mylib",
                "--output-dir",
                str(tmp_path / "out"),
                "--install",
            ],
        ):
            main()

        mock_install.assert_called_once()

    @patch(f"{MODULE}.install_test_ids")
    @patch(f"{MODULE}.generate_for_ts_dataset")
    def test_dataset_mode_with_install(
        self, mock_gen: MagicMock, mock_install: MagicMock, tmp_path: Path
    ):
        """Lines 990-992: dataset mode + --install."""
        from tools.generate_test_ids_ts import main

        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps([{"repo": "org/r", "test": {"test_dir": "t"}}]))
        mock_gen.return_value = {"r": 5, "s": -3}
        mock_install.return_value = 2

        with patch(
            "sys.argv",
            [
                "prog",
                str(ds_file),
                "--output-dir",
                str(tmp_path / "out"),
                "--install",
            ],
        ):
            main()

        mock_install.assert_called_once()

    @patch(f"{MODULE}.save_test_ids")
    @patch(f"{MODULE}.collect_ts_test_ids_local")
    def test_single_repo_auto_framework_defaults_to_jest(
        self, mock_collect: MagicMock, mock_save: MagicMock, tmp_path: Path
    ):
        """Line 935: framework=auto → defaults to jest in single-repo mode."""
        from tools.generate_test_ids_ts import main

        mock_collect.return_value = ["__tests__/a.test.ts > t1"]
        mock_save.return_value = tmp_path / "out" / "fake.bz2"

        with patch(
            "sys.argv",
            [
                "prog",
                "--repo-dir",
                str(tmp_path),
                "--name",
                "mylib",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            main()

        assert mock_collect.call_args[1]["framework"] == "jest"

    @patch(f"{MODULE}.generate_for_ts_dataset")
    def test_dataset_mode_summary_with_mixed_results(
        self, mock_gen: MagicMock, tmp_path: Path
    ):
        """Lines 974-984: summary logging with positive, zero, and negative results."""
        from tools.generate_test_ids_ts import main

        ds_file = tmp_path / "dataset.json"
        ds_file.write_text(json.dumps([{"repo": "org/r", "test": {"test_dir": "t"}}]))
        mock_gen.return_value = {"a": 10, "b": 0, "c": -5}

        with patch(
            "sys.argv",
            [
                "prog",
                str(ds_file),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            main()

        mock_gen.assert_called_once()


# ---------------------------------------------------------------------------
# Docker string output (non-bytes) handling
# ---------------------------------------------------------------------------


class TestDockerStringOutput:
    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_docker_returns_string_not_bytes(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Line 569-570: container returns str instead of bytes."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = (
            "/testbed/a.test.ts\n"  # str, not bytes
        )
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert result == ["a.test.ts"]

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_container_error_with_string_stderr(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Lines 573-577: ContainerError.stderr is str, not bytes."""
        import docker.errors as de

        error = de.ContainerError(
            container="c",
            exit_status=1,
            command="cmd",
            image="img",
            stderr="/testbed/x.test.ts\n",  # str stderr
        )
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = error
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert result == ["x.test.ts"]

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_container_error_with_none_stderr(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Lines 573-577: ContainerError.stderr is None."""
        import docker.errors as de

        error = de.ContainerError(
            container="c", exit_status=1, command="cmd", image="img", stderr=None
        )
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = error
        mock_docker.return_value = mock_client

        result = collect_ts_test_ids_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert result == []

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_validate_docker_string_output(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Lines 663-665 in validate: container returns str."""
        mock_client = MagicMock()
        mock_client.containers.run.return_value = "/testbed/a.test.ts\n"  # str
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert count == 1

    @patch(
        "commit0.harness.docker_utils.get_docker_platform", return_value="linux/amd64"
    )
    @patch("docker.from_env")
    def test_validate_docker_container_error(
        self, mock_docker: MagicMock, mock_plat: MagicMock
    ):
        """Lines 666-672 in validate: ContainerError with string stderr."""
        import docker.errors as de

        error = de.ContainerError(
            container="c",
            exit_status=1,
            command="cmd",
            image="img",
            stderr="error output",
        )
        mock_client = MagicMock()
        mock_client.containers.run.side_effect = error
        mock_docker.return_value = mock_client

        count, snippet = validate_ts_base_commit_docker(
            repo_name="r", framework="jest", image_name="img:v0"
        )
        assert isinstance(snippet, str)
