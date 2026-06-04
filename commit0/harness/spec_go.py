"""Go-specific Spec subclass and factory for commit0 Go integration."""

from dataclasses import dataclass
from pathlib import Path
from typing import Union

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
)
from commit0.harness.constants_go import GoRepoInstance
from commit0.harness.spec import Spec


@dataclass
class Commit0GoSpec(Spec):
    @property
    def base_image_key(self) -> str:
        return "commit0.base.go:latest"

    @property
    def base_dockerfile(self) -> str:
        dockerfile_path = Path(__file__).parent / "dockerfiles" / "Dockerfile.go"
        return dockerfile_path.read_text()

    @property
    def repo_dockerfile(self) -> str:
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
            "WORKDIR /testbed/",
            "",
        ]
        return "\n".join(lines)

    def make_repo_script_list(self) -> list[str]:
        repo = self.instance["repo"]
        env_setup_commit = self.instance["reference_commit"]
        base_commit = self.instance["base_commit"]
        setup = self.instance.get("setup", {}) or {}
        pre_install = setup.get("pre_install")

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

        setup_commands.extend(
            [
                "go mod download 2>/dev/null || true",
                "go build ./... 2>/dev/null || true",
                f"git reset --hard {base_commit}",
            ]
        )

        return setup_commands

    def make_eval_script_list(self) -> list[str]:
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        test_cmd = self.instance["test"].get("test_cmd", "go test -json -count=1 ./...")
        base_commit = self.instance["base_commit"]
        revert_test_paths = (
            f"git checkout {base_commit} -- "
            f"'*_test.go' '**/*_test.go' "
            f"testdata/ '**/testdata/' "
            f"'**/test/' '**/tests/' "
            f"Makefile '**/Makefile' go.mod go.sum "
            f"sitecustomize.py usercustomize.py .env .gitmodules .gitattributes "
            f"2>/dev/null || true"
        )

        eval_script_list = [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"if [ -s {diff_path} ]; then",
            f"  git apply -v {diff_path}",
            "  if [ $? -ne 0 ]; then",
            '    echo \'{"Action":"fail","Package":"PATCH_APPLY_FAILED","Output":"git apply failed"}\'  > test_output.json',
            '    echo "git apply failed" > test_stderr.txt',
            "    echo 1 > go_test_exit_code.txt",
            f"    exit 0",
            "  fi",
            "fi",
            revert_test_paths,
            "find . -name '*.go' -not -name '*_test.go' -not -path '*/vendor/*' | xargs goimports -w",
            "git status",
            f"{test_cmd} > test_output.json 2> test_stderr.txt",
            "echo $? > go_test_exit_code.txt",
        ]
        return eval_script_list


def make_go_spec(
    instance: Union[GoRepoInstance, RepoInstance, dict],
    dataset_type: str = "commit0",
    absolute: bool = True,
) -> Commit0GoSpec:
    if isinstance(instance, dict):
        repo = instance["repo"]
    else:
        repo = instance.repo

    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR

    return Commit0GoSpec(
        absolute=absolute,
        repo=repo,
        repo_directory=repo_directory,
        instance=instance,
    )


__all__ = [
    "Commit0GoSpec",
    "make_go_spec",
]
