"""C-specific Spec subclass and factory for commit0 C integration.

Co-located with spec.py (never modifies the base). MVP scope: CMake-only build
system. All dataset strings passed to shell commands are quoted with
``shlex.quote`` to prevent injection through dataset payloads.
"""

from dataclasses import dataclass
import shlex
from pathlib import Path
from typing import Union

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
)
from commit0.harness.constants_c import CRepoInstance
from commit0.harness.spec import Spec


def _q(s: str) -> str:
    return shlex.quote(s)


@dataclass
class Commit0CSpec(Spec):
    @property
    def base_image_key(self) -> str:
        return "commit0.base.c:latest"

    @property
    def base_dockerfile(self) -> str:
        dockerfile_path = Path(__file__).parent / "dockerfiles" / "Dockerfile.c"
        return dockerfile_path.read_text()

    @property
    def repo_dockerfile(self) -> str:
        setup = self._get_setup_dict()
        apt = setup.get("apt") or []

        lines = [
            f"FROM {self.base_image_key}",
            "",
            'ARG http_proxy=""',
            'ARG https_proxy=""',
            'ARG HTTP_PROXY=""',
            'ARG HTTPS_PROXY=""',
            'ARG no_proxy="localhost,127.0.0.1,::1"',
            'ARG NO_PROXY="localhost,127.0.0.1,::1"',
            "",
            "COPY ./setup.sh /root/",
            "RUN chmod +x /root/setup.sh && /bin/bash /root/setup.sh",
            "",
        ]
        if apt:
            joined = " ".join(_q(p) for p in apt)
            lines.append(
                f"RUN apt-get update && apt-get install -y --no-install-recommends {joined} "
                "&& rm -rf /var/lib/apt/lists/*"
            )
            lines.append("")
        lines.extend(
            [
                "WORKDIR /testbed/",
                "",
            ]
        )
        return "\n".join(lines)

    def make_repo_script_list(self) -> list[str]:
        repo = self.instance["repo"]
        env_setup_commit = self.instance["reference_commit"]
        base_commit = self.instance["base_commit"]
        setup = self._get_setup_dict()
        pre_install = setup.get("pre_install")
        cmake_flags = setup.get("cmake_flags", "")
        quoted_cmake_flags = " ".join(_q(tok) for tok in cmake_flags.split())

        setup_commands = [
            f"git clone -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch origin {env_setup_commit} {base_commit}",
            f"git reset --hard {env_setup_commit}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
        ]

        if pre_install:
            if isinstance(pre_install, list):
                for cmd in pre_install:
                    setup_commands.append(cmd)
            else:
                setup_commands.append(pre_install)

        # MVP: CMake only. Generates compile_commands.json for the stubber.
        setup_commands.append(
            "cmake -S . -B build -G Ninja -DBUILD_TESTING=ON "
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON "
            f"-DCMAKE_BUILD_TYPE=Debug {quoted_cmake_flags} && cmake --build build -j"
        )
        # Clean build artifacts before resetting to base_commit so a stale
        # CMakeCache.txt from reference_commit cannot bleed into eval.
        setup_commands.append(f"git reset --hard {base_commit}")
        setup_commands.append("rm -rf build")
        return setup_commands

    def make_eval_script_list(self) -> list[str]:
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        test = self.instance.get("test", {}) if isinstance(self.instance, dict) else self.instance["test"]
        test = test or {}
        setup = self._get_setup_dict()
        cmake_flags = setup.get("cmake_flags", "")
        quoted_cmake_flags = " ".join(_q(tok) for tok in cmake_flags.split())
        test_cmd = test.get("test_cmd") or (
            "ctest --test-dir build --output-on-failure "
            "--output-junit /testbed/test_report.xml"
        )
        base_commit = self.instance["base_commit"]
        revert_test_paths = (
            f"git checkout {base_commit} -- "
            f"tests/ '**/tests/' test/ '**/test/' "
            f"'test_*.c' '**/test_*.c' '*_test.c' '**/*_test.c' "
            f"'test_*.h' '**/test_*.h' "
            f"CMakeLists.txt '**/CMakeLists.txt' Makefile '**/Makefile' meson.build '**/meson.build' "
            f"2>/dev/null || true"
        )

        return [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"if [ -s {diff_path} ]; then",
            f"  git apply -v {diff_path} || {{ "
            "echo PATCH_APPLY_FAILED > compile_errors.txt; "
            "echo 1 > test_exit_code.txt; exit 0; }",
            "fi",
            revert_test_paths,
            "git status",
            "cmake -S . -B build -G Ninja -DBUILD_TESTING=ON "
            f"-DCMAKE_BUILD_TYPE=Debug {quoted_cmake_flags} "
            "&& cmake --build build -j 2> compile_errors.txt",
            "BUILD_RC=$?",
            "if [ $BUILD_RC -ne 0 ]; then",
            "  echo COMPILE_FAILED >> compile_errors.txt",
            "  echo 0 > test_exit_code.txt",
            "  exit 0",
            "fi",
            f"{test_cmd} > test_output.txt 2>&1",
            "echo $? > test_exit_code.txt",
        ]


def make_c_spec(
    instance: Union[CRepoInstance, RepoInstance, dict],
    dataset_type: str = "commit0",
    absolute: bool = True,
) -> Commit0CSpec:
    if isinstance(instance, dict):
        repo = instance["repo"]
    else:
        repo = instance.repo

    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR

    return Commit0CSpec(
        absolute=absolute,
        repo=repo,
        repo_directory=repo_directory,
        instance=instance,
    )


__all__ = [
    "Commit0CSpec",
    "make_c_spec",
]
