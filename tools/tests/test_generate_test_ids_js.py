from __future__ import annotations

import shlex

import pytest

from tools.generate_test_ids_js import _build_collect_command


class TestBuildCollectCommand:
    @pytest.mark.parametrize(
        ("framework", "expected_head"),
        [
            ("jest", ["npx", "jest"]),
            ("vitest", ["npx", "vitest"]),
            ("mocha", ["npx", "mocha"]),
            ("node_test", ["node", "--test"]),
        ],
    )
    def test_returns_list_per_framework(
        self, framework: str, expected_head: list[str]
    ) -> None:
        parts = _build_collect_command(framework, "tests")
        assert parts[: len(expected_head)] == expected_head
        assert parts[-1] == "tests"

    def test_unknown_framework_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown framework"):
            _build_collect_command("qunit", "tests")


class TestTestDirShellQuoting:
    @pytest.mark.parametrize(
        "test_dir",
        [
            "; rm -rf /",
            "$(rm -rf /)",
            "`rm -rf /`",
            "&& evil",
            "|| evil",
            "| sh",
            "test\ndir",
            "test\rdir",
            'a"b',
            "a'b",
            "../../etc/passwd",
            "tests; cat /etc/shadow",
        ],
    )
    def test_injection_cannot_escape_quoting(self, test_dir: str) -> None:
        cmd_parts = _build_collect_command("jest", test_dir)
        collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        quoted = shlex.quote(test_dir)
        assert quoted in collect_cmd
        assert collect_cmd.endswith(quoted)
        for danger in ("; rm", "&& evil", "|| evil", "| sh", "$(rm", "`rm"):
            if danger in test_dir:
                assert f" {danger}" not in collect_cmd or quoted.count(danger) >= 1

    def test_semicolon_injection_produces_single_quoted_form(self) -> None:
        cmd_parts = _build_collect_command("jest", "; rm -rf /")
        collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        assert "'; rm -rf /'" in collect_cmd
        assert not collect_cmd.endswith("; rm -rf /")

    def test_bash_cmd_construction_safe(self) -> None:
        test_dir = "; rm -rf /"
        cmd_parts = _build_collect_command("jest", test_dir)
        collect_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        bash_cmd = f"cd /testbed && {collect_cmd} 2>/dev/null; true"
        assert "'; rm -rf /'" in bash_cmd
        assert "/testbed && npx jest --json --forceExit '; rm -rf /'" in bash_cmd


class TestValidateBaseCommitDocker:
    def test_zero_tests_returns_zero_count_with_snippet(self) -> None:
        from unittest.mock import MagicMock, patch

        from tools.generate_test_ids_js import validate_js_base_commit_docker

        fake_client = MagicMock()
        fake_client.containers.run.return_value = b"no tests found at this commit"

        with (
            patch(
                "tools.generate_test_ids_js._find_docker_image",
                return_value="commit0.repo.foo:v0",
            ),
            patch("docker.from_env", return_value=fake_client),
        ):
            count, snippet = validate_js_base_commit_docker(
                repo_name="foo",
                test_dir="__tests__",
                framework="jest",
            )

        assert count == 0
        assert "no tests found" in snippet

    def test_nonempty_jest_json_yields_test_count(self) -> None:
        import json
        from unittest.mock import MagicMock, patch

        from tools.generate_test_ids_js import validate_js_base_commit_docker

        jest_listing = json.dumps(
            {
                "testResults": [
                    {
                        "name": "/testbed/__tests__/foo.test.js",
                        "assertionResults": [
                            {"fullName": "foo > a", "status": "passed"},
                            {"fullName": "foo > b", "status": "passed"},
                        ],
                    }
                ]
            }
        )
        fake_client = MagicMock()
        fake_client.containers.run.return_value = jest_listing.encode("utf-8")

        with (
            patch(
                "tools.generate_test_ids_js._find_docker_image",
                return_value="commit0.repo.foo:v0",
            ),
            patch("docker.from_env", return_value=fake_client),
        ):
            count, snippet = validate_js_base_commit_docker(
                repo_name="foo",
                test_dir="__tests__",
                framework="jest",
            )

        assert count >= 1
        assert isinstance(snippet, str)

    def test_container_error_yields_stderr_text_via_decode_branch(self) -> None:
        import sys
        from unittest.mock import MagicMock, patch

        import docker as real_docker
        import docker.errors as real_docker_errors

        from tools.generate_test_ids_js import validate_js_base_commit_docker

        fake_client = MagicMock()

        class _StubError(real_docker_errors.ContainerError):
            def __init__(self):
                self.stderr = b"ENOENT: __tests__ not found"
                self.command = "bash"
                self.exit_status = 1
                self.image = "commit0.repo.foo:v0"

        def _raise_stub(*_args, **_kwargs):
            raise _StubError()

        fake_client.containers.run.side_effect = _raise_stub

        saved_docker = sys.modules.get("docker")
        saved_errors = sys.modules.get("docker.errors")
        sys.modules["docker"] = real_docker
        sys.modules["docker.errors"] = real_docker_errors
        try:
            with (
                patch(
                    "tools.generate_test_ids_js._find_docker_image",
                    return_value="commit0.repo.foo:v0",
                ),
                patch("docker.from_env", return_value=fake_client),
            ):
                count, snippet = validate_js_base_commit_docker(
                    repo_name="foo",
                    test_dir="__tests__",
                    framework="jest",
                )
        finally:
            if saved_docker is not None:
                sys.modules["docker"] = saved_docker
            if saved_errors is not None:
                sys.modules["docker.errors"] = saved_errors

        assert count == 0
        assert "__tests__ not found" in snippet

    def test_uses_shlex_quoted_test_dir_in_command(self) -> None:
        from unittest.mock import MagicMock, patch

        from tools.generate_test_ids_js import validate_js_base_commit_docker

        captured: dict = {}

        def _capture(image, command, **_kwargs):
            captured["image"] = image
            captured["command"] = command
            return b""

        fake_client = MagicMock()
        fake_client.containers.run.side_effect = _capture

        with (
            patch(
                "tools.generate_test_ids_js._find_docker_image",
                return_value="commit0.repo.foo:v0",
            ),
            patch("docker.from_env", return_value=fake_client),
        ):
            validate_js_base_commit_docker(
                repo_name="foo",
                test_dir="; rm -rf /",
                framework="jest",
            )

        assert captured["command"][0] == "bash"
        assert captured["command"][1] == "-c"
        bash_script = captured["command"][2]
        assert "'; rm -rf /'" in bash_script
