"""Discover Java test IDs by scanning test source directories and build output."""
import os
import re
import subprocess
import logging
from pathlib import Path
from typing import List

from commit0.harness.constants_java import (
    JAVA_TEST_CONVENTION,
    JAVA_SKIP_FILENAMES,
    resolve_build_cmd,
)

logger = logging.getLogger(__name__)

_TEST_CLASS_RE = re.compile(
    r"^\s*(?:public\s+)?(?:final\s+)?class\s+(\w+)", re.MULTILINE
)
_TEST_METHOD_JUNIT5_RE = re.compile(
    r"@(?:Test|ParameterizedTest|RepeatedTest)\s+.*?(?:public\s+|protected\s+|private\s+)?void\s+(\w+)\s*\(",
    re.DOTALL,
)
_TEST_METHOD_JUNIT4_RE = re.compile(
    r"@Test\s+.*?(?:public\s+)?void\s+(\w+)\s*\(", re.DOTALL
)
_TEST_METHOD_TESTNG_RE = re.compile(
    r"@Test\s+.*?(?:public\s+)?void\s+(\w+)\s*\(", re.DOTALL
)
_TEST_METHOD_JUNIT3_RE = re.compile(
    r"public\s+void\s+(test\w+)\s*\(", re.MULTILINE
)
_EXTENDS_TESTCASE_RE = re.compile(
    r"class\s+\w+\s+extends\s+\w*TestCase\b", re.MULTILINE
)


def _find_test_source_dirs(repo_path: str) -> List[Path]:
    """Find all test source directories (supports multi-module Maven/Gradle).

    Handles three layouts:
    1. Standard Maven/Gradle: ``src/test/java/`` (leaf dir named ``java``)
    2. Monorepo shorthand: ``<module>/test/`` with Java packages directly inside
       (e.g. guava-tests/test/com/google/...)
    3. Fallback: ``JAVA_TEST_CONVENTION`` constant
    """
    root = Path(repo_path)
    standard_dirs: list[Path] = []
    shorthand_dirs: list[Path] = []

    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in {"target", "build", ".gradle", ".mvn", ".git", "node_modules"}
        ]
        p = Path(dirpath)
        rel = str(p.relative_to(root))

        # Standard: leaf dir named "java" under a test path
        if p.name == "java" and "test" in rel:
            if any(f.endswith(".java") for f in os.listdir(p) if os.path.isfile(p / f)):
                standard_dirs.append(p)
            elif any(True for _ in p.rglob("*.java")):
                standard_dirs.append(p)

        # Monorepo shorthand: dir named "test" containing Java packages directly
        elif p.name == "test" and p.parent.name != "src":
            if any(True for _ in p.rglob("*.java")):
                shorthand_dirs.append(p)

    # Prefer standard layout; use shorthand only when standard not found
    test_dirs = standard_dirs if standard_dirs else shorthand_dirs

    if not test_dirs:
        default = root / JAVA_TEST_CONVENTION
        if default.exists():
            test_dirs.append(default)

    return test_dirs


def _extract_test_ids_from_source(java_file: Path) -> List[str]:
    """Parse a .java file and extract test class#method IDs."""
    try:
        content = java_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    class_match = _TEST_CLASS_RE.search(content)
    if not class_match:
        return []

    class_name = class_match.group(1)

    package = ""
    pkg_match = re.search(r"^\s*package\s+([\w.]+)\s*;", content, re.MULTILINE)
    if pkg_match:
        package = pkg_match.group(1) + "."

    fqcn = package + class_name

    methods = set()
    for pattern in (_TEST_METHOD_JUNIT5_RE, _TEST_METHOD_JUNIT4_RE, _TEST_METHOD_TESTNG_RE):
        methods.update(m.group(1) for m in pattern.finditer(content))

    if not methods and _EXTENDS_TESTCASE_RE.search(content):
        methods.update(m.group(1) for m in _TEST_METHOD_JUNIT3_RE.finditer(content))

    if not methods:
        return []

    return [f"{fqcn}#{method}" for method in sorted(methods)]


def get_test_ids_from_sources(repo_path: str) -> List[str]:
    """Discover test IDs by scanning Java test source files directly.

    This is the primary discovery method — it doesn't require compilation
    and works reliably across Maven and Gradle projects.
    """
    test_dirs = _find_test_source_dirs(repo_path)
    test_ids = []

    for test_dir in test_dirs:
        for java_file in sorted(test_dir.rglob("*.java")):
            if java_file.name in JAVA_SKIP_FILENAMES:
                continue
            ids = _extract_test_ids_from_source(java_file)
            test_ids.extend(ids)

    logger.info(f"Discovered {len(test_ids)} test IDs from source in {repo_path}")
    return test_ids


def _scan_compiled_test_classes(test_classes_dir: Path) -> List[str]:
    """Scan a compiled test-classes directory for test class names.

    Mirrors maven-surefire-plugin's default include patterns:
    ``**/Test*.class``, ``**/*Test.class``, ``**/*Tests.class``, ``**/*TestCase.class``
    (see ``SurefireMojo.getDefaultIncludes()``).
    """
    if not test_classes_dir.is_dir():
        return []

    test_classes: List[str] = []
    for class_file in sorted(test_classes_dir.rglob("*.class")):
        name = class_file.stem
        if "$" in name:
            continue
        if not (
            name.startswith("Test")
            or name.endswith("Test")
            or name.endswith("Tests")
            or name.endswith("TestCase")
        ):
            continue
        fqcn = (
            str(class_file.relative_to(test_classes_dir))
            .replace(os.sep, ".")
            .removesuffix(".class")
        )
        test_classes.append(fqcn)

    return test_classes


def get_test_ids_maven(repo_path: str) -> List[str]:
    """Discover test IDs by compiling tests and scanning compiled classes.

    Uses ``mvn test-compile`` to compile without running tests, then scans
    ``target/test-classes`` for class files matching Surefire's default
    include patterns.  Falls back to source scanning on failure.

    Note: Maven Surefire has no dry-run / list-only mode.  The previous
    ``-DdryRun=true`` flag was silently ignored, causing tests to actually
    execute.
    """
    root = Path(repo_path)
    mvn = resolve_build_cmd("maven", repo_path)
    try:
        subprocess.run(
            [mvn, "test-compile", "-B", "-q"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        test_ids: List[str] = []
        for test_classes_dir in sorted(root.rglob("target/test-classes")):
            test_ids.extend(_scan_compiled_test_classes(test_classes_dir))
        if test_ids:
            return test_ids
        logger.debug("mvn test-compile succeeded but no test classes found in target/test-classes")
    except subprocess.CalledProcessError as e:
        logger.debug(f"mvn test-compile failed (exit {e.returncode}): {e.stderr[:200] if e.stderr else ''}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug(f"Maven test-compile failed: {e}")

    return get_test_ids_from_sources(repo_path)


def get_test_ids_gradle(repo_path: str) -> List[str]:
    """Discover test IDs by compiling tests and scanning compiled classes.

    Uses ``gradle testClasses`` to compile without running tests, then scans
    ``build/classes/java/test`` for class files matching Surefire's default
    include patterns.  Falls back to source scanning on failure.

    Note: ``gradle test --dry-run`` only shows the task execution plan
    (e.g. ``:test SKIPPED``), not test class names.
    """
    root = Path(repo_path)
    gradle = resolve_build_cmd("gradle", repo_path)
    try:
        subprocess.run(
            [gradle, "testClasses", "--no-daemon", "-q"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        test_ids: List[str] = []
        for test_classes_dir in sorted(root.rglob("build/classes/java/test")):
            test_ids.extend(_scan_compiled_test_classes(test_classes_dir))
        if test_ids:
            return test_ids
        logger.debug("gradle testClasses succeeded but no test classes found in build/classes/java/test")
    except subprocess.CalledProcessError as e:
        logger.debug(f"gradle testClasses failed (exit {e.returncode}): {e.stderr[:200] if e.stderr else ''}")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug(f"Gradle testClasses failed: {e}")

    return get_test_ids_from_sources(repo_path)


def get_java_test_ids(
    instance: dict,
    strategy: str = "auto",
) -> List[str]:
    """Discover test IDs for a Java repository.

    Args:
    ----
        instance: Repo instance dict with repo_path, build_system.
        strategy: "auto" (try build tool, fallback to source), "source" (scan only),
                  "maven", or "gradle" (build-tool-specific).

    """
    repo_path = instance.get("repo_path", ".")
    build_system = instance.get("build_system", "maven")

    if strategy == "source":
        return get_test_ids_from_sources(repo_path)
    elif strategy == "maven":
        return get_test_ids_maven(repo_path)
    elif strategy == "gradle":
        return get_test_ids_gradle(repo_path)

    if build_system == "gradle":
        return get_test_ids_gradle(repo_path)
    return get_test_ids_maven(repo_path)
