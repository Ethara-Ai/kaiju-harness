"""Go-specific Spec subclass and factory for commit0 Go integration."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
)
from commit0.harness.constants_go import GoRepoInstance
from commit0.harness.eval_hardening import (
    revert_and_clean_lines,
    guard_snapshot_lines,
    guard_heal_lines,
)
from commit0.harness.spec import Spec

# A commit-ish interpolated into the eval bash MUST be a bare git SHA (full or
# abbreviated). Anything else is rejected so a dataset-supplied value can't break
# out of the command (`deadbeef; rm -rf /`). Mirrors spec_rust._COMMITISH_RE.
_COMMITISH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

# `test_cmd` is interpolated VERBATIM into the eval bash (there is no `test_ids`
# splice on the Go path — the whole command comes from the dataset). It is
# trusted dataset content, not model/agent input, but we still reject the shell
# metacharacters that would let a malformed/poisoned dataset row run a SECOND
# command or smuggle a `go test` flag such as `-exec`/`-toolexec` (arbitrary code
# exec). We forbid: newline / carriage-return, `;`, backtick, `$(`, `${`, `&`,
# `<`, `>`. `|` is deliberately ALLOWED because `go test -run 'TestA|TestB'` uses
# regex alternation (a very common, legitimate filter) — and a bare pipe can't
# chain a payload without also using one of the still-forbidden tokens (`;`,
# newline, `$(`, backtick, redirection). Everything else the command legitimately
# needs (alnum, space, tab, `./_-.:=,'"[]*@+~^!()|`) is permitted.
_TEST_CMD_RE = re.compile(r"\A[\w \t:./@#=+*\-\[\](),~^!'\"|]*\Z")
_TEST_CMD_FORBIDDEN = ("\n", "\r", ";", "`", "$(", "${", "&", "<", ">")


def _require_commitish(value: str, field: str) -> str:
    """Validate a commit SHA before interpolating it into the eval script."""
    if not isinstance(value, str) or not _COMMITISH_RE.match(value.strip()):
        raise ValueError(
            f"Refusing to build eval script: {field!r} is not a bare git SHA: {value!r}"
        )
    return value.strip()


def _require_safe_test_cmd(value: str) -> str:
    """Reject a dataset ``test_cmd`` that could inject a second shell command or a
    dangerous ``go test`` flag before it is spliced into the eval bash.
    """
    if not isinstance(value, str):
        raise ValueError(f"Refusing to build eval script: test_cmd is not a str: {value!r}")
    if any(tok in value for tok in _TEST_CMD_FORBIDDEN) or not _TEST_CMD_RE.match(value):
        raise ValueError(
            f"Refusing to build eval script: unsafe characters in test_cmd: {value!r}"
        )
    # `go test` flags that either run an arbitrary program or rewrite build/link
    # output are code-exec or scoring-influence vectors that bypass the scored
    # source. Reject them all (dataset content is trusted, but this is cheap
    # defense-in-depth against a poisoned row):
    #   -exec / -toolexec   run a program in place of / wrapping the test binary
    #   -toolexec / -vettool point the tool/vet chain at an arbitrary binary
    #   -ldflags            `-X pkg.Var=val` overwrites impl string vars at link
    #                       time (can make an assertion pass without solving);
    #                       `-extldflags` reaches the external linker
    #   -gcflags            can inject compiler behaviour / point at plugins
    # Match on token boundaries so a legit substring (e.g. a package path that
    # merely CONTAINS "exec") isn't rejected, while `-exec`, `--exec`, and
    # `-exec=...` all are.
    lowered = value.lower()
    _FORBIDDEN_FLAGS = ("exec", "toolexec", "ldflags", "gcflags", "vettool")
    _tokens = re.split(r"[\s=]+", lowered)
    for tok in _tokens:
        stripped = tok.lstrip("-")
        if tok.startswith("-") and stripped in _FORBIDDEN_FLAGS:
            raise ValueError(
                f"Refusing to build eval script: test_cmd uses forbidden go flag "
                f"{tok!r}: {value!r}"
            )
    return value


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
        env_setup_commit = _require_commitish(
            self.instance["reference_commit"], "reference_commit"
        )
        base_commit = _require_commitish(self.instance["base_commit"], "base_commit")
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
        test_cmd = _require_safe_test_cmd(
            self.instance["test"].get("test_cmd", "go test -json -count=1 ./...")
        )
        base_commit = _require_commitish(self.instance["base_commit"], "base_commit")
        # Robust per-pathspec revert + delete of model-ADDED test/build files, via
        # the shared hardened helper (git ls-files --others sees untracked adds a
        # `git apply` leaves behind). Go tests live ONLY in *_test.go, so a full
        # revert + delete of those replaces rust's in-src restore. We reset EVERY
        # surface a model could edit to change WHAT/HOW `go test` runs without
        # touching a scored (non-_test.go) source file:
        #   *_test.go / **/*_test.go -> the tests themselves (+ TestMain, benches)
        #   testdata/ test/ tests/    -> golden files the tests read
        #   go.mod/go.sum/go.work/    -> dependency graph & module wiring; a swapped
        #   go.work.sum                  version changes imported (impl) behaviour
        #   vendor/                   -> WITH a vendor dir `go test` uses vendored
        #                                deps by default; a model editing a vendored
        #                                package injects impl outside the scored src
        #   Makefile                  -> a `make test` shim the dataset may call
        #   .golangci.yml/tools.go    -> lint/tooling config
        # NOTE: a model CANNOT make base tests pass by adding a build-tagged file
        # that shadows a test, because base *_test.go are restored verbatim and any
        # ADDED *_test.go (build-tagged or not) is deleted below.
        revert_lines = revert_and_clean_lines(
            base_commit,
            revert_targets=[
                "*_test.go", "testdata/", "test/", "tests/",
                "Makefile", "go.mod", "go.sum", "go.work", "go.work.sum",
                "vendor/", ".golangci.yml", ".golangci.yaml", "tools.go",
                "sitecustomize.py", "usercustomize.py",
                ".env", ".gitmodules", ".gitattributes",
            ],
            delete_added_globs=[
                "*_test.go", "**/*_test.go",
                "go.work", "**/go.work",
                "go.work.sum", "**/go.work.sum",
                "sitecustomize.py", "**/sitecustomize.py",
                "usercustomize.py", "**/usercustomize.py",
            ],
        )
        # A model-ADDED vendor/ (or one it tampered) is not deleted by the helper
        # (deleting a legit base vendor/ dir would break the build), but the
        # per-pathspec revert above restores every tracked vendored file to base.
        # Cheat-guard: warn if vendor/ still differs from base after the revert.
        revert_lines.append(
            f"if ! git diff --quiet {base_commit} -- vendor/ '**/vendor/' 2>/dev/null; then "
            "echo 'CHEAT-GUARD: vendor/ still differs from base after revert' >&2; fi"
        )

        # Per-suite `go test -timeout`: without it Go's binary uses a 10-min
        # default that, on expiry, panics with a full goroutine dump (polluting
        # test_output.json) rather than a clean fail. We pin it to a generous,
        # env-overridable bound and wrap the whole command in coreutils `timeout`
        # (SIGTERM then SIGKILL) so a hung test can't run to the outer Docker
        # timeout and lose the partial output. `-timeout` is injected only for a
        # `go test` command that doesn't already carry its own.
        go_timeout = (
            f"{test_cmd} -timeout ${{GO_TEST_TIMEOUT:-600s}}"
            if test_cmd.lstrip().startswith("go test") and "-timeout" not in test_cmd
            else test_cmd
        )
        run_line = (
            'timeout --kill-after=10 "${EVAL_TEST_TIMEOUT:-600}" '
            + go_timeout
            + " > test_output.json 2> test_stderr.txt"
        )

        eval_script_list = [
            f"cd {self.repo_directory}",
            f"git reset --hard {base_commit}",
            f"if [ -s {diff_path} ]; then",
            f"  git apply -v {diff_path}",
            "  if [ $? -ne 0 ]; then",
            '    echo \'{"Action":"fail","Package":"PATCH_APPLY_FAILED","Output":"git apply failed"}\'  > test_output.json',
            '    echo "git apply failed" > test_stderr.txt',
            "    echo 1 > go_test_exit_code.txt",
            "    exit 0",
            "  fi",
            "fi",
            # Layer-2 guard: snapshot the model's applied tree before the
            # anti-cheat reverts AND `goimports -w` (which rewrites every impl
            # file). Heal is placed AFTER goimports so a formatter that corrupts a
            # valid file is caught too — a compiling submission is never a false
            # COMPILE_FAILED from harness reconstruction.
            *guard_snapshot_lines(),
            *revert_lines,
            "find . -name '*.go' -not -name '*_test.go' -not -path '*/vendor/*' -print0 | xargs -0 -r goimports -w",
            "git status",
            *guard_heal_lines(),
            run_line,
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
