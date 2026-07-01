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
        revert_test_paths = (
            f"git checkout {base_commit} -- "
            f"tests/ '**/tests/' test/ '**/test/' "
            f"'test_*.cpp' '**/test_*.cpp' '*_test.cpp' '**/*_test.cpp' "
            f"'test_*.cc' '**/test_*.cc' '*_test.cc' '**/*_test.cc' "
            f"'test_*.h' '**/test_*.h' "
            f"googletest/ gtest/ "
            f"CMakeLists.txt '**/CMakeLists.txt' Makefile '**/Makefile' meson.build '**/meson.build' "
            f"sitecustomize.py usercustomize.py .env .gitmodules .gitattributes "
            f"2>/dev/null || true"
        )

        return [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"git apply -v {diff_path} || git apply {diff_path} || true",
            revert_test_paths,
            "git status",
            f"{{{{ {build_cmd} && {test_cmd} {{test_ids}}; }}}} > test_output.txt 2>&1",
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
