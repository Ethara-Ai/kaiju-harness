from __future__ import annotations

import inspect
import shlex
from abc import ABC
from typing import Any, cast
from unittest.mock import patch

import pytest

from commit0.harness.constants import RepoInstance
from commit0.harness.constants_js import (
    DEFAULT_NODE_VERSION,
    JS_BASE_BRANCH,
    MAX_PATCH_BYTES,
)
from commit0.harness.spec import Spec
from commit0.harness.spec_js import Commit0JsSpec, make_js_spec


def _make_js_instance(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "instance_id": "commit-0/p-queue",
        "repo": "sindresorhus/p-queue",
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
    defaults.update(overrides)
    return defaults


def _make_spec(
    instance: dict[str, Any] | None = None, absolute: bool = True
) -> Commit0JsSpec:
    return make_js_spec(instance or _make_js_instance(), absolute=absolute)


class TestB1NoLanguageField:
    def test_no_language_in_dataclass_fields(self) -> None:
        assert "language" not in Commit0JsSpec.__dataclass_fields__

    def test_dataclass_fields_are_exactly_four(self) -> None:
        assert set(Commit0JsSpec.__dataclass_fields__.keys()) == {
            "absolute",
            "repo",
            "repo_directory",
            "instance",
        }

    def test_instantiation_rejects_language_kwarg(self) -> None:
        extra_kwargs: dict[str, Any] = {"language": "javascript"}
        with pytest.raises(TypeError):
            Commit0JsSpec(
                absolute=True,
                repo="r",
                repo_directory="/testbed",
                instance=cast(Any, {}),
                **extra_kwargs,
            )

    def test_make_js_spec_signature_no_language_kwarg(self) -> None:
        sig = inspect.signature(make_js_spec)
        assert "language" not in sig.parameters


class TestMro:
    def test_mro_chain(self) -> None:
        assert Commit0JsSpec.__mro__ == (Commit0JsSpec, Spec, ABC, object)

    def test_is_subclass_of_spec(self) -> None:
        assert issubclass(Commit0JsSpec, Spec)

    def test_is_abc_subclass(self) -> None:
        assert issubclass(Commit0JsSpec, ABC)


class TestMakeJsSpecKwargs:
    def test_returns_commit0_js_spec(self) -> None:
        spec = _make_spec()
        assert isinstance(spec, Commit0JsSpec)

    def test_constructor_uses_exactly_four_kwargs(self) -> None:
        inst = _make_js_instance()
        with patch(
            "commit0.harness.spec_js.Commit0JsSpec", wraps=Commit0JsSpec
        ) as mock_cls:
            make_js_spec(inst, absolute=True)
        assert mock_cls.call_count == 1
        positional, keyword = mock_cls.call_args
        assert positional == ()
        assert set(keyword.keys()) == {"absolute", "repo", "repo_directory", "instance"}

    def test_absolute_true_uses_testbed(self) -> None:
        spec = _make_spec(absolute=True)
        assert spec.repo_directory == "/testbed"

    def test_absolute_false_uses_relative(self) -> None:
        spec = _make_spec(absolute=False)
        assert spec.repo_directory == "testbed"

    def test_repo_from_dict_uses_repo_key(self) -> None:
        spec = _make_spec(_make_js_instance(repo="owner/lib"))
        assert spec.repo == "owner/lib"

    def test_repo_falls_back_to_instance_id(self) -> None:
        inst = _make_js_instance()
        del inst["repo"]
        spec = make_js_spec(inst, absolute=True)
        assert spec.repo == inst["instance_id"]


class TestNodeVersionDispatch:
    def test_default_node20_image_key(self) -> None:
        inst = _make_js_instance()
        inst["setup"] = {"install": "npm install"}
        del inst["node_version"]
        spec = _make_spec(inst)
        assert spec.base_image_key == f"commit0.base.node{DEFAULT_NODE_VERSION}:latest"

    def test_setup_node22_takes_precedence(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = 22
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node22:latest"

    def test_instance_level_node_version_used(self) -> None:
        inst = _make_js_instance()
        inst["setup"] = {"install": "npm install"}
        inst["node_version"] = 22
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node22:latest"

    def test_base_dockerfile_delegates_with_int_version(self) -> None:
        spec = _make_spec()
        with patch(
            "commit0.harness.spec_js._get_node_dockerfile_base",
            return_value="FROM node:20",
        ) as mock_fn:
            result = spec.base_dockerfile
        mock_fn.assert_called_once_with(20)
        assert result == "FROM node:20"

    def test_base_dockerfile_dispatches_node22(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = 22
        spec = _make_spec(inst)
        with patch(
            "commit0.harness.spec_js._get_node_dockerfile_base",
            return_value="FROM node:22",
        ) as mock_fn:
            _ = spec.base_dockerfile
        mock_fn.assert_called_once_with(22)


class TestRepoScriptList:
    def test_starts_with_set_pipefail(self) -> None:
        spec = _make_spec()
        steps = spec.make_repo_script_list()
        assert steps[0] == "set -euo pipefail"

    def test_contains_shallow_clone_depth_50(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_repo_script_list())
        assert "git clone --depth=50" in text

    def test_contains_origin_remote(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_repo_script_list())
        assert "-o origin" in text

    def test_resets_to_base_sha_after_install(self) -> None:
        inst = _make_js_instance(base_commit="c" * 40)
        spec = _make_spec(inst)
        text = "\n".join(spec.make_repo_script_list())
        assert f"git reset --hard {shlex.quote('c' * 40)}" in text

    def test_checks_out_base_commit_which_has_the_lockfile(self) -> None:
        # Setup checks out the BASE (stubbed) commit, not the reference: prepare
        # commits the (possibly generated) lockfile into the base branch, and the
        # original/reference commit may have none — `npm ci` needs the lockfile, so
        # checking out reference would break the frozen install. Both commits are
        # still fetched (reference is used by the eval stage).
        inst = _make_js_instance(
            base_commit="c" * 40, reference_commit="a" * 40
        )
        spec = _make_spec(inst)
        text = "\n".join(spec.make_repo_script_list())
        assert f"git checkout {shlex.quote('c' * 40)}" in text
        assert (
            f"git fetch --depth=1 origin {shlex.quote('a' * 40)} "
            f"{shlex.quote('c' * 40)}"
        ) in text

    def test_falls_back_to_base_when_reference_missing(self) -> None:
        inst = _make_js_instance(base_commit="d" * 40)
        del inst["reference_commit"]
        spec = _make_spec(inst)
        text = "\n".join(spec.make_repo_script_list())
        assert f"git checkout {shlex.quote('d' * 40)}" in text

    def test_contains_install_cmd(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_repo_script_list())
        assert "npm ci" in text

    def test_repo_url_shlex_quoted(self) -> None:
        inst = _make_js_instance(repo="evil;rm -rf /")
        spec = _make_spec(inst)
        text = "\n".join(spec.make_repo_script_list())
        # `repo` is wrapped into a full GitHub clone URL, then shlex-quoted; the whole
        # URL must be quoted so the metacharacters cannot inject into the shell.
        assert shlex.quote("https://github.com/evil;rm -rf /") in text

    def test_base_commit_shlex_quoted(self) -> None:
        inst = _make_js_instance(base_commit="ab;injected")
        spec = _make_spec(inst)
        text = "\n".join(spec.make_repo_script_list())
        assert shlex.quote("ab;injected") in text

    def test_target_dir_shlex_quoted(self) -> None:
        spec = _make_spec(absolute=True)
        text = "\n".join(spec.make_repo_script_list())
        assert shlex.quote("/testbed") in text


class TestEvalScriptList:
    def test_contains_git_apply_allow_empty(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert "git apply --allow-empty" in text

    def test_contains_diff_path(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert "/patch.diff" in text

    def test_contains_preventive_test_revert(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        # Anti-cheat: test files/configs are reverted to the BASE commit via
        # revert_and_clean_lines -> `git checkout <base> -- <pathspec>`.
        assert "git checkout" in text
        for pat in ("**/*.test.js", "**/*.spec.js", "**/__tests__/"):
            assert pat in text

    def test_contains_jest_mocha_vitest_config_revert(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert "jest.config.*" in text
        assert "vitest.config.*" in text
        assert ".mocharc.*" in text

    def test_node_check_batched_via_xargs(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert "xargs -0 -r -n 100 sh -c" in text
        assert "node --check" in text
        assert "cat /tmp/_node_check_max_rc" in text
        assert '[ "$rc" -gt "$cur" ]' in text
        assert "; true' _ || true" in text

    def test_collects_js_files_via_git_ls_files(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert "git ls-files -z '*.js' '*.mjs' '*.cjs'" in text

    def test_resets_to_base_commit(self) -> None:
        spec = _make_spec(_make_js_instance(base_commit="d" * 40))
        text = "\n".join(spec.make_eval_script_list())
        assert f"git reset --hard {shlex.quote('d' * 40)}" in text

    def test_writes_exit_codes(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert "echo $? > install_exit_code.txt" in text
        assert "syntax_exit_code.txt" in text
        assert "echo $? > test_exit_code.txt" in text

    def test_diff_path_shlex_quoted(self) -> None:
        spec = _make_spec()
        text = "\n".join(spec.make_eval_script_list())
        assert shlex.quote("/patch.diff") in text

    def test_hostile_base_sha_rejected_in_eval(self) -> None:
        # eval_hardening now REJECTS any base_commit that is not a bare hex SHA,
        # so an injection payload can never reach the eval shell (stronger than
        # relying on shlex quoting alone).
        inst = _make_js_instance(base_commit="ev;il")
        spec = _make_spec(inst)
        with pytest.raises(ValueError, match="bare hex git SHA"):
            _ = "\n".join(spec.make_eval_script_list())

    def test_valid_base_sha_shlex_quoted_in_eval(self) -> None:
        inst = _make_js_instance(base_commit="c" * 40)
        spec = _make_spec(inst)
        text = "\n".join(spec.make_eval_script_list())
        assert shlex.quote("c" * 40) in text


class TestFrameworkDispatch:
    @pytest.mark.parametrize(
        ("framework", "fragment"),
        [
            ("jest", "npx jest --json --outputFile=test_results.json"),
            ("vitest", "npx vitest run --reporter=json"),
            ("mocha", "npx mocha --reporter json"),
            ("node_test", "node --test --test-reporter=tap"),
        ],
    )
    def test_each_framework(self, framework: str, fragment: str) -> None:
        inst = _make_js_instance()
        inst["setup"]["test_framework"] = framework
        inst["test_framework"] = framework
        spec = _make_spec(inst)
        text = "\n".join(spec.make_eval_script_list())
        assert fragment in text

    def test_unsupported_framework_raises_keyerror(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["test_framework"] = "qunit"
        inst["test_framework"] = "qunit"
        spec = _make_spec(inst)
        with pytest.raises(KeyError):
            spec.make_eval_script_list()


class TestDatasetTestCmdPrefix:
    @pytest.mark.parametrize(
        ("pm", "test_cmd", "framework", "fragment"),
        [
            (
                "pnpm",
                "pnpm exec jest",
                "jest",
                "pnpm exec jest --json --outputFile=test_results.json",
            ),
            (
                "yarn",
                "yarn jest",
                "jest",
                "yarn jest --json --outputFile=test_results.json",
            ),
            (
                "bun",
                "bunx jest",
                "jest",
                "bunx jest --json --outputFile=test_results.json",
            ),
            (
                "pnpm",
                "pnpm exec vitest run",
                "vitest",
                "pnpm exec vitest run --reporter=json",
            ),
            (
                "yarn",
                "yarn vitest run",
                "vitest",
                "yarn vitest run --reporter=json",
            ),
            (
                "pnpm",
                "pnpm exec mocha",
                "mocha",
                "pnpm exec mocha --reporter json",
            ),
        ],
    )
    def test_uses_dataset_test_cmd_prefix(
        self, pm: str, test_cmd: str, framework: str, fragment: str
    ) -> None:
        inst = _make_js_instance()
        inst["setup"]["package_manager"] = pm
        inst["package_manager"] = pm
        inst["setup"]["test_framework"] = framework
        inst["test_framework"] = framework
        inst["test"]["test_cmd"] = test_cmd
        spec = _make_spec(inst)
        text = "\n".join(spec.make_eval_script_list())
        assert fragment in text

    def test_dataset_test_cmd_missing_falls_back_to_pm_default(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["package_manager"] = "pnpm"
        inst["package_manager"] = "pnpm"
        inst["setup"]["test_framework"] = "jest"
        inst["test_framework"] = "jest"
        inst["test"]["test_cmd"] = ""
        spec = _make_spec(inst)
        text = "\n".join(spec.make_eval_script_list())
        assert "pnpm exec jest --json --outputFile=test_results.json" in text

    def test_dataset_test_cmd_framework_mismatch_falls_back(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["package_manager"] = "yarn"
        inst["package_manager"] = "yarn"
        inst["setup"]["test_framework"] = "vitest"
        inst["test_framework"] = "vitest"
        inst["test"]["test_cmd"] = "yarn jest"
        spec = _make_spec(inst)
        text = "\n".join(spec.make_eval_script_list())
        assert "yarn vitest run --reporter=json" in text


class TestPackageManagerDispatch:
    @pytest.mark.parametrize(
        ("pm", "fragment"),
        [
            ("npm", "npm ci"),
            ("pnpm", "pnpm install --frozen-lockfile"),
            ("yarn", "yarn install --frozen-lockfile"),
            ("bun", "bun install --frozen-lockfile"),
        ],
    )
    def test_each_pm(self, pm: str, fragment: str) -> None:
        inst = _make_js_instance()
        inst["setup"]["package_manager"] = pm
        inst["package_manager"] = pm
        spec = _make_spec(inst)
        text = "\n".join(spec.make_repo_script_list())
        assert fragment in text

    def test_all_install_commands_use_ignore_scripts(self) -> None:
        for pm in ("npm", "pnpm", "yarn", "bun"):
            inst = _make_js_instance()
            inst["setup"]["package_manager"] = pm
            inst["package_manager"] = pm
            spec = _make_spec(inst)
            text = "\n".join(spec.make_repo_script_list())
            assert "--ignore-scripts" in text, f"missing --ignore-scripts for {pm}"


class TestSetupScript:
    def test_starts_with_shebang(self) -> None:
        spec = _make_spec()
        assert spec.setup_script.startswith("#!/bin/bash")

    def test_setup_script_uses_set_euxo(self) -> None:
        spec = _make_spec()
        assert "set -euxo pipefail" in spec.setup_script

    def test_eval_script_starts_with_shebang(self) -> None:
        spec = _make_spec()
        assert spec.eval_script.startswith("#!/bin/bash")


class TestJsBaseBranchExact:
    def test_equals_commit0(self) -> None:
        assert JS_BASE_BRANCH == "commit0"


class TestFrameworkMetacharacterRejection:
    @pytest.mark.parametrize(
        "framework",
        [
            "jest; rm -rf /",
            "vitest && evil",
            "mocha`whoami`",
            "$(id)",
            "jest|sh",
            "jest > /etc/passwd",
            "jest\nrm -rf /",
            "jest\x00",
            "qunit",
            "tap",
            "",
            "   ",
            "JEST",
            "jest ",
            " jest",
        ],
    )
    def test_unsupported_or_metachar_framework_raises_keyerror(
        self, framework: str
    ) -> None:
        inst = _make_js_instance()
        inst["setup"]["test_framework"] = framework
        inst["test_framework"] = framework
        spec = _make_spec(inst)
        with pytest.raises(KeyError):
            spec.make_eval_script_list()


class TestNodeVersionCoercionError:
    @pytest.mark.parametrize("raw", ["twenty", "abc", "20.5", "", "v20"])
    def test_non_int_string_node_version_raises_valueerror(self, raw: str) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = raw
        spec = _make_spec(inst)
        with pytest.raises(ValueError):
            _ = spec.base_image_key

    def test_float_node_version_truncates_in_base_image_key(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = 1.5
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node1:latest"

    def test_float_node_version_rejected_by_base_dockerfile(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = 1.5
        spec = _make_spec(inst)
        with pytest.raises(ValueError, match="Unsupported Node version"):
            _ = spec.base_dockerfile

    def test_none_node_version_falls_back_to_default(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = None
        del inst["node_version"]
        spec = _make_spec(inst)
        assert spec.base_image_key == f"commit0.base.node{DEFAULT_NODE_VERSION}:latest"

    def test_bool_true_coerces_to_one_and_breaks_base_dockerfile(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = True
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node1:latest"
        with pytest.raises(ValueError, match="Unsupported Node version"):
            _ = spec.base_dockerfile

    def test_bool_false_coerces_to_zero_and_breaks_base_dockerfile(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = False
        del inst["node_version"]
        spec = _make_spec(inst)
        assert spec.base_image_key == "commit0.base.node0:latest"
        with pytest.raises(ValueError, match="Unsupported Node version"):
            _ = spec.base_dockerfile


class TestRepoInstanceModelBranch:
    def _make_repo_instance(self, **overrides: Any) -> RepoInstance:
        defaults: dict[str, Any] = {
            "instance_id": "commit-0/p-queue",
            "repo": "sindresorhus/p-queue",
            "base_commit": "a" * 40,
            "reference_commit": "b" * 40,
            "setup": {
                "install": "npm install",
                "packages": [],
                "pre_install": [],
                "specification": "",
                "package_manager": "npm",
                "test_framework": "jest",
            },
            "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
            "src_dir": "src",
        }
        defaults.update(overrides)
        return RepoInstance(**defaults)

    def test_get_node_version_with_repo_instance_no_setup_node_falls_back_to_default(
        self,
    ) -> None:
        inst = self._make_repo_instance()
        spec = make_js_spec(inst, absolute=True)
        assert spec.base_image_key == (
            f"commit0.base.node{DEFAULT_NODE_VERSION}:latest"
        )

    def test_get_node_version_with_repo_instance_setup_node_used(self) -> None:
        inst = self._make_repo_instance(
            setup={
                "node_version": 22,
                "install": "npm install",
                "packages": [],
                "pre_install": [],
                "specification": "",
                "package_manager": "npm",
                "test_framework": "jest",
            }
        )
        spec = make_js_spec(inst, absolute=True)
        assert spec.base_image_key == "commit0.base.node22:latest"

    def test_instance_str_with_repo_instance_reads_field(self) -> None:
        inst = self._make_repo_instance(repo="owner/lib")
        spec = make_js_spec(inst, absolute=True)
        text = "\n".join(spec.make_repo_script_list())
        assert shlex.quote("owner/lib") in text

    def test_instance_str_with_repo_instance_missing_key_falls_back_to_default(
        self,
    ) -> None:
        inst = self._make_repo_instance()
        spec = make_js_spec(inst, absolute=True)
        assert spec._instance_str("does_not_exist", default="sentinel") == "sentinel"

    def test_make_js_spec_uses_repo_attribute_on_repo_instance(self) -> None:
        inst = self._make_repo_instance(repo="owner/lib")
        spec = make_js_spec(inst, absolute=True)
        assert spec.repo == "owner/lib"


class TestEvalScriptHasPatchSizeGuard:
    def test_wc_byte_check_present_before_git_apply(self) -> None:
        spec = _make_spec()
        steps = spec.make_eval_script_list()
        joined = "\n".join(steps)
        assert "wc -c < " in joined
        assert str(MAX_PATCH_BYTES) in joined
        wc_idx = joined.index("wc -c < ")
        apply_idx = joined.index("git apply --allow-empty")
        assert wc_idx < apply_idx, (
            "F-012: patch size guard must precede `git apply` so an oversized "
            "diff never reaches the apply step"
        )

    def test_guard_exits_with_distinct_code(self) -> None:
        spec = _make_spec()
        joined = "\n".join(spec.make_eval_script_list())
        assert "exit 4" in joined
        assert "PATCH_TOO_LARGE" in joined

    def test_max_patch_bytes_is_ten_megabytes(self) -> None:
        assert MAX_PATCH_BYTES == 10 * 1024 * 1024


class TestEvalScriptIncludesNodeVersionCheck:
    def test_node_version_check_present_at_eval_time(self) -> None:
        spec = _make_spec()
        joined = "\n".join(spec.make_eval_script_list())
        assert "process.versions.node" in joined
        assert "NODE_VERSION_MISMATCH" in joined

    def test_node_version_check_compares_expected_to_actual(self) -> None:
        inst = _make_js_instance()
        inst["setup"]["node_version"] = 22
        spec = _make_spec(inst)
        joined = "\n".join(spec.make_eval_script_list())
        assert "actual_node_major" in joined
        assert "22" in joined

    def test_node_version_check_precedes_install(self) -> None:
        spec = _make_spec()
        joined = "\n".join(spec.make_eval_script_list())
        check_idx = joined.index("actual_node_major")
        install_idx = joined.index("npm ci")
        assert check_idx < install_idx, (
            "F-020: node version verification must happen before any install "
            "or test step so a mismatched image fails fast"
        )
