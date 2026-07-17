"""Generate pytest test ID files (.bz2) for commit0 repos.

Runs ``pytest --collect-only`` against each repo via the resolved runtime
(commit0 venv → uv → docker), discovers test node IDs, and writes bz2-
compressed lists compatible with the commit0 evaluation harness.

Per-status upload policy (see :class:`tools.python_runtime.TestCollectionStatus`):

* ``OK``                  → upload
* ``NO_TESTS``            → skip (use ``--lenient`` to upload tagged)
* ``IMPORT_ERROR``        → skip (use ``--lenient``)
* ``VERSION_MISMATCH``    → auto-retry once with next-lower compatible Python
* ``MISSING_SYSTEM_DEPS`` → quarantine (write status, no test IDs)
* ``TIMEOUT``             → auto-retry once with ``timeout * 2``
* ``COLLECTION_FAILED``   → skip (use ``--lenient``)

Usage:
    # From a dataset entries JSON (uses entry['setup']['python']):
    python -m tools.generate_test_ids dataset_entries.json --output-dir ./test_ids

    # From a local repo directory (auto-detects version):
    python -m tools.generate_test_ids --repo-dir /path/to/repo --name mylib --output-dir ./test_ids

    # Force Docker tier:
    python -m tools.generate_test_ids dataset_entries.json --prefer docker

    # Install bz2 files into commit0's data directory:
    python -m tools.generate_test_ids dataset_entries.json --install
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import re
import subprocess
from pathlib import Path

from tools._test_id_sentinel import BASE_VALIDATION_FAILED_MSG, result_count
from tools.python_runtime import (
    NoRuntimeError,
    TestCollectionResult,
    TestCollectionStatus,
    next_lower_supported,
    resolve_runtime,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Statuses that auto-retry once
_RETRY_STATUSES = {
    TestCollectionStatus.VERSION_MISMATCH,
    TestCollectionStatus.TIMEOUT,
}

# Statuses where the entry should be written (with status tag) even though
# no test IDs were collected. Distinct from the upload-or-skip decision,
# which is policy-controlled via --lenient.
_NON_OK_STATUSES = {
    TestCollectionStatus.NO_TESTS,
    TestCollectionStatus.IMPORT_ERROR,
    TestCollectionStatus.VERSION_MISMATCH,
    TestCollectionStatus.MISSING_SYSTEM_DEPS,
    TestCollectionStatus.TIMEOUT,
    TestCollectionStatus.COLLECTION_FAILED,
    TestCollectionStatus.RUNTIME_UNAVAILABLE,
}


# ---------------------------------------------------------------------------
# Test ID parsing (unchanged from pre-refactor — battle-tested)
# ---------------------------------------------------------------------------


def _normalize_test_ids(test_ids: list[str], test_dir: str) -> list[str]:
    """Ensure every test ID starts with the test_dir prefix.

    pytest may output IDs relative to rootdir (which can be the test directory
    itself if conftest.py lives there), stripping the test_dir prefix.
    This normalizes all IDs to be relative to the repo root.

    Example: test_dir="tests"
      "test_align.py::test_foo" -> "tests/test_align.py::test_foo"
      "tests/test_align.py::test_foo" -> unchanged
    """
    if not test_dir or test_dir == ".":
        return test_ids

    # A single-file test suite at the repo root (e.g. python-slugify's
    # ``test.py``) is passed as test_dir="test.py". The nodeid's file_part
    # already IS that file, so prefixing would DOUBLE it
    # ("test.py/test.py::TestSlugify::...") — which then never matches pytest's
    # real nodeids at eval time, scoring every passing test as failed (a false
    # 0/N). A ``.py`` test_dir is a FILE, not a directory prefix: leave ids as-is.
    if test_dir.endswith(".py"):
        return test_ids

    prefix = test_dir.rstrip("/") + "/"
    normalized: list[str] = []
    for tid in test_ids:
        if not tid.strip():
            continue
        file_part = tid.split("::")[0]
        if not file_part.startswith(prefix) and not file_part.startswith("/"):
            tid = prefix + tid
        normalized.append(tid)
    return normalized


# pytest --collect-only TREE nodes whose ``name`` is a PATH segment (joined with
# ``/``); everything else (Class/Function/Coroutine/TestCaseFunction/…) joins the
# id with ``::``. Anything NOT in _TREE_CONTAINER_KINDS is treated as a test ITEM
# (so unknown future leaf kinds still emit an id rather than being silently lost).
_TREE_PATH_KINDS = frozenset({"Dir", "Package", "Module"})
_TREE_CONTAINER_KINDS = frozenset(
    {"Session", "Dir", "Package", "Module", "Class", "UnitTestCase", "Instance"}
)
# Legacy pytest ``<Instance ()>`` node — a container that contributes nothing to
# the node id (drop it so we don't emit ``mod.py::TestFoo::()::test_bar``).
_TREE_SKIP_KINDS = frozenset({"Instance"})
_TREE_NODE_RE = re.compile(r"^(?P<indent> *)<(?P<kind>\w+) (?P<name>.+)>$")
# Banners that mark the END of the collection tree. Everything after them
# (warnings summary, short test summary, the trailing ``N tests collected``
# banner) is NOT a node id and MUST NOT be scanned — a deprecation note such as
# ``Test: tests/x.py::y, argvalues type: generator`` contains ``::`` and would
# otherwise be mis-parsed into a garbage id like ``Test:``.
_COLLECT_END_RE = re.compile(
    r"^=+.*\b(warnings summary|short test summary|passed|failed|errors?|"
    r"tests? collected|no tests ran|slowest)\b.*=*$",
    re.IGNORECASE,
)


def _build_tree_nodeid(stack: list[tuple[int, str, str]]) -> str:
    """Assemble a pytest node id from an ancestry stack of ``(indent, kind, name)``.

    Path-like ancestors (Dir/Package/Module) join with ``/``; item ancestors
    (Class/Function/…) join with ``::``. The outermost node (the rootdir
    container) is dropped so ids come out rootdir-relative — identical to what
    ``pytest --collect-only -q`` prints.
    """
    path_segs: list[str] = []
    id_tail: list[str] = []
    for _indent, kind, name in stack[1:]:  # skip the rootdir container
        if kind in _TREE_SKIP_KINDS:
            continue
        if kind in _TREE_PATH_KINDS:
            path_segs.append(name)
        else:
            id_tail.append(name)
    path = "/".join(path_segs)
    return f"{path}::{'::'.join(id_tail)}" if id_tail else path


def _parse_collect_tree(lines: list[str]) -> list[str]:
    """Parse pytest's INDENTED verbose ``--collect-only`` tree.

    Modern pytest (7/8/9) prints each collected node on its own indented line
    (``<Dir>`` / ``<Package>`` / ``<Module>`` / ``<Function>``), NOT the legacy
    single-line ``<Module x>::<Function y>`` form. Returns ``[]`` when the output
    is not tree-shaped so the caller falls back to line-based parsing.
    """
    ids: list[str] = []
    stack: list[tuple[int, str, str]] = []
    for raw in lines:
        if _COLLECT_END_RE.match(raw.strip()):
            break  # reached the warnings/summary tail — stop scanning
        m = _TREE_NODE_RE.match(raw)
        if not m:
            continue  # header/blank/other line — ignore
        indent = len(m.group("indent"))
        kind = m.group("kind")
        name = m.group("name")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, kind, name))
        if kind not in _TREE_CONTAINER_KINDS:
            ids.append(_build_tree_nodeid(stack))
    return ids


def _parse_collect_output(stdout: str) -> list[str]:
    """Parse pytest ``--collect-only`` output in any supported format.

    Tries the modern INDENTED tree first (the verbose default on pytest 7+),
    then falls back to line-based parsing for quiet ``-q`` node ids, the legacy
    single-line ``<Module x>::<Function y>`` tree, and per-file summary counts.
    """
    lines = stdout.split("\n")
    tree_ids = _parse_collect_tree(lines)
    if tree_ids:
        return tree_ids
    return _parse_collect_lines(lines)


def _parse_collect_lines(lines: list[str]) -> list[str]:
    """Line-based fallback parser.

    Handles:
    - **Quiet node IDs** (``-q``): ``tests/test_foo.py::TestFoo::test_bar``
    - **Legacy inline tree**: ``<Module tests/test_foo.py>::<Function test_bar>``
    - **Per-file summary** (custom reporters / plugins that suppress node IDs):
      ``tests/test_foo.py: 11`` — emitted as a file-level pseudo-ID that pytest
      still accepts as a run target. Used **only as a fallback** when no
      per-test IDs were found, since fail_to_pass/pass_to_pass operate at
      test-level granularity.

    Robust to mixed output with separator lines, error lines, and empty lines.
    """
    test_ids: list[str] = []
    summary_paths: list[str] = []
    # Matches ``tests/test_foo.py: 11`` and ``tests/test_foo.py[param]: 11``.
    # Anchored to the start of the line and requires at least one whitespace
    # between the colon and the count to avoid eating IDs like ``foo:bar``.
    summary_re = re.compile(r"^(\S+\.py)(?:\[[^\]]+\])?:\s+(\d+)\s*$")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Stop at the warnings/summary tail: lines there (e.g. a deprecation
        # note ``Test: tests/x.py::y, argvalues type: generator``) can contain
        # ``::`` and would otherwise be mis-read as node ids.
        if _COLLECT_END_RE.match(line):
            break
        if line.startswith(("=", "-", "no tests ran")):
            continue
        if "error" in line.lower() and "::" not in line:
            continue

        if line.startswith("<") and "::" in line:
            parts = line.split("::")
            id_parts: list[str] = []
            for part in parts:
                part = part.strip()
                if part.startswith("<") and part.endswith(">"):
                    inner = part[1:-1]
                    idx = inner.find(" ")
                    if idx != -1:
                        id_parts.append(inner[idx + 1 :])
                    else:
                        id_parts.append(inner)
                elif part:
                    id_parts.append(part)
            if id_parts:
                test_ids.append("::".join(id_parts))
            continue

        if "::" in line:
            test_id = line.split(" ")[0]
            # A real node id carries ``::`` in its FIRST whitespace-delimited
            # token. Warnings/summary lines like ``Test: a.py::b argvalues ...``
            # place the ``::`` in a LATER token, leaving ``Test:`` as token[0] —
            # reject those rather than saving a garbage id.
            if "::" in test_id:
                test_ids.append(test_id)
            continue

        m = summary_re.match(line)
        if m:
            summary_paths.append(m.group(1))

    # Prefer per-test IDs. Fall back to file-level IDs only when the quiet
    # and verbose attempts both failed to produce any — a degraded but
    # actionable signal for the rare repos that ship a custom collector.
    return test_ids if test_ids else summary_paths


# ---------------------------------------------------------------------------
# Collection entry points
# ---------------------------------------------------------------------------


def collect_test_ids(
    repo_dir: Path,
    python_version: str,
    *,
    repo_name: str | None = None,
    reference_commit: str | None = None,
    test_dir: str = "tests",
    timeout: int = 300,
    prefer: list[str] | None = None,
    explicit_interpreter: Path | None = None,
    supported_versions: set[str] | None = None,
    allow_retry: bool = True,
) -> TestCollectionResult:
    """Resolve the right runtime for ``python_version`` and collect test IDs.

    Auto-retries on :attr:`TestCollectionStatus.VERSION_MISMATCH` (next lower
    supported Python) and :attr:`TestCollectionStatus.TIMEOUT` (2x timeout).
    Set ``allow_retry=False`` to disable the retry pass.
    """
    if supported_versions is None:
        from commit0.harness.constants import SUPPORTED_PYTHON_VERSIONS

        supported_versions = set(SUPPORTED_PYTHON_VERSIONS)

    try:
        runtime = resolve_runtime(
            python_version=python_version,
            repo_dir=repo_dir,
            repo_name=repo_name,
            reference_commit=reference_commit,
            prefer=prefer,
            explicit_interpreter=explicit_interpreter,
        )
    except NoRuntimeError as exc:
        logger.error("No runtime for %s @ python%s: %s", repo_dir, python_version, exc)
        return TestCollectionResult(
            test_ids=[],
            status=TestCollectionStatus.RUNTIME_UNAVAILABLE,
            stderr_snippet=str(exc),
        )

    logger.info("  Runtime: %s", runtime.describe())
    result = runtime.collect_test_ids(
        repo_dir=repo_dir,
        test_dir=test_dir,
        timeout=timeout,
        parse_fn=_parse_collect_output,
    )

    if result.status == TestCollectionStatus.OK:
        result = TestCollectionResult(
            test_ids=_normalize_test_ids(result.test_ids, test_dir),
            status=TestCollectionStatus.OK,
        )
        return result

    if not allow_retry or result.status not in _RETRY_STATUSES:
        return result

    # ---- retry pass ----
    if result.status == TestCollectionStatus.TIMEOUT:
        # Two-step retry ladder: 2x then 4x (capped at 900s).
        # Most timeouts that aren't true infinite-loops finish well under 2x;
        # 4x catches large-suite outliers without unbounded waits.
        retry_result: TestCollectionResult = result
        for factor in (2, 4):
            retry_timeout = min(timeout * factor, 900)
            if retry_timeout <= timeout:
                break  # nothing to gain from a non-increasing retry
            logger.warning(
                "  Collection timed out; retrying with %ds (was %ds, factor=%dx)",
                retry_timeout, timeout, factor,
            )
            retry_result = runtime.collect_test_ids(
                repo_dir=repo_dir,
                test_dir=test_dir,
                timeout=retry_timeout,
                parse_fn=_parse_collect_output,
            )
            if retry_result.status == TestCollectionStatus.OK:
                return TestCollectionResult(
                    test_ids=_normalize_test_ids(retry_result.test_ids, test_dir),
                    status=TestCollectionStatus.OK,
                )
            if retry_result.status != TestCollectionStatus.TIMEOUT:
                return retry_result  # different failure now — stop retrying
        return retry_result

    if result.status == TestCollectionStatus.VERSION_MISMATCH:
        next_ver = next_lower_supported(python_version, supported_versions)
        if next_ver is None:
            logger.warning(
                "  Version mismatch but no lower supported Python available "
                "(current %s, missing %s)",
                python_version,
                result.failing_module,
            )
            return result
        logger.warning(
            "  Version mismatch on '%s' — retrying with Python %s (was %s)",
            result.failing_module,
            next_ver,
            python_version,
        )
        return collect_test_ids(
            repo_dir=repo_dir,
            python_version=next_ver,
            repo_name=repo_name,
            reference_commit=reference_commit,
            test_dir=test_dir,
            timeout=timeout,
            prefer=prefer,
            explicit_interpreter=None,  # let the resolver re-pick for new version
            supported_versions=supported_versions,
            allow_retry=False,  # one retry only — no infinite chain
        )

    return result


# ---------------------------------------------------------------------------
# Base-commit validation (Docker only — exercised by --validate-base)
# ---------------------------------------------------------------------------


def validate_base_commit_docker(
    repo_name: str,
    test_dir: str = "tests",
    image_name: str | None = None,
    timeout: int = 300,
) -> tuple[int, str]:
    """Run ``pytest --collect-only`` at base_commit (stubbed code) inside Docker.

    Returns ``(tests_collected, stderr_snippet)``. ``tests_collected == 0``
    means the stubbed code broke imports — the pipeline will produce a 0%
    pass rate.
    """
    from tools.python_runtime import DockerRuntime, find_docker_image_for_repo

    image = image_name or find_docker_image_for_repo(repo_name)
    if image is None:
        image = f"commit0.repo.{repo_name.lower().replace('/', '_')}:v0"

    try:
        runtime = DockerRuntime(image=image)
    except Exception as exc:  # noqa: BLE001
        return 0, f"docker init failed: {exc}"

    result = runtime.collect_test_ids(
        repo_dir=Path.cwd(),  # ignored by DockerRuntime (uses container path)
        test_dir=test_dir,
        timeout=timeout,
        parse_fn=_parse_collect_output,
    )
    if result.status == TestCollectionStatus.OK:
        return len(result.test_ids), ""
    # Try parsing the summary line for a count even when individual IDs failed
    snippet = result.stderr_snippet
    m = re.search(r"(\d+)\s+tests?\s+collected", snippet)
    if m:
        return int(m.group(1)), snippet[-500:]
    return 0, snippet[-500:]


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def save_test_ids(test_ids: list[str], name: str, output_dir: Path) -> Path:
    """Save test IDs as a bz2-compressed file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    name = name.lower().replace(".", "-")
    output_file = output_dir / f"{name}.bz2"
    content = "\n".join(test_ids)
    with bz2.open(output_file, "wt") as f:
        f.write(content)
    return output_file


def install_test_ids(
    source_dir: Path,
    repo_names: list[str] | None = None,
) -> int:
    """Copy test ID .bz2 files into commit0's data directory."""
    try:
        import commit0
    except ImportError:
        logger.error("commit0 package not found — cannot install test IDs")
        return 0

    data_dir = Path(os.path.dirname(commit0.__file__)) / "data" / "test_ids"
    data_dir.mkdir(parents=True, exist_ok=True)
    installed = 0

    import shutil

    for bz2_file in sorted(source_dir.glob("*.bz2")):
        name = bz2_file.stem
        if repo_names and name not in [r.lower().replace(".", "-") for r in repo_names]:
            continue
        dest = data_dir / bz2_file.name
        shutil.copy2(bz2_file, dest)
        logger.info("  Installed: %s -> %s", bz2_file.name, dest)
        installed += 1

    return installed


def _find_repo_dir(
    clone_dir: Path | None,
    fork_repo: str,
    original_repo: str,
) -> Path | None:
    """Locate the cloned repo directory, checking fork name then original name."""
    base = clone_dir or Path("./repos_staging")
    candidates = [fork_repo]
    if original_repo and original_repo != fork_repo:
        candidates.append(original_repo)
    for name in candidates:
        candidate = base / name.replace("/", "__")
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Per-entry orchestration
# ---------------------------------------------------------------------------


def _should_upload(status: TestCollectionStatus, lenient: bool) -> bool:
    """Per-status upload decision.

    OK is always uploaded. Non-OK statuses are uploaded only when ``lenient``
    is set — and even then, MISSING_SYSTEM_DEPS is always quarantined because
    the test list would be misleading.
    """
    if status == TestCollectionStatus.OK:
        return True
    if status == TestCollectionStatus.MISSING_SYSTEM_DEPS:
        return False
    if status == TestCollectionStatus.RUNTIME_UNAVAILABLE:
        return False
    return lenient


def generate_for_dataset(
    dataset_path: Path,
    output_dir: Path,
    *,
    clone_dir: Path | None = None,
    timeout: int = 300,
    max_repos: int | None = None,
    validate_base: bool = False,
    prefer: list[str] | None = None,
    lenient: bool = False,
    quarantine_dir: Path | None = None,
) -> dict[str, dict]:
    """Generate test IDs for all repos in a dataset entries JSON.

    Returns a map ``{repo_name: {status, count, source}}`` for the caller's
    summary line.
    """
    # T3 fix: previously bare json.loads crashed with raw traceback on malformed
    # dataset. Surface a clean error the operator can act on.
    try:
        data = json.loads(dataset_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Dataset JSON at {dataset_path} is malformed: {e}"
        ) from e
    if isinstance(data, dict) and "data" in data:
        entries = data["data"]
    elif isinstance(data, list):
        entries = data
    else:
        raise ValueError(f"Unknown dataset format in {dataset_path}")

    results: dict[str, dict] = {}

    for i, entry in enumerate(entries):
        if max_repos and i >= max_repos:
            break

        repo = entry.get("repo", "")
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        test_dir = entry.get("test", {}).get("test_dir", "tests")
        instance_id = entry.get("instance_id", repo_name)
        python_version = entry.get("setup", {}).get("python")
        reference_commit = entry.get("reference_commit")

        if not python_version:
            logger.error(
                "  Entry %s has no setup.python — run prepare_repo first",
                instance_id,
            )
            results[repo_name] = {
                "status": TestCollectionStatus.RUNTIME_UNAVAILABLE.value,
                "count": 0,
                "source": "no-python-field",
            }
            continue

        logger.info(
            "\n[%d/%d] Collecting test IDs for %s (python=%s)...",
            i + 1,
            min(len(entries), max_repos or len(entries)),
            instance_id,
            python_version,
        )

        repo_dir = _find_repo_dir(clone_dir, repo, entry.get("original_repo", ""))
        if repo_dir is None and "docker" not in (prefer or ["docker"]):
            logger.warning(
                "  Repo dir not found — skipping (tried fork + original name)"
            )
            results[repo_name] = {
                "status": TestCollectionStatus.RUNTIME_UNAVAILABLE.value,
                "count": 0,
                "source": "no-repo-dir",
            }
            continue

        if repo_dir is not None and reference_commit:
            try:
                subprocess.run(
                    ["git", "checkout", reference_commit],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("  Could not checkout reference_commit: %s", e)

        result = collect_test_ids(
            repo_dir=repo_dir or Path.cwd(),
            python_version=python_version,
            repo_name=repo,
            reference_commit=reference_commit,
            test_dir=test_dir,
            timeout=timeout,
            prefer=prefer,
        )

        upload = _should_upload(result.status, lenient)

        # QC-C6-005: base-validation count for the shared negative-sentinel
        # contract (None = --validate-base not run).
        base_collected = None

        if result.status == TestCollectionStatus.OK:
            out_file = save_test_ids(result.test_ids, repo_name, output_dir)
            logger.info("  Saved %d test IDs to %s", len(result.test_ids), out_file)

            if validate_base and "docker" in (prefer or []):
                base_collected, stderr = validate_base_commit_docker(
                    repo_name=repo,
                    test_dir=test_dir,
                    timeout=timeout,
                )
                if base_collected == 0:
                    logger.warning("  %s", BASE_VALIDATION_FAILED_MSG)
                    logger.warning("  Last output: %s", stderr[:200])
        else:
            logger.warning(
                "  status=%s (failing_module=%s)",
                result.status.value,
                result.failing_module,
            )
            if upload:
                save_test_ids(
                    result.test_ids,
                    repo_name,
                    output_dir,
                )
                logger.info(
                    "  --lenient: wrote %d test IDs (status=%s)",
                    len(result.test_ids),
                    result.status.value,
                )
            elif quarantine_dir is not None:
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                (quarantine_dir / f"{repo_name.lower().replace('.', '-')}.json").write_text(
                    json.dumps(
                        {
                            "repo": repo,
                            "status": result.status.value,
                            "failing_module": result.failing_module,
                            "stderr_snippet": result.stderr_snippet,
                        },
                        indent=2,
                    )
                )

        results[repo_name] = {
            "status": result.status.value,
            # QC-C6-005: negative sentinel when the stubbed base is degenerate
            # (base_collected == 0), identical semantics to c/cpp/go/js/ts/java.
            "count": result_count(result.test_ids, base_collected),
            "source": result.failing_module or "",
        }

    return results


# ---------------------------------------------------------------------------
# --repo-dir mode helpers (P0-A/P0-B/P1-B — see MISSING_TEST_IDS_BZ2_ISSUE.md)
# ---------------------------------------------------------------------------


_BREADCRUMB_RELATIVE = Path(".kaiju/entries.json")


def _discover_breadcrumb(
    repo_dir: Path,
    *,
    name: str | None,
    output_dir: Path | None,
    explicit: Path | None,
) -> dict | None:
    """Locate the ``.kaiju/entries.json`` breadcrumb for ``repo_dir``.

    Search order (first hit wins):

      1. ``explicit`` (CLI ``--entries-json`` override)
      2. ``repo_dir/.kaiju/entries.json`` (written by ``prepare_repo.py``)
      3. ``output_dir/<name>_entries.json`` (Argo-wrapper convention)
      4. ``repo_dir/../<name>_entries.json``
      5. ``repo_dir/../entries.json``
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    candidates.append(repo_dir / _BREADCRUMB_RELATIVE)
    if name:
        if output_dir is not None:
            candidates.append(output_dir / f"{name}_entries.json")
        candidates.append(repo_dir.parent / f"{name}_entries.json")
    candidates.append(repo_dir.parent / "entries.json")

    for path in candidates:
        if not path or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # Accept either the breadcrumb schema or a raw entries list/dict
        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict) and ("setup" in data or "test" in data):
            logger.info("Using breadcrumb: %s", path)
            return data
    return None


def _resolve_repo_dir_args(
    *,
    repo_dir: Path,
    name: str,
    cli_python_version: str | None,
    cli_test_dir: str | None,
    cli_reference_commit: str | None,
    cli_entries_json: Path | None,
    output_dir: Path,
) -> tuple[str, str, str | None, dict | None]:
    """Resolve (python_version, test_dir, reference_commit, breadcrumb).

    CLI flags > breadcrumb > python_version.detect() / sensible defaults.
    Returns the breadcrumb dict too so callers can also pull system_deps_hint.
    """
    from commit0.harness.constants import (
        DEFAULT_PYTHON_VERSION,
        SUPPORTED_PYTHON_VERSIONS,
    )
    from tools.python_version import detect as detect_python

    breadcrumb = _discover_breadcrumb(
        repo_dir,
        name=name,
        output_dir=output_dir,
        explicit=cli_entries_json,
    )
    setup = (breadcrumb or {}).get("setup") or {}
    test_block = (breadcrumb or {}).get("test") or {}

    python_version = (
        cli_python_version
        or setup.get("python")
        or None
    )
    if not python_version:
        det = detect_python(
            repo_dir,
            SUPPORTED_PYTHON_VERSIONS,
            fallback=DEFAULT_PYTHON_VERSION,
        )
        python_version = det.version or DEFAULT_PYTHON_VERSION
        logger.info(
            "Auto-detected python=%s (source=%s)", python_version, det.source,
        )
    test_dir = cli_test_dir or test_block.get("test_dir") or "tests"
    reference_commit = (
        cli_reference_commit
        or (breadcrumb or {}).get("reference_commit")
    )
    return python_version, test_dir, reference_commit, breadcrumb


class _ReferenceCommitCheckout:
    """Context manager: checkout ``reference_commit``, restore HEAD on exit.

    Stashes any uncommitted changes (e.g. the stubbed code from prepare_repo)
    so the checkout doesn't abort with "Your local changes would be overwritten".
    On exit, restores the original HEAD and pops the stash. Always safe to use
    — a no-op when ``reference_commit`` is ``None``.
    """

    def __init__(self, repo_dir: Path, reference_commit: str | None):
        self.repo_dir = repo_dir
        self.reference_commit = reference_commit
        self._prior_head: str | None = None
        self._stashed = False

    def __enter__(self) -> "_ReferenceCommitCheckout":
        if not self.reference_commit:
            return self
        try:
            self._prior_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_dir, text=True, timeout=15,
            ).strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("  Cannot rev-parse HEAD before checkout: %s", exc)
            return self
        # T5: Stash if dirty. If git status itself fails (detached HEAD,
        # permissions issue), an empty stdout would be treated as "clean" and
        # we'd proceed on corrupted state; log a warning and treat rc!=0 as
        # "assume dirty, try to stash" so we don't checkout on top of unknown.
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.repo_dir, capture_output=True, text=True, timeout=15,
        )
        if dirty.returncode != 0:
            logger.warning(
                "  git status failed in %s (rc=%s stderr=%s); assuming dirty tree",
                self.repo_dir, dirty.returncode, dirty.stderr.strip()[:200],
            )
        if dirty.stdout.strip() or dirty.returncode != 0:
            stash = subprocess.run(
                ["git", "stash", "push", "-u", "-m", "kaiju-generate-test-ids"],
                cwd=self.repo_dir, capture_output=True, text=True, timeout=30,
            )
            self._stashed = stash.returncode == 0
            if not self._stashed:
                logger.warning(
                    "  git stash push failed in %s (rc=%s stderr=%s)",
                    self.repo_dir, stash.returncode, stash.stderr.strip()[:200],
                )
        try:
            subprocess.run(
                ["git", "checkout", self.reference_commit],
                cwd=self.repo_dir, capture_output=True, text=True,
                timeout=30, check=True,
            )
            logger.info("  Checked out reference_commit=%s", self.reference_commit[:12])
        except subprocess.CalledProcessError as exc:
            # T11: previously we logged a warning then swallowed the error, so
            # callers proceeded to collect test IDs on the WRONG commit.
            # Roll back the stash first, then raise so the caller gets a hard
            # signal instead of a silent inventory-vs-reality drift.
            _stderr = (exc.stderr or exc.stdout or "").strip()[:400]
            logger.error(
                "  Could not checkout reference_commit %s: %s",
                self.reference_commit, _stderr,
            )
            self._pop_stash()
            self._prior_head = None
            raise RuntimeError(
                f"failed to checkout reference_commit {self.reference_commit!r}"
                f" in {self.repo_dir}: {_stderr}"
            ) from exc
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        # T4: git checkout on cleanup can fail silently (dirty tree, permissions,
        # detached ref). Without a returncode check the worktree is left in an
        # inconsistent state while callers assume clean restore. Log so the
        # operator can spot half-cleaned repos.
        if self._prior_head:
            co = subprocess.run(
                ["git", "checkout", self._prior_head],
                cwd=self.repo_dir, capture_output=True, text=True,
                timeout=30, check=False,
            )
            if co.returncode != 0:
                logger.warning(
                    "  cleanup git checkout %s failed in %s (rc=%s stderr=%s);"
                    " worktree may be at wrong commit",
                    self._prior_head, self.repo_dir, co.returncode,
                    co.stderr.strip()[:200],
                )
        self._pop_stash()

    def _pop_stash(self) -> None:
        if not self._stashed:
            return
        sp = subprocess.run(
            ["git", "stash", "pop"],
            cwd=self.repo_dir, capture_output=True, text=True,
            timeout=30, check=False,
        )
        if sp.returncode != 0:
            logger.warning(
                "  cleanup git stash pop failed in %s (rc=%s stderr=%s);"
                " stashed changes remain in stash",
                self.repo_dir, sp.returncode, sp.stderr.strip()[:200],
            )
        self._stashed = False


def _write_status_json(
    *,
    output_dir: Path,
    name: str,
    repo_dir: Path,
    result: TestCollectionResult,
    python_version: str,
    test_dir: str,
    reference_commit: str | None,
    bz2_written: bool,
    extra: dict | None = None,
) -> Path:
    """Write the always-emit ``<name>.status.json`` artifact.

    Schema documented in :mod:`MISSING_TEST_IDS_BZ2_ISSUE.md` follow-up.
    """
    import datetime

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "repo_dir": str(repo_dir),
        "reference_commit": reference_commit,
        "python_version": python_version,
        "test_dir": test_dir,
        "status": result.status.value,
        "test_count": len(result.test_ids),
        "bz2_written": bz2_written,
        "failing_module": result.failing_module,
        "stderr_snippet": result.stderr_snippet,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "_schema": "kaiju-test-id-status/1",
    }
    if extra:
        payload.update(extra)
    out = output_dir / f"{name.lower().replace('.', '-')}.status.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out



# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate pytest test ID files for commit0 repos"
    )
    parser.add_argument(
        "dataset_file",
        nargs="?",
        help="Input dataset_entries.json or custom_dataset.json",
    )
    parser.add_argument("--repo-dir", type=str, help="Single local repo directory")
    parser.add_argument("--name", type=str, help="Repo name (required with --repo-dir)")
    parser.add_argument(
        "--python-version",
        type=str,
        default=None,
        help="Python X.Y for --repo-dir mode (auto-detected if omitted)",
    )
    parser.add_argument(
        "--test-dir",
        type=str,
        default="tests",
        help="Test directory within repo (default: tests)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./test_ids",
        help="Output directory for .bz2 files (default: ./test_ids)",
    )
    parser.add_argument(
        "--clone-dir",
        type=str,
        default=None,
        help="Directory where repos are cloned (default: ./repos_staging)",
    )
    parser.add_argument(
        "--prefer",
        type=str,
        default=None,
        help="Comma-separated runtime tier order (default: local,uv,docker)",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Shortcut for --prefer docker",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install generated .bz2 files into commit0's data directory",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per repo for pytest collection (default: 300s)",
    )
    parser.add_argument("--max-repos", type=int, default=None, help="Max repos to process")
    parser.add_argument(
        "--validate-base",
        action="store_true",
        help="After collecting IDs, validate base_commit (stubbed code) also collects. Requires Docker tier.",
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="Upload entries even when status != OK (excluding MISSING_SYSTEM_DEPS).",
    )
    parser.add_argument(
        "--quarantine-dir",
        type=str,
        default=None,
        help="Write per-repo failure reports here for non-OK entries that are skipped.",
    )
    parser.add_argument(
        "--strict-exit",
        action="store_true",
        help="In --repo-dir mode, exit non-zero on any non-OK status (default: only exit 1 when truly unrecoverable).",
    )
    parser.add_argument(
        "--entries-json",
        type=str,
        default=None,
        help="Explicit path to a breadcrumb / entries.json with setup.python + test.test_dir + reference_commit.",
    )
    parser.add_argument(
        "--reference-commit",
        type=str,
        default=None,
        help="Git SHA to checkout before collection (auto-discovered from breadcrumb if omitted).",
    )
    parser.add_argument(
        "--no-checkout",
        action="store_true",
        help="Skip the auto-checkout of reference_commit (collect against current worktree state).",
    )

    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    prefer = (
        args.prefer.split(",") if args.prefer else (["docker"] if args.docker else None)
    )
    # Repos handled by *this* run — used to scope --install so it only copies
    # this run's test IDs into commit0's shared data dir (not every language's).
    install_repo_names: list[str] = []

    if args.repo_dir:
        if not args.name:
            parser.error("--name is required with --repo-dir")
        install_repo_names = [args.name]
        repo_dir = Path(args.repo_dir)

        python_version, test_dir, reference_commit, breadcrumb = _resolve_repo_dir_args(
            repo_dir=repo_dir,
            name=args.name,
            cli_python_version=args.python_version,
            cli_test_dir=args.test_dir if args.test_dir != "tests" else None,
            cli_reference_commit=args.reference_commit,
            cli_entries_json=Path(args.entries_json) if args.entries_json else None,
            output_dir=output_dir,
        )
        # If breadcrumb-less and CLI didn't override --test-dir, fall back to "tests"
        if not test_dir:
            test_dir = "tests"
        sys_deps_hint = ((breadcrumb or {}).get("setup") or {}).get("system_deps_hint") or []

        logger.info(
            "Collecting test IDs from %s (python=%s, test_dir=%s, ref=%s)",
            repo_dir, python_version, test_dir,
            (reference_commit or "<none>")[:12],
        )

        # Checkout reference_commit (un-stubbed code) so pytest collection works.
        do_checkout = (not args.no_checkout) and bool(reference_commit)
        ctx_ref = reference_commit if do_checkout else None
        with _ReferenceCommitCheckout(repo_dir, ctx_ref):
            result = collect_test_ids(
                repo_dir=repo_dir,
                python_version=python_version,
                repo_name=args.name,
                reference_commit=reference_commit,
                test_dir=test_dir,
                timeout=args.timeout,
                prefer=prefer,
            )

        bz2_written = False
        if result.status == TestCollectionStatus.OK:
            out_file = save_test_ids(result.test_ids, args.name, output_dir)
            bz2_written = True
            logger.info("Saved %d test IDs to %s", len(result.test_ids), out_file)
        else:
            logger.warning(
                "status=%s (failing_module=%s)",
                result.status.value,
                result.failing_module,
            )
            if args.lenient and result.test_ids:
                save_test_ids(result.test_ids, args.name, output_dir)
                bz2_written = True
                # T20: partial/broken lenient uploads previously logged INFO,
                # which was easy to miss in noisy CI logs. Downstream evaluators
                # trust <name>.bz2 as a canonical inventory; if a repo lands via
                # --lenient after IMPORT_ERROR or COLLECTION_FAILED, the inventory
                # is incomplete and future scores are silently inflated/deflated.
                # Escalate to a boxed WARNING so it survives log-scanning.
                logger.warning(
                    "\n" + "!" * 72
                    + "\n!! LENIENT UPLOAD: wrote %d test IDs for %s despite status=%s."
                    + "\n!! Downstream eval scores against this inventory are unreliable."
                    + "\n!! Rerun without --lenient after fixing infra (see .status.json)."
                    + "\n" + "!" * 72,
                    len(result.test_ids), args.name, result.status.value,
                )

        # ALWAYS write the .status.json artifact so downstream sweep tools can
        # detect the gap (the Argo wrapper's *.bz2-only glob silently ignores
        # this file today; a post-prep sweep can read it).
        status_path = _write_status_json(
            output_dir=output_dir,
            name=args.name,
            repo_dir=repo_dir,
            result=result,
            python_version=python_version,
            test_dir=test_dir,
            reference_commit=reference_commit,
            bz2_written=bz2_written,
            extra={"system_deps_hint": sys_deps_hint} if sys_deps_hint else None,
        )
        logger.info("Status: %s -> %s", result.status.value, status_path)

        # Default: exit 0 even on non-OK (the .status.json carries the signal).
        # --strict-exit promotes any non-OK to exit code 1 for callers that
        # DO check return codes.
        if args.strict_exit and result.status != TestCollectionStatus.OK:
            raise SystemExit(1)

    elif args.dataset_file:
        dataset_path = Path(args.dataset_file)
        if not dataset_path.exists():
            parser.error(f"File not found: {dataset_path}")

        clone_dir = Path(args.clone_dir) if args.clone_dir else None
        quarantine_dir = Path(args.quarantine_dir) if args.quarantine_dir else None

        results = generate_for_dataset(
            dataset_path=dataset_path,
            output_dir=output_dir,
            clone_dir=clone_dir,
            timeout=args.timeout,
            max_repos=args.max_repos,
            validate_base=args.validate_base,
            prefer=prefer,
            lenient=args.lenient,
            quarantine_dir=quarantine_dir,
        )
        install_repo_names = list(results.keys())

        total = sum(r["count"] for r in results.values())
        ok_count = sum(
            1 for r in results.values() if r["status"] == TestCollectionStatus.OK.value
        )
        logger.info(
            "\nDone: %d test IDs across %d repos (%d OK, %d non-OK)",
            total,
            len(results),
            ok_count,
            len(results) - ok_count,
        )
        # Status breakdown
        from collections import Counter

        status_counts = Counter(r["status"] for r in results.values())
        for status, count in sorted(status_counts.items()):
            logger.info("  %-22s %d", status, count)

    else:
        parser.error("Provide either dataset_file or --repo-dir")
        return

    if args.install:
        installed = install_test_ids(output_dir, repo_names=install_repo_names or None)
        logger.info("Installed %d test ID files into commit0 data directory", installed)


# ---------------------------------------------------------------------------
# Backward-compat shim. Pre-refactor, this private helper was imported by
# sibling generators (generate_test_ids_rust.py, generate_test_ids_ts.py).
# Keep it as a thin alias to avoid churning the language-specific files.
# ---------------------------------------------------------------------------


def _find_docker_image(repo_name: str) -> str | None:
    """Backward-compat alias for :func:`tools.python_runtime.find_docker_image_for_repo`."""
    from tools.python_runtime import find_docker_image_for_repo

    return find_docker_image_for_repo(repo_name)


if __name__ == "__main__":
    main()
