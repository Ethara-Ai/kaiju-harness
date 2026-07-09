"""Reward-hacking / injection hardening for the Go eval spec + test runner.

These guard the two batch-critical properties audited against the hardened Rust
baseline:

  * REVERT COMPLETENESS: the generated eval script resets EVERY Go test/build
    surface a model could tamper (test files, testdata, module graph, vendored
    deps, Makefile, lint/tooling config) so base tests can't be neutered without
    editing a scored (non-``_test.go``) source file.
  * INJECTION: neither the dataset ``test_cmd`` (spliced verbatim into the eval
    bash) nor ``test_ids`` (log-path only, defense-in-depth) can smuggle a second
    shell command or a dangerous ``go test`` flag (``-exec``/``-toolexec``).
"""

from __future__ import annotations

import pytest

from commit0.harness.spec_go import (
    _require_commitish,
    _require_safe_test_cmd,
    make_go_spec,
)
from commit0.harness.run_go_tests import _TEST_IDS_RE


def _script(test_cmd: str = "go test -json -count=1 ./...") -> str:
    inst = {
        "repo": "acme/widget",
        "reference_commit": "a" * 40,
        "base_commit": "b" * 40,
        "test": {"test_cmd": test_cmd},
    }
    return make_go_spec(inst, absolute=True).eval_script


# --------------------------------------------------------------------------- #
# Revert completeness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "target",
    [
        "*_test.go", "testdata/", "test/", "tests/",
        "Makefile", "go.mod", "go.sum", "go.work", "go.work.sum",
        "vendor/", ".golangci.yml", "tools.go",
    ],
)
def test_every_go_build_surface_is_reverted(target):
    sc = _script()
    assert f"git checkout {'b' * 40} -- {target}" in sc or \
        f"git checkout {'b' * 40} -- '{target}'" in sc, \
        f"{target} not reverted to base"


def test_model_added_test_and_work_files_are_deleted():
    sc = _script()
    # Added *_test.go (build-tagged or not) and go.work(.sum) are deleted, so a
    # model can't shadow a base test with a NEW file.
    assert "ls-files --others" in sc
    for g in ("'*_test.go'", "'**/*_test.go'", "go.work", "go.work.sum"):
        assert g in sc


def test_vendor_cheat_guard_present():
    sc = _script()
    assert "CHEAT-GUARD: vendor/ still differs from base" in sc


# --------------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------------- #
def test_go_test_and_outer_timeouts_present_and_generous():
    sc = _script()
    assert 'timeout --kill-after=10 "${EVAL_TEST_TIMEOUT:-600}"' in sc
    assert "-timeout ${GO_TEST_TIMEOUT:-600s}" in sc


def test_timeout_not_double_injected_when_test_cmd_has_own():
    sc = _script("go test -timeout 30s ./...")
    assert sc.count("-timeout") == 1  # no second GO_TEST_TIMEOUT splice
    # non-`go test` commands (e.g. a make shim) get no go-level -timeout
    sc2 = _script("make test")
    assert "GO_TEST_TIMEOUT" not in sc2


# --------------------------------------------------------------------------- #
# test_cmd injection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cmd",
    [
        "go test ./...; rm -rf /",
        "go test ./...\nrm -rf /x",
        "go test `id`",
        "go test $(id)",
        "go test ./... && curl evil",
        "go test ./... > /etc/passwd",
        "go test ./... &",
        "go test -exec 'sh -c id' ./...",
        "go test -exec=/bin/sh ./...",
        "go test -toolexec=/bin/sh ./...",
        "go test --toolexec /bin/sh ./...",
        # link/compile-time influence & arbitrary-tool vectors — `-ldflags -X`
        # can overwrite impl string vars (score without solving), `-gcflags` /
        # `-vettool` point the tool chain at an arbitrary binary.
        "go test -ldflags=-X pkg.Var=x ./...",
        'go test -ldflags "-X main.version=1" ./...',
        "go test -gcflags=-m ./...",
        "go test -vettool=/bin/sh ./...",
        "go test ${IFS}./...",
    ],
)
def test_malicious_test_cmd_is_rejected(cmd):
    with pytest.raises(ValueError):
        _require_safe_test_cmd(cmd)
    with pytest.raises(ValueError):
        _script(cmd)  # blocks eval_script construction end-to-end


@pytest.mark.parametrize(
    "cmd",
    [
        "go test -json -count=1 ./...",
        "go test ./pkg/... -run TestFoo",
        'go test -run "TestA|TestB" ./...',
        "go test -run 'TestA|TestB' ./...",
        "make test",
        # package paths that merely CONTAIN a forbidden-flag substring must not
        # be rejected — the guard matches dash-prefixed flag TOKENS, not any
        # substring.
        "go test -count=1 ./execpkg/...",
        "go test ./cmd/executor/...",
    ],
)
def test_legitimate_test_cmd_is_allowed(cmd):
    assert _require_safe_test_cmd(cmd) == cmd


# --------------------------------------------------------------------------- #
# commit-ish injection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["deadbeef; rm -rf /", "main", "HEAD~1", "", "x" * 4])
def test_non_sha_commitish_rejected(bad):
    with pytest.raises(ValueError):
        _require_commitish(bad, "base_commit")


def test_valid_sha_commitish_accepted():
    assert _require_commitish("A1b2C3d4", "base_commit") == "A1b2C3d4"


# --------------------------------------------------------------------------- #
# test_ids defense-in-depth guard (log-path only, but mirrors rust)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "tid", ["foo\nrm -rf /x", "foo; id", "foo`id`", "foo$(id)", "a|b", "a&b"]
)
def test_test_ids_regex_rejects_shell_metachars(tid):
    assert not _TEST_IDS_RE.match(tid)


@pytest.mark.parametrize("tid", ["", "pkg/TestFoo", "./...", "TestA,TestB", "a::b"])
def test_test_ids_regex_allows_legit(tid):
    assert _TEST_IDS_RE.match(tid)
