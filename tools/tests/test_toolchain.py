"""Regression tests for ``tools._toolchain`` (invariants I1-I11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import _toolchain as tc
from tools._toolchain import (
    LTS_ALIASES,
    STATE_SCHEMA_VERSION,
    SUPPORTED_TOOLS,
    InstallMethod,
    NodeSwitchResult,
    ProvisionResult,
    Tool,
    ToolchainAllowlistError,
    ToolchainConcurrencyError,
    ToolchainConfig,
    ToolchainError,
    ToolchainInstallFailedError,
    ToolchainMissingError,
    ToolchainSwitchUnavailableError,
    ToolchainTrustDeniedError,
    ToolchainVersionMismatchError,
    TrustMode,
    build_env_for_repo,
    ensure_node,
    ensure_pm,
    list_installed,
    load_config,
    uninstall,
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "kaiju-state"
    monkeypatch.setenv("KAIJU_TOOLCHAIN_STATE_DIR", str(d))
    return d


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("KAIJU_"):
            monkeypatch.delenv(k, raising=False)


class TestEnums:
    def test_tool_values(self) -> None:
        assert Tool.YARN.value == "yarn"
        assert Tool.PNPM.value == "pnpm"
        assert Tool.NPM.value == "npm"
        assert Tool.BUN.value == "bun"

    def test_trust_mode_values(self) -> None:
        assert TrustMode.STRICT.value == "strict"
        assert TrustMode.NORMAL.value == "normal"
        assert TrustMode.PERMISSIVE.value == "permissive"

    def test_install_method_covers_all_paths(self) -> None:
        assert {m.value for m in InstallMethod} >= {
            "already-present",
            "npm-global",
            "corepack-activate",
            "bun-installer-script",
        }


class TestErrorTaxonomy:
    def test_all_subclasses_inherit_from_base(self) -> None:
        for cls in (
            ToolchainMissingError,
            ToolchainInstallFailedError,
            ToolchainTrustDeniedError,
            ToolchainVersionMismatchError,
            ToolchainConcurrencyError,
            ToolchainSwitchUnavailableError,
            ToolchainAllowlistError,
        ):
            assert issubclass(cls, ToolchainError)


class TestEnvBool:
    def test_unset_returns_default_true(self) -> None:
        assert tc._env_bool({}, "X", default=True) is True

    def test_unset_returns_default_false(self) -> None:
        assert tc._env_bool({}, "X", default=False) is False

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy(self, v: str) -> None:
        assert tc._env_bool({"X": v}, "X", default=False) is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off", "FALSE"])
    def test_falsy(self, v: str) -> None:
        assert tc._env_bool({"X": v}, "X", default=True) is False

    def test_unknown_returns_default(self) -> None:
        assert tc._env_bool({"X": "maybe"}, "X", default=True) is True
        assert tc._env_bool({"X": "maybe"}, "X", default=False) is False


class TestValidateToolName:
    def test_allowlist_accepts_supported(self) -> None:
        for tool in ("npm", "pnpm", "yarn", "bun"):
            tc._validate_tool_name(tool)

    @pytest.mark.parametrize("bad", ["yarn-classic", "pnpn", "npmm", "denov1"])
    def test_allowlist_rejects_others(self, bad: str) -> None:
        with pytest.raises(ToolchainAllowlistError):
            tc._validate_tool_name(bad)


class TestParsePackageManagerField:
    def test_basic(self) -> None:
        pm, ver, integrity = tc._parse_package_manager_field("yarn@4.5.0")
        assert (pm, ver, integrity) == ("yarn", "4.5.0", None)

    def test_with_integrity(self) -> None:
        pm, ver, integrity = tc._parse_package_manager_field(
            "yarn@4.5.0+sha224.abcdef1234567890"
        )
        assert pm == "yarn"
        assert ver == "4.5.0"
        assert integrity == "sha224.abcdef1234567890"

    def test_pnpm_variant(self) -> None:
        pm, ver, _ = tc._parse_package_manager_field("pnpm@9.0.0")
        assert (pm, ver) == ("pnpm", "9.0.0")

    def test_missing_at_raises(self) -> None:
        with pytest.raises(ToolchainVersionMismatchError):
            tc._parse_package_manager_field("yarn")

    def test_disallowed_pm_raises_allowlist(self) -> None:
        with pytest.raises(ToolchainAllowlistError):
            tc._parse_package_manager_field("yarn-classic@1.22.0")

    def test_empty_version_raises(self) -> None:
        with pytest.raises(ToolchainVersionMismatchError):
            tc._parse_package_manager_field("yarn@")


class TestLoadConfig:
    def test_defaults(self, clean_env: None) -> None:
        cfg = load_config({})
        assert cfg.trust_mode == TrustMode.NORMAL
        assert cfg.no_auto_install is False
        assert cfg.dry_run is False
        assert cfg.offline is False
        assert cfg.node_switcher_preference == "auto"
        assert cfg.pin_node is None
        assert cfg.version_pins == {}

    def test_trust_mode_override(self) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_TRUST": "strict"})
        assert cfg.trust_mode == TrustMode.STRICT

    def test_invalid_trust_falls_back_normal(self) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_TRUST": "bogus"})
        assert cfg.trust_mode == TrustMode.NORMAL

    def test_no_auto_install(self) -> None:
        cfg = load_config({"KAIJU_NO_AUTO_INSTALL": "1"})
        assert cfg.no_auto_install is True

    def test_pin_node(self) -> None:
        cfg = load_config({"KAIJU_PIN_NODE": "20"})
        assert cfg.pin_node == "20"

    def test_bun_version_pin(self) -> None:
        cfg = load_config({"KAIJU_BUN_VERSION": "1.1.34"})
        assert cfg.version_pins["bun"] == "1.1.34"

    def test_switcher_preference_valid(self) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_NODE_SWITCHER": "fnm"})
        assert cfg.node_switcher_preference == "fnm"

    def test_switcher_preference_invalid_fallback(self) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_NODE_SWITCHER": "bogus"})
        assert cfg.node_switcher_preference == "auto"


class TestStateDir:
    def test_explicit_override(self) -> None:
        d = tc._default_state_dir({"KAIJU_TOOLCHAIN_STATE_DIR": "/tmp/foo"})
        assert d == Path("/tmp/foo")

    def test_xdg_state_home(self) -> None:
        d = tc._default_state_dir({"XDG_STATE_HOME": "/xdg"})
        assert d == Path("/xdg/kaiju/toolchain")

    def test_home_fallback(self) -> None:
        d = tc._default_state_dir({"HOME": "/home/user"})
        assert d == Path("/home/user/.kaiju/toolchain")


class TestEntryIdUniqueness:
    def test_ids_unique(self) -> None:
        ids = {tc._new_entry_id() for _ in range(100)}
        assert len(ids) == 100


class TestStateJournal:
    def _cfg(self, state_dir: Path) -> ToolchainConfig:
        return load_config({"KAIJU_TOOLCHAIN_STATE_DIR": str(state_dir)})

    def test_empty_when_missing(self, state_dir: Path) -> None:
        cfg = self._cfg(state_dir)
        data = tc._load_state(cfg)
        assert data["version"] == STATE_SCHEMA_VERSION
        assert data["entries"] == []

    def test_intent_then_outcome(self, state_dir: Path) -> None:
        cfg = self._cfg(state_dir)
        entry_id = tc._record_intent(
            cfg, "yarn", "1.22.22", InstallMethod.NPM_GLOBAL, ["npm", "i", "-g", "yarn"]
        )
        data = tc._load_state(cfg)
        assert len(data["entries"]) == 1
        assert data["entries"][0]["phase"] == "intent"

        tc._record_outcome(cfg, entry_id, "success", "/opt/homebrew/bin/yarn", "")
        data = tc._load_state(cfg)
        assert data["entries"][0]["phase"] == "success"
        assert data["entries"][0]["install_path"] == "/opt/homebrew/bin/yarn"
        assert data["entries"][0]["finished_at"] is not None

    def test_failure_captures_stderr_tail(self, state_dir: Path) -> None:
        cfg = self._cfg(state_dir)
        entry_id = tc._record_intent(
            cfg, "yarn", "latest", InstallMethod.NPM_GLOBAL, ["npm"]
        )
        long_stderr = "x" * 5000
        tc._record_outcome(cfg, entry_id, "failure", "", long_stderr)
        data = tc._load_state(cfg)
        assert len(data["entries"][0]["stderr_tail"]) == tc._STDERR_TAIL_CHARS

    def test_state_atomic_write_survives_crash(self, state_dir: Path) -> None:
        cfg = self._cfg(state_dir)
        tc._save_state(cfg, {"version": STATE_SCHEMA_VERSION, "entries": [{"a": 1}]})
        assert not (state_dir / "state.json.tmp").exists()
        assert (state_dir / "state.json").exists()

    def test_schema_mismatch_treated_as_fresh(self, state_dir: Path) -> None:
        cfg = self._cfg(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(json.dumps({"version": 999, "entries": [{"a": 1}]}))
        data = tc._load_state(cfg)
        assert data["entries"] == []


class TestLock:
    def test_lock_acquired_and_released(self, state_dir: Path) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_STATE_DIR": str(state_dir)})
        with tc._acquire_lock(cfg):
            assert (state_dir / "toolchain.lock").exists()

    def test_lock_contention_timeout(self, state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KAIJU_TOOLCHAIN_STATE_DIR", str(state_dir))
        monkeypatch.setenv("KAIJU_TOOLCHAIN_LOCK_TIMEOUT", "1")
        cfg = load_config()

        acquired = threading.Event()
        release = threading.Event()

        def _holder() -> None:
            with tc._acquire_lock(cfg):
                acquired.set()
                release.wait(timeout=10)

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        assert acquired.wait(timeout=5)
        with pytest.raises(ToolchainConcurrencyError):
            with tc._acquire_lock(cfg):
                pass
        release.set()
        t.join(timeout=5)


class TestLTSAliases:
    def test_all_aliases_map_to_supported(self) -> None:
        for alias, ver in LTS_ALIASES.items():
            assert ver.isdigit()

    def test_resolve_known(self) -> None:
        assert tc._resolve_lts_alias("lts/iron") == "20"
        assert tc._resolve_lts_alias("LTS/IRON") == "20"

    def test_resolve_unknown(self) -> None:
        assert tc._resolve_lts_alias("lts/bogus") is None


class TestToolVersionsFile:
    def test_nodejs_pin(self, tmp_path: Path) -> None:
        (tmp_path / ".tool-versions").write_text("nodejs 18.19.0\n")
        assert tc._collect_tool_versions(tmp_path) == "18"

    def test_node_alias(self, tmp_path: Path) -> None:
        (tmp_path / ".tool-versions").write_text("node 20.10.0\n")
        assert tc._collect_tool_versions(tmp_path) == "20"

    def test_lts_alias_in_toolversions(self, tmp_path: Path) -> None:
        (tmp_path / ".tool-versions").write_text("nodejs lts/iron\n")
        assert tc._collect_tool_versions(tmp_path) == "20"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert tc._collect_tool_versions(tmp_path) is None

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        (tmp_path / ".tool-versions").write_text(
            "# comment\n\npython 3.11.5\nnodejs 22.0.0\n"
        )
        assert tc._collect_tool_versions(tmp_path) == "22"


class TestDetectSwitcher:
    def test_none_preference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_NODE_SWITCHER": "none"})
        assert tc._detect_switcher(cfg) is None

    def test_explicit_preference_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_NODE_SWITCHER": "fnm"})
        monkeypatch.setattr(tc, "_which", lambda t: None)
        monkeypatch.setattr(tc, "_nvm_available", lambda: False)
        assert tc._detect_switcher(cfg) is None

    def test_auto_prefers_fnm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_NODE_SWITCHER": "auto"})
        monkeypatch.setattr(tc, "_which", lambda t: "/usr/bin/fnm" if t == "fnm" else None)
        monkeypatch.setattr(tc, "_nvm_available", lambda: False)
        assert tc._detect_switcher(cfg) == "fnm"


class TestEnsurePM:
    def test_fast_path_when_installed(self, monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
        monkeypatch.setattr(tc, "_which", lambda t: "/usr/bin/yarn" if t == "yarn" else None)
        monkeypatch.setattr(tc, "_installed_version", lambda t: "1.22.22")
        result = ensure_pm("yarn")
        assert result.from_cache is True
        assert result.install_method == InstallMethod.ALREADY_PRESENT
        assert result.install_path == "/usr/bin/yarn"

    def test_allowlist_enforced(self) -> None:
        with pytest.raises(ToolchainAllowlistError):
            ensure_pm("bogus-pm")

    def test_no_auto_install_opt_out(
        self, monkeypatch: pytest.MonkeyPatch, state_dir: Path
    ) -> None:
        monkeypatch.setenv("KAIJU_NO_AUTO_INSTALL", "1")
        monkeypatch.setattr(tc, "_which", lambda t: None)
        with pytest.raises(ToolchainMissingError):
            ensure_pm("yarn")


class TestEnsureNodeInvariantI1:
    def test_no_switcher_returns_empty_env_delta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        monkeypatch.setattr(tc, "_detect_switcher", lambda cfg: None)
        monkeypatch.setattr(tc, "_installed_version", lambda t: "20.10.0")
        (tmp_path / "package.json").write_text('{"engines": {"node": ">=20"}}')
        result = ensure_node(tmp_path)
        assert result.env_delta == {}

    def test_no_switcher_warns_when_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        clean_env: None,
    ) -> None:
        monkeypatch.setattr(tc, "_detect_switcher", lambda cfg: None)
        monkeypatch.setattr(tc, "_installed_version", lambda t: "26.5.0")
        (tmp_path / "package.json").write_text('{"engines": {"node": ">=20 <21"}}')
        import logging
        with caplog.at_level(logging.WARNING):
            ensure_node(tmp_path)
        assert any("no switcher" in rec.message for rec in caplog.records)


class TestBuildEnvForRepo:
    def test_returns_dict_with_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        monkeypatch.setattr(tc, "_detect_switcher", lambda cfg: None)
        monkeypatch.setattr(tc, "_installed_version", lambda t: "20.10.0")
        env = build_env_for_repo(tmp_path)
        assert "PATH" in env
        assert env is not os.environ

    def test_does_not_mutate_os_environ_I1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
    ) -> None:
        before = dict(os.environ)
        monkeypatch.setattr(tc, "_detect_switcher", lambda cfg: None)
        monkeypatch.setattr(tc, "_installed_version", lambda t: "20.10.0")
        build_env_for_repo(tmp_path)
        assert dict(os.environ) == before


class TestUninstall:
    def test_refuses_npm(self, state_dir: Path) -> None:
        with pytest.raises(ToolchainError, match="npm"):
            uninstall("npm")

    def test_refuses_untracked_tool(self, state_dir: Path) -> None:
        with pytest.raises(ToolchainError, match="no successful install"):
            uninstall("yarn")


class TestListInstalled:
    def test_empty_when_no_state(self, state_dir: Path) -> None:
        assert list_installed() == []

    def test_filters_phase_success(self, state_dir: Path) -> None:
        cfg = load_config({"KAIJU_TOOLCHAIN_STATE_DIR": str(state_dir)})
        good = tc._record_intent(cfg, "yarn", "1.22.22", InstallMethod.NPM_GLOBAL, [])
        tc._record_outcome(cfg, good, "success", "/opt/homebrew/bin/yarn", "")
        bad = tc._record_intent(cfg, "pnpm", "9.0.0", InstallMethod.NPM_GLOBAL, [])
        tc._record_outcome(cfg, bad, "failure", "", "err")
        results = list_installed()
        tools = [e["tool"] for e in results]
        assert "yarn" in tools
        assert "pnpm" not in tools


class TestBunManifest:
    def test_load_returns_default_and_versions(self) -> None:
        manifest = tc._load_bun_manifest()
        assert "default" in manifest
        assert "versions" in manifest
        assert manifest["default"] in manifest["versions"]

    def test_resolve_pinned_version(self) -> None:
        cfg = load_config({"KAIJU_BUN_VERSION": "1.1.29"})
        version, entry = tc._resolve_bun_version(cfg)
        assert version == "1.1.29"
        assert "installer_url" in entry

    def test_unknown_pinned_version_raises(self) -> None:
        cfg = load_config({"KAIJU_BUN_VERSION": "99.99.99"})
        with pytest.raises(ToolchainVersionMismatchError):
            tc._resolve_bun_version(cfg)


class TestI6IntentBeforeInstall:
    def test_bun_install_failure_records_intent(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tc, "_which", lambda t: "/usr/bin/npm" if t == "npm" else None)

        def _boom(*a, **kw):
            raise ToolchainInstallFailedError("simulated bun install crash")

        monkeypatch.setattr(tc, "_install_bun_hybrid", _boom)
        cfg = load_config({"KAIJU_TOOLCHAIN_STATE_DIR": str(state_dir)})
        with pytest.raises(ToolchainInstallFailedError):
            tc._do_install("bun", None, cfg)
        data = tc._load_state(cfg)
        bun_entries = [e for e in data["entries"] if e.get("tool") == "bun"]
        assert len(bun_entries) == 1
        assert bun_entries[0]["phase"] == "failure"
        assert "simulated bun install crash" in bun_entries[0]["stderr_tail"]

    def test_pnpm_npm_global_failure_records_intent(
        self, state_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tc, "_which", lambda t: "/usr/bin/npm" if t == "npm" else None)

        def _boom(*a, **kw):
            raise ToolchainInstallFailedError("simulated npm global crash")

        monkeypatch.setattr(tc, "_install_pm_via_npm", _boom)
        cfg = load_config({"KAIJU_TOOLCHAIN_STATE_DIR": str(state_dir)})
        with pytest.raises(ToolchainInstallFailedError):
            tc._do_install("pnpm", None, cfg)
        data = tc._load_state(cfg)
        pnpm_entries = [e for e in data["entries"] if e.get("tool") == "pnpm"]
        assert len(pnpm_entries) >= 1
        assert pnpm_entries[-1]["phase"] == "failure"


class TestResolveInstallCommand:
    """Yarn Classic vs Berry flag selection. The signature_pad regression:
    prepare passed `yarn install --frozen-lockfile --ignore-scripts` (Classic
    flags) to a Yarn 4 (Berry) repo, which errors with "Unsupported option
    name" and — under check=False — leaves node_modules EMPTY, so tsc + test-id
    discovery silently reported a repo with 4 test files as having zero tests.
    """

    def _pkg(self, tmp_path: Path, pm_field: str | None = None,
             yarnrc_yml: bool = False) -> Path:
        pj = {"name": "x", "version": "1.0.0"}
        if pm_field:
            pj["packageManager"] = pm_field
        (tmp_path / "package.json").write_text(json.dumps(pj), encoding="utf-8")
        if yarnrc_yml:
            (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
        return tmp_path

    def test_yarn_major_from_package_manager_field(self, tmp_path):
        repo = self._pkg(tmp_path, "yarn@4.13.0")
        assert tc.yarn_major(repo) == 4

    def test_yarn_major_from_yarnrc_yml(self, tmp_path):
        repo = self._pkg(tmp_path, None, yarnrc_yml=True)
        assert tc.yarn_major(repo) == 2  # Berry-only file -> >=2

    def test_berry_uses_immutable_and_skip_build(self, tmp_path):
        repo = self._pkg(tmp_path, "yarn@4.13.0")
        assert tc.resolve_install_command("yarn", repo, frozen=True) == [
            "yarn", "install", "--immutable", "--mode=skip-build",
        ]
        assert tc.resolve_install_command("yarn", repo, frozen=False) == [
            "yarn", "install", "--mode=skip-build",
        ]

    def test_berry_never_emits_classic_flags(self, tmp_path):
        repo = self._pkg(tmp_path, "yarn@3.6.0")
        for frozen in (True, False):
            cmd = tc.resolve_install_command("yarn", repo, frozen=frozen)
            assert "--frozen-lockfile" not in cmd
            assert "--ignore-scripts" not in cmd

    def test_classic_yarn_uses_frozen_lockfile(self, tmp_path):
        repo = self._pkg(tmp_path, "yarn@1.22.19")
        assert tc.resolve_install_command("yarn", repo, frozen=True) == [
            "yarn", "install", "--frozen-lockfile", "--ignore-scripts",
        ]
        assert tc.resolve_install_command("yarn", repo, frozen=False) == [
            "yarn", "install", "--ignore-scripts",
        ]

    def test_unknown_yarn_defaults_to_classic(self, tmp_path):
        # No packageManager, no .yarnrc.yml, and (in CI) maybe no yarn on PATH.
        repo = self._pkg(tmp_path, None)
        with patch.object(tc, "_which", return_value=None):
            cmd = tc.resolve_install_command("yarn", repo, frozen=True)
        assert cmd == ["yarn", "install", "--frozen-lockfile", "--ignore-scripts"]

    def test_npm_pnpm_bun_unaffected(self, tmp_path):
        repo = self._pkg(tmp_path, None)
        assert tc.resolve_install_command("npm", repo, frozen=True)[:2] == ["npm", "ci"]
        assert tc.resolve_install_command("npm", repo, frozen=False)[:2] == ["npm", "install"]
        assert "--package-lock=true" in tc.resolve_install_command("npm", repo, frozen=False)
        assert tc.resolve_install_command("pnpm", repo, frozen=True) == [
            "pnpm", "install", "--ignore-scripts", "--frozen-lockfile",
        ]
        assert tc.resolve_install_command("bun", repo, frozen=False) == [
            "bun", "install", "--ignore-scripts",
        ]

    def test_rejects_unknown_pm(self, tmp_path):
        repo = self._pkg(tmp_path, None)
        with pytest.raises(tc.ToolchainError):
            tc.resolve_install_command("cargo", repo, frozen=True)
