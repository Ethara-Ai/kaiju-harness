from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from commit0.harness import build_js


def _entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "instance_id": "commit-0/p-queue",
        "repo": "owner/p-queue",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node_version": 20,
            "install": "npm install",
            "packages": [],
            "pre_install": [],
            "specification": "",
            "package_manager": "npm",
            "test_framework": "jest",
        },
        "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
        "src_dir": "src",
        "language": "javascript",
        "test_framework": "jest",
        "package_manager": "npm",
        "node_version": 20,
    }
    base.update(overrides)
    return base


@pytest.fixture
def docker_env(monkeypatch: pytest.MonkeyPatch):
    import sys

    fake_client = MagicMock()
    fake_client.close = MagicMock()
    saved_docker = sys.modules.get("docker")
    saved_errors = sys.modules.get("docker.errors")
    fake_module = MagicMock(from_env=MagicMock(return_value=fake_client))
    monkeypatch.setattr(
        "commit0.harness.build_js.docker", fake_module, raising=False
    )
    monkeypatch.setitem(sys.modules, "docker", fake_module)
    yield fake_client
    if saved_docker is not None:
        sys.modules["docker"] = saved_docker
    else:
        sys.modules.pop("docker", None)
    if saved_errors is not None:
        sys.modules["docker.errors"] = saved_errors


class TestMissingRepoFieldSkipped:
    def test_entry_missing_repo_does_not_match_split(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bad = _entry()
        del bad["repo"]
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([bad]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        with caplog.at_level(logging.WARNING, logger="commit0.harness.build_js"):
            build_js.main(
                dataset_name="x",
                dataset_split="test",
                split="all",
                num_workers=1,
                verbose=0,
            )
        assert any("Nothing to build" in rec.message for rec in caplog.records)


class TestEmptySplitReturnsWithoutBuild:
    def test_warns_and_returns_without_sys_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry(repo="other/lib")]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["nonexistent-repo"]
        )
        with caplog.at_level(logging.WARNING, logger="commit0.harness.build_js"):
            build_js.main(
                dataset_name="x",
                dataset_split="test",
                split="bogus",
                num_workers=1,
                verbose=0,
            )
        assert any(
            "Nothing to build" in rec.message and "bogus" in rec.message
            for rec in caplog.records
        )

    def test_does_not_call_build_repo_images_when_specs_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
    ) -> None:
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: []
        )
        build_mock = MagicMock()
        monkeypatch.setattr(build_js, "build_repo_images", build_mock)
        build_js.main(
            dataset_name="x",
            dataset_split="test",
            split="all",
            num_workers=1,
            verbose=0,
        )
        build_mock.assert_not_called()


class TestHealthCheckFailureDoesNotExit:
    def test_failing_health_check_logs_warning_only(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry()]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            build_js,
            "build_repo_images",
            MagicMock(return_value=(["p-queue"], {})),
        )
        monkeypatch.setattr(
            build_js,
            "run_js_health_checks",
            MagicMock(
                return_value=[
                    (False, "node_modules", "node_modules missing: ENOENT"),
                    (True, "node_version", "Node 20"),
                ]
            ),
        )
        with caplog.at_level(logging.WARNING, logger="commit0.harness.build_js"):
            build_js.main(
                dataset_name="x",
                dataset_split="test",
                split="all",
                num_workers=1,
                verbose=0,
            )
        assert any("Health check FAILED" in rec.message for rec in caplog.records)

    def test_build_failure_triggers_sys_exit_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
    ) -> None:
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry()]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            build_js,
            "build_repo_images",
            MagicMock(return_value=([], {"img": "boom"})),
        )
        monkeypatch.setattr(
            build_js,
            "run_js_health_checks",
            MagicMock(return_value=[]),
        )
        with pytest.raises(SystemExit) as excinfo:
            build_js.main(
                dataset_name="x",
                dataset_split="test",
                split="all",
                num_workers=1,
                verbose=0,
            )
        assert excinfo.value.code == 1


class TestPkgManagerCacheSharedAcrossMainCalls:
    def test_cache_is_module_level_singleton_seen_by_build_js(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
    ) -> None:
        from commit0.harness import health_check_js

        health_check_js._PKG_MANAGER_CACHE.clear()
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([_entry()]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            build_js,
            "build_repo_images",
            MagicMock(return_value=(["p-queue"], {})),
        )

        def _populate_cache(*_args, **_kwargs):
            health_check_js._PKG_MANAGER_CACHE["img:from-main"] = "pnpm"
            return []

        monkeypatch.setattr(build_js, "run_js_health_checks", _populate_cache)
        build_js.main(
            dataset_name="x",
            dataset_split="test",
            split="all",
            num_workers=1,
            verbose=0,
        )
        assert health_check_js._PKG_MANAGER_CACHE.get("img:from-main") == "pnpm"
        build_js.main(
            dataset_name="x",
            dataset_split="test",
            split="all",
            num_workers=1,
            verbose=0,
        )
        assert health_check_js._PKG_MANAGER_CACHE.get("img:from-main") == "pnpm"
        health_check_js._PKG_MANAGER_CACHE.clear()


class TestPackagesAsStringIteratesCharacters:
    def test_string_packages_propagated_as_string_to_health_checks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
    ) -> None:
        entry = _entry()
        entry["setup"]["packages"] = "lodash"
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([entry]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            build_js,
            "build_repo_images",
            MagicMock(return_value=(["p-queue"], {})),
        )
        captured: dict[str, Any] = {}

        def _capture(*_args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(build_js, "run_js_health_checks", _capture)
        build_js.main(
            dataset_name="x",
            dataset_split="test",
            split="all",
            num_workers=1,
            verbose=0,
        )
        assert captured["packages"] == "lodash"
        assert isinstance(captured["packages"], str)


class TestNodeVersionNullPassedThrough:
    def test_null_node_version_passes_none_to_health_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
        docker_env: MagicMock,
    ) -> None:
        entry = _entry()
        entry["setup"]["node_version"] = None
        monkeypatch.setattr(
            build_js,
            "load_dataset_from_config",
            lambda *_a, **_kw: iter([entry]),
        )
        monkeypatch.setattr(
            build_js, "resolve_split", lambda *_a, **_kw: ["p-queue"]
        )
        monkeypatch.setattr(
            build_js,
            "build_repo_images",
            MagicMock(return_value=(["p-queue"], {})),
        )
        captured: dict[str, Any] = {}

        def _capture(*_args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(build_js, "run_js_health_checks", _capture)
        build_js.main(
            dataset_name="x",
            dataset_split="test",
            split="all",
            num_workers=1,
            verbose=0,
        )
        assert captured["node_version"] is None
