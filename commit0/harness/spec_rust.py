from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union, cast

from commit0.harness.constants import (
    ABSOLUTE_REPO_DIR,
    RELATIVE_REPO_DIR,
    RepoInstance,
    SimpleInstance,
)
from commit0.harness.constants_rust import RUST_BASE_IMAGE_TAG
from commit0.harness.spec import Spec
from commit0.harness.eval_hardening import (
    revert_and_clean_lines,
    guard_snapshot_lines,
    guard_heal_lines,
)
from commit0.harness.dockerfiles.__init__rust import (
    get_dockerfile_base_rust,
    get_dockerfile_repo_rust,
)

logger = logging.getLogger(__name__)

_INSRC_RESTORE_PY = (Path(__file__).parent / "insrc_restore.py").read_text()

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
        return RUST_BASE_IMAGE_TAG

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
        # Per-pathspec revert + delete of model-ADDED build files, via the shared
        # hardened helper (same as every other language). This closes the two
        # holes the old rust-inline version had: (a) it deleted added files with
        # `git diff --diff-filter=A`, which can't see UNTRACKED files (a patch
        # applied with `git apply` leaves adds untracked), so a model-added
        # build.rs/.cargo survived; the helper uses `git ls-files --others`.
        # (b) shlex-quoting + `-z` reads for path-with-space safety.
        revert_lines = revert_and_clean_lines(
            base_commit,
            revert_targets=revert_targets,
            delete_added_globs=[
                "build.rs", "**/build.rs",
                ".cargo", "**/.cargo",
                ".config", "**/.config",
                "xtask", "**/xtask",
                # H2 cheat-vector close: model must not be able to SWITCH the
                # toolchain by ADDING a rust-toolchain(.toml) that the base repo
                # doesn't ship. `revert_targets` above already restores an
                # EXISTING base version; only added-but-not-in-base files need
                # deletion. Without this, an added `rust-toolchain.toml` with
                # `channel = "nightly"` would silently switch the compiler and
                # let a model use unstable features / lints to skew scoring.
                "rust-toolchain", "**/rust-toolchain",
                "rust-toolchain.toml", "**/rust-toolchain.toml",
            ],
        )
        # Fail loudly (and force a non-passing result) if tests still differ.
        revert_lines.append(
            f"if ! git diff --quiet {base_commit} -- tests/ '**/tests/' 2>/dev/null; then "
            f"echo 'CHEAT-GUARD: tests/ still differs from base after revert' >&2; fi"
        )

        # In-src test restore (robust, universal). Rust unit tests live INSIDE
        # src/*.rs — as `#[cfg(test)] mod` blocks AND/OR top-level `#[test]` fns,
        # possibly interspersed with impl. The model implements stubs and must
        # never touch tests. Prefer the python pass (reconstructs impl + BASE's
        # tests for ANY layout — see _INSRC_RESTORE_PY); fall back to the
        # cfg(test)-module bash splice only when python3 is absent.
        # python3-absent fallback. The old line-number splice here had NO
        # well-formedness check and could corrupt a valid file (the byteorder
        # macro_rules! class of bug) — and crucially, when python3 is missing the
        # Layer-2 heal guard can't run either, so there is no backstop. A corrupted
        # tree (false COMPILE_FAILED on working code) is strictly worse than a
        # skipped restore (still backstopped by the marker-count cheat guard). So
        # when python3 is unavailable we SKIP the in-src restore rather than risk
        # corruption. (python3 is present in every eval image; this is dead-safe.)
        insrc_bash = (
            "echo 'kaiju: python3 unavailable — skipping in-src test restore "
            "(marker-count guard still applies)' >&2"
        )
        insrc_restore = (
            "if command -v python3 >/dev/null 2>&1; then\n"
            f"python3 - {base_commit} <<'KAIJU_INSRC_PY' || true\n"
            + _INSRC_RESTORE_PY.strip("\n") + "\n"
            "KAIJU_INSRC_PY\n"
            "else\n"
            + insrc_bash + "\n"
            "fi"
        )
        revert_lines.append(insrc_restore)
        revert_test_paths = "\n".join(revert_lines)

        return [
            f"cd {self.repo_directory}",
            f"git reset --hard {self.instance['base_commit']}",
            f"if [ -s {diff_path} ]; then",
            # Apply order matters. The patch is a clean `git diff base..branch`,
            # so PLAIN `git apply` reconstructs the tree byte-exactly and is fully
            # deterministic. `--3way` is a 3-WAY MERGE fallback that can return
            # success (rc=0) while SILENTLY mis-resolving into a syntactically
            # broken file (observed: a duplicated `}` -> `unexpected closing
            # delimiter` -> false COMPILE_FAILED even though the agent's branch
            # compiles and passes every test). Its merge behaviour is also
            # git-version dependent. So try the exact paths first and keep the
            # merge as a genuine last resort:
            #   plain -> --recount (tolerate off-by-N hunk headers, still exact
            #   positional) -> --3way (merge, may fuzz). Every other language's
            #   eval script already uses plain `git apply`; this brings Rust in
            #   line and eliminates the silent-corruption failure mode.
            f"  git apply --allow-empty -v {diff_path} 2>git_apply_stderr.log",
            "  apply_rc=$?",
            "  if [ $apply_rc -ne 0 ]; then",
            "    echo \"INFO: plain apply failed (rc=$apply_rc); retrying with --recount\" >&2",
            f"    git apply --allow-empty --recount -v {diff_path} 2>>git_apply_stderr.log",
            "    apply_rc=$?",
            "  fi",
            "  if [ $apply_rc -ne 0 ]; then",
            "    echo \"INFO: --recount apply failed (rc=$apply_rc); last resort --3way merge\" >&2",
            f"    git apply --allow-empty --3way -v {diff_path} 2>>git_apply_stderr.log",
            "    apply_rc=$?",
            "  fi",
            "  if [ $apply_rc -ne 0 ]; then",
            '    echo "PATCH APPLY FAILED" > test_output.txt',
            '    cat git_apply_stderr.log >> test_output.txt 2>/dev/null || true',
            "    echo 1 > cargo_test_exit_code.txt",
            "    exit 0",
            "  fi",
            "fi",
            # Layer-2 guard: snapshot the model's applied (known-good) tree BEFORE
            # the anti-cheat rewrites below, so any rewrite that corrupts a valid
            # file can be healed just before the build (see eval_hardening).
            *guard_snapshot_lines(),
            revert_test_paths,
            "git status",
            # Heal any file a rewrite turned balanced->unbalanced (harness-induced
            # corruption) back to the model's applied version, so a compiling
            # submission is never scored COMPILE_FAILED by the harness itself.
            *guard_heal_lines(),
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
            'timeout --kill-after=10 "${EVAL_TEST_TIMEOUT:-600}" '
            + test_cmd
            + " __TEST_IDS__ > test_output.txt 2>&1",
            "echo $? > cargo_test_exit_code.txt",

            # N30 note: cargo has no zero-cost pre-gate for Rust — `cargo check`
            # does a full type-check compile (same order as `cargo test`'s build
            # phase), so running it before the test invocation would double the
            # wall time on large workspaces without adding classifier signal
            # (`cargo test` already surfaces `error[E####]:` diagnostics that
            # evaluate_rust._count_compile_errors classifies). Go DOES get a
            # cheap `go vet ./...` pre-gate in spec_go because vet is
            # semantic-only and doesn't recompile.
            # A10: a network/registry fetch failure makes `cargo test` fail with a
            # download error that is NOT the model's fault. The setup-time
            # `cargo fetch` is intentionally tolerant (deps may resolve at build
            # time), so we detect the failure HERE and mark it INFRA so the
            # evaluator scores it as infrastructure-broken, not a real 0%.
            # IMPORTANT: anchor to cargo's OWN diagnostic prefix (`error:` at line
            # start) so a test name / panic / asserted string that merely CONTAINS
            # "failed to download" can't false-trigger an INFRA classification on a
            # legitimately passing/failing run.
            # M8: harden the pattern. Cargo copy has churned across versions (e.g.
            # "failed to fetch" vs "failed to get", the "update registry" wording,
            # download variants), so we widen coverage to include: the historical
            # prefixes above, TLS / certificate errors, proxy failures, generic
            # network timeouts, and "failed to (query|update|read) index/registry".
            "if grep -qE '^error: (failed to (download|fetch|get|load source|query registry|update registry|read the registry index|resolve dependencies from the network|write the registry index)|could not resolve host|network failure|spurious network error|connection (refused|reset by peer|timed out)|(unable to get local issuer certificate|certificate verify failed|SSL certificate problem|TLS handshake)|proxy authentication required)' test_output.txt 2>/dev/null; then "
            "echo 'INFRA_FETCH_FAILED: cargo could not fetch dependencies (network/registry/TLS)' >> test_output.txt; fi",
            # CHEAT-GUARD (A3) — COUNT-BASED backstop, runs AFTER cargo test (which
            # truncates test_output.txt via `>`), so we APPEND. It flags a cheat
            # only on a NET DECREASE in in-src test markers/assertions vs base,
            # rather than grepping the diff for any removed `assert!` line. The old
            # line-grep false-positived on benign edits: a variable RENAME inside a
            # test (`let count` -> `let rev_count`) shows a removed `-assert_eq!(...)`
            # line even though the assertion is unchanged. Counting is rename/
            # reformat-proof: at base the impl is stubbed (no asserts — only tests
            # carry `#[test]`/`#[cfg(test)]`/`assert!`), and the restore above keeps
            # BASE's tests, so the reconstructed count can only DROP if a real test
            # survived un-restored and was removed/weakened — the true cheat.
            f"_kj_base={base_commit}\n"
            r"_kj_pat='#\[(cfg\(test\)|test|tokio::test)\]|assert(_eq|_ne)?!'" "\n"
            "_kj_b=0; _kj_n=0\n"
            f"for _kf in $(git diff --name-only $_kj_base -- src 2>/dev/null | grep '\\.rs$'); do\n"
            "  _kj_b=$((_kj_b + $(git show \"$_kj_base:$_kf\" 2>/dev/null | grep -cE \"$_kj_pat\")))\n"
            "  [ -f \"$_kf\" ] && _kj_n=$((_kj_n + $(grep -cE \"$_kj_pat\" \"$_kf\" 2>/dev/null)))\n"
            "done\n"
            "if [ \"$_kj_n\" -lt \"$_kj_b\" ]; then echo \"CHEAT_DETECTED: in-src test markers/asserts dropped from $_kj_b to $_kj_n (test code removed)\" >> test_output.txt; fi",
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
