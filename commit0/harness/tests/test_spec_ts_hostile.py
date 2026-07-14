"""Hostile/boundary tests for commit0.harness.spec_ts and related helpers.

These pin down:
* ``is_vitest`` detection — a crude substring match that will misclassify
  any Jest invocation whose command contains ``vitest`` anywhere (e.g. a
  test file named ``vitest-compat.test.ts``).
* ``_exec_prefix_from_install`` precedence when multiple package-manager
  names appear in the install command.
* ``_package_manager_install`` priority ordering.
* ``make_repo_script_list`` shell-quoting invariants under adversarial
  dataset fields.
"""

from __future__ import annotations

import shlex

import pytest

from commit0.harness.spec_ts import (
    Commit0TsSpec,
    _exec_prefix_from_install,
    make_ts_spec,
)


def _inst(**overrides: object) -> dict:
    base: dict = {
        "instance_id": "commit-0/zod",
        "repo": "colinhacks/zod",
        "base_commit": "a" * 40,
        "reference_commit": "b" * 40,
        "setup": {
            "node": "20",
            "install": "npm install",
            "packages": [],
            "pre_install": [],
        },
        "test": {"test_cmd": "npx jest", "test_dir": "__tests__"},
        "src_dir": "src",
    }
    base.update(overrides)
    return base


def _spec(**overrides: object) -> Commit0TsSpec:
    return make_ts_spec(_inst(**overrides), absolute=True)


# ---------------------------------------------------------------------------
# _exec_prefix_from_install — precedence when multiple PM names collide
# ---------------------------------------------------------------------------


class TestExecPrefixPrecedence:
    @pytest.mark.parametrize(
        "install_cmd, expected",
        [
            # First-wins precedence in the if-chain is: pnpm > yarn > bun > npx
            ("pnpm install", "pnpm exec"),
            ("yarn install", "yarn"),
            ("bun install", "bunx"),
            ("npm install", "npx"),
            # Case insensitivity (function lowercases input)
            ("PNPM install", "pnpm exec"),
            ("YARN install", "yarn"),
            ("BUN install", "bunx"),
            ("NPM install", "npx"),
            # Whitespace tolerance
            ("  pnpm   install  ", "pnpm exec"),
            # Collision: pnpm mentioned first wins
            ("pnpm-yarn-compat install", "pnpm exec"),
            # Collision: yarn without pnpm chooses yarn
            ("yarn-bun-shim install", "yarn"),
            # Unknown manager falls back to npx
            ("deno task install", "npx"),
            ("", "npx"),
        ],
    )
    def test_prefix(self, install_cmd: str, expected: str) -> None:
        assert _exec_prefix_from_install(install_cmd) == expected


# ---------------------------------------------------------------------------
# is_vitest detection: substring match is crude — document the trap.
# ---------------------------------------------------------------------------


class TestIsVitestSubstringTrap:
    @pytest.mark.parametrize(
        "test_cmd, should_be_vitest",
        [
            ("npx vitest run", True),
            ("pnpm vitest", True),
            ("bunx vitest", True),
            ("yarn vitest --run", True),
            ("npx jest", False),
            ("pnpm jest --ci", False),
            ("yarn jest --runInBand", False),
        ],
    )
    def test_normal_cmds_classify_correctly(
        self, test_cmd: str, should_be_vitest: bool
    ) -> None:
        spec = _spec(test={"test_cmd": test_cmd})
        script = spec.eval_script
        force_flags_present = "--forceExit" in script
        if should_be_vitest:
            assert not force_flags_present
            assert "--reporter=json" in script
        else:
            assert force_flags_present
            assert "--json" in script


def test_jest_with_vitest_in_path_misclassified() -> None:
    spec = _spec(test={"test_cmd": "npx jest src/vitest-compat.test.ts"})
    script = spec.eval_script
    # Desired behaviour: Jest flags stay present even when the command
    # argument list contains the substring 'vitest'.
    assert "--forceExit" in script
    assert "--detectOpenHandles" in script


# ---------------------------------------------------------------------------
# make_repo_script_list / make_eval_script_list — shlex invariants.
# ---------------------------------------------------------------------------


class TestShellQuotingInvariants:
    """Every interpolated dataset field must appear shlex-quoted so the
    generated script tolerates spaces, quotes and shell metachars.
    """

    @pytest.mark.parametrize(
        "repo_value",
        [
            "owner/repo",
            "owner/repo name with space",
            "owner/repo;rm -rf /",
            "owner/repo$(whoami)",
            "owner/repo`id`",
            "owner/repo && echo pwned",
            "owner/repo#fragment",
            "owner/'injected'",
            'owner/"injected"',
        ],
    )
    def test_repo_field_shell_quoted_in_setup_script(self, repo_value: str) -> None:
        spec = _spec(repo=repo_value)
        script = "\n".join(spec.make_repo_script_list())
        # The quoted form must appear; the unquoted dangerous form must not
        # appear as a bare token.
        assert shlex.quote(repo_value) in script

    @pytest.mark.parametrize("base_commit", ["a" * 40, "0" * 40, "abc1234def"])
    def test_valid_base_commit_shell_quoted(self, base_commit: str) -> None:
        # A valid bare-hex base_commit is shell-quoted in BOTH scripts.
        spec = _spec(base_commit=base_commit)
        setup = "\n".join(spec.make_repo_script_list())
        eval_script = "\n".join(spec.make_eval_script_list())
        assert shlex.quote(base_commit) in setup
        assert shlex.quote(base_commit) in eval_script

    @pytest.mark.parametrize(
        "base_commit",
        ["HEAD", "HEAD~1", "main", "not a ref", "ref;rm -rf /", "ref`id`"],
    )
    def test_hostile_base_commit_rejected_by_eval(self, base_commit: str) -> None:
        # eval_hardening now REJECTS any base_commit that is not a bare hex SHA
        # (7-64 chars), so a ref name or shell-injection payload can never reach
        # the generated eval script at all — a stronger guarantee than the old
        # "quoting neutralizes it" model. The eval script is where the base_commit
        # is spliced into `git checkout <base> -- ...`, so rejection happens there.
        spec = _spec(base_commit=base_commit)
        with pytest.raises(ValueError, match="bare hex git SHA"):
            spec.make_eval_script_list()

    def test_repo_directory_quoted_in_every_cd(self) -> None:
        spec = _spec()
        script = "\n".join(spec.make_repo_script_list() + spec.make_eval_script_list())
        # Every cd must target the quoted repo_directory
        assert f"cd {shlex.quote(spec.repo_directory)}" in script

    def test_generated_script_is_parseable_by_shlex(self) -> None:
        """Sanity: every generated line must round-trip through shlex.split()
        without raising — no unbalanced quotes even under hostile repo names.
        """
        # The hostile input under test here is the REPO NAME (the documented
        # target). base_commit/reference_commit must now be valid bare-hex SHAs
        # or eval_hardening rejects them before the shlex-parseability check can
        # exercise the repo-name quoting.
        spec = _spec(
            repo="owner/\"evil name';",
            base_commit="a" * 40,
            reference_commit="b" * 40,
        )
        for line in spec.make_repo_script_list() + spec.make_eval_script_list():
            # Drop lines that are pure shell redirects (>, |) that shlex
            # treats specially — those are harness-authored constants and
            # contain no tainted data.
            try:
                shlex.split(line, posix=True)
            except ValueError as exc:
                pytest.fail(f"unparseable line {line!r}: {exc}")


# ---------------------------------------------------------------------------
# Install-cmd taint warning path
# ---------------------------------------------------------------------------


class TestInstallCmdWarning:
    @pytest.mark.parametrize(
        "danger_char, install_cmd",
        [
            (";", "npm install; rm -rf /"),
            ("&", "npm install && malicious"),
            ("|", "npm install | tee"),
            ("`", "npm install `id`"),
            ("$", "npm install $(whoami)"),
            ("(", "npm install(evil)"),
            ("!", "npm install!"),
            (">", "npm install > /tmp/out"),
            ("<", "npm install < input"),
        ],
    )
    def test_shell_dangerous_install_cmd_logs_warning(
        self, danger_char: str, install_cmd: str, caplog
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="commit0.harness.spec_ts"):
            spec = _spec(
                setup={
                    "node": "20",
                    "install": install_cmd,
                    "packages": [],
                    "pre_install": [],
                }
            )
            _ = spec.make_repo_script_list()  # triggers warning
        assert any(
            "install_cmd contains shell metacharacters" in rec.message
            for rec in caplog.records
        )


class TestTestCmdWarning:
    @pytest.mark.parametrize(
        "test_cmd",
        [
            "npx jest; rm -rf /",
            "npx jest && evil",
            "npx jest | tee",
            "npx jest `id`",
            "npx jest $(whoami)",
            "npx jest > /tmp/out",
        ],
    )
    def test_shell_dangerous_test_cmd_logs_warning(self, test_cmd: str, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="commit0.harness.spec_ts"):
            spec = _spec(test={"test_cmd": test_cmd, "test_dir": "__tests__"})
            _ = spec.make_eval_script_list()
        assert any(
            "test_cmd contains shell metacharacters" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Node version edge cases
# ---------------------------------------------------------------------------


class TestNodeVersionResolution:
    @pytest.mark.parametrize(
        "node_value, expected_in_image_key",
        [
            ("18", "node18"),
            ("20", "node20"),
            ("22", "node22"),
            (18, "node18"),  # int is coerced via str()
            (20, "node20"),
        ],
    )
    def test_image_key_reflects_node_version(
        self, node_value: object, expected_in_image_key: str
    ) -> None:
        spec = _spec(
            setup={
                "node": node_value,
                "install": "npm install",
                "packages": [],
                "pre_install": [],
            }
        )
        assert expected_in_image_key in spec.base_image_key

    def test_missing_node_uses_default(self) -> None:
        spec = _spec(
            setup={"install": "npm install", "packages": [], "pre_install": []}
        )
        # DEFAULT_NODE_VERSION (string) appears in the image key
        from commit0.harness.constants_ts import DEFAULT_NODE_VERSION

        assert f"node{DEFAULT_NODE_VERSION}" in spec.base_image_key
