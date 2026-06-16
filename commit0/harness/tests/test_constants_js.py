from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.constants_js import (
    ALLOWED_APT_PACKAGES,
    CONTAINER_WORKDIR,
    DEFAULT_NODE_VERSION,
    JS_BASE_BRANCH,
    JS_DATASET_BRANCH,
    JS_GITIGNORE_ENTRIES,
    JS_SOURCE_EXTS,
    JS_SPLIT,
    JS_STUB_MARKER,
    JS_TEST_FILE_PATTERNS,
    RUN_JS_TEST_LOG_DIR,
    SUPPORTED_NODE_VERSIONS,
    SUPPORTED_PACKAGE_MANAGERS,
    SUPPORTED_TEST_FRAMEWORKS,
    JsLanguage,
    JsRepoInstance,
    resolve_js_split,
)


class TestBranches:
    def test_base_branch_is_commit0(self) -> None:
        assert JS_BASE_BRANCH == "commit0"

    def test_base_branch_has_no_hyphen(self) -> None:
        assert "-" not in JS_BASE_BRANCH

    def test_dataset_branch(self) -> None:
        assert JS_DATASET_BRANCH == "commit0_all"


class TestStubMarker:
    def test_exact_value(self) -> None:
        assert JS_STUB_MARKER == "// __COMMIT0_STUB__"

    def test_is_line_comment(self) -> None:
        assert JS_STUB_MARKER.startswith("//")


class TestNodeVersions:
    def test_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_NODE_VERSIONS, frozenset)

    def test_contains_20_and_22(self) -> None:
        assert SUPPORTED_NODE_VERSIONS == frozenset({20, 22})

    def test_default_is_in_supported(self) -> None:
        assert DEFAULT_NODE_VERSION in SUPPORTED_NODE_VERSIONS

    def test_default_is_20(self) -> None:
        assert DEFAULT_NODE_VERSION == 20

    def test_elements_are_ints(self) -> None:
        for v in SUPPORTED_NODE_VERSIONS:
            assert isinstance(v, int)


class TestPackageManagers:
    def test_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_PACKAGE_MANAGERS, frozenset)

    def test_exact_set(self) -> None:
        assert SUPPORTED_PACKAGE_MANAGERS == frozenset(
            {"npm", "pnpm", "yarn", "bun"}
        )

    @pytest.mark.parametrize("pm", ["npm", "pnpm", "yarn", "bun"])
    def test_each_supported(self, pm: str) -> None:
        assert pm in SUPPORTED_PACKAGE_MANAGERS


class TestTestFrameworks:
    def test_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_TEST_FRAMEWORKS, frozenset)

    def test_exact_set(self) -> None:
        assert SUPPORTED_TEST_FRAMEWORKS == frozenset(
            {"jest", "mocha", "vitest", "node_test"}
        )

    @pytest.mark.parametrize("fw", ["jest", "mocha", "vitest", "node_test"])
    def test_each_supported(self, fw: str) -> None:
        assert fw in SUPPORTED_TEST_FRAMEWORKS

    def test_node_test_uses_underscore_not_colon(self) -> None:
        assert "node_test" in SUPPORTED_TEST_FRAMEWORKS
        assert "node:test" not in SUPPORTED_TEST_FRAMEWORKS


class TestAllowedAptPackages:
    def test_is_frozenset(self) -> None:
        assert isinstance(ALLOWED_APT_PACKAGES, frozenset)

    def test_includes_baseline(self) -> None:
        for pkg in ("git", "ca-certificates", "build-essential"):
            assert pkg in ALLOWED_APT_PACKAGES

    def test_no_curl_wget(self) -> None:
        assert "curl" not in ALLOWED_APT_PACKAGES
        assert "wget" not in ALLOWED_APT_PACKAGES


class TestSourceExts:
    def test_contains_all_js_flavours(self) -> None:
        for ext in (".js", ".mjs", ".cjs", ".jsx"):
            assert ext in JS_SOURCE_EXTS

    def test_excludes_typescript(self) -> None:
        assert ".ts" not in JS_SOURCE_EXTS
        assert ".tsx" not in JS_SOURCE_EXTS


class TestTestFilePatterns:
    def test_includes_test_glob(self) -> None:
        assert any(p.endswith("*.test.js") for p in JS_TEST_FILE_PATTERNS)

    def test_includes_spec_glob(self) -> None:
        assert any(p.endswith("*.spec.js") for p in JS_TEST_FILE_PATTERNS)

    def test_includes_tests_dir(self) -> None:
        assert any("__tests__" in p for p in JS_TEST_FILE_PATTERNS)


class TestGitignoreEntries:
    def test_required_entries(self) -> None:
        for entry in ("node_modules/", "dist/", ".aider*", "logs/"):
            assert entry in JS_GITIGNORE_ENTRIES


class TestJsLanguage:
    def test_js_value(self) -> None:
        assert JsLanguage.JS == "js"

    def test_is_str_subclass(self) -> None:
        assert isinstance(JsLanguage.JS, str)


class TestJsRepoInstance:
    def test_typed_dict_total_false(self) -> None:
        assert JsRepoInstance.__total__ is False

    def test_construct_empty(self) -> None:
        inst: JsRepoInstance = JsRepoInstance()
        assert isinstance(inst, dict)

    def test_construct_with_fields(self) -> None:
        inst: JsRepoInstance = JsRepoInstance(
            instance_id="commit-0/p-queue",
            repo="sindresorhus/p-queue",
            base_commit="a" * 40,
            test_framework="jest",
            package_manager="npm",
            node_version=20,
        )
        assert inst["instance_id"] == "commit-0/p-queue"


class TestContainerWorkdir:
    def test_exact(self) -> None:
        assert CONTAINER_WORKDIR == "/testbed"


class TestLogDir:
    def test_is_path(self) -> None:
        assert isinstance(RUN_JS_TEST_LOG_DIR, Path)

    def test_includes_js(self) -> None:
        assert "js" in str(RUN_JS_TEST_LOG_DIR).lower()

    def test_resolves_to_absolute_when_consumed(self) -> None:
        resolved = RUN_JS_TEST_LOG_DIR.resolve()
        assert resolved.is_absolute()
        assert (RUN_JS_TEST_LOG_DIR / "x").resolve().is_absolute()


class TestResolveJsSplit:
    @pytest.mark.parametrize("split", ["tier1", "tier2", "all"])
    def test_returns_list_of_str(self, split: str) -> None:
        result = resolve_js_split(split)
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, str)

    def test_tier1_is_list(self) -> None:
        assert isinstance(resolve_js_split("tier1"), list)

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown JS split"):
            resolve_js_split("does_not_exist")

    def test_returns_underlying_list_reference(self) -> None:
        result = resolve_js_split("tier1")
        assert result is JS_SPLIT["tier1"]

    def test_mutation_via_returned_list_is_visible_on_next_call(self) -> None:
        original = list(JS_SPLIT["tier1"])
        try:
            result = resolve_js_split("tier1")
            result.append("evil/repo")
            again = resolve_js_split("tier1")
            assert "evil/repo" in again
            assert again is JS_SPLIT["tier1"]
        finally:
            JS_SPLIT["tier1"][:] = original

    def test_all_split_keys_resolvable(self) -> None:
        for k in JS_SPLIT:
            resolve_js_split(k)


class TestJsRepoInstanceMissingKeyAccess:
    def test_get_on_missing_instance_id_returns_none(self) -> None:
        inst: JsRepoInstance = JsRepoInstance(repo="sindresorhus/p-queue")
        assert inst.get("instance_id") is None

    def test_get_with_default_returns_default(self) -> None:
        inst: JsRepoInstance = JsRepoInstance(repo="sindresorhus/p-queue")
        assert inst.get("instance_id", "fallback") == "fallback"

    def test_empty_instance_get_does_not_raise(self) -> None:
        inst: JsRepoInstance = JsRepoInstance()
        assert inst.get("instance_id") is None
        assert inst.get("repo") is None
        assert inst.get("base_commit") is None

    def test_chained_get_mirrors_make_js_spec_fallback(self) -> None:
        inst: JsRepoInstance = JsRepoInstance(instance_id="commit-0/p-queue")
        resolved = inst.get("repo", inst.get("instance_id", ""))
        assert resolved == "commit-0/p-queue"
