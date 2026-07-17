"""TypeScript spec — co-located alongside spec.py."""

import logging
import shlex
from dataclasses import dataclass
from typing import Union, cast

from commit0.harness.spec import Spec
from commit0.harness.eval_hardening import (
    revert_and_clean_lines,
    guard_snapshot_lines,
    guard_heal_lines,
)
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
        # Canonical key is `node_version` (what prepare emits); also accept the
        # `node` shorthand so either schema resolves rather than silently defaulting.
        for key in ("node_version", "node"):
            if key in setup and setup[key] not in (None, ""):
                return str(setup[key])
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

        _SHELL_DANGER = set(";&|`$(){}!><\\\n\r")
        if any(c in _SHELL_DANGER for c in install_cmd):
            # Defense against a malicious/corrupt dataset row: NEVER execute a
            # metachar-bearing install command. Warn and fall back to the safe
            # default rather than raising (which would crash the whole build for
            # this repo) — mirrors spec_js's warn-and-fall-back behavior.
            logger.warning(
                "install_cmd contains shell metacharacters (injection risk): %r; "
                "falling back to the safe default 'npm install'.", install_cmd,
            )
            install_cmd = "npm install"

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
        install_fail = "(echo 'INSTALL_FAILED' >&2; exit 1)"
        if "yarn" in install_cmd.lower():
            install_step = (
                "if yarn --version 2>/dev/null | grep -q '^1\\.'; then "
                f"{install_cmd} --ignore-scripts || {install_cmd} --ignore-scripts || {install_fail}; "
                "else "
                f"yarn install --immutable --mode=skip-build || yarn install --immutable --mode=skip-build || {install_fail}; "
                "fi"
            )
        else:
            install_step = (
                f"{install_cmd} --ignore-scripts || {install_cmd} --ignore-scripts || {install_fail}"
            )
        steps.extend(
            [
                install_step,
                # Post-install verification: catches silent partial installs.
                # Yarn Berry PnP mode leaves NO node_modules dir (only .pnp.cjs);
                # --ignore-scripts can skip lockfile write on some yarn/pnpm builds.
                # Without this, node_modules is silently absent at agent-run time,
                # producing cryptic 'findPackageLocation' / 'Cannot find module' errors
                # from tsc / vitest / aider that look like agent bugs but are harness bugs.
                (
                    'if [ -d node_modules ] && [ -n "$(ls -A node_modules 2>/dev/null)" ]; then '
                    'echo "OK: node_modules populated"; '
                    'elif [ -f .pnp.cjs ]; then '
                    'echo "OK: yarn berry PnP mode (.pnp.cjs present)"; '
                    'else '
                    'echo "INSTALL_VERIFICATION_FAILED: no node_modules and no .pnp.cjs after install" >&2; '
                    'exit 1; '
                    'fi'
                ),
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
        # Metachar guard runs on the RAW test_cmd (BEFORE the timeout wrapper),
        # because the wrapper itself contains `$` and `{}` from ${EVAL_TEST_TIMEOUT:-900}
        # which are intentional interpolation, not injection. Doing this check
        # after wrapping would false-positive on every legitimate command.
        _raw_test_cmd = (
            test.get("test_cmd", default_test)
            if isinstance(test, dict)
            else default_test
        )
        _SHELL_DANGER = set(";&|`$(){}!><\\\n\r")
        if any(c in _SHELL_DANGER for c in _raw_test_cmd):
            logger.warning(
                "test_cmd contains shell metacharacters (injection risk): %r; "
                "falling back to the safe default %r.", _raw_test_cmd, default_test,
            )
            _raw_test_cmd = default_test
        # Prepend `timeout` so a runaway test suite is killed cleanly instead of
        # hanging until the outer Docker container timeout. Exit 124 = timeout.
        _to = 'timeout --kill-after=10 "${EVAL_TEST_TIMEOUT:-900}" '
        test_cmd = _to + _raw_test_cmd

        try:
            _tokens = shlex.split(_raw_test_cmd)
        except ValueError:
            _tokens = _raw_test_cmd.split()
        _basenames = {t.rsplit("/", 1)[-1] for t in _tokens}
        is_vitest = "vitest" in _basenames
        is_node_test = (
            "--test" in _tokens
            and any(b == "node" for b in _basenames)
        )
        if is_node_test:
            json_flags = (
                "--test-reporter=tap --test-reporter-destination=report.tap"
            )
        elif is_vitest:
            json_flags = "--reporter=json --outputFile=report.json"
        else:
            json_flags = "--json --outputFile=report.json"

        force_flags = (
            "" if (is_vitest or is_node_test)
            else " --forceExit --detectOpenHandles"
        )

        base_commit = (
            self.instance["base_commit"]
            if isinstance(self.instance, dict)
            else self.instance.base_commit
        )

        _pathspecs = [
            "test/", "tests/", "__tests__/",
            ":(glob)**/test/**", ":(glob)**/tests/**", ":(glob)**/__tests__/**",
            ":(icase,glob)Test/**", ":(icase,glob)Tests/**",
            ":(glob)**/*.test.js", ":(glob)**/*.test.jsx",
            ":(glob)**/*.test.mjs", ":(glob)**/*.test.cjs",
            ":(glob)**/*.test.ts", ":(glob)**/*.test.tsx",
            ":(glob)**/*.test.mts", ":(glob)**/*.test.cts",
            ":(glob)**/*.spec.js", ":(glob)**/*.spec.jsx",
            ":(glob)**/*.spec.mjs", ":(glob)**/*.spec.cjs",
            ":(glob)**/*.spec.ts", ":(glob)**/*.spec.tsx",
            "e2e/", "cypress/", "playwright/", "integration/",
            ":(glob)**/e2e/**", ":(glob)**/cypress/**", ":(glob)**/playwright/**",
            "jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs",
            ":(glob)**/jest.config.js", ":(glob)**/jest.config.ts",
            ":(glob)**/jest.config.mjs", ":(glob)**/jest.config.cjs",
            "vitest.config.js", "vitest.config.ts", "vitest.config.mjs",
            "vitest.workspace.js", "vitest.workspace.ts",
            ":(glob)**/vitest.config.js", ":(glob)**/vitest.config.ts",
            ":(glob)**/vitest.config.mjs", ":(glob)**/vitest.workspace.js",
            ":(glob)**/vitest.workspace.ts",
            "cypress.config.js", "cypress.config.ts", "cypress.config.mjs",
            "playwright.config.js", "playwright.config.ts", "playwright.config.mjs",
            "karma.conf.js", "karma.conf.ts", "wallaby.conf.js",
            "babel.config.js", "babel.config.json", ".mocharc.js", ".mocharc.json",
            "tsconfig.test.json", "tsconfig.spec.json",
            ":(glob)**/tsconfig.test.json", ":(glob)**/tsconfig.spec.json",
            "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
            "pnpm-workspace.yaml",
            "sitecustomize.py", "usercustomize.py",
            ".env", ".gitmodules", ".gitattributes",
        ]
        # Per-pathspec revert (the list already carries root + nested :(glob)
        # forms, so nested=False) + delete model-added test/config files. Pass
        # raw pathspecs — revert_and_clean_lines shlex-quotes each itself.
        revert_lines = revert_and_clean_lines(
            base_commit,
            revert_targets=list(_pathspecs),
            delete_added_globs=[
                "*.test.ts", "**/*.test.ts", "*.spec.ts", "**/*.spec.ts",
                "*.test.js", "**/*.test.js", "*.spec.js", "**/*.spec.js",
                "jest.config.*", "**/jest.config.*",
                "vitest.config.*", "**/vitest.config.*",
                "babel.config.*", "**/babel.config.*",
                ".mocharc.*", "**/.mocharc.*",
            ],
            nested=False,
        )
        steps: list[str] = [
            f"cd {shlex.quote(self.repo_directory)}",
            f"git reset --hard {shlex.quote(base_commit)}",
            # A patch that fails to apply must NOT fall through to the test run on
            # an unpatched (stub) tree — that produces a 0/N indistinguishable from
            # a real 0% score. Try exact then --recount, and on failure write the
            # sentinel evaluate_ts.py maps to PATCH_APPLY_FAILED, then stop.
            (
                f"if git apply --allow-empty -v {shlex.quote(diff_path)} || "
                f"git apply --allow-empty --recount -v {shlex.quote(diff_path)}; "
                "then :; else echo PATCH_APPLY_FAILED > test_output.txt; "
                "echo 1 > test_exit_code.txt; exit 0; fi"
            ),
            # Layer-2 guard: snapshot applied tree; heal any file the anti-cheat
            # rewrites corrupt (balanced->unbalanced) before tests run.
            *guard_snapshot_lines(),
            *revert_lines,
            "git status",
            *guard_heal_lines(),
        ]
        if is_node_test:
            steps.append("npm install --no-save --silent tsx 2>/dev/null || true")
            steps.append(
                f"NODE_OPTIONS='--import tsx' {test_cmd} {json_flags}{force_flags} > test_output.txt 2>&1"
            )
        else:
            steps.append(
                f"{test_cmd} {json_flags}{force_flags} > test_output.txt 2>&1"
            )
        steps.append("echo $? > test_exit_code.txt")
        return steps


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
