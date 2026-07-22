from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TypedDict


JS_BASE_BRANCH: str = "commit0"
from commit0.harness.constants import REMOTE_BRANCH

JS_DATASET_BRANCH: str = REMOTE_BRANCH  # canonical alias; see commit0.harness.constants
JS_STUB_MARKER: str = "// __COMMIT0_STUB__"
# The Babel stubber co-emits BOTH the comment marker above AND this throw on
# separate lines (see tools/jsstubber/stub_js.ts buildStubBody). Detection must
# accept EITHER signal so a stub carrying only one of them (e.g. a hand-written
# throw, or a comment stripped by a formatter) is never silently treated as
# already-implemented and dropped from the agent's target-edit set.
JS_STUB_THROW: str = 'throw new Error("STUB")'

DEFAULT_NODE_VERSION: int = 20
CONTAINER_WORKDIR: str = "/testbed"

MAX_PATCH_BYTES: int = 10 * 1024 * 1024

JS_SOURCE_EXTS: tuple[str, ...] = (".js", ".mjs", ".cjs", ".jsx")

JS_TEST_FILE_PATTERNS: tuple[str, ...] = (
    "**/*.test.js", "**/*.test.mjs", "**/*.test.cjs", "**/*.test.jsx",
    "**/*.spec.js", "**/*.spec.mjs", "**/*.spec.cjs", "**/*.spec.jsx",
    "**/__tests__/**/*.js", "**/__tests__/**/*.mjs",
    "**/test/**/*.js", "**/tests/**/*.js",
)

# 18 (older libs, EOL upstream but still widely targeted), 20/22 (active LTS),
# 24 (current). Each has a matching Dockerfile.node<version>.
SUPPORTED_NODE_VERSIONS: frozenset[int] = frozenset({14, 16, 18, 20, 22, 24})

SUPPORTED_PACKAGE_MANAGERS: frozenset[str] = frozenset(
    {"npm", "pnpm", "yarn", "bun"}
)

SUPPORTED_TEST_FRAMEWORKS: frozenset[str] = frozenset(
    # ava runs with `ava --tap` and is parsed via the shared TAP parser (same as
    # node_test), so it needs no dedicated parser branch.
    {"jest", "mocha", "vitest", "node_test", "ava"}
)

JS_SHELL_METACHARS: frozenset[str] = frozenset(";&|`$(){}!><\n\r\t\\\"'")

JS_TEST_CMD_RUNNERS: frozenset[str] = frozenset(
    {"npx", "pnpm", "yarn", "bun", "bunx", "npm", "node"}
)

ALLOWED_APT_PACKAGES: frozenset[str] = frozenset({
    "git",
    "ca-certificates",
    "build-essential",
    "python3",
    "libssl-dev",
    "libffi-dev",
    "pkg-config",
})

RUN_JS_TEST_LOG_DIR = Path("logs/js_test")

JS_GITIGNORE_ENTRIES: list[str] = ["node_modules/", "dist/", ".aider*", "logs/"]


class JsLanguage(StrEnum):
    JS = "js"


class JsRepoInstance(TypedDict, total=False):
    instance_id: str
    repo: str
    base_commit: str
    test_framework: str
    package_manager: str
    node_version: int
    test_ids: list[str]
    stub_targets: list[str]


JS_SPLIT: dict[str, list[str]] = {
    "tier1": [],
    "tier2": [],
    "all": [],
}


def resolve_js_split(name: str) -> list[str]:
    if name not in JS_SPLIT:
        raise ValueError(f"unknown JS split: {name!r}; valid: {sorted(JS_SPLIT)}")
    return JS_SPLIT[name]
