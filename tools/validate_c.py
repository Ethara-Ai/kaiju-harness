"""Standalone C-candidate validator.

Implements the acceptance gates from C-PLAN §7. Returns ``(ok, reason)``.
Used by ``prepare_repo_c.py`` before stubbing and is reusable by CI as a
pre-commit gate to keep junk repos out of dataset PRs.

MVP rules (these can be relaxed in Sprint C-3 as adapters are added):
* Licence must be in a permissive allowlist (no GPL/LGPL).
* Autotools repos are rejected.
* CMakeLists.txt must exist (no Make-only repos yet).
* The project must declare a test target / contain a ``tests/`` or ``test/``
  directory; ``enable_testing()`` is preferred.
* CTest is the only supported test framework in MVP.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Iterable, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


PERMISSIVE_LICENCES = (
    "mit",
    "bsd-2",
    "bsd-3",
    "apache-2.0",
    "apache",
    "isc",
    "zlib",
    "unlicense",
)

LICENCE_PATTERNS = {
    "mit": re.compile(r"\bMIT License\b|\bPermission is hereby granted\b", re.I),
    "bsd-2": re.compile(r"\bBSD 2-Clause\b|Redistribution and use.+2\.", re.I),
    "bsd-3": re.compile(r"\bBSD 3-Clause\b|neither the name of", re.I),
    "apache-2.0": re.compile(r"Apache License,?\s*Version\s*2\.0", re.I),
    "isc": re.compile(r"\bISC License\b", re.I),
    "zlib": re.compile(r"\bzlib License\b|This software is provided 'as-is'", re.I),
    "unlicense": re.compile(r"\bThis is free and unencumbered software\b", re.I),
}

# Anti-patterns that disqualify a project regardless of permissive-looking text.
COPYLEFT_PATTERNS = re.compile(
    r"\b(GNU General Public License|GPL-?[23]|LGPL|AGPL|GPLv2|GPLv3)\b", re.I
)


def has_license_file(path: Path) -> Path | None:
    for cand in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING", "COPYING.txt"):
        p = path / cand
        if p.exists():
            return p
    return None


def detect_licence(text: str) -> str | None:
    if COPYLEFT_PATTERNS.search(text):
        return "copyleft"
    for name, pattern in LICENCE_PATTERNS.items():
        if pattern.search(text):
            return name
    return None


def is_permissive(licence: str | None) -> bool:
    if not licence:
        return False
    if licence == "copyleft":
        return False
    return licence.lower() in PERMISSIVE_LICENCES


def has_autotools(path: Path) -> bool:
    return (path / "configure.ac").exists() or (path / "configure").exists()


def has_cmake(path: Path) -> bool:
    return (path / "CMakeLists.txt").exists()


def _walk_for(path: Path, names: Iterable[str], max_depth: int = 4) -> Path | None:
    names_set = {n.lower() for n in names}
    for dirpath, dirnames, filenames in _walk_capped(path, max_depth):
        for d in list(dirnames):
            if d.lower() in names_set:
                return Path(dirpath) / d
    return None


def _walk_capped(path: Path, max_depth: int):
    import os

    base_depth = str(path).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(path):
        depth = dirpath.count(os.sep) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
            continue
        # Don't descend into common noise.
        dirnames[:] = [d for d in dirnames if d not in (".git", "build", "_build")]
        yield dirpath, dirnames, filenames


def has_tests_dir(path: Path) -> bool:
    return _walk_for(path, ("tests", "test")) is not None


def detect_test_framework(path: Path) -> str:
    """Best-effort detection. Returns one of: ``ctest`` (default), ``unity``,
    ``cmocka``, ``criterion``, ``unknown``.
    """
    cmakelists = path / "CMakeLists.txt"
    cmake_text = cmakelists.read_text(errors="replace") if cmakelists.exists() else ""

    if "enable_testing()" in cmake_text or "add_test(" in cmake_text:
        framework = "ctest"
    else:
        framework = "unknown"

    # Header probe: scan tests/ + test/ + top-level test source files.
    probe_paths = []
    for sub in ("tests", "test"):
        p = path / sub
        if p.exists():
            probe_paths.append(p)
    headers_seen = set()
    for probe in probe_paths:
        for c_file in probe.rglob("*.c"):
            try:
                text = c_file.read_text(errors="replace")
            except OSError:
                continue
            if "<cmocka.h>" in text:
                headers_seen.add("cmocka")
            if "<criterion/" in text or "<criterion.h>" in text:
                headers_seen.add("criterion")
            if "<unity.h>" in text or '"unity.h"' in text:
                headers_seen.add("unity")
            if "<check.h>" in text:
                headers_seen.add("check")

    if framework == "ctest" and headers_seen:
        # CTest happily dispatches to any of these — but for MVP we still
        # require CTest as the runner.
        return "ctest"
    if not headers_seen:
        return framework
    return next(iter(headers_seen))


def validate_c_candidate(path: Path) -> Tuple[bool, str]:
    """Apply C-PLAN §7 acceptance gates. Returns ``(ok, reason)``."""
    if not path.exists():
        return False, f"path does not exist: {path}"

    licence_path = has_license_file(path)
    if not licence_path:
        return False, "no LICENSE / COPYING file found"
    licence = detect_licence(licence_path.read_text(errors="replace"))
    if not is_permissive(licence):
        return False, f"licence not in allowlist: {licence or 'unknown'}"

    if has_autotools(path):
        return False, "autotools rejected (Sprint 1 scope)"
    if not has_cmake(path):
        return False, "CMake required for MVP (Make support in Sprint C-3)"
    if not has_tests_dir(path):
        return False, "no tests/ or test/ directory"

    framework = detect_test_framework(path)
    if framework != "ctest":
        return False, f"framework {framework!r} unsupported in MVP"

    return True, framework


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a C candidate repo")
    parser.add_argument(
        "path",
        type=Path,
        help="Path to a cloned C repository to validate",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout",
    )
    args = parser.parse_args()

    ok, reason = validate_c_candidate(args.path)
    if args.json:
        print(json.dumps({"ok": ok, "reason": reason, "path": str(args.path)}))
    else:
        status = "OK" if ok else "REJECT"
        print(f"{status}: {args.path} ({reason})")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
