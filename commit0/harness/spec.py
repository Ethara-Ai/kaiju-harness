import functools
import hashlib
import logging
import os
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Union, cast, Optional

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    DEFAULT_PYTHON_VERSION,
    RELATIVE_REPO_DIR,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.dockerfiles import (
    get_dockerfile_base,
    get_dockerfile_repo,
)

logger = logging.getLogger(__name__)


@dataclass
class Spec(ABC):
    """A dataclass that represents a test specification for a single instance of SWE-bench."""

    absolute: bool
    repo: str
    # repo dir on docker
    repo_directory: str
    instance: Union[RepoInstance, SimpleInstance]

    @functools.cached_property
    def setup_script(self) -> str:
        repo_script_list = self.make_repo_script_list()
        return (
            "\n".join(["#!/bin/bash", "set -euxo pipefail"] + repo_script_list) + "\n"
        )

    @functools.cached_property
    def eval_script(self) -> str:
        eval_script_list = self.make_eval_script_list()
        return "\n".join(["#!/bin/bash", "set -uxo pipefail"] + eval_script_list) + "\n"
        # Don't exit early because we need to revert tests at the end

    @property
    def base_image_key(self) -> str:
        python_version = self._get_python_version()
        return f"commit0.base.python{python_version}:latest"

    def _get_python_version(self) -> str:
        setup = self._get_setup_dict()
        if "python" in setup and setup["python"]:
            return str(setup["python"])
        logger.warning(
            "Instance %s has no setup.python; falling back to %s. "
            "This indicates the entry was not produced by tools/prepare_repo.py "
            "or version detection failed. See tools/python_version.py.",
            getattr(self.instance, "instance_id", "<unknown>"),
            DEFAULT_PYTHON_VERSION,
        )
        return DEFAULT_PYTHON_VERSION

    def _get_setup_dict(self) -> dict:
        """Extract setup dict from instance regardless of whether it's a Pydantic model or plain dict."""
        if isinstance(self.instance, dict) and "setup" in self.instance:
            return self.instance["setup"] or {}
        if isinstance(self.instance, RepoInstance):
            return self.instance.setup or {}
        return {}

    @property
    def repo_image_key(self) -> str:
        """The key for the environment image is based on the hash of the environment script list.
        If the environment script list changes, the image will be rebuilt automatically.

        Note that old images are not automatically deleted, so consider cleaning up old images periodically.
        """
        hash_object = hashlib.sha256()
        hash_object.update(str(self.setup_script).encode("utf-8"))
        hash_value = hash_object.hexdigest()
        val = hash_value[:22]  # 22 characters is still very likely to be unique
        repo = self.repo.split("/")[-1].split("__")[-1].split("-")[0]
        # this is the image name created locally
        # once this image created, it will be tagged with repo_image_tag
        return f"commit0.repo.{repo}.{val}:v0".lower()

    @property
    def repo_image_tag(self) -> str:
        """Repo image tag that will be used throughout."""
        repo = self.repo.split("/")[-1]
        tag = f"wentingzhao/{repo}:v0".lower()
        if "__" in repo:  # this is a swebench instance
            repo = repo.split("__")[-1].split("-")[0]
            hash_object = hashlib.sha256()
            hash_object.update(str(self.setup_script).encode("utf-8"))
            hash_value = hash_object.hexdigest()
            val = hash_value[:22]  # 22 characters is still very likely to be unique
            tag = f"wentingzhao/{repo}.{val}:v0".lower()
        return tag

    def get_container_name(self, run_id: Optional[str] = None) -> str:
        repo = self.repo.split("/")[-1]
        if not run_id:
            return f"commit0.eval.{repo}"
        return f"commit0.eval.{repo}.{run_id}".lower()

    @property
    def base_dockerfile(self) -> str:
        return get_dockerfile_base(self._get_python_version())

    @property
    def repo_dockerfile(self) -> str:
        specs = self._get_setup_dict()
        return get_dockerfile_repo(
            base_image=self.base_image_key,
            pre_install=specs.get("pre_install"),
            packages=specs.get("packages"),
            pip_packages=specs.get("pip_packages"),
            install_cmd=specs.get("install"),
        )

    @property
    def platform(self) -> str:
        """Comma-separated platforms for multi-arch OCI tarball builds.

        Override via COMMIT0_BUILD_PLATFORMS env var (e.g. "linux/amd64" for
        single-arch builds).
        """
        return os.environ.get("COMMIT0_BUILD_PLATFORMS", "linux/amd64,linux/arm64")

    @abstractmethod
    def make_repo_script_list(self) -> list[str]:
        pass

    @abstractmethod
    def make_eval_script_list(self) -> list[str]:
        pass


class Commit0Spec(Spec):
    def make_repo_script_list(self) -> list[str]:
        """Create a list of bash commands to set up the repository for testing.
        This is the setup script for the instance image.
        Dependencies are installed via Dockerfile layers (not setup.sh).
        """
        repo = self.instance["repo"]
        env_setup_commit = self.instance["reference_commit"]
        base_commit = self.instance["base_commit"]

        setup_commands = [
            f"git clone --depth 1 -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch --depth 1 origin {env_setup_commit} {base_commit}",
            f"git reset --hard {env_setup_commit}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
            f"git reset --hard {base_commit}",
        ]
        return setup_commands

    def make_eval_script_list(self) -> list[str]:
        """Run the tests."""
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        eval_script_list = [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"git apply --allow-empty -v {diff_path}",
            "git status",
            f"{shlex.quote(self.instance['test']['test_cmd'])} --json-report --json-report-file=report.json --continue-on-collection-errors{{coverage}} {{test_ids}} > test_output.txt 2>&1",
            "echo $? > pytest_exit_code.txt",
        ]
        return eval_script_list


class SimpleSpec(Spec):
    def make_repo_script_list(self) -> list[str]:
        """Create a list of bash commands to set up the repository for testing.
        This is the setup script for the instance image.
        """
        setup_commands = [
            f"mkdir {self.repo_directory} && cd {self.repo_directory}",
            "which python",
        ]
        return setup_commands

    def make_eval_script_list(self) -> list[str]:
        """Run the tests."""
        eval_script_list = [
            f"cd {self.repo_directory}",
            "cat /patch.diff > test.py",
            "pytest test.py > test_output.txt 2>&1",
            "echo $? > pytest_exit_code.txt",
        ]
        return eval_script_list


class SWEBenchSpec(Spec):
    def make_repo_script_list(self) -> list[str]:
        """Create a list of bash commands to set up the repository for testing.
        This is the setup script for the instance image.
        Dependencies are installed via Dockerfile layers (not setup.sh).
        """
        repo = self.instance["repo"]
        base_commit = self.instance["base_commit"]
        setup_commands = [
            f"git clone --depth 1 -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch --depth 1 origin {base_commit}",
            "git remote remove origin",
        ]
        return setup_commands

    def make_eval_script_list(self) -> list[str]:
        """Run the tests."""
        specs = self.instance["setup"]
        results = []
        if "install" in specs and specs["install"] is not None:
            installs = specs["install"].split("; ")
            for one in installs:
                install = one
                if "python -m pip install" in install:
                    install = install.replace("python -m pip install", "pip install")
                elif install.startswith("pip install"):
                    pass
                elif install.startswith("python setup.py"):
                    pass
                results.append(install)
        eval_script_list = (
            [
                f"cd {self.repo_directory}",
                f"git reset --hard {self.instance['base_commit']}",
                "git apply --allow-empty -v /patch.diff",
            ]
            + results
            + [
                "git status",
                f"{shlex.quote(self.instance['test']['test_cmd'])} --json-report --json-report-file=report.json --continue-on-collection-errors{{coverage}} {{test_ids}} > test_output.txt 2>&1",
                "echo $? > pytest_exit_code.txt",
            ]
        )
        return eval_script_list


def get_specs_from_dataset(
    dataset: Union[list[Union[RepoInstance, SimpleInstance]], list[Spec]],
    dataset_type: str,
    absolute: bool,
) -> list[Spec]:
    """Idempotent function that converts a list of RepoInstance objects to a list of Spec objects."""
    if isinstance(dataset[0], Spec):
        return cast(list[Spec], dataset)
    return list(
        map(
            lambda instance: make_spec(instance, dataset_type, absolute),
            cast(list["RepoInstance"], dataset),
        )
    )


def make_spec(
    instance: Union[RepoInstance, SimpleInstance], dataset_type: str, absolute: bool
) -> Spec:
    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR
    if isinstance(instance, Spec):
        return instance
    if dataset_type == "commit0":
        return Commit0Spec(
            repo=instance["instance_id"],
            repo_directory=repo_directory,
            instance=instance,
            absolute=absolute,
        )
    elif dataset_type == "swebench":
        return SWEBenchSpec(
            repo=instance["instance_id"],
            repo_directory=repo_directory,
            instance=instance,
            absolute=absolute,
        )
    elif dataset_type == "simple":
        return SimpleSpec(
            repo="simple",  # all benchmarks with mere function writing will share the simple docker image
            repo_directory=repo_directory,
            instance=instance,
            absolute=absolute,
        )
    else:
        raise NotImplementedError(
            f"{dataset_type} is not supported.\nWe only support commit0 and swebench instances for now."
        )


__all__ = []
