"""Unit tests for commit0.harness.spec_go."""

from __future__ import annotations

import pytest

from commit0.harness.spec_go import (
    Commit0GoSpec,
    _COMMITISH_RE,
    _TEST_CMD_FORBIDDEN,
    _TEST_CMD_RE,
    _require_commitish,
    _require_safe_test_cmd,
    make_go_spec,
)


class TestCommitishRegex:
    @pytest.mark.parametrize(
        "sha",
        [
            "abcd",  # 4 hex chars (min)
            "1234567",  # 7 hex chars
            "0123456789abcdef" * 4,  # 64 hex chars (max)
            "ABCDEF01",  # uppercase permitted
            "9d8f7e6a5b4c3d2e1f0a",  # 20 char abbrev
        ],
    )
    def test_accepts_valid_sha(self, sha: str) -> None:
        """Test that _COMMITISH_RE matches valid git SHAs (4-64 hex chars)."""
        assert _COMMITISH_RE.match(sha)

    @pytest.mark.parametrize(
        "bad",
        [
            "abc",  # 3 chars: too short
            "0" * 65,  # too long
            "ghijklmn",  # non-hex
            "abcd; rm -rf /",  # shell injection
            "abcd def",  # space
            "",
            "master",  # ref name, not SHA
            "HEAD~1",
        ],
    )
    def test_rejects_invalid(self, bad: str) -> None:
        """Test that _COMMITISH_RE rejects non-SHA inputs (injection defense)."""
        assert not _COMMITISH_RE.match(bad)


class TestRequireCommitish:
    def test_returns_stripped_sha(self) -> None:
        """Test that _require_commitish strips whitespace and returns the SHA."""
        assert _require_commitish("  abcd  ", "field") == "abcd"

    def test_raises_on_ref_name(self) -> None:
        """Test that a non-SHA branch name raises ValueError with the field."""
        with pytest.raises(ValueError, match="field"):
            _require_commitish("main", "field")

    def test_raises_on_shell_injection(self) -> None:
        """Test that a value containing shell metacharacters is rejected."""
        with pytest.raises(ValueError):
            _require_commitish("abcd; rm -rf /", "base_commit")

    def test_raises_on_non_string(self) -> None:
        """Test that a non-string value raises ValueError."""
        with pytest.raises(ValueError):
            _require_commitish(12345, "field")  # type: ignore[arg-type]

    def test_raises_on_empty(self) -> None:
        """Test that an empty string is rejected."""
        with pytest.raises(ValueError):
            _require_commitish("", "field")


class TestTestCmdForbidden:
    def test_is_tuple(self) -> None:
        """Test that _TEST_CMD_FORBIDDEN is an immutable tuple."""
        assert isinstance(_TEST_CMD_FORBIDDEN, tuple)

    def test_contains_shell_command_separators(self) -> None:
        """Test that ; & backtick $( ${ redirects are all forbidden."""
        for token in ("\n", "\r", ";", "`", "$(", "${", "&", "<", ">"):
            assert token in _TEST_CMD_FORBIDDEN

    def test_pipe_intentionally_allowed(self) -> None:
        """Test that pipe (|) is NOT forbidden — go test -run uses regex OR."""
        assert "|" not in _TEST_CMD_FORBIDDEN


class TestTestCmdRegex:
    @pytest.mark.parametrize(
        "cmd",
        [
            "go test -count=1 ./...",
            "go test -json ./pkg/...",
            "go test -run 'TestA|TestB' ./pkg",
            "make test",
            "./run.sh",
            "go test -tags=integration ./...",
        ],
    )
    def test_accepts_valid(self, cmd: str) -> None:
        """Test that legitimate go test commands match _TEST_CMD_RE."""
        assert _TEST_CMD_RE.match(cmd)


class TestRequireSafeTestCmd:
    def test_accepts_plain_go_test(self) -> None:
        """Test that a plain 'go test ./...' passes."""
        assert _require_safe_test_cmd("go test ./...") == "go test ./..."

    def test_accepts_regex_pipe(self) -> None:
        """Test that -run 'TestA|TestB' is accepted (pipe not a chain here)."""
        cmd = "go test -run 'TestA|TestB' -count=1 ./pkg"
        assert _require_safe_test_cmd(cmd) == cmd

    @pytest.mark.parametrize(
        "unsafe",
        [
            "go test; rm -rf /",
            "go test\nrm -rf /",
            "go test && echo pwned",
            "go test `id`",
            "go test $(whoami)",
            "go test ${HOME}",
            "go test > /etc/passwd",
            "go test < /etc/shadow",
        ],
    )
    def test_rejects_shell_metachars(self, unsafe: str) -> None:
        """Test that shell metachars raise ValueError."""
        with pytest.raises(ValueError, match="unsafe characters"):
            _require_safe_test_cmd(unsafe)

    @pytest.mark.parametrize(
        "flag_cmd",
        [
            "go test -exec /bin/pwned ./...",
            "go test --exec=/bin/pwned ./...",
            "go test -toolexec /bin/x ./...",
            "go test -ldflags '-X pkg.Var=cheated' ./...",
            "go test -gcflags 'all=-N -l' ./...",
            "go test -vettool /bin/x ./...",
        ],
    )
    def test_rejects_forbidden_flags(self, flag_cmd: str) -> None:
        """Test that -exec/-toolexec/-ldflags/-gcflags/-vettool are rejected."""
        with pytest.raises(ValueError, match="forbidden go flag"):
            _require_safe_test_cmd(flag_cmd)

    def test_accepts_substring_containing_exec(self) -> None:
        """Test that a package path containing 'exec' as substring is OK."""
        # `-run` value/package path containing 'exec' is not the forbidden flag
        cmd = "go test -run TestExecutor ./cmd/executor"
        assert _require_safe_test_cmd(cmd) == cmd

    def test_rejects_non_string(self) -> None:
        """Test that a non-string test_cmd raises ValueError."""
        with pytest.raises(ValueError):
            _require_safe_test_cmd(None)  # type: ignore[arg-type]


class TestCommit0GoSpec:
    def _instance(self) -> dict:
        return {
            "repo": "example/repo",
            "base_commit": "aabb1122",
            "reference_commit": "ccdd3344",
            "test": {"test_cmd": "go test -count=1 ./..."},
            "setup": {},
        }

    def test_base_image_key(self) -> None:
        """Test that base_image_key returns the version-pinned Go base image tag."""
        spec = make_go_spec(self._instance(), absolute=True)
        from commit0.harness.constants_go import GO_BASE_IMAGE_TAG
        assert spec.base_image_key == GO_BASE_IMAGE_TAG
        assert spec.base_image_key.startswith("commit0.base.go:")

    def test_make_go_spec_returns_commit0gospec(self) -> None:
        """Test that make_go_spec produces a Commit0GoSpec instance."""
        spec = make_go_spec(self._instance(), absolute=True)
        assert isinstance(spec, Commit0GoSpec)

    def test_make_go_spec_absolute_uses_absolute_repo_dir(self) -> None:
        """Test that absolute=True routes to ABSOLUTE_REPO_DIR."""
        from commit0.harness.constants import ABSOLUTE_REPO_DIR
        spec = make_go_spec(self._instance(), absolute=True)
        assert spec.repo_directory == ABSOLUTE_REPO_DIR

    def test_make_go_spec_relative_uses_relative_repo_dir(self) -> None:
        """Test that absolute=False routes to RELATIVE_REPO_DIR."""
        from commit0.harness.constants import RELATIVE_REPO_DIR
        spec = make_go_spec(self._instance(), absolute=False)
        assert spec.repo_directory == RELATIVE_REPO_DIR

    def test_repo_script_clones_and_resets(self) -> None:
        """Test that make_repo_script_list contains a clone + reset --hard."""
        spec = make_go_spec(self._instance(), absolute=True)
        cmds = spec.make_repo_script_list()
        joined = "\n".join(cmds)
        assert "git clone" in joined
        assert "git reset --hard aabb1122" in joined
        assert "git reset --hard ccdd3344" in joined

    def test_repo_script_rejects_bad_base_commit(self) -> None:
        """Test that an injection-shaped base_commit fails during script build."""
        inst = self._instance()
        inst["base_commit"] = "abc; rm -rf /"
        spec = make_go_spec(inst, absolute=True)
        with pytest.raises(ValueError):
            spec.make_repo_script_list()

    def test_eval_script_rejects_bad_test_cmd(self) -> None:
        """Test that an unsafe test_cmd fails during eval script build."""
        inst = self._instance()
        inst["test"] = {"test_cmd": "go test; rm -rf /"}
        spec = make_go_spec(inst, absolute=True)
        with pytest.raises(ValueError):
            spec.make_eval_script_list()

    def test_eval_script_default_test_cmd(self) -> None:
        """Test that a missing test_cmd falls back to the default go test invocation."""
        inst = self._instance()
        inst["test"] = {}
        spec = make_go_spec(inst, absolute=True)
        script = "\n".join(spec.make_eval_script_list())
        assert "go test" in script

    def test_eval_script_injects_timeout(self) -> None:
        """Test that -timeout is injected when the test_cmd is a go test without one."""
        spec = make_go_spec(self._instance(), absolute=True)
        script = "\n".join(spec.make_eval_script_list())
        assert "-timeout" in script

    def test_eval_script_does_not_double_timeout(self) -> None:
        """Test that a user-supplied -timeout is not overridden."""
        inst = self._instance()
        inst["test"] = {"test_cmd": "go test -timeout 30s ./..."}
        spec = make_go_spec(inst, absolute=True)
        script = "\n".join(spec.make_eval_script_list())
        # -timeout should appear exactly once (from the user cmd)
        assert script.count("-timeout") == 1

    def test_eval_script_wraps_with_timeout_binary(self) -> None:
        """Test that the eval script wraps the test invocation in coreutils timeout."""
        spec = make_go_spec(self._instance(), absolute=True)
        script = "\n".join(spec.make_eval_script_list())
        assert "timeout --kill-after=10" in script


class TestModuleExports:
    def test_all_exports(self) -> None:
        """Test that Commit0GoSpec and make_go_spec are exported via __all__."""
        import commit0.harness.spec_go as mod
        assert "Commit0GoSpec" in mod.__all__
        assert "make_go_spec" in mod.__all__
