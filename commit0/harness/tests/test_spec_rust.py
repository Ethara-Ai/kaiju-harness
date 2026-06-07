"""Exhaustive unit tests for commit0.harness.spec_rust."""

from unittest.mock import patch

MODULE = "commit0.harness.spec_rust"

from commit0.harness.spec_rust import (
    RustSpec,
    make_rust_spec,
    get_rust_specs_from_dataset,
)
from commit0.harness.spec import Spec
from commit0.harness.constants import ABSOLUTE_REPO_DIR, RELATIVE_REPO_DIR


def _make_instance(**overrides):
    d = {
        "instance_id": "test-1",
        "repo": "Rust-commit0/taffy",
        "base_commit": "abc123",
        "reference_commit": "def456",
        "setup": {"pre_install": ["apt-get update"], "install": "cargo build"},
        "test": {"test_cmd": "cargo nextest run"},
        "src_dir": "src",
    }
    d.update(overrides)
    return d


def _make_spec(instance=None, absolute=True, repo_directory=None):
    inst = instance or _make_instance()
    rd = repo_directory or (ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR)
    return RustSpec(
        repo=inst["instance_id"],
        repo_directory=rd,
        instance=inst,
        absolute=absolute,
    )


# ===== RustSpec class =====
class TestRustSpecInheritance:
    def test_inherits_spec(self):
        assert issubclass(RustSpec, Spec)

    def test_is_dataclass(self):
        assert hasattr(RustSpec, "__dataclass_fields__")


class TestRustSpecBaseImageKey:
    def test_value(self):
        spec = _make_spec()
        assert spec.base_image_key == "commit0.base.rust:latest"

    def test_always_same(self):
        spec1 = _make_spec()
        spec2 = _make_spec(instance=_make_instance(repo="Rust-commit0/bon"))
        assert spec1.base_image_key == spec2.base_image_key

    def test_is_string(self):
        assert isinstance(_make_spec().base_image_key, str)


class TestRustSpecBaseDockerfile:
    @patch(f"{MODULE}.get_dockerfile_base_rust", return_value="FROM rust:stable\n")
    def test_calls_get_dockerfile_base_rust(self, mock_fn):
        spec = _make_spec()
        result = spec.base_dockerfile
        mock_fn.assert_called_once()
        assert result == "FROM rust:stable\n"


class TestRustSpecRepoDockerfile:
    @patch(
        f"{MODULE}.get_dockerfile_repo_rust",
        return_value="FROM base\nRUN cargo build\n",
    )
    def test_calls_get_dockerfile_repo_rust(self, mock_fn):
        spec = _make_spec()
        result = spec.repo_dockerfile
        mock_fn.assert_called_once()
        assert "FROM base" in result

    @patch(f"{MODULE}.get_dockerfile_repo_rust", return_value="FROM base\n")
    def test_passes_base_image_key(self, mock_fn):
        spec = _make_spec()
        spec.repo_dockerfile
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["base_image"] == "commit0.base.rust:latest"

    @patch(f"{MODULE}.get_dockerfile_repo_rust", return_value="FROM base\n")
    def test_passes_pre_install(self, mock_fn):
        spec = _make_spec()
        spec.repo_dockerfile
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["pre_install"] == ["apt-get update"]

    @patch(f"{MODULE}.get_dockerfile_repo_rust", return_value="FROM base\n")
    def test_passes_install_cmd(self, mock_fn):
        spec = _make_spec()
        spec.repo_dockerfile
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["install_cmd"] == "cargo build"

    @patch(f"{MODULE}.get_dockerfile_repo_rust", return_value="FROM base\n")
    def test_no_setup_passes_none(self, mock_fn):
        spec = _make_spec(instance=_make_instance(setup={}))
        spec.repo_dockerfile
        call_kwargs = mock_fn.call_args[1]
        assert call_kwargs["pre_install"] is None
        assert call_kwargs["install_cmd"] is None


class TestRustSpecMakeRepoScriptList:
    def test_returns_list(self):
        spec = _make_spec()
        result = spec.make_repo_script_list()
        assert isinstance(result, list)

    def test_contains_git_clone(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("git clone" in s for s in scripts)

    def test_clone_uses_repo(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        clone_line = [s for s in scripts if "git clone" in s][0]
        assert "Rust-commit0/taffy" in clone_line

    def test_contains_git_fetch(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("git fetch" in s for s in scripts)

    def test_fetch_includes_both_commits(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        fetch_line = [s for s in scripts if "git fetch" in s][0]
        assert "def456" in fetch_line
        assert "abc123" in fetch_line

    def test_contains_git_reset(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        reset_lines = [s for s in scripts if "git reset" in s]
        assert len(reset_lines) >= 1

    def test_contains_cargo_fetch(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("cargo fetch" in s for s in scripts)

    def test_contains_submodule_update(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("submodule" in s for s in scripts)

    def test_contains_chmod(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("chmod" in s for s in scripts)

    def test_uses_repo_directory(self):
        spec = _make_spec(absolute=True)
        scripts = spec.make_repo_script_list()
        assert any(ABSOLUTE_REPO_DIR in s for s in scripts)

    def test_relative_repo_directory(self):
        spec = _make_spec(absolute=False)
        scripts = spec.make_repo_script_list()
        assert any(RELATIVE_REPO_DIR in s for s in scripts)

    def test_removes_origin(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("remote remove origin" in s for s in scripts)


class TestRustSpecMakeEvalScriptList:
    def test_returns_list(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert isinstance(scripts, list)

    def test_contains_cd(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("cd " in s for s in scripts)

    def test_contains_git_reset(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("git reset" in s for s in scripts)

    def test_reset_to_base_commit(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        reset_line = [s for s in scripts if "git reset" in s][0]
        assert "abc123" in reset_line

    def test_contains_git_apply(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("git apply" in s for s in scripts)

    def test_absolute_uses_absolute_diff_path(self):
        spec = _make_spec(absolute=True)
        scripts = spec.make_eval_script_list()
        apply_line = [s for s in scripts if "git apply" in s][0]
        assert "/patch.diff" in apply_line

    def test_relative_uses_relative_diff_path(self):
        spec = _make_spec(absolute=False)
        scripts = spec.make_eval_script_list()
        apply_line = [s for s in scripts if "git apply" in s][0]
        assert "../patch.diff" in apply_line

    def test_uses_custom_test_cmd(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        test_line = [
            s
            for s in scripts
            if "test" in s.lower() and "git" not in s and "echo" not in s
        ]
        assert any("cargo nextest run" in s for s in test_line)

    def test_default_test_cmd_cargo_test(self):
        spec = _make_spec(instance=_make_instance(test={}))
        scripts = spec.make_eval_script_list()
        test_lines = [s for s in scripts if s.strip().startswith("cargo test")]
        assert len(test_lines) >= 1

    def test_test_ids_placeholder(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("{test_ids}" in s for s in scripts)

    def test_captures_exit_code(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("cargo_test_exit_code.txt" in s for s in scripts)

    def test_captures_test_output(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("test_output.txt" in s for s in scripts)

    def test_contains_git_status(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("git status" in s for s in scripts)


class TestRustSpecSetupScript:
    def test_starts_with_shebang(self):
        spec = _make_spec()
        assert spec.setup_script.startswith("#!/bin/bash")

    def test_contains_pipefail(self):
        spec = _make_spec()
        assert "pipefail" in spec.setup_script


class TestRustSpecEvalScript:
    def test_starts_with_shebang(self):
        spec = _make_spec()
        assert spec.eval_script.startswith("#!/bin/bash")


# ===== make_rust_spec =====
class TestMakeRustSpec:
    def test_returns_rust_spec(self):
        inst = _make_instance()
        spec = make_rust_spec(inst, absolute=True)
        assert isinstance(spec, RustSpec)

    def test_absolute_uses_absolute_dir(self):
        inst = _make_instance()
        spec = make_rust_spec(inst, absolute=True)
        assert spec.repo_directory == ABSOLUTE_REPO_DIR

    def test_relative_uses_relative_dir(self):
        inst = _make_instance()
        spec = make_rust_spec(inst, absolute=False)
        assert spec.repo_directory == RELATIVE_REPO_DIR

    def test_repo_is_instance_id(self):
        inst = _make_instance(instance_id="my-test-id")
        spec = make_rust_spec(inst, absolute=True)
        assert spec.repo == "my-test-id"

    def test_instance_preserved(self):
        inst = _make_instance()
        spec = make_rust_spec(inst, absolute=True)
        assert spec.instance["repo"] == "Rust-commit0/taffy"


# ===== get_rust_specs_from_dataset =====
class TestGetRustSpecsFromDataset:
    def test_converts_dicts_to_specs(self):
        dataset = [_make_instance(), _make_instance(instance_id="t2")]
        specs = get_rust_specs_from_dataset(dataset, absolute=True)
        assert all(isinstance(s, RustSpec) for s in specs)
        assert len(specs) == 2

    def test_passthrough_if_already_specs(self):
        spec1 = _make_spec()
        spec2 = _make_spec(instance=_make_instance(instance_id="t2"))
        result = get_rust_specs_from_dataset([spec1, spec2], absolute=True)
        assert result[0] is spec1
        assert result[1] is spec2

    def test_empty_dataset(self):
        result = get_rust_specs_from_dataset([], absolute=True)
        assert result == []

    def test_single_element(self):
        result = get_rust_specs_from_dataset([_make_instance()], absolute=True)
        assert len(result) == 1

    def test_absolute_flag_propagated(self):
        result = get_rust_specs_from_dataset([_make_instance()], absolute=False)
        assert result[0].repo_directory == RELATIVE_REPO_DIR
        assert result[0].absolute is False


# ===== __all__ exports =====
class TestModuleExports:
    def test_exports(self):
        import commit0.harness.spec_rust as mod

        assert set(mod.__all__) == {
            "RustSpec",
            "make_rust_spec",
            "get_rust_specs_from_dataset",
        }


class TestRustSpecEvalScriptEdge:
    def test_default_cargo_test_when_no_test_key(self):
        inst = _make_instance()
        del inst["test"]
        spec = _make_spec(instance=inst)
        scripts = spec.make_eval_script_list()
        assert any("cargo test" in s for s in scripts)

    def test_default_cargo_test_when_test_not_dict(self):
        inst = _make_instance()
        inst["test"] = "run_all"
        spec = _make_spec(instance=inst)
        scripts = spec.make_eval_script_list()
        assert any("cargo test" in s for s in scripts)

    def test_default_cargo_test_when_test_dict_no_cmd(self):
        inst = _make_instance()
        inst["test"] = {"timeout": 300}
        spec = _make_spec(instance=inst)
        scripts = spec.make_eval_script_list()
        assert any("cargo test" in s for s in scripts)

    def test_custom_test_cmd_used(self):
        inst = _make_instance()
        inst["test"] = {"test_cmd": "cargo nextest run"}
        spec = _make_spec(instance=inst)
        scripts = spec.make_eval_script_list()
        assert any("cargo nextest run" in s for s in scripts)

    def test_eval_script_contains_git_reset(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("git reset --hard" in s for s in scripts)

    def test_eval_script_contains_git_apply(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("git apply" in s for s in scripts)

    def test_eval_script_contains_test_output_redirect(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("test_output.txt" in s for s in scripts)

    def test_eval_script_contains_exit_code(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("cargo_test_exit_code.txt" in s for s in scripts)

    def test_eval_script_absolute_uses_slash_patch(self):
        spec = _make_spec(absolute=True)
        scripts = spec.make_eval_script_list()
        assert any("/patch.diff" in s for s in scripts)

    def test_eval_script_relative_uses_dotdot_patch(self):
        spec = _make_spec(absolute=False, repo_directory=RELATIVE_REPO_DIR)
        scripts = spec.make_eval_script_list()
        assert any("../patch.diff" in s for s in scripts)

    def test_eval_script_contains_test_ids_placeholder(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert any("{test_ids}" in s for s in scripts)

    def test_eval_script_starts_with_cd(self):
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        assert scripts[0].startswith("cd ")


class TestRustSpecRepoScriptEdge:
    def test_repo_script_contains_git_clone(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("git clone" in s for s in scripts)

    def test_repo_script_contains_chmod(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("chmod -R 777" in s for s in scripts)

    def test_repo_script_contains_git_fetch(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("git fetch" in s for s in scripts)

    def test_repo_script_contains_submodule_update(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("submodule update" in s for s in scripts)

    def test_repo_script_contains_cargo_fetch(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("cargo fetch" in s for s in scripts)

    def test_repo_script_removes_origin(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("remote remove origin" in s for s in scripts)

    def test_repo_script_resets_to_base_commit(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("abc123" in s and "reset --hard" in s for s in scripts)

    def test_repo_script_clone_uses_repo_name(self):
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        assert any("Rust-commit0/taffy" in s for s in scripts)


class TestMakeRustSpecEdge:
    def test_returns_rust_spec_type(self):
        result = make_rust_spec(_make_instance(), absolute=True)
        assert isinstance(result, RustSpec)

    def test_repo_field_is_instance_id(self):
        result = make_rust_spec(_make_instance(instance_id="my-id"), absolute=True)
        assert result.repo == "my-id"

    def test_absolute_true_uses_absolute_dir(self):
        result = make_rust_spec(_make_instance(), absolute=True)
        assert result.repo_directory == ABSOLUTE_REPO_DIR

    def test_absolute_false_uses_relative_dir(self):
        result = make_rust_spec(_make_instance(), absolute=False)
        assert result.repo_directory == RELATIVE_REPO_DIR

    def test_instance_preserved(self):
        inst = _make_instance()
        result = make_rust_spec(inst, absolute=True)
        assert result.instance["repo"] == inst["repo"]


class TestGetRustSpecsEdge:
    def test_already_rust_specs_returned_as_is(self):
        spec = _make_spec()
        result = get_rust_specs_from_dataset([spec], absolute=True)
        assert result[0] is spec

    def test_mixed_not_detected_as_specs(self):
        result = get_rust_specs_from_dataset([_make_instance()], absolute=True)
        assert isinstance(result[0], RustSpec)

    def test_preserves_order(self):
        insts = [
            _make_instance(instance_id="z"),
            _make_instance(instance_id="a"),
        ]
        result = get_rust_specs_from_dataset(insts, absolute=True)
        assert result[0].repo == "z"
        assert result[1].repo == "a"

    def test_large_dataset(self):
        insts = [_make_instance(instance_id=f"id-{i}") for i in range(50)]
        result = get_rust_specs_from_dataset(insts, absolute=True)
        assert len(result) == 50
