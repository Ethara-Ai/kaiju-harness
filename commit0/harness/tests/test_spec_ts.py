from __future__ import annotations

from unittest.mock import patch

import pytest

from commit0.harness.constants_ts import TsRepoInstance
from commit0.harness.spec_ts import Commit0TsSpec, make_ts_spec


def _make_ts_instance(**overrides: object) -> dict:
    defaults: dict = {
        "instance_id": "commit-0/zod",
        "repo": "colinhacks/zod",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node": "20",
            "install": "npm install",
            "packages": [],
            "pre_install": [],
        },
        "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
        "src_dir": "src",
    }
    defaults.update(overrides)
    return defaults


def _make_spec(instance: dict | None = None, absolute: bool = True) -> Commit0TsSpec:
    inst = instance or _make_ts_instance()
    return make_ts_spec(inst, absolute=absolute)


class TestMakeTsSpec:
    def test_returns_commit0_ts_spec(self) -> None:
        spec = _make_spec()
        assert isinstance(spec, Commit0TsSpec)

    def test_from_dict(self) -> None:
        spec = _make_spec()
        assert spec.repo == "commit-0/zod"

    def test_from_dict_missing_instance_id_uses_repo(self) -> None:
        inst = _make_ts_instance()
        del inst["instance_id"]
        spec = make_ts_spec(inst)
        assert spec.repo == "colinhacks/zod"

    def test_absolute_true_uses_testbed(self) -> None:
        spec = _make_spec(absolute=True)
        assert spec.repo_directory == "/testbed"

    def test_absolute_false_uses_relative(self) -> None:
        spec = _make_spec(absolute=False)
        assert spec.repo_directory == "testbed"

    def test_from_ts_repo_instance(self) -> None:
        inst = TsRepoInstance(
            instance_id="commit-0/zod",
            repo="colinhacks/zod",
            base_commit="a" * 40,
            reference_commit="b" * 40,
            setup={"node": "20", "install": "npm install"},
            test={"test_cmd": "npx jest"},
            src_dir="src",
        )
        spec = make_ts_spec(inst, absolute=True)
        assert isinstance(spec, Commit0TsSpec)
        assert spec.repo == "commit-0/zod"


class TestBaseImageKey:
    def test_default_node20(self) -> None:
        spec = _make_spec()
        assert spec.base_image_key == "commit0.base.node20:latest"

    def test_explicit_node22(self) -> None:
        inst = _make_ts_instance(setup={"node": "22"})
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node22:latest"

    def test_no_node_version_defaults_to_20(self) -> None:
        inst = _make_ts_instance(setup={})
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node20:latest"


class TestRepoImageKey:
    def test_hash_consistency(self) -> None:
        spec1 = _make_spec()
        spec2 = _make_spec()
        assert spec1.repo_image_key == spec2.repo_image_key

    def test_different_scripts_differ(self) -> None:
        spec1 = _make_spec(_make_ts_instance(base_commit="x" * 40))
        spec2 = _make_spec(_make_ts_instance(base_commit="y" * 40))
        assert spec1.repo_image_key != spec2.repo_image_key

    def test_lowercase(self) -> None:
        spec = _make_spec()
        assert spec.repo_image_key == spec.repo_image_key.lower()


class TestSetupScript:
    def test_starts_with_shebang(self) -> None:
        spec = _make_spec()
        assert spec.setup_script.startswith("#!/bin/bash")

    def test_contains_git_clone(self) -> None:
        spec = _make_spec()
        assert "git clone" in spec.setup_script

    def test_contains_reference_commit(self) -> None:
        spec = _make_spec()
        assert "b" * 40 in spec.setup_script

    def test_contains_npm_install(self) -> None:
        spec = _make_spec()
        assert "npm install" in spec.setup_script

    def test_contains_node_gyp(self) -> None:
        spec = _make_spec()
        assert "node-gyp" in spec.setup_script

    def test_custom_install_cmd(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "yarn install"})
        spec = _make_spec(inst)
        assert "yarn install" in spec.setup_script


class TestEvalScript:
    def test_starts_with_shebang(self) -> None:
        spec = _make_spec()
        assert spec.eval_script.startswith("#!/bin/bash")

    def test_contains_git_apply(self) -> None:
        spec = _make_spec()
        assert "git apply" in spec.eval_script

    def test_contains_test_cmd(self) -> None:
        spec = _make_spec()
        assert "npx jest" in spec.eval_script

    def test_contains_force_exit(self) -> None:
        spec = _make_spec()
        assert "--forceExit" in spec.eval_script

    def test_absolute_uses_absolute_diff_path(self) -> None:
        spec = _make_spec(absolute=True)
        assert "/patch.diff" in spec.eval_script

    def test_relative_uses_relative_diff_path(self) -> None:
        spec = _make_spec(absolute=False)
        assert "../patch.diff" in spec.eval_script

    def test_custom_test_cmd(self) -> None:
        inst = _make_ts_instance(test={"test_cmd": "npx vitest run"})
        spec = _make_spec(inst)
        assert "npx vitest run" in spec.eval_script

    def test_missing_test_cmd_defaults_to_jest(self) -> None:
        inst = _make_ts_instance(test={"test_dir": "__tests__"})
        spec = _make_spec(inst)
        assert "npx jest" in spec.eval_script


class TestPackageManagerInstall:
    def test_pnpm_install_global(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "pnpm install"})
        spec = _make_spec(inst)
        assert "npm install -g pnpm" in spec.setup_script

    def test_yarn_install_global(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "yarn install"})
        spec = _make_spec(inst)
        assert "npm install -g yarn" in spec.setup_script

    def test_bun_install_global(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "bun install"})
        spec = _make_spec(inst)
        assert "npm install -g bun" in spec.setup_script

    def test_npm_no_global_install(self) -> None:
        spec = _make_spec()
        script = spec.setup_script
        assert "npm install -g pnpm" not in script
        assert "npm install -g yarn" not in script
        assert "npm install -g bun" not in script


class TestNodeGypPkgManager:
    def test_npm_uses_npx_yes(self) -> None:
        spec = _make_spec()
        assert "npx --yes node-gyp rebuild" in spec.setup_script

    def test_pnpm_no_yes_flag(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "pnpm install"})
        spec = _make_spec(inst)
        assert "pnpm exec node-gyp rebuild" in spec.setup_script
        assert "pnpm exec --yes" not in spec.setup_script

    def test_yarn_no_yes_flag(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "yarn install"})
        spec = _make_spec(inst)
        assert "yarn node-gyp rebuild" in spec.setup_script
        assert "yarn --yes" not in spec.setup_script

    def test_bun_no_yes_flag(self) -> None:
        inst = _make_ts_instance(setup={"node": "20", "install": "bun install"})
        spec = _make_spec(inst)
        assert "bunx node-gyp rebuild" in spec.setup_script
        assert "bunx --yes" not in spec.setup_script


class TestEvalScriptPkgManager:
    def test_pnpm_test_cmd_used(self) -> None:
        inst = _make_ts_instance(
            setup={"node": "20", "install": "pnpm install"},
            test={"test_cmd": "pnpm exec jest"},
        )
        spec = _make_spec(inst)
        assert "pnpm exec jest" in spec.eval_script

    def test_missing_test_cmd_uses_detected_prefix(self) -> None:
        inst = _make_ts_instance(
            setup={"node": "20", "install": "pnpm install"},
            test={"test_dir": "__tests__"},
        )
        spec = _make_spec(inst)
        assert "pnpm exec jest" in spec.eval_script

    def test_bun_missing_test_cmd_uses_bunx(self) -> None:
        inst = _make_ts_instance(
            setup={"node": "20", "install": "bun install"},
            test={"test_dir": "__tests__"},
        )
        spec = _make_spec(inst)
        assert "bunx jest" in spec.eval_script


class TestBaseDockerfile:
    def test_delegates_to_get_dockerfile_base_ts(self) -> None:
        spec = _make_spec()
        with patch(
            "commit0.harness.spec_ts.get_dockerfile_base_ts",
            return_value="FROM node:20",
        ) as mock_fn:
            result = spec.base_dockerfile
            mock_fn.assert_called_once_with("20")
            assert result == "FROM node:20"


class TestRepoDockerfile:
    def test_delegates_to_get_dockerfile_repo_ts(self) -> None:
        spec = _make_spec()
        with patch(
            "commit0.harness.spec_ts.get_dockerfile_repo_ts",
            return_value="FROM commit0.base.node20:latest",
        ) as mock_fn:
            result = spec.repo_dockerfile
            mock_fn.assert_called_once()
            assert result == "FROM commit0.base.node20:latest"


# ---------------------------------------------------------------------------
# _exec_prefix_from_install — edge cases
# ---------------------------------------------------------------------------


class TestExecPrefixFromInstall:
    def test_empty_string_returns_npx(self) -> None:
        from commit0.harness.spec_ts import _exec_prefix_from_install

        assert _exec_prefix_from_install("") == "npx"

    def test_mixed_case_pnpm(self) -> None:
        from commit0.harness.spec_ts import _exec_prefix_from_install

        assert _exec_prefix_from_install("PNPM install") == "pnpm exec"

    def test_unknown_manager_returns_npx(self) -> None:
        from commit0.harness.spec_ts import _exec_prefix_from_install

        assert _exec_prefix_from_install("deno install") == "npx"

    def test_bun_detected(self) -> None:
        from commit0.harness.spec_ts import _exec_prefix_from_install

        assert _exec_prefix_from_install("bun install") == "bunx"

    def test_yarn_detected(self) -> None:
        from commit0.harness.spec_ts import _exec_prefix_from_install

        assert _exec_prefix_from_install("yarn install") == "yarn"


# ---------------------------------------------------------------------------
# shlex.quote defense-in-depth
# ---------------------------------------------------------------------------


class TestShlexQuoting:
    def test_repo_with_shell_metacharacters(self) -> None:
        import shlex

        inst = _make_ts_instance(repo="evil;rm -rf /")
        spec = _make_spec(inst)
        setup = spec.setup_script
        quoted = shlex.quote("evil;rm -rf /")
        assert quoted in setup
        assert quoted != "evil;rm -rf /"

    def test_base_commit_with_shell_metacharacters(self) -> None:
        inst = _make_ts_instance(base_commit="abc;inject")
        spec = _make_spec(inst)
        setup = spec.setup_script
        assert "'abc;inject'" in setup

    def test_reference_commit_quoted_in_setup(self) -> None:
        inst = _make_ts_instance(reference_commit="ref;bad")
        spec = _make_spec(inst)
        setup = spec.setup_script
        assert "'ref;bad'" in setup

    def test_hostile_base_commit_rejected_in_eval_script(self) -> None:
        # The eval script splices base_commit into `git checkout <base> -- ...`;
        # eval_hardening now REJECTS a non-hex base_commit there, so an injection
        # payload can never reach the shell (stronger than the setup-script
        # quoting checked above).
        inst = _make_ts_instance(base_commit="abc;inject")
        spec = _make_spec(inst)
        with pytest.raises(ValueError, match="bare hex git SHA"):
            _ = spec.eval_script

    def test_valid_base_commit_in_eval_script(self) -> None:
        inst = _make_ts_instance(base_commit="a" * 40)
        spec = _make_spec(inst)
        evl = spec.eval_script
        # A valid bare-hex SHA needs no shell quoting; it appears verbatim.
        assert "a" * 40 in evl


# ---------------------------------------------------------------------------
# Eval script: --forceExit flag behavior
# ---------------------------------------------------------------------------


class TestForceExitFlag:
    def test_vitest_does_not_contain_force_exit(self) -> None:
        inst = _make_ts_instance(test={"test_cmd": "npx vitest run"})
        spec = _make_spec(inst)
        assert "--forceExit" not in spec.eval_script
        assert "--detectOpenHandles" not in spec.eval_script

    def test_jest_contains_force_exit(self) -> None:
        inst = _make_ts_instance(test={"test_cmd": "npx jest"})
        spec = _make_spec(inst)
        assert "--forceExit" in spec.eval_script
        assert "--detectOpenHandles" in spec.eval_script

    def test_vitest_uses_reporter_json(self) -> None:
        inst = _make_ts_instance(test={"test_cmd": "npx vitest run"})
        spec = _make_spec(inst)
        assert "--reporter=json" in spec.eval_script

    def test_jest_uses_json_flag(self) -> None:
        inst = _make_ts_instance(test={"test_cmd": "npx jest"})
        spec = _make_spec(inst)
        assert "--json" in spec.eval_script
        assert "--outputFile=report.json" in spec.eval_script


# ---------------------------------------------------------------------------
# make_ts_spec with TsRepoInstance — additional edge cases
# ---------------------------------------------------------------------------


class TestMakeTsSpecEdgeCases:
    def test_ts_repo_instance_with_custom_setup(self) -> None:
        inst = TsRepoInstance(
            instance_id="commit-0/prisma",
            repo="prisma/prisma",
            base_commit="c" * 40,
            reference_commit="d" * 40,
            setup={"node": "22", "install": "pnpm install"},
            test={"test_cmd": "pnpm exec vitest run"},
            src_dir="packages",
        )
        spec = make_ts_spec(inst, absolute=True)
        assert spec.base_image_key == "commit0.base.node22:latest"
        assert "--forceExit" not in spec.eval_script
        assert "pnpm exec vitest run" in spec.eval_script

    def test_ts_repo_instance_absolute_false(self) -> None:
        inst = TsRepoInstance(
            instance_id="commit-0/zod",
            repo="colinhacks/zod",
            base_commit="a" * 40,
            reference_commit="b" * 40,
            setup={"node": "20", "install": "npm install"},
            test={"test_cmd": "npx jest"},
            src_dir="src",
        )
        spec = make_ts_spec(inst, absolute=False)
        assert spec.repo_directory == "testbed"
        assert "../patch.diff" in spec.eval_script

    def test_object_without_instance_id_uses_str(self) -> None:
        class FakeInstance:
            pass

        fake = FakeInstance()
        spec = make_ts_spec(fake, absolute=True)
        assert isinstance(spec, Commit0TsSpec)
