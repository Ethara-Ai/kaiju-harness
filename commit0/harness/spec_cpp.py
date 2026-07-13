from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Union, cast

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.spec import Spec
from commit0.harness.eval_hardening import (
    revert_and_clean_lines,
    guard_snapshot_lines,
    guard_heal_lines,
)
from commit0.harness.dockerfiles.__init__cpp import (
    get_dockerfile_base_cpp,
    get_dockerfile_repo_cpp,
)

logger = logging.getLogger(__name__)


BUILD_CMD_MAP = {
    "cmake": "cmake --build build --parallel $(nproc)",
    "meson": "ninja -C builddir",
    "autotools": "make -j$(nproc)",
    "make": "make -j$(nproc)",
}

CONFIGURE_CMD_MAP = {
    "cmake": "cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    "meson": "meson setup builddir",
    "autotools": "./configure",
    "make": "",
}


@dataclass
class CppSpec(Spec):
    @property
    def base_image_key(self) -> str:
        return "commit0.base.cpp:latest"

    @property
    def base_dockerfile(self) -> str:
        return get_dockerfile_base_cpp()

    @property
    def repo_dockerfile(self) -> str:
        specs = self._get_setup_dict()
        return get_dockerfile_repo_cpp(
            base_image=self.base_image_key,
            pre_install=specs.get("pre_install"),
            install_cmd=specs.get("install"),
            packages=specs.get("packages"),
        )

    def _get_build_system(self) -> str:
        if isinstance(self.instance, dict):
            return self.instance.get("build_system", "cmake")
        return getattr(self.instance, "build_system", "cmake")

    def make_repo_script_list(self) -> list[str]:
        repo = self.instance["repo"]
        env_setup_commit = self.instance["reference_commit"]
        base_commit = self.instance["base_commit"]
        build_system = self._get_build_system()

        configure_cmd = CONFIGURE_CMD_MAP.get(build_system, "")
        build_cmd = BUILD_CMD_MAP.get(build_system, "make -j$(nproc)")

        scripts = [
            f"git clone --depth 1 -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch --depth 1 origin {env_setup_commit} {base_commit}",
            f"git reset --hard {env_setup_commit}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
            f"git reset --hard {base_commit}",
        ]

        if configure_cmd:
            scripts.append(f"{configure_cmd} 2>/dev/null || true")
        scripts.append(f"{build_cmd} 2>/dev/null || true")

        return scripts

    def make_eval_script_list(self) -> list[str]:
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        test_cmd = "ctest --test-dir build --verbose"
        if isinstance(self.instance, dict) and "test" in self.instance:
            test_info = self.instance["test"]
            if isinstance(test_info, dict) and "test_cmd" in test_info:
                test_cmd = test_info["test_cmd"]

        build_system = self._get_build_system()
        build_cmd = BUILD_CMD_MAP.get(build_system, "make -j$(nproc)")
        base_commit = self.instance["base_commit"]
        # Per-pathspec revert + delete model-added build/test files. C++ counts
        # from raw stdout (GTest/Catch2/...), so evaluate_cpp also gets a
        # stdout-injection guard; here we make sure the build/test files and
        # framework dirs can't be swapped for a fake harness. See eval_hardening.
        revert_lines = revert_and_clean_lines(
            base_commit,
            revert_targets=[
                "tests/", "test/",
                "test_*.cpp", "*_test.cpp", "test_*.cc", "*_test.cc", "test_*.h",
                "googletest/", "gtest/", "catch2/", "Catch2/", "doctest/",
                "CMakeLists.txt", "Makefile", "GNUmakefile", "meson.build",
                "meson_options.txt", "CMakePresets.json",
                "sitecustomize.py", "usercustomize.py",
                ".env", ".gitmodules", ".gitattributes",
            ],
            delete_added_globs=[
                "test_*.cpp", "**/test_*.cpp", "*_test.cpp", "**/*_test.cpp",
                "test_*.cc", "**/test_*.cc", "*_test.cc", "**/*_test.cc",
                "CMakeLists.txt", "**/CMakeLists.txt",
                "Makefile", "**/Makefile", "GNUmakefile", "**/GNUmakefile",
                "meson.build", "**/meson.build", "CMakePresets.json",
            ],
        )

        return [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            # Try exact apply, then --recount (tolerates off-by-N hunk headers). A
            # genuine apply failure must NOT be swallowed by `|| true`: that would
            # run the build/tests on an unpatched tree and report the resulting 0/N
            # as if it were a real model score. Instead write a sentinel that
            # evaluate_cpp.py maps to PATCH_APPLY_FAILED, and stop.
            f"if [ -s {diff_path} ]; then",
            f"  git apply -v {diff_path} || git apply --recount -v {diff_path} || {{ "
            'echo PATCH_APPLY_FAILED > test_output.txt; '
            "echo 1 > test_exit_code.txt; exit 0; }",
            "fi",
            # Layer-2 guard: snapshot applied tree; heal harness-corrupted files
            # before the build so a compiling submission isn't a false COMPILE_FAILED.
            *guard_snapshot_lines(),
            *revert_lines,
            "git status",
            *guard_heal_lines(),
            f"{{ {build_cmd}; {test_cmd} {{test_ids}}; }} > test_output.txt 2>&1",
            "echo $? > test_exit_code.txt",
        ]


def make_cpp_spec(
    instance: Union[RepoInstance, dict],
    absolute: bool,
) -> CppSpec:
    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR
    return CppSpec(
        repo=instance["instance_id"],
        repo_directory=repo_directory,
        instance=cast(Union[RepoInstance, SimpleInstance], instance),
        absolute=absolute,
    )


def get_cpp_specs_from_dataset(
    dataset: Union[list[Union[RepoInstance, dict]], list[CppSpec]],
    absolute: bool,
) -> list[CppSpec]:
    if dataset and isinstance(dataset[0], CppSpec):
        return cast(list[CppSpec], dataset)
    return [
        make_cpp_spec(cast(Union[RepoInstance, dict], inst), absolute)
        for inst in dataset
    ]


__all__ = [
    "CppSpec",
    "make_cpp_spec",
    "get_cpp_specs_from_dataset",
]
