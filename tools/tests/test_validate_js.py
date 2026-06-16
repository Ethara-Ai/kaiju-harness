from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.validate_js import (
    NATIVE_BINDING_DEPS,
    REQUIRED_PKG_FIELDS,
    SUPPORTED_PMS_BY_LOCKFILE,
    detect_pm_and_framework,
    validate_js_candidate,
)


def _make_repo(
    tmp_path: Path,
    package: dict | None = None,
    lockfile: str = "package-lock.json",
) -> Path:
    pkg = package or {
        "name": "lib",
        "version": "1.0.0",
        "scripts": {"test": "jest"},
        "devDependencies": {"jest": "29.0.0"},
    }
    (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    if lockfile:
        (tmp_path / lockfile).write_text("{}", encoding="utf-8")
    return tmp_path


class TestC1NativeBindings:
    def test_native_bindings_are_exactly_four(self) -> None:
        assert NATIVE_BINDING_DEPS == (
            "node-gyp",
            "node-pre-gyp",
            "node-addon-api",
            "prebuild-install",
        )

    @pytest.mark.parametrize(
        "binding",
        ["node-gyp", "node-pre-gyp", "node-addon-api", "prebuild-install"],
    )
    def test_each_binding_rejected(self, binding: str, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29", binding: "1"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any(binding in r for r in reasons)

    def test_binding_in_dependencies_also_rejected(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "dependencies": {"node-gyp": "1"},
                "devDependencies": {"jest": "29"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("node-gyp" in r for r in reasons)


class TestEnginesNodeBrowserGate:
    def test_browser_truthy_without_engines_node_rejected(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "browser-lib",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
                "browser": "./browser-entry.js",
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("browser-only" in r for r in reasons)

    def test_browser_truthy_with_engines_node_accepted(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "iso-lib",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
                "browser": "./browser-entry.js",
                "engines": {"node": ">=20"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is True, f"unexpected reasons: {reasons}"

    def test_no_browser_field_no_engines_node_accepted(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "node-lib",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is True, f"unexpected reasons: {reasons}"

    def test_browser_empty_string_does_not_trigger_rejection(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "lib",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
                "browser": "",
            },
        )
        ok, _ = validate_js_candidate(tmp_path)
        assert ok is True


class TestRequiredFields:
    def test_required_fields_constant(self) -> None:
        assert REQUIRED_PKG_FIELDS == ("name", "version")

    def test_missing_name(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("name" in r for r in reasons)

    def test_missing_version(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("version" in r for r in reasons)


class TestLockfileRules:
    def test_no_package_json(self, tmp_path: Path) -> None:
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert reasons == ["no package.json"]

    def test_no_lockfile(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "version": "1.0.0",
                "scripts": {"test": "jest"},
                "devDependencies": {"jest": "29"},
            },
            lockfile="",
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("lockfile" in r for r in reasons)

    def test_multiple_lockfiles(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("multiple lockfiles" in r for r in reasons)

    def test_supported_pms_by_lockfile_complete(self) -> None:
        assert SUPPORTED_PMS_BY_LOCKFILE == {
            "package-lock.json": "npm",
            "pnpm-lock.yaml": "pnpm",
            "yarn.lock": "yarn",
            "bun.lockb": "bun",
        }


class TestMissingScriptsTest:
    def test_missing_test_script_rejected(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "version": "1.0.0",
                "devDependencies": {"jest": "29"},
            },
        )
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("scripts.test" in r for r in reasons)


class TestUnreadablePackageJson:
    def test_invalid_json_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("unreadable" in r for r in reasons)

    def test_array_root_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("[]", encoding="utf-8")
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is False
        assert any("JSON object" in r for r in reasons)


class TestDetectPmAndFramework:
    @pytest.mark.parametrize(
        ("lockfile", "expected_pm"),
        [
            ("package-lock.json", "npm"),
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
        ],
    )
    def test_pm_detection_via_lockfile(
        self, tmp_path: Path, lockfile: str, expected_pm: str
    ) -> None:
        _make_repo(tmp_path, lockfile=lockfile)
        info = detect_pm_and_framework(tmp_path)
        assert info["package_manager"] == expected_pm

    @pytest.mark.parametrize(
        ("dep", "expected_framework"),
        [
            ("jest", "jest"),
            ("vitest", "vitest"),
            ("mocha", "mocha"),
        ],
    )
    def test_framework_detection_via_devdeps(
        self, tmp_path: Path, dep: str, expected_framework: str
    ) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "version": "1.0.0",
                "scripts": {"test": dep},
                "devDependencies": {dep: "1.0.0"},
            },
        )
        info = detect_pm_and_framework(tmp_path)
        assert info["test_framework"] == expected_framework

    def test_node_test_framework_detected_via_script(self, tmp_path: Path) -> None:
        _make_repo(
            tmp_path,
            package={
                "name": "x",
                "version": "1.0.0",
                "scripts": {"test": "node --test"},
                "devDependencies": {},
            },
        )
        info = detect_pm_and_framework(tmp_path)
        assert info["test_framework"] == "node_test"


class TestHappyPath:
    def test_minimal_valid_repo(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        ok, reasons = validate_js_candidate(tmp_path)
        assert ok is True
        assert reasons == []


class TestCloneRepoSubprocessForm:
    def test_clone_uses_list_form_no_shell(self) -> None:
        import subprocess
        from unittest.mock import patch

        from tools.validate_js import _clone_repo

        captured: dict = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["shell"] = kwargs.get("shell", False)
            captured["timeout"] = kwargs.get("timeout")
            captured["check"] = kwargs.get("check", False)
            result = subprocess.CompletedProcess(cmd, 0, "", "")
            return result

        with patch("tools.validate_js.subprocess.run", _fake_run):
            _clone_repo("owner/repo", Path("/tmp/nonexistent_clone_root"))

        cmd = captured["cmd"]
        assert isinstance(cmd, list), "must use list form to avoid shell injection"
        assert captured["shell"] is False
        assert cmd[0] == "git"
        assert cmd[1] == "clone"
        assert "--depth" in cmd
        assert "--branch" in cmd
        assert "https://github.com/owner/repo.git" in cmd
        assert captured["timeout"] == 120
        assert captured["check"] is True

    def test_evil_branch_passed_as_positional_argument(self) -> None:
        import subprocess
        from unittest.mock import patch

        from tools.validate_js import _clone_repo

        captured: dict = {}

        def _fake_run(cmd, **_kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("tools.validate_js.subprocess.run", _fake_run):
            _clone_repo(
                "owner/repo",
                Path("/tmp/nonexistent_clone_root_b"),
                branch="--upload-pack=evil",
            )

        cmd = captured["cmd"]
        branch_idx = cmd.index("--branch")
        assert cmd[branch_idx + 1] == "--upload-pack=evil"
        assert cmd[branch_idx + 1] != "evil"

    def test_clone_failure_cleans_up_partial_dir_after_subprocess_failure(
        self, tmp_path: Path
    ) -> None:
        import subprocess
        from unittest.mock import patch

        from tools.validate_js import _clone_repo

        target = tmp_path / "owner__repo"

        def _fake_run(cmd, **_kwargs):
            target.mkdir(exist_ok=True)
            (target / ".git_partial").write_text("half")
            raise subprocess.CalledProcessError(returncode=128, cmd=cmd)

        with patch("tools.validate_js.subprocess.run", _fake_run):
            with pytest.raises(subprocess.CalledProcessError):
                _clone_repo("owner/repo", tmp_path)
        assert not target.exists()

    def test_existing_clone_dir_short_circuits(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from tools.validate_js import _clone_repo

        existing = tmp_path / "owner__repo"
        existing.mkdir()
        (existing / "package.json").write_text("{}")

        with patch("tools.validate_js.subprocess.run") as mock_run:
            result = _clone_repo("owner/repo", tmp_path)
        mock_run.assert_not_called()
        assert result == existing
