from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Union, cast

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.spec import Spec
from commit0.harness.dockerfiles.__init__rust import (
    get_dockerfile_base_rust,
    get_dockerfile_repo_rust,
)

logger = logging.getLogger(__name__)


@dataclass
class RustSpec(Spec):
    @property
    def base_image_key(self) -> str:
        return "commit0.base.rust:latest"

    @property
    def base_dockerfile(self) -> str:
        return get_dockerfile_base_rust()

    @property
    def repo_dockerfile(self) -> str:
        specs = self._get_setup_dict()
        return get_dockerfile_repo_rust(
            base_image=self.base_image_key,
            pre_install=specs.get("pre_install"),
            install_cmd=specs.get("install"),
        )

    def make_repo_script_list(self) -> list[str]:
        repo = self.instance["repo"]
        env_setup_commit = self.instance["reference_commit"]
        base_commit = self.instance["base_commit"]

        return [
            f"git clone --depth 1 -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch --depth 1 origin {env_setup_commit} {base_commit}",
            f"git reset --hard {env_setup_commit}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
            f"git reset --hard {base_commit}",
            "cargo fetch 2>/dev/null || true",
        ]

    def make_eval_script_list(self) -> list[str]:
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        test_cmd = "cargo test"
        if isinstance(self.instance, dict) and "test" in self.instance:
            test_info = self.instance["test"]
            if isinstance(test_info, dict) and "test_cmd" in test_info:
                test_cmd = test_info["test_cmd"]
        base_commit = self.instance["base_commit"]
        revert_test_paths = (
            f"git checkout {base_commit} -- "
            f"tests/ '**/tests/' benches/ '**/benches/' "
            f"Cargo.toml '**/Cargo.toml' Cargo.lock '**/Cargo.lock' "
            f"sitecustomize.py usercustomize.py .env .gitmodules .gitattributes "
            f"2>/dev/null || true"
        )

        return [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"if [ -s {diff_path} ]; then",
            f"  git apply -v {diff_path}",
            "  if [ $? -ne 0 ]; then",
            '    echo "PATCH APPLY FAILED" > test_output.txt',
            "    echo 1 > cargo_test_exit_code.txt",
            "    exit 0",
            "  fi",
            "fi",
            revert_test_paths,
            "git status",
            f"{test_cmd} {{test_ids}} > test_output.txt 2>&1",
            "echo $? > cargo_test_exit_code.txt",
        ]


def make_rust_spec(
    instance: Union[RepoInstance, dict],
    absolute: bool,
) -> RustSpec:
    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR
    return RustSpec(
        repo=instance["instance_id"],
        repo_directory=repo_directory,
        instance=cast(Union[RepoInstance, SimpleInstance], instance),
        absolute=absolute,
    )


def get_rust_specs_from_dataset(
    dataset: Union[list[Union[RepoInstance, dict]], list[RustSpec]],
    absolute: bool,
) -> list[RustSpec]:
    if dataset and isinstance(dataset[0], RustSpec):
        return cast(list[RustSpec], dataset)
    return [
        make_rust_spec(cast(Union[RepoInstance, dict], inst), absolute)
        for inst in dataset
    ]


__all__ = [
    "RustSpec",
    "make_rust_spec",
    "get_rust_specs_from_dataset",
]
