"""Tests for spec_c — roundtrip, image-key determinism, shell-quote safety."""

from __future__ import annotations

import hashlib

import pytest

from commit0.harness.spec_c import Commit0CSpec, make_c_spec


def _instance(**overrides) -> dict:
    base: dict = {
        "instance_id": "cJSON_c",
        "repo": "DaveGamble/cJSON",
        "base_commit": "abc1234",
        "reference_commit": "def5678",
        "setup": {"build_system": "cmake", "apt": ["libcmocka-dev"]},
        "test": {"test_cmd": "ctest --test-dir build --output-on-failure"},
        "src_dir": ".",
    }
    base.update(overrides)
    return base


class TestMakeCSpec:
    def test_returns_commit0_c_spec(self):
        spec = make_c_spec(_instance())
        assert isinstance(spec, Commit0CSpec)

    def test_absolute_true_uses_testbed(self):
        spec = make_c_spec(_instance(), absolute=True)
        assert spec.repo_directory == "/testbed"

    def test_absolute_false_uses_relative(self):
        spec = make_c_spec(_instance(), absolute=False)
        assert spec.repo_directory == "testbed"


class TestBaseImageKey:
    def test_base_image_key(self):
        spec = make_c_spec(_instance())
        assert spec.base_image_key == "commit0.base.c:latest"

    def test_base_dockerfile_loads_text(self):
        spec = make_c_spec(_instance())
        df = spec.base_dockerfile
        assert "FROM ubuntu:24.04" in df
        assert "clang-18" in df
        assert "gcc-13" in df
        assert "libcmocka-dev" in df


class TestRepoImageKeyDeterminism:
    def test_same_instance_same_hash(self):
        s1 = make_c_spec(_instance())
        s2 = make_c_spec(_instance())
        assert s1.repo_image_key == s2.repo_image_key

    def test_different_commits_different_hash(self):
        s1 = make_c_spec(_instance(base_commit="aaa"))
        s2 = make_c_spec(_instance(base_commit="bbb"))
        assert s1.repo_image_key != s2.repo_image_key

    def test_image_key_format(self):
        spec = make_c_spec(_instance())
        # commit0.repo.cjson.<22hex>:v0
        assert spec.repo_image_key.startswith("commit0.repo.cjson.")
        assert spec.repo_image_key.endswith(":v0")
        hash_part = spec.repo_image_key.split(".")[-1].split(":")[0]
        assert len(hash_part) == 22

    def test_image_key_lowercased(self):
        spec = make_c_spec(_instance(repo="DaveGamble/Cjson"))
        assert spec.repo_image_key == spec.repo_image_key.lower()


class TestRepoDockerfile:
    def test_apt_packages_quoted(self):
        spec = make_c_spec(
            _instance(setup={"build_system": "cmake", "apt": ["libcmocka-dev"]})
        )
        df = spec.repo_dockerfile
        assert "FROM commit0.base.c:latest" in df
        assert "libcmocka-dev" in df
        assert "WORKDIR /testbed/" in df

    def test_no_apt_skips_apt_layer(self):
        spec = make_c_spec(_instance(setup={"build_system": "cmake", "apt": []}))
        df = spec.repo_dockerfile
        assert "apt-get install" not in df

    def test_apt_injection_attempt_is_quoted(self):
        # malicious apt list element with shell metacharacters
        evil = "libcmocka-dev; rm -rf /"
        spec = make_c_spec(
            _instance(setup={"build_system": "cmake", "apt": [evil]})
        )
        df = spec.repo_dockerfile
        # shlex.quote wraps the whole string in single-quotes
        assert "'libcmocka-dev; rm -rf /'" in df


class TestSetupScript:
    def test_clones_correct_url(self):
        spec = make_c_spec(_instance())
        script = spec.setup_script
        assert "git clone -o origin https://github.com/DaveGamble/cJSON /testbed" in script

    def test_resets_to_base_commit_after_build(self):
        spec = make_c_spec(_instance(base_commit="base123"))
        script = spec.setup_script
        idx_build = script.find("cmake --build build")
        idx_reset = script.find("git reset --hard base123")
        # Reset to base_commit must happen AFTER the build step.
        assert idx_build != -1 and idx_reset != -1
        assert idx_build < idx_reset

    def test_rm_rf_build_after_reset(self):
        """CR-11: stale CMakeCache must not survive reset to base."""
        spec = make_c_spec(_instance())
        script = spec.setup_script
        assert "rm -rf build" in script

    def test_passes_cmake_flags(self):
        spec = make_c_spec(
            _instance(
                setup={
                    "build_system": "cmake",
                    "cmake_flags": "-DENABLE_CJSON_TEST=On",
                }
            )
        )
        assert "-DENABLE_CJSON_TEST=On" in spec.setup_script

    def test_pre_install_list_appended(self):
        spec = make_c_spec(
            _instance(
                setup={
                    "build_system": "cmake",
                    "pre_install": ["apt-get install foo", "make prereq"],
                }
            )
        )
        script = spec.setup_script
        assert "apt-get install foo" in script
        assert "make prereq" in script


class TestEvalScript:
    def test_collects_compile_errors(self):
        spec = make_c_spec(_instance())
        eval_script = spec.eval_script
        assert "2> compile_errors.txt" in eval_script

    def test_early_exit_on_build_failure(self):
        """If build fails, eval must skip tests and not silently proceed."""
        spec = make_c_spec(_instance())
        eval_script = spec.eval_script
        assert "COMPILE_FAILED" in eval_script
        # The early-exit on $BUILD_RC -ne 0 must precede the test_cmd line.
        idx_compile_failed = eval_script.find("echo COMPILE_FAILED")
        idx_ctest = eval_script.find("ctest --test-dir build")
        assert idx_compile_failed != -1 and idx_ctest != -1
        assert idx_compile_failed < idx_ctest

    def test_patch_apply_failed_marker(self):
        spec = make_c_spec(_instance())
        assert "PATCH_APPLY_FAILED" in spec.eval_script

    def test_writes_test_exit_code(self):
        spec = make_c_spec(_instance())
        eval_script = spec.eval_script
        assert "echo $? > test_exit_code.txt" in eval_script

    def test_default_test_cmd_is_ctest_junit(self):
        spec = make_c_spec(_instance(test={}))
        assert "ctest --test-dir build --output-on-failure" in spec.eval_script
        assert "--output-junit /testbed/test_report.xml" in spec.eval_script

    def test_custom_test_cmd_honoured(self):
        custom = "ctest --test-dir build -R parse_only"
        spec = make_c_spec(_instance(test={"test_cmd": custom}))
        assert custom in spec.eval_script

    def test_cmake_flags_in_eval_quoted(self):
        spec = make_c_spec(
            _instance(setup={"build_system": "cmake", "cmake_flags": "-DA='B C'"})
        )
        # quoted via shlex.quote in spec_c
        assert "'-DA=" in spec.eval_script


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
