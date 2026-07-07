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
            # A10: don't fully MASK a fetch failure. Setup stays tolerant (deps
            # may still resolve at build/test time), but we record the failure to
            # a log so it's diagnosable rather than silently swallowed by `|| true`.
            "timeout 600 cargo fetch 2>cargo_fetch.log || echo 'CARGO_FETCH_FAILED (setup-time; see cargo_fetch.log)' >> cargo_fetch.log",
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
        # Anti-cheat revert allowlist. Beyond test dirs + manifests we also reset
        # every file that can change WHAT or HOW tests run at build/run time, so a
        # model can't neuter the suite without editing a scored source file:
        #   build.rs / */build.rs   -> arbitrary build-time code (cargo auto-runs it)
        #   .cargo/                  -> rustflags, custom test runner, target dir
        #   .config/nextest.toml     -> nextest filters / skip / retry / slow-timeout
        #   rust-toolchain(.toml)    -> pin a toolchain that skips/passes tests
        #   xtask/                   -> a `cargo xtask test` shim some crates use
        #   .cargo/config(.toml)     -> alias `test` to a no-op
        revert_targets = [
            "tests/",
            "benches/",
            "Cargo.toml",
            "Cargo.lock",
            ".env",
            ".gitmodules",
            ".gitattributes",
            "build.rs",
            ".cargo/",
            ".config/",
            "rust-toolchain",
            "rust-toolchain.toml",
            "xtask/",
        ]
        revert_lines = []
        for tgt in revert_targets:
            revert_lines.append(
                f"git checkout {base_commit} -- {tgt} 2>>revert_stderr.log || true"
            )
            revert_lines.append(
                f"git checkout {base_commit} -- '**/{tgt}' 2>>revert_stderr.log || true"
            )
        # Also DELETE any build.rs / .cargo / .config / xtask the model ADDED that
        # did not exist at base (git checkout only restores tracked paths; a newly
        # added, untracked build.rs would survive the revert and still run).
        revert_lines.append(
            f"for _p in $(git diff --name-only --diff-filter=A {base_commit} -- "
            "'**/build.rs' 'build.rs' '.cargo/**' '.config/**' 'xtask/**' 2>/dev/null); do "
            'rm -rf "$_p" 2>/dev/null || true; done'
        )
        # Fail loudly (and force a non-passing result) if tests still differ.
        revert_lines.append(
            f"if ! git diff --quiet {base_commit} -- tests/ '**/tests/' 2>/dev/null; then "
            f"echo 'CHEAT-GUARD: tests/ still differs from base after revert' >&2; fi"
        )

        # In-src `#[cfg(test)]` restore. Unit tests living INSIDE src/*.rs are not
        # covered by the tests/ revert, yet the model only ever needs to implement
        # stubs — never touch test code. For each src file that has an in-src test
        # module at base, keep the model's impl but reset the test module (from the
        # first `#[cfg(test)]` to EOF — the universal Rust convention) to base. If
        # the model changed a signature the base tests need, this yields a genuine
        # COMPILE_FAILED (correct — signatures must not change).
        insrc_restore = (
            f"for f in $(git diff --name-only {base_commit} -- 'src/*.rs' 'src/**/*.rs' 2>/dev/null); do\n"
            '  [ -f "$f" ] || continue\n'
            f'  base_ln=$(git show {base_commit}:"$f" 2>/dev/null | '
            "grep -nE '^[[:space:]]*#\\[cfg\\(test\\)\\]' | head -1 | cut -d: -f1)\n"
            '  [ -z "$base_ln" ] && continue\n'
            "  model_ln=$(grep -nE '^[[:space:]]*#\\[cfg\\(test\\)\\]' \"$f\" | head -1 | cut -d: -f1)\n"
            '  if [ -n "$model_ln" ] && [ "$model_ln" -gt 1 ]; then\n'
            '    head -n $((model_ln-1)) "$f" > "$f.kaiju_impl" 2>/dev/null || continue\n'
            '  else\n'
            '    : > "$f.kaiju_impl"\n'
            '    [ -z "$model_ln" ] && cp "$f" "$f.kaiju_impl"\n'
            '  fi\n'
            f'  git show {base_commit}:"$f" 2>/dev/null | tail -n +"$base_ln" >> "$f.kaiju_impl"\n'
            '  mv "$f.kaiju_impl" "$f"\n'
            "done"
        )
        revert_lines.append(insrc_restore)
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
            # Force serial test execution (A4): async/UDP crates bind real sockets on
            # fixed ports; parallel libtest threads collide -> spurious, nondeterministic
            # failures unrelated to the model. RUST_TEST_THREADS=1 makes runs reproducible.
            "export RUST_TEST_THREADS=1",
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
            # A10: a network/registry fetch failure makes `cargo test` fail with a
            # download error that is NOT the model's fault. The setup-time
            # `cargo fetch` is intentionally tolerant (deps may resolve at build
            # time), so we detect the failure HERE and mark it INFRA so the
            # evaluator scores it as infrastructure-broken, not a real 0%.
            # IMPORTANT: anchor to cargo's OWN diagnostic prefix (`error:` at line
            # start) so a test name / panic / asserted string that merely CONTAINS
            # "failed to download" can't false-trigger an INFRA classification on a
            # legitimately passing/failing run.
            "if grep -qE '^error: (failed to (download|fetch|get|load source)|"
            "could not resolve host|network failure|spurious network error)' test_output.txt 2>/dev/null; then "
            "echo 'INFRA_FETCH_FAILED: cargo could not fetch dependencies (network/registry)' >> test_output.txt; fi",
            # CHEAT-GUARD (A3) — runs AFTER cargo test (which truncates test_output.txt
            # via `>`), so we APPEND. Many crates keep tests inside src/ as
            # `#[cfg(test)]` modules that the tests/ revert above never touches. At base
            # the impl is stubbed, so `#[test]`/`#[cfg(test)]`/removed `assert!` lines
            # exist ONLY in test code — any such change in the model's diff is tampering.
            # (Adding an assert! in impl is legitimate, so we flag only removed/changed
            # asserts and any toggled test attribute.)
            # Only flag REMOVED (`^-`) test attributes / asserts. The agent may
            # legitimately ADD test code or impl asserts (those are `^+`); flagging
            # additions false-positived on real implementations and zeroed valid
            # runs. Removing/altering an existing `#[test]`/`#[cfg(test)]`/`assert!`
            # — all of which exist only in test code at base — is the real cheat.
            f"if git diff {base_commit} -- src/ 2>/dev/null | "
            r"grep -qE '^-[[:space:]]*#\[(tokio::)?test\]|^-[[:space:]]*#\[cfg\(test\)\]|^-[[:space:]]*assert(_eq|_ne)?!'; then "
            "echo 'CHEAT_DETECTED: model removed/altered in-src test code' >> test_output.txt; fi",
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
