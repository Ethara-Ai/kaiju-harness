from __future__ import annotations

import json
import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness import health_check_js
from commit0.harness.health_check_js import (
    _LOCKFILE_TO_PM,
    _NPM_PACKAGE_RE,
    MultipleLockfilesError,
    _build_require_cmd,
    _detect_image_pkg_manager,
    check_node_modules,
    check_node_version,
    check_require,
    detect_test_framework_from_package_json,
    run_js_health_checks,
)


def _mock_client(output: bytes = b"") -> MagicMock:
    client = MagicMock(spec=["containers"])
    client.containers.run.return_value = output
    return client


class TestNpmPackageReAccepts:
    @pytest.mark.parametrize(
        "name",
        [
            "lodash",
            "react",
            "@scope/pkg",
            "some-pkg.name",
            "pkg_with_underscore",
            "@types/node",
            "a",
            "@a/b",
            "pkg.with.dots",
            "pkg-with-dashes",
            "pkg~with~tildes",
        ],
    )
    def test_accepts_valid(self, name: str) -> None:
        assert _NPM_PACKAGE_RE.fullmatch(name) is not None


class TestNpmPackageReRejects:
    @pytest.mark.parametrize(
        "name",
        [
            "lodash; rm -rf /",
            "../etc/passwd",
            "@/foo",
            "@scope/",
            "",
            "   ",
            "lodash\x00.js",
            "@scope/pkg\"; process.exit(1)",
            "..",
            "pkg/../other",
            "lodаsh",
            "lodash\n; evil",
            "‮Reverse",
            "PKG",
            ".dotfile",
            "@SCOPE/pkg",
            "$injection",
            "`backtick`",
            "pkg with space",
            "pkg|sh",
            "pkg&evil",
            "pkg$(echo)",
        ],
    )
    def test_rejects_invalid(self, name: str) -> None:
        assert _NPM_PACKAGE_RE.fullmatch(name) is None

    def test_leading_dash_accepted_by_regex_but_documented(self) -> None:
        assert _NPM_PACKAGE_RE.fullmatch("-evil") is not None


class TestBuildRequireCmd:
    @pytest.mark.parametrize(
        ("pm", "expected_head"),
        [
            ("npm", ["node", "-e"]),
            ("pnpm", ["pnpm", "exec", "node", "-e"]),
            ("yarn", ["yarn", "node", "-e"]),
            ("bun", ["bun", "-e"]),
        ],
    )
    def test_per_pkg_manager(self, pm: str, expected_head: list[str]) -> None:
        cmd = _build_require_cmd("lodash", pm)
        assert cmd[: len(expected_head)] == expected_head
        assert cmd[-1] == 'require("lodash")'

    def test_unknown_pm_falls_back_to_node(self) -> None:
        cmd = _build_require_cmd("lodash", "deno")
        assert cmd[0] == "node"

    @pytest.mark.parametrize(
        "name",
        [
            "lodash",
            "@scope/pkg",
            "@types/node",
            "pkg.with.dots",
        ],
    )
    def test_json_dumps_quotes_name(self, name: str) -> None:
        cmd = _build_require_cmd(name, "npm")
        script = cmd[-1]
        assert script == f"require({json.dumps(name)})"
        assert script.count('"') == 2


class TestCheckRequireRejectsInjection:
    @pytest.mark.parametrize(
        "name",
        [
            "lodash; rm -rf /",
            "../etc/passwd",
            "@/foo",
            "@scope/",
            "",
            "   ",
            "lodash\x00.js",
            "@scope/pkg\"; process.exit(1)",
            "..",
            "pkg/../other",
            "lodаsh",
            "lodash\n; evil",
            "‮Reverse",
            "$injection",
            "`backtick`",
            "pkg|sh",
        ],
    )
    def test_invalid_names_short_circuit_before_docker(self, name: str) -> None:
        client = _mock_client()
        ok, detail = check_require(client, "img:v1", name)
        assert ok is False
        assert "Invalid package name" in detail
        client.containers.run.assert_not_called()

    def test_types_package_skipped(self) -> None:
        client = _mock_client()
        ok, detail = check_require(client, "img:v1", "@types/node")
        assert ok is True
        assert "Skipped" in detail
        client.containers.run.assert_not_called()

    def test_valid_name_invokes_docker(self) -> None:
        client = _mock_client(b"")
        with patch(
            "commit0.harness.health_check_js._detect_image_pkg_manager",
            return_value="npm",
        ):
            ok, detail = check_require(client, "img:v1", "lodash")
        assert ok is True
        assert "OK" in detail
        client.containers.run.assert_called_once()
        args, kwargs = client.containers.run.call_args
        cmd = args[1] if len(args) > 1 else kwargs.get("command")
        assert cmd[0] == "node"
        assert cmd[-1] == 'require("lodash")'


@pytest.fixture(autouse=True)
def _clear_pkg_cache():
    health_check_js._PKG_MANAGER_CACHE.clear()
    yield
    health_check_js._PKG_MANAGER_CACHE.clear()


class TestPkgManagerCacheConcurrency:
    def test_concurrent_callers_converge_to_same_value(self) -> None:
        client = MagicMock(spec=["containers"])
        call_count = {"n": 0}
        lock = threading.Lock()

        def _slow_run(*_args, **_kwargs):
            with lock:
                call_count["n"] += 1
            return b"pnpm-lock.yaml"

        client.containers.run.side_effect = _slow_run

        results: list[str] = []
        result_lock = threading.Lock()

        def _worker():
            value = _detect_image_pkg_manager(client, "img:concurrent")
            with result_lock:
                results.append(value)

        threads = [threading.Thread(target=_worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 20
        assert all(r == "pnpm" for r in results)
        assert health_check_js._PKG_MANAGER_CACHE["img:concurrent"] == "pnpm"

    def test_repeat_call_after_cache_warm_does_not_invoke_docker(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"yarn.lock"
        _detect_image_pkg_manager(client, "img:warm")
        call_count_after_first = client.containers.run.call_count
        for _ in range(5):
            _detect_image_pkg_manager(client, "img:warm")
        assert client.containers.run.call_count == call_count_after_first


class TestDetectImagePkgManagerFallback:
    def test_docker_error_logs_debug_and_returns_npm(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.side_effect = RuntimeError("docker died")
        with caplog.at_level(logging.DEBUG, logger="commit0.harness.health_check_js"):
            result = _detect_image_pkg_manager(client, "img:fail")
        assert result == "npm"
        assert any(
            "pkg manager detect failed" in rec.message for rec in caplog.records
        )

    def test_fallback_value_cached(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.side_effect = RuntimeError("docker died")
        _detect_image_pkg_manager(client, "img:cached_fallback")
        assert health_check_js._PKG_MANAGER_CACHE["img:cached_fallback"] == "npm"


class TestLockfilePriorityOrder:
    @pytest.mark.parametrize(
        ("probe_output", "expected_pm"),
        [
            (b"pnpm-lock.yaml", "pnpm"),
            (b"yarn.lock", "yarn"),
            (b"bun.lockb", "bun"),
            (b"package-lock.json", "npm"),
            (b"none", "npm"),
            (b"", "npm"),
        ],
    )
    def test_single_lockfile_resolves(
        self, probe_output: bytes, expected_pm: str
    ) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = probe_output
        result = _detect_image_pkg_manager(client, f"img:{expected_pm}_single")
        assert result == expected_pm

    @pytest.mark.parametrize(
        "markers",
        [
            "pnpm-lock.yaml yarn.lock",
            "pnpm-lock.yaml package-lock.json",
            "yarn.lock package-lock.json",
            "yarn.lock bun.lockb",
            "bun.lockb package-lock.json",
        ],
    )
    def test_multiple_lockfiles_raises(self, markers: str) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = markers.encode()
        with pytest.raises(MultipleLockfilesError) as excinfo:
            _detect_image_pkg_manager(client, "img:multi")
        assert excinfo.value.lockfiles == markers.split()
        assert "img:multi" not in health_check_js._PKG_MANAGER_CACHE

    def test_multiple_lockfiles_surfaces_as_failed_health_check(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"pnpm-lock.yaml yarn.lock"
        results = run_js_health_checks(client, "img:multi_health")
        pm_results = [r for r in results if r[1] == "package_manager"]
        assert pm_results
        assert pm_results[0][0] is False
        assert "Multiple lockfiles" in pm_results[0][2]

    def test_multiple_lockfiles_check_require_returns_false(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"pnpm-lock.yaml yarn.lock"
        ok, detail = check_require(client, "img:multi_req", "lodash")
        assert ok is False
        assert "Multiple lockfiles" in detail

    def test_lockfile_to_pm_table_pinned(self) -> None:
        assert _LOCKFILE_TO_PM == {
            "pnpm-lock.yaml": "pnpm",
            "yarn.lock": "yarn",
            "bun.lockb": "bun",
            "package-lock.json": "npm",
        }


class TestDetectTestFrameworkScriptFalsePositive:
    def test_npm_run_node_test_script_matches_node_test(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = json.dumps(
            {"scripts": {"test": "npm run node --test"}}
        ).encode()
        result = detect_test_framework_from_package_json(client, "img:false_pos")
        assert result == "node_test"

    def test_leading_node_test_script_matches(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = json.dumps(
            {"scripts": {"test": "node --test test/"}}
        ).encode()
        result = detect_test_framework_from_package_json(client, "img:lead")
        assert result == "node_test"

    def test_unrelated_script_returns_unknown(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = json.dumps(
            {"scripts": {"test": "echo no tests"}}
        ).encode()
        result = detect_test_framework_from_package_json(client, "img:none")
        assert result == "unknown"


class TestDetectTestFrameworkJsonEdgeCases:
    def test_bom_prefixed_json_returns_unknown(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = "\ufeff{}".encode("utf-8")
        result = detect_test_framework_from_package_json(client, "img:bom")
        assert result == "unknown"

    def test_trailing_comma_returns_unknown(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b'{"dependencies": {"jest": "29",},}'
        result = detect_test_framework_from_package_json(client, "img:trail")
        assert result == "unknown"

    def test_dependencies_as_string_raises_type_error(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = json.dumps(
            {"dependencies": "not-a-dict"}
        ).encode()
        with pytest.raises(TypeError):
            detect_test_framework_from_package_json(client, "img:bad_deps")

    def test_dev_dependencies_as_string_raises_type_error(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = json.dumps(
            {"devDependencies": "oops"}
        ).encode()
        with pytest.raises(TypeError):
            detect_test_framework_from_package_json(client, "img:bad_dev_deps")


class TestCheckNodeModulesGarbageJson:
    def test_garbage_output_propagates_inside_except_and_returns_false(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"not json at all"
        ok, detail = check_node_modules(client, "img:garbage")
        assert ok is False
        assert "node_modules check error" in detail

    def test_partial_json_returns_false_with_error(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b'{"count": '
        ok, detail = check_node_modules(client, "img:partial")
        assert ok is False
        assert "node_modules check error" in detail


class TestCheckNodeVersionTypeContract:
    def test_int_expected_mismatches_string_actual(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"20\n"
        ok, detail = check_node_version(client, "img:int_exp", expected=20)
        assert ok is False
        assert "Expected Node 20" in detail
        assert "got 20" in detail

    def test_str_expected_matches_string_actual(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"20\n"
        ok, detail = check_node_version(client, "img:str_exp", expected="20")
        assert ok is True
        assert "Node 20" in detail

    def test_none_expected_uses_default_node_version_as_string(self) -> None:
        client = MagicMock(spec=["containers"])
        client.containers.run.return_value = b"20\n"
        ok, detail = check_node_version(client, "img:none_exp")
        assert ok is True
        assert "Node 20" in detail
