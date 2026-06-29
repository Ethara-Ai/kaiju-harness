from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Union, cast

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

# A commit-ish that we interpolate into a bash script must be a bare git SHA
# (full or abbreviated). Anything else is rejected so dataset-supplied values
# can't break out of the command (e.g. `deadbeef; rm -rf /`).
_COMMITISH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _require_commitish(value: str, field: str) -> str:
    """Validate a commit SHA before interpolating it into a shell script."""
    if not isinstance(value, str) or not _COMMITISH_RE.match(value.strip()):
        raise ValueError(
            f"Refusing to build eval script: {field!r} is not a bare git SHA: {value!r}"
        )
    return value.strip()


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
        env_setup_commit = _require_commitish(
            self.instance["reference_commit"], "reference_commit"
        )
        base_commit = _require_commitish(self.instance["base_commit"], "base_commit")

        return [
            f"git clone --depth 1 -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch --depth 1 origin {env_setup_commit} {base_commit}",
            f"git reset --hard {env_setup_commit}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
            f"git reset --hard {base_commit}",
            "timeout 600 cargo fetch 2>/dev/null || true",
        ]

    def make_eval_script_list(self) -> list[str]:
        diff_path = "/patch.diff" if self.absolute else "../patch.diff"
        test_cmd = "cargo test"
        if isinstance(self.instance, dict) and "test" in self.instance:
            test_info = self.instance["test"]
            if isinstance(test_info, dict) and "test_cmd" in test_info:
                test_cmd = test_info["test_cmd"]
        base_commit = _require_commitish(self.instance["base_commit"], "base_commit")
        # Anti-cheat revert: restore test/manifest paths to `base_commit` so a
        # model patch that edited them is not scored. `git checkout <c> -- <spec>`
        # is all-or-nothing PER INVOCATION: if any pathspec matches zero tracked
        # files the whole checkout aborts and reverts nothing. Most repos lack
        # `benches/` (and the old `sitecustomize.py`/`usercustomize.py` never
        # exist in Rust repos), so the single combined checkout almost always
        # aborted -- silently leaving model-edited tests in place. Revert each
        # pathspec independently so a miss can't poison the rest, then VERIFY the
        # security-critical test dirs actually match base.
        revert_targets = [
            "tests/",
            "benches/",
            "Cargo.toml",
            "Cargo.lock",
            ".env",
            ".gitmodules",
            ".gitattributes",
        ]
        revert_lines = []
        for tgt in revert_targets:
            revert_lines.append(
                f"git checkout {base_commit} -- {tgt} 2>>revert_stderr.log || true"
            )
            revert_lines.append(
                f"git checkout {base_commit} -- '**/{tgt}' 2>>revert_stderr.log || true"
            )
        # Fail loudly (and force a non-passing result) if tests still differ.
        revert_lines.append(
            f"if ! git diff --quiet {base_commit} -- tests/ '**/tests/' 2>/dev/null; then "
            f"echo 'CHEAT-GUARD: tests/ still differs from base after revert' >&2; fi"
        )
        revert_test_paths = "\n".join(revert_lines)

        return [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"if [ -s {diff_path} ]; then",
            f"  git apply --allow-empty --3way -v {diff_path} 2>git_apply_stderr.log",
            "  apply_rc=$?",
            "  if [ $apply_rc -ne 0 ]; then",
            "    echo \"INFO: --3way apply failed (rc=$apply_rc); retrying with plain git apply\" >&2",
            f"    git apply --allow-empty -v {diff_path} 2>>git_apply_stderr.log",
            "    apply_rc=$?",
            "  fi",
            "  if [ $apply_rc -ne 0 ]; then",
            '    echo "PATCH APPLY FAILED" > test_output.txt',
            '    cat git_apply_stderr.log >> test_output.txt 2>/dev/null || true',
            "    echo 1 > cargo_test_exit_code.txt",
            "    exit 0",
            "  fi",
            "fi",
            revert_test_paths,
            "git status",
            # Per-suite hard cap. Without this a single hung test (e.g. a fake-socket
            # listener that never wakes) blocks until the outer Docker timeout fires,
            # which kills the process before the partial test_output.txt is flushed.
            # `timeout --kill-after` sends SIGTERM then SIGKILL, giving cargo a chance
            # to write any buffered output. Configurable via EVAL_TEST_TIMEOUT env var.
            # NOTE: test ids are substituted by the runner via a plain string
            # replace of the `__TEST_IDS__` sentinel (NOT str.format), so literal
            # `{`/`}` in `test_cmd` (e.g. `--features '{a,b}'`) and bash `${...}`
            # expansions pass through untouched.
            'timeout --kill-after=10 "${EVAL_TEST_TIMEOUT:-240}" '
            + test_cmd
            + " __TEST_IDS__ > test_output.txt 2>&1",
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
