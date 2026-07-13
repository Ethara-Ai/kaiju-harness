from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import cast

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.constants_js import (
    DEFAULT_NODE_VERSION,
    JS_SHELL_METACHARS,
    JS_TEST_CMD_RUNNERS,
    MAX_PATCH_BYTES,
    JsRepoInstance,
)
import logging

logger = logging.getLogger(__name__)
from commit0.harness.spec import Spec
from commit0.harness.eval_hardening import (
    revert_and_clean_lines,
    guard_snapshot_lines,
    guard_heal_lines,
)
from commit0.harness.dockerfiles_js import (
    get_dockerfile_base as _get_node_dockerfile_base,
    get_dockerfile_repo as _get_node_dockerfile_repo,
)

_ALLOWED_PACKAGE_MANAGERS: frozenset[str] = frozenset({"npm", "pnpm", "yarn", "bun"})


@dataclass
class Commit0JsSpec(Spec):
    def _get_node_version(self) -> int:
        setup = self._get_setup_dict()
        if setup.get("node_version") is not None:
            return int(setup["node_version"])
        if isinstance(self.instance, dict):
            raw = self.instance.get("node_version")
            if raw is not None:
                return int(raw)
        return DEFAULT_NODE_VERSION

    @property
    def base_image_key(self) -> str:
        return f"commit0.base.node{self._get_node_version()}:latest"

    @property
    def base_dockerfile(self) -> str:
        return _get_node_dockerfile_base(self._get_node_version())

    @property
    def repo_dockerfile(self) -> str:
        setup = self._get_setup_dict()
        return _get_node_dockerfile_repo(
            base_image_key=self.base_image_key,
            install_cmd=self._install_cmd(),
            packages=setup.get("packages"),
            pre_install=setup.get("pre_install"),
        )

    def make_repo_script_list(self) -> list[str]:
        repo_url = self._instance_str("repo")
        base_commit = self._instance_str("base_commit")
        reference_commit = self._instance_str("reference_commit", base_commit)
        clone_url = shlex.quote(repo_url)
        base_sha = shlex.quote(base_commit)
        ref_sha = shlex.quote(reference_commit)
        target = shlex.quote(self.repo_directory)
        return [
            "set -euo pipefail",
            f"git clone --depth=50 -o origin {clone_url} {target}",
            f"cd {target}",
            f"git fetch --depth=1 origin {ref_sha} {base_sha} || true",
            f"git checkout {ref_sha}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "rm -rf node_modules .nyc_output coverage dist build .next .turbo .cache",
            self._install_cmd(),
            f"git reset --hard {base_sha}",
        ]

    def make_eval_script_list(self) -> list[str]:
        framework = self._detect_test_framework()
        # Repo-relative (the eval has already `cd`ed into the repo). Absolute
        # /tmp paths broke result collection: files_to_collect / the Docker
        # copy_from_container / LocalInplace all key off {repo_dir}/{fname}, so
        # /tmp artifacts never reached log_dir and the reader saw nothing.
        results_path = "test_results.json"
        diff_path = shlex.quote("/patch.diff")
        base_commit = self._instance_str("base_commit")
        base_sha = shlex.quote(base_commit)
        target = shlex.quote(self.repo_directory)
        expected_node_major = shlex.quote(str(self._get_node_version()))
        # Robust per-pathspec revert + delete model-added test/config files.
        # babel.config.* was previously NOT reverted — a Babel plugin can
        # intercept Jest and forge results, so it (and lockfiles/.env/.npmrc) are
        # now covered. See eval_hardening.
        revert_lines = revert_and_clean_lines(
            base_sha,
            revert_targets=[
                "*.test.js", "*.test.mjs", "*.test.cjs", "*.test.jsx",
                "*.spec.js", "*.spec.mjs", "*.spec.cjs", "*.spec.jsx",
                "__tests__/", "test/", "tests/",
                "jest.config.*", "vitest.config.*", ".mocharc.*",
                "babel.config.js", "babel.config.cjs", "babel.config.mjs",
                ".babelrc", ".babelrc.js", ".babelrc.json",
                "package.json", "package-lock.json", "npm-shrinkwrap.json",
                "yarn.lock", "pnpm-lock.yaml", ".npmrc",
                ".env", ".gitmodules", ".gitattributes",
            ],
            delete_added_globs=[
                "*.test.js", "**/*.test.js", "*.spec.js", "**/*.spec.js",
                "jest.config.*", "**/jest.config.*",
                "vitest.config.*", "**/vitest.config.*",
                ".mocharc.*", "**/.mocharc.*",
                "babel.config.*", "**/babel.config.*",
                ".babelrc*", "**/.babelrc*",
            ],
        )
        steps: list[str] = [
            "set -uo pipefail",
            f"cd {target}",
            (
                "actual_node_major=$(node -e "
                "\"process.stdout.write(process.versions.node.split('.')[0])\""
                "); "
                f"if [ \"$actual_node_major\" != {expected_node_major} ]; then "
                "echo \"NODE_VERSION_MISMATCH: image has Node $actual_node_major, "
                f"setup pins {expected_node_major}\" >&2; "
                "exit 70; "
                "fi"
            ),
            f"git reset --hard {base_sha}",
            (
                f"_diff_bytes=$(wc -c < {diff_path}); "
                f"if [ \"$_diff_bytes\" -gt {MAX_PATCH_BYTES} ]; then "
                f"echo \"PATCH_TOO_LARGE: $_diff_bytes bytes exceeds "
                f"{MAX_PATCH_BYTES} byte cap\" >&2; "
                "exit 4; "
                "fi"
            ),
            f"git apply --allow-empty -v {diff_path}",
            # Layer-2 guard: snapshot applied tree; heal harness-corrupted files
            # (balanced->unbalanced) so a valid submission isn't failed by the
            # reconstruction. Heal before the syntax check + tests see the file.
            *guard_snapshot_lines(),
            *revert_lines,
            *guard_heal_lines(),
            self._install_cmd(),
            "echo $? > install_exit_code.txt",
            "rm -f /tmp/_node_check_max_rc; : > /tmp/_node_check_max_rc",
            "git ls-files -z '*.js' '*.mjs' '*.cjs' 2>/dev/null | xargs -0 -r -n 100 sh -c 'node --check \"$@\" || { rc=$?; cur=$(cat /tmp/_node_check_max_rc 2>/dev/null || echo 0); [ \"$rc\" -gt \"$cur\" ] && echo \"$rc\" > /tmp/_node_check_max_rc; }; true' _ || true",
            "if [ -s /tmp/_node_check_max_rc ]; then echo $(cat /tmp/_node_check_max_rc) > syntax_exit_code.txt; else echo 0 > syntax_exit_code.txt; fi",
        ]
        prefix = self._test_command_prefix(framework)
        test_cmd = {
            "jest": f"{prefix} --json --outputFile={results_path} --reporters=default",
            "vitest": f"{prefix} --reporter=json --outputFile={results_path}",
            "mocha": f"{prefix} --reporter json --reporter-options output={results_path}",
            "node_test": f"{prefix} --test-reporter=tap > {results_path}",
        }[framework]
        steps += [
            test_cmd,
            "echo $? > test_exit_code.txt",
            f"[ -s {results_path} ] || echo 'EMPTY_RESULTS' > {results_path}",
        ]
        return steps

    def _instance_str(self, key: str, default: str = "") -> str:
        inst = self.instance
        if isinstance(inst, dict):
            value = inst.get(key, default)
            return str(value) if value is not None else default
        if isinstance(inst, RepoInstance):
            try:
                value = inst[key]
            except KeyError:
                return default
            return str(value) if value is not None else default
        return default

    def _detect_package_manager(self) -> str:
        setup = self._get_setup_dict()
        if setup.get("package_manager"):
            candidate = str(setup["package_manager"])
        elif isinstance(self.instance, dict):
            candidate = str(self.instance.get("package_manager", "npm"))
        else:
            candidate = "npm"
        if candidate not in _ALLOWED_PACKAGE_MANAGERS:
            raise ValueError(
                f"Invalid package_manager: {candidate!r}. "
                f"Allowed: {sorted(_ALLOWED_PACKAGE_MANAGERS)}"
            )
        return candidate

    def _detect_test_framework(self) -> str:
        setup = self._get_setup_dict()
        if setup.get("test_framework"):
            return str(setup["test_framework"])
        if isinstance(self.instance, dict):
            return str(self.instance.get("test_framework", "jest"))
        return "jest"

    def _get_test_dict(self) -> dict:
        inst = self.instance
        if isinstance(inst, dict):
            value = inst.get("test")
            if isinstance(value, dict):
                return value
        if isinstance(inst, RepoInstance):
            try:
                value = inst["test"]
            except KeyError:
                return {}
            if isinstance(value, dict):
                return value
        return {}

    def _test_command_prefix(self, framework: str) -> str:
        test_dict = self._get_test_dict()
        raw = test_dict.get("test_cmd")
        if isinstance(raw, str):
            candidate = raw.strip()
            if candidate:
                if any(c in JS_SHELL_METACHARS for c in candidate):
                    logger.warning(
                        "test_cmd rejected (shell metacharacters): %r; "
                        "falling back to default for framework=%s",
                        candidate, framework,
                    )
                elif self._cmd_matches_framework(candidate, framework):
                    return candidate
        return self._default_test_command_prefix(framework)

    @staticmethod
    def _cmd_matches_framework(cmd: str, framework: str) -> bool:
        tokens = cmd.split()
        if not tokens:
            return False
        if framework == "node_test":
            return tokens[:2] == ["node", "--test"]
        if tokens[0] not in JS_TEST_CMD_RUNNERS:
            return False
        non_runner = [t for t in tokens[1:] if not t.startswith("-")]
        return bool(non_runner) and non_runner[0] == framework

    def _default_test_command_prefix(self, framework: str) -> str:
        if framework == "node_test":
            return "node --test"
        pm = self._detect_package_manager()
        runner = {"pnpm": "pnpm exec", "yarn": "yarn", "bun": "bunx"}.get(pm, "npx")
        framework_token = "vitest run" if framework == "vitest" else framework
        return f"{runner} {framework_token}"

    def _install_cmd(self) -> str:
        pm = self._detect_package_manager()
        return {
            "npm": "npm ci --no-audit --no-fund --ignore-scripts",
            "pnpm": "pnpm install --frozen-lockfile --ignore-scripts",
            "yarn": "yarn install --frozen-lockfile --ignore-scripts",
            "bun": "bun install --frozen-lockfile --ignore-scripts",
        }[pm]


def make_js_spec(
    instance: JsRepoInstance | RepoInstance | dict,
    absolute: bool = True,
) -> Commit0JsSpec:
    """Factory mirroring ``make_ts_spec``."""
    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR
    if isinstance(instance, dict):
        repo = str(instance.get("repo", instance.get("instance_id", "")))
    else:
        repo = instance.repo
    return Commit0JsSpec(
        absolute=absolute,
        repo=repo,
        repo_directory=repo_directory,
        instance=cast("RepoInstance | SimpleInstance", instance),
    )


__all__ = [
    "Commit0JsSpec",
    "make_js_spec",
]
