#!/usr/bin/env python3
"""Lint cpp dataset test_cmd entries for shapes that silently break Stage 3.

Stage 3 (test-feedback refinement) only iterates when the test_cmd exits
non-zero on build/test failure. Two recurring bugs caused $0 / zero-turn
Stage 3 runs in the past:

  1. `cmake --build ... -k 0` without a `--` separator -- cmake itself
     rejects `-k` as "Unknown argument -k", the build never runs.
  2. trailing `|| true` (or `|| :`, `|| exit 0`, `; true`) -- masks every
     failure to exit code 0, so the agent's cmd_test wrapper sees "no errors"
     and skips coder.run() with the captured test output.

This validator scans every `*_cpp_dataset*.json` (or any file passed on the
command line) and exits non-zero when either antipattern is present.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

_TEST_RUNNERS: dict[str, str] = {
    "ctest": "ctest --test-dir build --output-junit /testbed/test_results.xml --timeout 60",
    "gtest": "ctest --test-dir build --output-junit /testbed/test_results.xml --timeout 60",
    "catch2": "./build/tests/test_all -r junit -o /testbed/test_results.xml",
    "catch": "./build/tests/test_all -r junit -o /testbed/test_results.xml",
    "doctest": "./build/tests/test_all --reporters=junit --out=/testbed/test_results.xml",
    "boost_test": "./build/tests/test_all --logger=JUNIT,message,/testbed/test_results.xml",
    "caf": "ctest --test-dir build --output-junit /testbed/test_results.xml --timeout 60",
}

_BUILD_PARTS: dict[str, str] = {
    "cmake": "cmake --build build -j$(nproc) -- -k 0",
    "meson": "ninja -C builddir -k 0",
    "autotools": "make -j$(nproc) -k",
    "make": "make -j$(nproc) -k",
}


def build_canonical_test_cmd(
    build_system: str,
    test_framework: str,
    configure_cmd: str = "",
) -> str:
    """Return the LATEST canonical cpp test_cmd for a (build_system, framework).

    Shape: ``{configure} && {build with keep-going} ; {test runner}``.

    The ``;`` between build and test is deliberate: it lets the test runner
    surface "not run" entries when the build fails partially, so the agent
    sees both compile errors AND missing-binary errors in one pass. The
    overall command still exits non-zero on failure (no trailing ``|| true``).

    ``configure_cmd`` is the project's cmake/meson configure invocation,
    typically the same as the dataset's ``setup.install`` configure prefix
    (e.g. ``cmake -B build -DFMT_TEST=ON ...``). Pass it whenever the agent
    must re-run configure on every iteration (most cpp commit0 repos).
    """
    bs = build_system.strip().lower()
    tf = test_framework.strip().lower()
    test_runner = _TEST_RUNNERS.get(tf, _TEST_RUNNERS["ctest"])
    build_part = _BUILD_PARTS.get(bs, _BUILD_PARTS["make"])
    if configure_cmd:
        return f"{configure_cmd.strip()} && {build_part}; {test_runner}"
    return f"{build_part}; {test_runner}"


ANTIPATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"cmake\s+--build\s+\S+(?:\s+(?!--(?:\s|$))[^\s]+)*\s+(?:-k|--keep-going)\b",
        ),
        "cmake --build receives -k/--keep-going BEFORE the `--` separator",
        "Move keep-going flags after `--` so cmake forwards them to the build tool:\n"
        "        cmake --build build -j$(nproc) -- -k 0",
    ),
    (
        re.compile(r"(?:\|\|\s*true|\|\|\s*:|\|\|\s*exit\s+0|;\s*true)\s*$"),
        "test_cmd ends with an exit-masking suffix (|| true, || :, ; true, || exit 0)",
        "Remove the trailing mask. Stage 3 needs a non-zero exit code to feed "
        "test output back to the coder.",
    ),
]


def lint_cmd(test_cmd: str) -> list[tuple[str, str]]:
    return [(label, hint) for pat, label, hint in ANTIPATTERNS if pat.search(test_cmd)]


def iter_entries(data: object) -> Iterable[dict]:
    if isinstance(data, list):
        for x in data:
            if isinstance(x, dict):
                yield x
    elif isinstance(data, dict):
        yield data


def lint_dataset(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ERROR: cannot read {path}: {e}", file=sys.stderr)
        return 1

    failed = 0
    saw_entry = False
    for entry in iter_entries(data):
        saw_entry = True
        repo = entry.get("repo") or entry.get("repo_id") or entry.get("instance_id") or "?"
        test_info = entry.get("test") if isinstance(entry.get("test"), dict) else {}
        test_cmd = (test_info or {}).get("test_cmd") or entry.get("test_cmd") or ""
        if not test_cmd:
            print(f"  {repo}: no test_cmd (skipped)")
            continue
        findings = lint_cmd(test_cmd)
        if not findings:
            print(f"  {repo}: OK")
            continue
        failed += 1
        print(f"  {repo}: FAIL ({len(findings)} issue(s))")
        print(f"    test_cmd: {test_cmd}")
        for label, hint in findings:
            print(f"    - {label}")
            for line in hint.splitlines():
                print(f"      {line}")

    if not saw_entry:
        print(f"  {path}: no dict entries found (skipped)")
    return failed


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "paths",
        nargs="+",
        help="JSON dataset file(s) to validate.",
    )
    args = p.parse_args(argv)

    total_failed = 0
    for raw in args.paths:
        path = Path(raw)
        if not path.is_file():
            print(f"ERROR: not a file: {path}", file=sys.stderr)
            total_failed += 1
            continue
        print(f"=== {path} ===")
        total_failed += lint_dataset(path)

    if total_failed:
        print(
            f"\n{total_failed} validation issue(s) found. "
            "Fix the test_cmd entries before re-running the pipeline.",
            file=sys.stderr,
        )
        return 1
    print("\nAll datasets pass test_cmd validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
