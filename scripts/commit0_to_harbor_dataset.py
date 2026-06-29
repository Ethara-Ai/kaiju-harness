#!/usr/bin/env python3
"""commit0_to_harbor_dataset.py

Converts raw kaiju/commit0 task directories into Harbor TaskConfig v1.2 dataset packages.

Input layout (per task):
  <commit0_root>/<task>/
    <task>_dataset.json          # RepoInstance record (kaiju/commit0/harness/constants.py)
    specs/<repo_name>.pdf.bz2    # spec PDF (optional; repo_name = last segment of data["repo"])
    test-ids/<repo_name>.bz2     # pytest node-id list (required)

Output layout (per task):
  <output_root>/<task>/
    task.toml
    instruction.md
    tests/
      test.sh
      test_ids.txt
    solution/
      solve.sh

Provenance of every value emitted into task.toml:
  - Per-task values (instance_id, original_repo, base_commit, reference_commit,
    src_dir, python_version, n_test_ids, docker_image): extracted from the kaiju
    RepoInstance JSON or derived from observed files (n_test_ids = line count).
  - Constants (category, difficulty, keywords, timeouts, cpus, memory_mb, os,
    workdir, allow_internet, test_mode): HARBOR_TEMPLATE_DEFAULTS — operational
    Harbor convention, NOT derived from kaiju data. Same values are used for
    every commit0 task and match the existing reference packages in
    Harbor_Data/Dataset/.

Usage:
  # Single task
  python commit0_to_harbor_dataset.py \\
    --input Commit0_Data/apispec \\
    --output Harbor_Data/Dataset

  # All tasks under a root (bulk mode)
  python commit0_to_harbor_dataset.py \\
    --input Commit0_Data \\
    --output Harbor_Data/Dataset \\
    --bulk

  # Bulk, selected tasks only
  python commit0_to_harbor_dataset.py \\
    --input Commit0_Data \\
    --output Harbor_Data/Dataset \\
    --bulk --tasks apispec flask django-rest-framework

  # Overwrite existing output, abort on first failure
  python commit0_to_harbor_dataset.py \\
    --input Commit0_Data \\
    --output Harbor_Data/Dataset \\
    --bulk --overwrite --fail-fast
"""

import argparse
import bz2
import json
import os
import pathlib
import re
import shutil
import sys
import textwrap
from typing import Any

try:
    import fitz  # PyMuPDF
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False

try:
    from harbor.models.task.config import TaskConfig  # type: ignore[import]
    HAS_HARBOR = True
except ImportError:
    HAS_HARBOR = False


# ---------------------------------------------------------------------------
# Constants — derived from external schemas / pipeline conventions
# ---------------------------------------------------------------------------

ECR_BASE = "426628337772.dkr.ecr.ap-south-1.amazonaws.com/kaiju-q1-coding-base"
ECR_BASE_BY_LANG: dict[str, str] = {
    "python": ECR_BASE,
    "go":     "426628337772.dkr.ecr.ap-south-1.amazonaws.com/kaiju_q2",
    "rust":   "426628337772.dkr.ecr.ap-south-1.amazonaws.com/kaiju_q2",
}
MAX_SPEC_LENGTH = 10_000   # chars; matches commit0 agent_utils max_spec_info_length
SCHEMA_VERSION = "1.3"
SOURCE = "commit0"

# Harbor PackageInfo.name validator (org/name format).
# Source: harbor/src/harbor/constants.py ORG_NAME_PATTERN
ORG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Required top-level keys in kaiju RepoInstance JSON.
# Source: kaiju/tools/create_dataset.py REQUIRED_FIELDS
REQUIRED_INSTANCE_FIELDS = (
    "instance_id", "repo", "original_repo", "base_commit",
    "reference_commit", "setup", "test", "src_dir",
)
# Subset of setup.* fields we actually consume.
REQUIRED_SETUP_FIELDS = ("python",)  # legacy Python default; see REQUIRED_SETUP_FIELDS_BY_LANG
REQUIRED_TEST_FIELDS = ("test_cmd", "test_dir")

# Language-aware required setup fields. Dataset JSON exposes `language` (default
# `"python"` if absent for backward-compat). Go datasets carry `go_version`
# instead of `python`.
REQUIRED_SETUP_FIELDS_BY_LANG: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "go": ("go_version",),
    "rust": ("rust_version",),
}


# ---------------------------------------------------------------------------
# Harbor template defaults
#
# These values are NOT derived from kaiju RepoInstance data. They are Harbor's
# operational conventions for commit0 tasks: uniform timeout policy, uniform
# resource allocation, uniform metadata tags. Every commit0 task in the existing
# Harbor_Data/Dataset/ reference set uses these exact values.
#
# To vary any of these per-task, either (a) source the value from the dataset
# JSON, or (b) add a CLI override. Do NOT silently change a default here without
# updating the reference packages — downstream parity verification depends on
# byte-for-byte stability.
# ---------------------------------------------------------------------------

HARBOR_TEMPLATE_DEFAULTS: dict[str, Any] = {
    # [task]
    "keywords": ["commit0", "code-generation", "python", "swe"],

    # [metadata]
    "category": "code-generation",
    "difficulty": "hard",
    "test_mode": "official_test_ids",

    # [agent] / [verifier]
    "agent_timeout_sec": 1800.0,
    "verifier_timeout_sec": 1800.0,

    # [environment]
    "env_build_timeout_sec": 1800.0,
    "env_os": "linux",
    "env_cpus": 2,
    "env_memory_mb": 4096,
    "env_network_mode": "public",
    "env_workdir": "/testbed",
}

HARBOR_TEMPLATE_DEFAULTS_GO: dict[str, Any] = {
    **HARBOR_TEMPLATE_DEFAULTS,
    "keywords": ["commit0", "code-generation", "go", "swe"],
}

HARBOR_TEMPLATE_DEFAULTS_RUST: dict[str, Any] = {
    **HARBOR_TEMPLATE_DEFAULTS,
    "keywords": ["commit0", "code-generation", "rust", "swe"],
}

HARBOR_TEMPLATE_DEFAULTS_BY_LANG: dict[str, dict[str, Any]] = {
    "python": HARBOR_TEMPLATE_DEFAULTS,
    "go": HARBOR_TEMPLATE_DEFAULTS_GO,
    "rust": HARBOR_TEMPLATE_DEFAULTS_RUST,
}

# Per-task ECR tag overrides for the ~19/300 commit0 tasks where the simple
# `instance_id.split('/')[-1]` derivation does NOT match the live ECR tag (due
# to dash/underscore/case normalization). Populate from the live ECR list:
#   aws ecr list-images --repository-name kaiju-q1-coding-base
# Keys = full instance_id ("commit-0/<name>"); values = correct ECR image tag.
ECR_TAG_OVERRIDES: dict[str, str] = {}


# ---------------------------------------------------------------------------
# File templates
# ---------------------------------------------------------------------------

# task.toml — Harbor TaskConfig v1.2
TASK_TOML_TEMPLATE = """\
schema_version = "{schema_version}"
source = "{source}"

[task]
name = "{harbor_name}"
description = "{description}"
keywords = {keywords}

[metadata]
category = "{category}"
difficulty = "{difficulty}"
original_repo = "{original_repo}"
instance_id = "{instance_id}"
base_commit = "{base_commit}"
reference_commit = "{reference_commit}"
src_dir = "{src_dir}"
{version_line}
test_mode = "{test_mode}"
n_test_ids = {n_test_ids}

[agent]
timeout_sec = {agent_timeout_sec}

[verifier]
timeout_sec = {verifier_timeout_sec}

[environment]
docker_image = "{docker_image}"
build_timeout_sec = {env_build_timeout_sec}
os = "{env_os}"
cpus = {env_cpus}
memory_mb = {env_memory_mb}
network_mode = "{env_network_mode}"
workdir = "{env_workdir}"
"""

# instruction.md — agent prompt: header + repo details + constraint + spec text
INSTRUCTION_TEMPLATE = """\
# Implement `{original_repo}`

You are given a Python repository at `/testbed`, reset to a skeleton commit: every function body has been replaced with a `pass` statement.

You need to complete the implementations for all functions (i.e., those with `pass` statements) and pass the unit tests.
Do not change the names of existing functions or classes, as they may be referenced from other code like unit tests, etc.
When you generate code, you must maintain the original formatting of the original function stubs (such as whitespaces), otherwise we will not be able to search/replace blocks for code modifications, and therefore you will receive a score of 0 for your generated code.

## Repository details

- Upstream project: `{original_repo}`
- Source directory to implement: `{src_dir}/`
- Test command: `{test_cmd}` (run against `{test_dir}`)
- Specification / docs: {specification_url}

Implement only the library source under the source directory. Do not modify the test files.

>>> Here is the Specification Information:

{spec_text}
"""

INSTRUCTION_TEMPLATE_GO = """\
# Implement `{original_repo}`

You are given a Go repository at `/testbed`, reset to a skeleton commit: every function body has been replaced with a stub that panics on call.

You need to complete the implementations for all functions and pass the unit tests.
Do not change the names of existing functions, types, or methods, as they may be referenced from other code like unit tests, etc.
When you generate code, you must maintain the original formatting of the original function stubs (such as whitespaces), otherwise we will not be able to search/replace blocks for code modifications, and therefore you will receive a score of 0 for your generated code.

## Repository details

- Upstream project: `{original_repo}`
- Source directory to implement: `{src_dir}/`
- Test command: `{test_cmd}` (run against `{test_dir}`)
- Specification / docs: {specification_url}

Implement only the library source under the source directory. Do not modify the test files.

>>> Here is the Specification Information:

{spec_text}
"""

INSTRUCTION_TEMPLATE_RUST = """\
# Implement `{original_repo}`

You are given a Rust repository at `/testbed`, reset to a skeleton commit: every function body has been replaced with a stub that calls `todo!()` (or `unimplemented!()`) on invocation.

You need to complete the implementations for all functions and pass the unit tests.
Do not change the names of existing functions, types, traits, or methods, as they may be referenced from other code like unit tests, etc.
When you generate code, you must maintain the original formatting of the original function stubs (such as whitespaces), otherwise we will not be able to search/replace blocks for code modifications, and therefore you will receive a score of 0 for your generated code.

## Repository details

- Upstream project: `{original_repo}`
- Source directory to implement: `{src_dir}/`
- Test command: `{test_cmd}` (run against `{test_dir}`)
- Specification / docs: {specification_url}

Implement only the library source under the source directory. Do not modify the test files.

>>> Here is the Specification Information:

{spec_text}
"""

INSTRUCTION_TEMPLATE_BY_LANG: dict[str, str] = {
    "python": INSTRUCTION_TEMPLATE,
    "go": INSTRUCTION_TEMPLATE_GO,
    "rust": INSTRUCTION_TEMPLATE_RUST,
}

# tests/test.sh — Harbor verifier (verbatim constant across all tasks).
# NOTE: pytest test IDs are passed via `$TEST_IDS` unquoted because commit0
# uses node IDs of the form `tests/<file>.py::<Class>::<method>` (no spaces).
# If a future task introduces parametrize markers containing literal spaces
# (e.g. `test_func[case 1]`), this line WILL split incorrectly. The fix is:
#   mapfile -t TEST_IDS < /tests/test_ids.txt
#   pytest "${TEST_IDS[@]}" ...
# Keeping the legacy form here preserves byte-for-byte parity with the
# existing reference packages; switch to the mapfile form once the reference
# is regenerated.
TEST_SH = r"""#!/bin/bash
# Harbor verifier for a commit0 task (runs inside the pre-built ECR image).
# Scope: the official commit0 test-id set (parity with pipeline_results).
# Writes a continuous reward (fraction passed) to /logs/verifier/reward.json.
#
# Lineage-defensive: commit0 images come in two flavors — an ubuntu image with the repo
# venv at /testbed/.venv, and a python-slim image using system site-packages. We activate
# the venv if it exists, else fall back to system python. We run pytest IN PLACE on the
# agent-edited tree (NOT commit0's reset+/patch.diff model — Harbor agents edit in place).
set -uo pipefail
mkdir -p /logs/verifier
cd /testbed

if [ -f /testbed/.venv/bin/activate ]; then
  source /testbed/.venv/bin/activate
fi

# Ensure pytest-json-report is available (don't upgrade pinned pytest/pytest-asyncio).
python -m pip install --quiet --no-cache-dir pytest-json-report >/dev/null 2>&1 || true

TEST_IDS="$(tr '\n' ' ' < /tests/test_ids.txt)"

pytest $TEST_IDS \
  --json-report --json-report-file=/logs/verifier/pytest_report.json \
  --continue-on-collection-errors \
  >/logs/verifier/pytest.log 2>&1 || true

python - <<'PY'
import json, pathlib
reward, passed, total = 0.0, 0, 0
try:
    r = json.loads(pathlib.Path('/logs/verifier/pytest_report.json').read_text())
    s = r.get('summary', {})
    total = s.get('total') or s.get('collected') or 0
    passed = s.get('passed', 0)
    reward = (passed / total) if total else 0.0
except Exception:
    reward = 0.0
resolved = 1 if (total and passed == total) else 0
pathlib.Path('/logs/verifier/reward.json').write_text(
    json.dumps({"reward": reward, "resolved": resolved,
                "passed": passed, "total": total}))
PY
"""

TEST_SH_GO = r"""#!/bin/bash
# Harbor verifier for a commit0 Go task (runs inside the pre-built image).
# Scope: the official commit0 test-id set (line-separated full IDs of the form
# `<package_path>/<TestName>`). Writes reward.json (fraction of expected IDs
# that passed). Run command mirrors commit0's Go evaluator: `go test -json
# -count=1 ./...`. We parse the JSONL action events, attribute pass/fail per
# test ID, and intersect against the expected set.
set -uo pipefail
mkdir -p /logs/verifier
cd /testbed

go test -json -count=1 ./... \
  > /logs/verifier/go_test_events.jsonl 2>/logs/verifier/go_test_stderr.log || true

python3 - <<'PY'
import json, pathlib
events = []
log = pathlib.Path('/logs/verifier/go_test_events.jsonl')
if log.exists():
    for line in log.read_text(errors='replace').splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
expected = {l.strip() for l in pathlib.Path('/tests/test_ids.txt').read_text().splitlines() if l.strip()}
per_test = {}
for e in events:
    action = e.get('Action')
    if action not in ('pass', 'fail', 'skip'):
        continue
    test = e.get('Test'); pkg = e.get('Package', '')
    if not test:
        continue
    full = f"{pkg}/{test}"
    if full in expected:
        per_test[full] = action
    elif test in expected:
        per_test[test] = action
total = len(expected)
passed = sum(1 for tid in expected if per_test.get(tid) == 'pass')
reward = (passed / total) if total else 0.0
resolved = 1 if (total and passed == total) else 0
pathlib.Path('/logs/verifier/reward.json').write_text(
    json.dumps({"reward": reward, "resolved": resolved,
                "passed": passed, "total": total}))
PY
"""

TEST_SH_RUST = r"""#!/bin/bash
# Harbor verifier for a commit0 Rust task (runs inside the pre-built image).
# Scope: the official commit0 test-id set (line-separated cargo test names of
# the form `<module_path>::<test_name>`). Writes reward.json (fraction of
# expected IDs that passed). Run command uses libtest's JSON formatter, which
# is unstable and requires `-Z unstable-options` under nightly. To stay on
# stable, we use `--format=pretty` and parse the standard `test <name> ... ok/FAILED`
# lines (cargo test's stable on-by-default format).
set -uo pipefail
mkdir -p /logs/verifier
cd /testbed

cargo test --no-fail-fast -- --format=pretty \
  > /logs/verifier/cargo_test.log 2>&1 || true

python3 - <<'PY'
import json, pathlib, re
log_path = pathlib.Path('/logs/verifier/cargo_test.log')
expected = {l.strip() for l in pathlib.Path('/tests/test_ids.txt').read_text().splitlines() if l.strip()}
results: dict[str, str] = {}
if log_path.exists():
    line_re = re.compile(r'^test (\S+) \.\.\. (ok|FAILED|ignored)\b')
    for line in log_path.read_text(errors='replace').splitlines():
        m = line_re.match(line)
        if not m:
            continue
        name, status = m.group(1), m.group(2)
        if name in expected:
            results[name] = status
total = len(expected)
passed = sum(1 for tid in expected if results.get(tid) == 'ok')
reward = (passed / total) if total else 0.0
resolved = 1 if (total and passed == total) else 0
pathlib.Path('/logs/verifier/reward.json').write_text(
    json.dumps({"reward": reward, "resolved": resolved,
                "passed": passed, "total": total}))
PY
"""

TEST_SH_BY_LANG: dict[str, str] = {
    "python": TEST_SH,
    "go": TEST_SH_GO,
    "rust": TEST_SH_RUST,
}

# solution/solve.sh — oracle: conditional fetch + reset to reference commit.
# `{{commit}}` -> literal `{commit}` (Python format-string escape).
SOLVE_SH_TEMPLATE = """\
#!/bin/bash
# Oracle solution for a commit0 task. The image ships the repo at the BASE (stubbed)
# commit only; the reference (solved) commit is fetched from the fork by SHA, then the
# working tree is reset to it. Needs internet access (the task sets allow_internet=true).
set -euo pipefail
cd /testbed
if ! git cat-file -e {reference_commit}^{{commit}} 2>/dev/null; then
  git fetch --depth 1 {fork_url} {reference_commit}
fi
git reset --hard {reference_commit}
echo "Reset to reference commit {reference_commit}"
"""

# Files that MUST exist in the output package (post-write completeness check).
REQUIRED_OUTPUT_FILES = (
    "task.toml",
    "instruction.md",
    "tests/test.sh",
    "tests/test_ids.txt",
    "solution/solve.sh",
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def escape_toml_string(value: str) -> str:
    """Escape a string for safe inclusion in a TOML basic-string literal.
    Escapes: backslash, double-quote, newline, carriage return, tab.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"escape_toml_string expects str, got {type(value).__name__}: {value!r}"
        )
    return (
        value
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def toml_string_list(items: list[str]) -> str:
    """Format a list[str] as a TOML inline array of basic strings, with escaping."""
    return "[" + ", ".join(f'"{escape_toml_string(s)}"' for s in items) + "]"


def validate_required_fields(data: dict, task_name: str, language: str = "python") -> None:
    """Validate the kaiju RepoInstance JSON has all fields the converter consumes.
    Raises ValueError with a clear message naming the missing field path.
    """
    missing = [f for f in REQUIRED_INSTANCE_FIELDS if f not in data]
    if missing:
        raise ValueError(
            f"task '{task_name}' is missing required top-level field(s): "
            f"{missing}. Required keys: {list(REQUIRED_INSTANCE_FIELDS)}"
        )
    setup = data.get("setup")
    if not isinstance(setup, dict):
        raise ValueError(
            f"task '{task_name}' has non-dict 'setup' field: "
            f"{type(setup).__name__ if setup is not None else 'None'}"
        )
    required_setup_fields = REQUIRED_SETUP_FIELDS_BY_LANG.get(language, REQUIRED_SETUP_FIELDS)
    missing_setup = [f for f in required_setup_fields if f not in setup]
    if missing_setup:
        raise ValueError(
            f"task '{task_name}' (language={language}) is missing required setup.<field>(s): "
            f"{missing_setup}"
        )
    test = data.get("test")
    if not isinstance(test, dict):
        raise ValueError(
            f"task '{task_name}' has non-dict 'test' field: "
            f"{type(test).__name__ if test is not None else 'None'}"
        )
    missing_test = [f for f in REQUIRED_TEST_FIELDS if f not in test]
    if missing_test:
        raise ValueError(
            f"task '{task_name}' is missing required test.<field>(s): "
            f"{missing_test}"
        )


def validate_instance_id(instance_id: str) -> None:
    """Validate that instance_id matches Harbor's PackageInfo.name regex.
    Harbor's TaskConfig.model_validate_toml will reject mismatches; we fail
    earlier here with a clear message.
    """
    if not isinstance(instance_id, str) or not ORG_NAME_PATTERN.match(instance_id):
        raise ValueError(
            f"instance_id '{instance_id}' does not match Harbor ORG_NAME_PATTERN "
            f"({ORG_NAME_PATTERN.pattern}). Must be 'org/name' with alphanumerics, "
            f"dots, dashes, underscores."
        )


def validate_fork_repo(fork_repo: str, task_name: str) -> None:
    """Validate data['repo'] is a plausible GitHub <org>/<name> path.
    Rejects empty strings, missing/extra slashes, shell-special characters.
    """
    if not fork_repo or not isinstance(fork_repo, str):
        raise ValueError(
            f"task '{task_name}' has empty/non-string repo field: {fork_repo!r}"
        )
    if fork_repo.count("/") != 1:
        raise ValueError(
            f"task '{task_name}' repo='{fork_repo}' must be 'org/name' format "
            f"(exactly one '/')."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", fork_repo):
        raise ValueError(
            f"task '{task_name}' repo='{fork_repo}' contains characters outside "
            f"the GitHub repo path whitelist [A-Za-z0-9_.-]."
        )


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def load_dataset_json(task_dir: pathlib.Path, task_name: str) -> dict[str, Any]:
    """Load <task>_dataset.json. Handles:
    - JSON array (canonical commit0 format) — returns first record (warns if >1)
    - Wrapped object {'entries' | 'instances' | 'data': [...]}
    - Bare JSON object — returned as-is
    """
    json_path = task_dir / f"{task_name}_dataset.json"
    if not json_path.exists():
        raise FileNotFoundError(f"Dataset JSON not found: {json_path}")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Dataset JSON at {json_path} is not valid JSON: {exc}") from exc

    # Unwrap common container shapes used by sibling tooling.
    if isinstance(data, dict):
        for key in ("entries", "instances", "data"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break

    if isinstance(data, list):
        if not data:
            raise ValueError(f"Empty dataset JSON array: {json_path}")
        if len(data) > 1:
            print(
                f"  [WARN] {task_name}: dataset JSON has {len(data)} records; "
                f"using only the first (commit0 convention is 1 record/file)."
            )
        data = data[0]

    if not isinstance(data, dict):
        raise ValueError(
            f"Dataset JSON at {json_path} is neither list nor object: "
            f"got {type(data).__name__}"
        )
    return data


def extract_spec_text(specs_dir: pathlib.Path, repo_name: str) -> str:
    """Decompress specs/<repo_name>.pdf.bz2 and extract text via PyMuPDF.
    Returns up to MAX_SPEC_LENGTH chars. Returns "" if:
      - the bz2 file does not exist
      - the bz2 archive is corrupt
      - the PDF cannot be opened (corrupt/truncated)
      - the PDF is password-encrypted

    Production hardening:
      - try/finally guarantees doc.close() (no fd leak on exceptions)
      - encrypted PDFs are detected via doc.needs_pass
      - per-page get_text() failures are isolated (don't abort the rest)
      - early break once MAX_SPEC_LENGTH chars are collected

    Raises ImportError only if PyMuPDF is missing AND a spec file is present.
    """
    pdf_bz2 = specs_dir / f"{repo_name}.pdf.bz2"
    if not pdf_bz2.exists():
        return ""
    if not HAS_FITZ:
        raise ImportError(
            "PyMuPDF (fitz) is required for spec extraction. "
            "Install with:  pip install pymupdf\n"
            "Or pass --skip-spec to omit the spec section."
        )

    try:
        pdf_bytes = bz2.decompress(pdf_bz2.read_bytes())
    except (OSError, EOFError, ValueError) as exc:
        print(f"  [WARN] {repo_name}: corrupt bz2 archive ({exc}); spec section will be empty")
        return ""

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        print(f"  [WARN] {repo_name}: PyMuPDF failed to open PDF ({exc}); spec section will be empty")
        return ""

    try:
        if doc.needs_pass:
            print(f"  [WARN] {repo_name}: spec PDF is password-encrypted; spec section will be empty")
            return ""

        parts: list[str] = []
        for page in doc:
            try:
                chunk = page.get_text()
            except Exception as exc:
                print(f"  [WARN] {repo_name}: failed to extract a page ({exc}); skipping")
                continue
            parts.append(chunk)
        return _clean_spec_text("".join(parts), MAX_SPEC_LENGTH)
    finally:
        doc.close()


_NAV_CRUFT_RES = (
    re.compile(r"^\s*Search\s*$"),
    re.compile(r"^\s*Summary\s*$"),
    re.compile(r"^\s*Read more\s*$"),
    re.compile(r"^\s*List of all items\s*$"),
    re.compile(r"^\s*Auto Trait Implementations\s*$"),
    re.compile(r"^\s*Blanket Implementations\s*$"),
    re.compile(r"^\s*Trait Implementations\s*$"),
    re.compile(r"^\s*Implementations\s*$"),
    re.compile(r"^\s*Structs\s*$"),
    re.compile(r"^\s*Enums\s*$"),
    re.compile(r"^\s*Traits\s*$"),
    re.compile(r"^\s*Modules\s*$"),
    re.compile(r"^\s*Functions\s*$"),
    re.compile(r"^\s*Type Aliases\s*$"),
    re.compile(r"^\s*Macros\s*$"),
    re.compile(r"^\s*Crate \S+\s*$"),
    re.compile(r"^\s*Struct \S+\s*$"),
    re.compile(r"^\s*Enum \S+\s*$"),
    re.compile(r"^\s*Trait \S+\s*$"),
    re.compile(r"^\s*Module \S+\s*$"),
    re.compile(r"^\s*\d+(?:\.\d+){1,3}(?:\s*\([^)]+\))?\s*·?\s*$"),
    re.compile(r"^\s*502 Bad Gateway\b.*$"),
    re.compile(r"^\s*error sending request:.*$"),
    re.compile(r"^[\s,;]+$"),
)

_CODE_LINE_RES = (
    re.compile(r"^\s*(?:impl|pub|fn|let|use|struct|enum|trait|mod|where|type|const|static|async)\b"),
    re.compile(r".*[{};]\s*$"),
    re.compile(r"^\s*//.*$"),
    re.compile(r"^\s*///.*$"),
    re.compile(r"^\s*#\[.*\]\s*$"),
    re.compile(r"^\s*\}\s*$"),
    re.compile(r"^\s*\)[^a-zA-Z]*$"),
    re.compile(r"^\s*[A-Za-z_]\w*:\s*[A-Z].*[,;]\s*$"),
    re.compile(r"^\s*[&*]?(?:mut\s+)?self\b.*$"),
    re.compile(r"^\s*->\s.*$"),
    re.compile(r".*<[A-Z][A-Za-z0-9_]*(?:,\s*[A-Z][A-Za-z0-9_]*)*>.*$"),
)

_SENTENCE_END_RE = re.compile(r"[.!?][)\]\"'`]?\s*$")


def _is_nav_cruft(line: str) -> bool:
    return any(p.match(line) for p in _NAV_CRUFT_RES)


def _is_code_like(line: str) -> bool:
    return any(p.match(line) for p in _CODE_LINE_RES)


_API_BOUNDARY_RES = (
    re.compile(r"^\s*(?:Modules|Structs|Enums|Traits|Functions|Macros|Constants|Type Aliases|Re-exports|Trait Implementations|Auto Trait Implementations|Blanket Implementations|Implementations)\s*$"),
    re.compile(r"^\s*impl(?:<.*>)?\s"),
    re.compile(r"^\s*pub (?:fn|const|static|struct|enum|trait|mod)\s"),
)

_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,/.&'’\-]{1,80}$")


def _is_api_boundary(line: str) -> bool:
    return any(p.match(line) for p in _API_BOUNDARY_RES)


def _clean_spec_text(raw: str, budget: int) -> str:
    """Extract the crate-level prose summary from a rustdoc PDF.

    Rustdoc-rendered PDFs in this dataset follow a consistent layout: a prose
    summary at the top, then API listings (Modules/Structs/Enums/...). The
    summary is what we want; everything past the first API boundary is noise.

    Steps:
      1. Drop nav/error cruft.
      2. Walk lines until the first API-boundary marker; collect everything
         before it as the candidate region.
      3. Within that region, reflow PDF column-wraps into sentences and join
         short headers with their following prose body.
      4. Drop any line/paragraph that is code-like (signatures, code fragments)
         or a pure symbol/identifier dump.
      5. Concatenate with paragraph breaks, truncate at last sentence boundary
         within `budget`.
    """
    lines: list[str] = []
    for line in raw.splitlines():
        s = line.rstrip()
        if _is_nav_cruft(s):
            continue
        if _is_api_boundary(s):
            break
        lines.append(s)

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(stripped)
        if _SENTENCE_END_RE.search(stripped):
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    prose: list[str] = []
    for para in paragraphs:
        text = re.sub(r"\s+", " ", para).strip()
        if not text:
            continue
        if _is_code_like(text):
            continue
        words = text.split()
        if len(words) < 3:
            continue
        symbol_words = sum(1 for w in words if "::" in w)
        if symbol_words > len(words) // 3:
            continue
        non_alpha = sum(1 for w in words if not any(c.isalpha() for c in w))
        if non_alpha > len(words) // 2:
            continue
        if not _SENTENCE_END_RE.search(text):
            if _SECTION_HEADER_RE.match(text) and len(words) <= 6:
                prose.append(text)
            continue
        prose.append(text)

    text = "\n\n".join(prose)
    if len(text) <= budget:
        return text

    truncated = text[:budget]
    cut = max(truncated.rfind(c) for c in ".!?")
    return truncated[: cut + 1].rstrip() if cut > 0 else ""


def load_test_ids(test_ids_dir: pathlib.Path, repo_name: str) -> list[str]:
    """Decompress test-ids/<repo_name>.bz2 and return list of pytest node IDs.
    - Strips trailing whitespace (CR/LF/space) from each line
    - Drops empty lines
    - Warns (does NOT auto-dedupe) if duplicates are present, so n_test_ids
      remains an accurate count of what test.sh will run
    """
    bz2_path = test_ids_dir / f"{repo_name}.bz2"
    if not bz2_path.exists():
        raise FileNotFoundError(f"Test-IDs archive not found: {bz2_path}")
    try:
        raw = bz2.decompress(bz2_path.read_bytes()).decode("utf-8")
    except (OSError, EOFError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"Failed to decompress/decode test-ids archive {bz2_path}: {exc}"
        ) from exc

    ids = [line.rstrip() for line in raw.splitlines()]
    ids = [tid for tid in ids if tid]

    dupes = len(ids) - len(set(ids))
    if dupes:
        print(
            f"  [WARN] {repo_name}: test-ids archive contains {dupes} duplicate ID(s); "
            f"n_test_ids = {len(ids)} reflects the inflated total."
        )
    return ids


def ecr_tag(instance_id: str) -> str:
    """Derive the ECR image tag from instance_id.

    Convention: tag = last '/'-segment.  ("commit-0/apispec" -> "apispec")

    KNOWN LIMITATION: ~19/300 commit0 tasks have ECR tags that differ from this
    simple derivation due to dash/underscore/case normalization in the ECR repo.
    Use ECR_TAG_OVERRIDES (populated from the live ECR list) to fix these.
    """
    if instance_id in ECR_TAG_OVERRIDES:
        return ECR_TAG_OVERRIDES[instance_id]
    parts = instance_id.split("/")
    return parts[-1] if len(parts) > 1 else instance_id.replace("/", "-")


# ---------------------------------------------------------------------------
# Per-task package builder
# ---------------------------------------------------------------------------

def build_task_package(
    task_name: str,
    task_dir: pathlib.Path,
    output_root: pathlib.Path,
    overwrite: bool = False,
    skip_spec: bool = False,
    allow_unverified_ecr: bool = False,
) -> bool:
    """Build a Harbor task package for one commit0 task.

    Returns True if the package was written, False if skipped (already exists).
    Raises on unrecoverable errors (missing required files, parse failures,
    schema-validation failures).
    """
    out = output_root / task_name
    if out.exists() and not overwrite:
        print(f"  [SKIP] {task_name}: output already exists (pass --overwrite to replace)")
        return False

    # ── 1. Dataset JSON ──────────────────────────────────────────────────────
    data = load_dataset_json(task_dir, task_name)
    language = data.get("language", "python")
    if language not in REQUIRED_SETUP_FIELDS_BY_LANG:
        raise ValueError(
            f"task '{task_name}': unsupported language '{language}'. "
            f"Supported: {sorted(REQUIRED_SETUP_FIELDS_BY_LANG)}"
        )
    validate_required_fields(data, task_name, language=language)

    instance_id      = data["instance_id"]
    if language == "go" and "/" not in instance_id and "_" in instance_id:
        author, rest = instance_id.split("_", 1)
        harbor_name = f"{author}/{rest}"
    else:
        harbor_name = instance_id
    validate_instance_id(harbor_name)

    fork_repo        = data["repo"]
    validate_fork_repo(fork_repo, task_name)

    original_repo    = data["original_repo"]
    base_commit      = data["base_commit"]
    reference_commit = data["reference_commit"]
    src_dir          = data["src_dir"]
    if language == "go":
        runtime_version = data["setup"]["go_version"]
        version_line    = f'go_version = "{escape_toml_string(str(runtime_version))}"'
    elif language == "rust":
        runtime_version = data["setup"]["rust_version"]
        version_line    = f'rust_version = "{escape_toml_string(str(runtime_version))}"'
    else:
        runtime_version = data["setup"]["python"]
        version_line    = f'python_version = "{escape_toml_string(str(runtime_version))}"'
    specification_url = data["setup"].get("specification", "")
    test_cmd         = data["test"]["test_cmd"]
    test_dir         = data["test"]["test_dir"]

    template_defaults = HARBOR_TEMPLATE_DEFAULTS_BY_LANG[language]
    instruction_tpl   = INSTRUCTION_TEMPLATE_BY_LANG[language]
    test_sh_body      = TEST_SH_BY_LANG[language]

    repo_name    = fork_repo.split("/")[-1]
    fork_url     = f"https://github.com/{fork_repo}"
    if language == "rust":
        owner, repo = original_repo.split("/", 1)
        tag = f"rust_{owner.lower().replace('-', '_').replace('.', '_')}_{repo.lower().replace('-', '_').replace('.', '_')}"
    elif language == "go":
        tag = repo_name.lower()
    else:
        tag = ecr_tag(instance_id)
    docker_image = f"{ECR_BASE_BY_LANG.get(language, ECR_BASE)}:{tag}"

    # Tags with underscores or uppercase may be normalized differently in the live ECR
    # repository (~19/300 commit0 tasks). Without ECR_TAG_OVERRIDES populated, the derived
    # tag may be wrong — emitting it would violate source-traceability. By default we raise
    # a hard error; pass --allow-unverified-ecr-tags to proceed with unverified tags.
    # Rust tags follow a deterministic `rust_<owner>_<repo>` schema (always contain
    # underscores by design) and were verified against ecr_push_audit.jsonl at the time
    # this branch was added, so the gate is bypassed for the rust language path.
    if language != "rust" and instance_id not in ECR_TAG_OVERRIDES and (
        "_" in tag or any(c.isupper() for c in tag)
    ):
        if not allow_unverified_ecr:
            raise ValueError(
                f"task '{task_name}': ECR tag '{tag}' has underscores/uppercase and "
                f"is not in ECR_TAG_OVERRIDES — the emitted docker_image may be wrong.\n"
                f"To fix: add the correct tag to ECR_TAG_OVERRIDES. Find it with:\n"
                f"  aws ecr list-images --repository-name kaiju-q1-coding-base | grep -i '{tag}'\n"
                f"Or pass --allow-unverified-ecr-tags to proceed with unverified tags."
            )
        print(
            f"  [WARN] {task_name}: ECR tag '{tag}' unverified (underscore/uppercase, "
            f"not in ECR_TAG_OVERRIDES); proceeding because --allow-unverified-ecr-tags is set."
        )

    # ── 2. Test IDs ──────────────────────────────────────────────────────────
    test_ids = load_test_ids(task_dir / "test-ids", repo_name)
    n_test_ids = len(test_ids)

    # ── 3. Spec text ─────────────────────────────────────────────────────────
    if skip_spec:
        spec_text = ""
    else:
        spec_text = extract_spec_text(task_dir / "specs", repo_name)

    # ── 4. Write output files (staged to temp dir, published atomically) ─────
    tmp = output_root / f".{task_name}_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        tmp.mkdir(parents=True)
        (tmp / "tests").mkdir()
        (tmp / "solution").mkdir()

        # 4a. task.toml — all string substitutions go through escape_toml_string
        description = f"commit0 from-scratch implementation task for {original_repo}."
        toml_text = TASK_TOML_TEMPLATE.format(
            schema_version       = SCHEMA_VERSION,
            source               = SOURCE,
            harbor_name          = escape_toml_string(harbor_name),
            instance_id          = escape_toml_string(instance_id),
            description          = escape_toml_string(description),
            keywords             = toml_string_list(template_defaults["keywords"]),
            category             = escape_toml_string(template_defaults["category"]),
            difficulty           = escape_toml_string(template_defaults["difficulty"]),
            original_repo        = escape_toml_string(original_repo),
            base_commit          = escape_toml_string(base_commit),
            reference_commit     = escape_toml_string(reference_commit),
            src_dir              = escape_toml_string(src_dir),
            version_line         = version_line,
            test_mode            = escape_toml_string(template_defaults["test_mode"]),
            n_test_ids           = n_test_ids,
            agent_timeout_sec    = template_defaults["agent_timeout_sec"],
            verifier_timeout_sec = template_defaults["verifier_timeout_sec"],
            docker_image         = escape_toml_string(docker_image),
            env_build_timeout_sec = template_defaults["env_build_timeout_sec"],
            env_os               = escape_toml_string(template_defaults["env_os"]),
            env_cpus             = template_defaults["env_cpus"],
            env_memory_mb        = template_defaults["env_memory_mb"],
            env_network_mode     = escape_toml_string(template_defaults["env_network_mode"]),
            env_workdir          = escape_toml_string(template_defaults["env_workdir"]),
        )
        (tmp / "task.toml").write_text(toml_text, encoding="utf-8")

        if HAS_HARBOR:
            try:
                TaskConfig.model_validate_toml(toml_text)
            except Exception as exc:
                raise ValueError(
                    f"task '{task_name}' task.toml failed Harbor schema validation: {exc}"
                ) from exc

        # 4b. instruction.md — spec_text via sentinel marker (bypasses format())
        _SPEC_MARKER = "\x00SPEC\x00"
        (tmp / "instruction.md").write_text(
            instruction_tpl.format(
                original_repo     = original_repo,
                src_dir           = src_dir,
                test_cmd          = test_cmd,
                test_dir          = test_dir,
                specification_url = specification_url,
                spec_text         = _SPEC_MARKER,
            ).replace(_SPEC_MARKER, spec_text),
            encoding="utf-8",
        )

        # 4c. tests/test_ids.txt — one ID per line, trailing newline
        (tmp / "tests" / "test_ids.txt").write_text(
            "\n".join(test_ids) + "\n",
            encoding="utf-8",
        )

        # 4d. tests/test.sh — verbatim constant
        test_sh_path = tmp / "tests" / "test.sh"
        test_sh_path.write_text(test_sh_body, encoding="utf-8")
        test_sh_path.chmod(0o755)

        # 4e. solution/solve.sh — per-task fork_url + reference_commit
        solve_sh_path = tmp / "solution" / "solve.sh"
        solve_sh_path.write_text(
            SOLVE_SH_TEMPLATE.format(
                reference_commit = reference_commit,
                fork_url         = fork_url,
            ),
            encoding="utf-8",
        )
        solve_sh_path.chmod(0o755)

        # 4f. Post-write completeness check (matches Harbor's verifier expectations).
        for rel in REQUIRED_OUTPUT_FILES:
            if not (tmp / rel).exists():
                raise RuntimeError(
                    f"task '{task_name}' build did not produce required file: {rel}"
                )

        # 4g. Publish via 3-stage atomic rename:
        #   - move existing 'out' to a backup name (atomic os.replace)
        #   - move tmp into 'out' (atomic os.replace)
        #   - delete the backup (best-effort)
        # If step 2 fails, restore the backup so 'out' is never observably missing.
        backup: pathlib.Path | None = None
        if out.exists():
            backup = output_root / f".{task_name}_backup"
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(str(out), str(backup))
        try:
            os.replace(str(tmp), str(out))
        except Exception:
            if backup is not None and not out.exists():
                os.replace(str(backup), str(out))
            raise
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    spec_status = f"{len(spec_text)} chars" if spec_text else "none"
    print(f"  [OK] {task_name}: {n_test_ids} test IDs, spec={spec_status}, image_tag={tag}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert kaiju/commit0 raw task directories to Harbor TaskConfig v1.2 packages."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Expected input layout (per task directory):
              <task>/
                <task>_dataset.json       # RepoInstance record (kaiju schema)
                specs/<repo>.pdf.bz2      # spec PDF (optional but recommended)
                test-ids/<repo>.bz2       # pytest node-id list (required)

            Generated output layout (per task):
              <task>/
                task.toml
                instruction.md
                tests/test.sh
                tests/test_ids.txt
                solution/solve.sh

            Note: per-task values in task.toml come from <task>_dataset.json
            (kaiju RepoInstance schema). All constants — category, difficulty,
            keywords, timeouts, cpus, memory_mb, os, workdir, allow_internet,
            test_mode — come from HARBOR_TEMPLATE_DEFAULTS in this file. These
            are Harbor operational conventions for commit0, NOT derived from
            kaiju data. See module docstring for full provenance.

            Examples (run from Caesar/Test/):
              python commit0_to_harbor_dataset.py \\
                -i Commit0_Data/apispec -o Harbor_Data/Dataset

              python commit0_to_harbor_dataset.py \\
                -i Commit0_Data -o Harbor_Data/Dataset --bulk

              python commit0_to_harbor_dataset.py \\
                -i Commit0_Data -o Harbor_Data/Dataset --bulk \\
                --tasks apispec flask django-rest-framework --overwrite

              python commit0_to_harbor_dataset.py \\
                -i Commit0_Data -o Harbor_Data/Dataset --bulk \\
                --overwrite --fail-fast
        """),
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help=(
            "Path to a single task directory (e.g. Commit0_Data/apispec), "
            "or with --bulk the root containing all task subdirectories."
        ),
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output root directory. Each task gets a subdirectory here.",
    )
    parser.add_argument(
        "--bulk",
        action="store_true",
        help=(
            "Treat --input as a root directory containing multiple task "
            "subdirectories. Processes all of them (or a subset via --tasks)."
        ),
    )
    parser.add_argument(
        "--tasks",
        nargs="*",
        metavar="TASK",
        help=(
            "With --bulk: process only these named tasks. "
            "Default: every subdirectory under --input."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output directories instead of skipping them.",
    )
    parser.add_argument(
        "--skip-spec",
        action="store_true",
        help=(
            "Skip PDF spec extraction and leave the spec section empty. "
            "Useful when PyMuPDF is not installed."
        ),
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="In bulk mode, abort on the first task error (default: continue).",
    )
    parser.add_argument(
        "--allow-unverified-ecr-tags",
        action="store_true",
        help=(
            "Allow ECR image tags with underscores or uppercase that are not in "
            "ECR_TAG_OVERRIDES. By default these are rejected to prevent emitting "
            "wrong docker_image values. Use only after manually verifying the ECR "
            "tag for each affected task (~19/300 commit0 tasks need overrides)."
        ),
    )
    args = parser.parse_args()

    input_path  = pathlib.Path(args.input)
    output_path = pathlib.Path(args.output)
    output_path.mkdir(parents=True, exist_ok=True)

    if not HAS_FITZ and not args.skip_spec:
        print(
            "ERROR: PyMuPDF (fitz) is required for spec extraction but is not installed.\n"
            "       Install with:  pip install pymupdf\n"
            "       Or pass --skip-spec to omit the spec section.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not HAS_HARBOR:
        print(
            "  [INFO] harbor package not installed — task.toml schema validation is SKIPPED.\n"
            "         For production runs, install harbor and re-run, or validate the\n"
            "         output with `harbor check <task-dir>`."
        )

    if args.bulk:
        if not input_path.is_dir():
            print(f"ERROR: --input '{input_path}' is not a directory.", file=sys.stderr)
            sys.exit(1)

        if args.tasks:
            task_names = args.tasks
        else:
            task_names = sorted(
                d.name for d in input_path.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )

        print(f"Processing {len(task_names)} task(s): {input_path} → {output_path}")
        ok = err = skipped = 0
        for task_name in task_names:
            task_dir = input_path / task_name
            if not task_dir.is_dir():
                print(f"  [WARN] '{task_name}' is not a directory under {input_path}, skipping.")
                skipped += 1
                continue
            try:
                built = build_task_package(
                    task_name, task_dir, output_path,
                    overwrite=args.overwrite,
                    skip_spec=args.skip_spec,
                    allow_unverified_ecr=args.allow_unverified_ecr_tags,
                )
                if built:
                    ok += 1
                else:
                    skipped += 1
            except Exception as exc:
                print(f"  [ERR] {task_name}: {type(exc).__name__}: {exc}")
                err += 1
                if args.fail_fast:
                    print("\n--fail-fast: aborting after first error.")
                    print(f"Summary: {ok} built, {skipped} skipped, {err} errors")
                    sys.exit(1)

        print(f"\nSummary: {ok} built, {skipped} skipped, {err} errors")
        if err:
            sys.exit(1)

    else:
        # Single-task mode: --input IS the task directory.
        task_dir  = input_path
        task_name = task_dir.name
        if not task_dir.is_dir():
            print(f"ERROR: --input '{task_dir}' is not a directory.", file=sys.stderr)
            sys.exit(1)
        try:
            build_task_package(
                task_name, task_dir, output_path,
                overwrite=args.overwrite,
                skip_spec=args.skip_spec,
                allow_unverified_ecr=args.allow_unverified_ecr_tags,
            )
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
