"""Hostile input audit for commit0.harness.health_check_ts.

Focus: ``check_require`` must refuse to substitute adversarial package names
into a JavaScript ``require()`` string executed inside Docker. The module
uses an npm-name regex as the only guard; this file pins that guard down.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from commit0.harness.dockerfiles_ts import detect_ts_system_dependencies
from commit0.harness.health_check_ts import check_require


def _silent_client() -> MagicMock:
    client = MagicMock(spec=["containers"])
    client.containers.run.return_value = b""
    return client


# ---------------------------------------------------------------------------
# check_require: adversarial package names must be rejected by the regex
# BEFORE any container is launched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        # Shell / JS injection attempts
        'express"); process.exit(1); require("',
        "foo`whoami`",
        "foo$(id)",
        "foo;ls",
        "foo|cat /etc/passwd",
        "foo && rm -rf /",
        # Path traversal
        "../../../etc/passwd",
        "..",
        "./local-file",
        "/absolute/path",
        # Quote escape attempts
        'foo"',
        "foo'",
        'foo\\"',
        # Whitespace / control chars / newlines
        "",
        " ",
        "\t",
        "express\x00",
        # Unicode / non-ASCII (regex is explicit [a-z0-9...])
        "éxpress",
        "express\u200b",  # zero-width space
        "ＥＸＰＲＥＳＳ",  # fullwidth
        # Uppercase (npm allows but regex pins lowercase only — this is a
        # known constraint)
        "Express",
        "EXPRESS",
        # Leading invalid chars
        ".dotfile",
        "_leading-underscore",
        "@",  # bare scope sigil
        "@/pkg",  # empty scope name
        "@scope/",  # empty package part
        "@SCOPE/pkg",  # uppercase scope
        # Multiple slashes
        "a/b/c",
        "scope/pkg/extra",
        # 300-char name (long but should still fail if contains ".." pattern)
        "a" * 3 + "/" * 2 + "b" * 3,
    ],
)
def test_adversarial_package_name_rejected_before_container_launch(
    bad_name: str,
) -> None:
    client = _silent_client()
    ok, detail = check_require(client, "img:v1", bad_name)
    assert ok is False, f"expected rejection for {bad_name!r}"
    assert "Invalid package name" in detail
    # Hard invariant: the container must NEVER be launched with a bad name.
    client.containers.run.assert_not_called()


@pytest.mark.parametrize(
    "good_name",
    [
        "express",
        "lodash",
        "react-dom",
        "my-pkg-2",
        "a",  # single-char package names are valid per npm
        "@scope/pkg",
        "@my-org/my-pkg",
        "@a/b",
        "pkg.with.dots",
        "pkg_with_under",
        "~pkg",
        "foo~bar",
    ],
)
def test_valid_package_name_passes_regex_and_runs_container(good_name: str) -> None:
    client = _silent_client()
    ok, detail = check_require(client, "img:v1", good_name)
    assert ok is True, f"expected acceptance for {good_name!r}"
    assert good_name in detail
    # check_require now (a) runs a package-manager detection probe and (b) runs
    # the require.resolve() probe, so containers.run may be called more than
    # once (the detection result is cached per image, so the count varies with
    # cache state). The invariant is that the require probe DID launch: the
    # final call is the resolve probe carrying the package name.
    assert client.containers.run.called
    last_cmd = client.containers.run.call_args[0][1]
    assert "require.resolve" in last_cmd[-1]
    assert good_name in last_cmd[-1]


def test_types_scope_short_circuits_before_regex() -> None:
    """@types/ packages are skipped entirely — regex is never consulted."""
    client = _silent_client()
    # Even a payload-like @types/... name is accepted because we short-circuit.
    ok, detail = check_require(client, "img:v1", "@types/injected;rm -rf /")
    assert ok is True
    assert "Skipped" in detail
    client.containers.run.assert_not_called()


def test_trailing_newline_is_currently_accepted_hold_flag() -> None:
    client = _silent_client()
    ok, _ = check_require(client, "img:v1", "express\n")
    # Desired behaviour: the regex uses `fullmatch` and rejects trailing
    # whitespace/newline, so the package name is treated as invalid.
    assert ok is False


def test_require_string_has_no_quote_injection_for_valid_name() -> None:
    """For any valid name, the resulting JS must use safe_name verbatim.

    This verifies the package name is interpolated as the *whole* argument
    of require(), not re-escaped.
    """
    client = _silent_client()
    check_require(client, "img:v1", "express")
    cmd = client.containers.run.call_args[0][1]
    # check_require now emits an install probe built by _build_require_cmd:
    #   ["node", "-e", 'try{require.resolve("express")}catch(e){...}']
    # The security invariant is unchanged: the package name is interpolated
    # via json.dumps (see _build_require_cmd), so it appears as a single,
    # fully-quoted JS string literal argument to require.resolve() with no
    # quote/shell injection possible.
    import json as _json

    assert cmd[0] == "node"
    assert cmd[1] == "-e"
    # The name is embedded exactly as a JSON string literal — verbatim, escaped.
    assert _json.dumps("express") in cmd[2]
    assert cmd[2] == (
        'try{require.resolve("express")}'
        'catch(e){if(e&&e.code=="MODULE_NOT_FOUND"){throw e}}'
    )


# ---------------------------------------------------------------------------
# detect_ts_system_dependencies: parser edge cases
# ---------------------------------------------------------------------------


class TestDetectTsSystemDependenciesParser:
    """The name-extraction loop strips version pins and normalises case.

    These cases pin the parser behaviour so future refactors don't silently
    lose native-dep mappings.
    """

    @pytest.mark.parametrize(
        "spec,expected_deps",
        [
            # Version operators
            ("sharp@1.0.0", ["libvips-dev"]),
            ("sharp@^1.0.0", ["libvips-dev"]),
            ("sharp@~1.2.3", ["libvips-dev"]),
            ("sharp@>=1.0.0", ["libvips-dev"]),
            ("sharp=1.0.0", ["libvips-dev"]),
            # Scoped + version
            ("@scope/sharp@1.0.0", []),  # name becomes @scope/sharp, not sharp
            # Case insensitivity
            ("SHARP", ["libvips-dev"]),
            ("Sharp", ["libvips-dev"]),
            ("sHaRp", ["libvips-dev"]),
            # Whitespace padding
            ("  sharp  ", ["libvips-dev"]),
            ("\tsharp\n", ["libvips-dev"]),
            # better-sqlite3 with version
            ("better-sqlite3@8.4.0", ["libsqlite3-dev"]),
            # Unknown package does not pollute
            ("totally-fake-pkg", []),
            # Tilde prefix (valid npm char) on unknown name — not a version op
            ("~pkg", []),
        ],
    )
    def test_parses_to_expected_apt_deps(
        self, spec: str, expected_deps: list[str]
    ) -> None:
        assert detect_ts_system_dependencies([spec]) == expected_deps

    def test_types_packages_skipped_regardless_of_trailing_junk(self) -> None:
        # @types/* are skipped even if the trailing content looks bad.
        assert detect_ts_system_dependencies(["@types/sharp@1.0.0"]) == []

    def test_base_apt_packages_filtered_out(self) -> None:
        """A pre_install package that happens to be in the base set is dropped."""
        # Fabricate a mapping that leaks a base package — then confirm filter.
        # (We test via a package that legitimately references libsqlite3-dev.)
        deps = detect_ts_system_dependencies(["sqlite3"])
        # None of the base-apt packages may appear
        assert "git" not in deps
        assert "build-essential" not in deps
        assert "curl" not in deps

    def test_multiple_packages_deduplicated_and_sorted(self) -> None:
        deps = detect_ts_system_dependencies(
            ["sqlite3", "better-sqlite3", "sqlite3@1.0.0"]
        )
        assert deps == ["libsqlite3-dev"]

    def test_canvas_multiple_deps_sorted(self) -> None:
        deps = detect_ts_system_dependencies(["canvas"])
        assert deps == sorted(deps)
        assert "libcairo2-dev" in deps

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "   ",
            "@",
            "@@",
            "@/",
            "/",
            "pkg@",
            "@scope/pkg@@@1.0",
        ],
    )
    def test_garbage_does_not_crash(self, garbage: str) -> None:
        """Parser must never raise on malformed input; returns [] at worst."""
        result = detect_ts_system_dependencies([garbage])
        assert isinstance(result, list)

    def test_empty_input_list_returns_empty(self) -> None:
        assert detect_ts_system_dependencies([]) == []
