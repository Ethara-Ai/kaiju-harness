"""TypeScript spec — co-located alongside spec.py."""

import logging
import shlex
from dataclasses import dataclass
from typing import Union, cast

from commit0.harness.spec import Spec
from commit0.harness.constants import (
    RepoInstance,
    SimpleInstance,
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
)
from commit0.harness.constants_ts import DEFAULT_NODE_VERSION, TsRepoInstance
from commit0.harness.dockerfiles_ts import (
    get_dockerfile_base_ts,
    get_dockerfile_repo_ts,
)

logger = logging.getLogger(__name__)


def _exec_prefix_from_install(install_cmd: str) -> str:
    """Return the local-binary runner for the package manager detected from *install_cmd*."""
    cmd = install_cmd.lower()
    if "pnpm" in cmd:
        return "pnpm exec"
    if "yarn" in cmd:
        return "yarn"
    if "bun" in cmd:
        return "bunx"
    return "npx"


@dataclass
class Commit0TsSpec(Spec):
    """TypeScript-specific spec that overrides Python-centric defaults."""

    def _get_node_version(self) -> str:
        setup = self._get_setup_dict()
        if "node_version" in setup:
            return str(setup["node_version"])
        logger.debug(
            "No node version specified, defaulting to %s", DEFAULT_NODE_VERSION
        )
        return DEFAULT_NODE_VERSION

    @property
    def base_image_key(self) -> str:
        return f"commit0.base.node{self._get_node_version()}:latest"

    @property
    def base_dockerfile(self) -> str:
        return get_dockerfile_base_ts(self._get_node_version())

    @property
    def repo_dockerfile(self) -> str:
        specs = self._get_setup_dict()
        return get_dockerfile_repo_ts(
            base_image=self.base_image_key,
            install_cmd=specs.get("install"),
            packages=specs.get("packages"),
            pre_install=specs.get("pre_install"),
        )

    @staticmethod
    def _package_manager_install(install_cmd: str) -> list[str]:
        """Return npm global install commands needed for the package manager in *install_cmd*."""
        cmd_lower = install_cmd.lower()
        if "pnpm" in cmd_lower:
            return ["command -v pnpm >/dev/null 2>&1 || npm install -g pnpm"]
        if "yarn" in cmd_lower:
            return ["command -v yarn >/dev/null 2>&1 || npm install -g yarn"]
        if "bun" in cmd_lower:
            return ["command -v bun >/dev/null 2>&1 || npm install -g bun"]
        return []

    def make_repo_script_list(self) -> list[str]:
        repo = self.instance["repo"]
        env_setup_commit = self.instance["reference_commit"]
        base_commit = self.instance["base_commit"]
        setup = self._get_setup_dict()
        install_cmd = setup.get("install", "npm install")

        _SHELL_DANGER = set(";&|`$(){}!><")
        if any(c in _SHELL_DANGER for c in install_cmd):
            logger.warning(
                "install_cmd contains shell metacharacters: %r — potential injection risk",
                install_cmd,
            )

        steps = [
            f"git clone --depth 1 -o origin https://github.com/{shlex.quote(repo)} {shlex.quote(self.repo_directory)}",
            f"chmod -R 777 {shlex.quote(self.repo_directory)}",
            f"cd {shlex.quote(self.repo_directory)}",
            f"git fetch --depth 1 origin {shlex.quote(env_setup_commit)} {shlex.quote(base_commit)}",
            f"git reset --hard {shlex.quote(env_setup_commit)}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
        ]
        prefix = _exec_prefix_from_install(install_cmd)
        steps.extend(self._package_manager_install(install_cmd))
        steps.extend(
            [
                f"{install_cmd} --ignore-scripts 2>/dev/null || {install_cmd} 2>/dev/null || true",
                f"{prefix}{' --yes' if prefix == 'npx' else ''} node-gyp rebuild 2>/dev/null || true",
                f"git reset --hard {shlex.quote(base_commit)}",
            ]
        )
        return steps

    def make_eval_script_list(self) -> list[str]:
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        test = (
            self.instance["test"]
            if isinstance(self.instance, dict)
            else self.instance.test
        )
        setup = self._get_setup_dict()
        install_cmd = setup.get("install", "npm install")
        default_test = f"{_exec_prefix_from_install(install_cmd)} jest"
        test_cmd = (
            test.get("test_cmd", default_test)
            if isinstance(test, dict)
            else default_test
        )

        _SHELL_DANGER = set(";&|`$(){}!><")
        if any(c in _SHELL_DANGER for c in test_cmd):
            logger.warning(
                "test_cmd contains shell metacharacters: %r — potential injection risk",
                test_cmd,
            )

        # Detect framework and add JSON report flags.
        # Tokenise the command and look for 'vitest'/'jest' as an argv token so that
        # a jest invocation referencing a file named 'vitest-compat.test.ts' is not
        # misclassified as vitest.
        try:
            _tokens = shlex.split(test_cmd)
        except ValueError:
            _tokens = test_cmd.split()
        _basenames = {t.rsplit("/", 1)[-1] for t in _tokens}
        is_vitest = "vitest" in _basenames
        if is_vitest:
            json_flags = "--reporter=json --outputFile=report.json"
        else:
            # Jest
            json_flags = "--json --outputFile=report.json"

        # --forceExit and --detectOpenHandles are Jest-only; Vitest rejects unknown flags
        force_flags = "" if is_vitest else " --forceExit --detectOpenHandles"

        base_commit = (
            self.instance["base_commit"]
            if isinstance(self.instance, dict)
            else self.instance.base_commit
        )

        revert_test_paths = (
            f"git checkout {shlex.quote(base_commit)} -- "
            f"test/ tests/ __tests__/ "
            f"jest.config.js jest.config.ts jest.config.mjs jest.config.cjs "
            f"vitest.config.js vitest.config.ts vitest.config.mjs "
            f"vitest.workspace.js vitest.workspace.ts "
            f"babel.config.js babel.config.json .mocharc.js .mocharc.json "
            f"2>/dev/null || true"
        )
        return [
            f"cd {shlex.quote(self.repo_directory)}",
            f"git reset --hard {shlex.quote(base_commit)}",
            f"git apply --allow-empty -v {shlex.quote(diff_path)}",
            revert_test_paths,
            "git status",
            f"{test_cmd} {json_flags}{force_flags} > test_output.txt 2>&1",
            "echo $? > test_exit_code.txt",
        ]


def make_ts_spec(
    instance: Union[TsRepoInstance, RepoInstance, dict],
    absolute: bool = True,
) -> Commit0TsSpec:
    """Factory function to create a Commit0TsSpec from a dataset entry."""
    if isinstance(instance, dict):
        instance_id = instance.get("instance_id", instance.get("repo", ""))
        dict_copy = dict(instance)
        if "instance_id" not in dict_copy:
            dict_copy["instance_id"] = instance_id
        repo_instance = RepoInstance(**dict_copy)
    elif hasattr(instance, "instance_id"):
        instance_id = instance.instance_id
        repo_instance = cast(Union[RepoInstance, SimpleInstance], instance)
    else:
        instance_id = str(instance)
        repo_instance = cast(Union[RepoInstance, SimpleInstance], instance)

    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR

    return Commit0TsSpec(
        absolute=absolute,
        repo=instance_id,
        repo_directory=repo_directory,
        instance=repo_instance,
    )
