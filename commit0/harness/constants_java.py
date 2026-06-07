"""Java constants and data models for Commit0.

Parallel to constants.py — does NOT modify constants.py.
"""
import defusedxml.ElementTree as ET
import re
from pathlib import Path
from typing import Dict, List, Optional

from commit0.harness.constants import RepoInstance


class JavaRepoInstance(RepoInstance):
    """Extended RepoInstance for Java repositories.

    RepoInstance is a Pydantic BaseModel (not a dataclass).
    We extend it with Java-specific fields using Pydantic field declarations.
    Inherits: instance_id, repo, base_commit, reference_commit, setup, test, src_dir
    """

    language: str = "java"
    java_version: str = "17"
    build_system: str = "maven"          # "maven" | "gradle"
    java_src_dir: str = "src/main/java"  # renamed to avoid clash with inherited `src_dir`
    test_dir: str = "src/test/java"
    test_framework: str = "junit5"       # "junit4" | "junit5" | "testng"
    has_modules: bool = False            # Multi-module project
    main_module: Optional[str] = None    # Root module for multi-module projects


# Version support
JAVA_VERSION_DEFAULT = "17"
SUPPORTED_JAVA_VERSIONS = {"11", "17", "21"}

# File conventions
JAVA_SOURCE_EXT = ".java"
JAVA_STUB_MARKER = 'UnsupportedOperationException("STUB: not implemented")'
JAVA_TEST_FILE_SUFFIX = "Test.java"

# Branch conventions (parallel to Python's BASE_BRANCH="commit0" and "commit0_all")
JAVA_BASE_BRANCH = "commit0_java"          # Local working branch (like Python's "commit0")
JAVA_REMOTE_BRANCH = "commit0_all"    # Remote branch pushed to fork (like Python's "commit0_all")
JAVA_SKIP_FILENAMES = {"module-info.java", "package-info.java"}

# Source layout
JAVA_SRC_CONVENTION = "src/main/java/"
JAVA_TEST_CONVENTION = "src/test/java/"
JAVA_RESOURCE_CONVENTION = "src/main/resources/"

# Build artifacts to exclude from patches
JAVA_BUILD_DIRS = {"target/", "build/", ".gradle/", ".mvn/wrapper/"}

# Docker
JAVA_BASE_IMAGE_PREFIX = "commit0-java"
JAVA_CONTAINER_PREFIX = "commit0-java"

# Splits (curated Java repos with strong test suites, Maven/Gradle, permissive licenses)
# Format: "org/repo" matching GitHub URLs (https://github.com/{org}/{repo})
JAVA_SPLIT_LITE: List[str] = [
    "apache/commons-lang",
    "apache/commons-collections",
    "google/gson",
    "JodaOrg/joda-time",
    "apache/commons-codec",
    "apache/commons-text",
    "FasterXML/jackson-core",
]
# Curated Java subsets only. The "all" subset is derived dynamically from the
# loaded dataset by ``commit0.harness.split_utils.resolve_split``.
JAVA_SPLIT: Dict[str, List[str]] = {
    "lite": JAVA_SPLIT_LITE,
}


def resolve_build_cmd(build_system: str, repo_path: Optional[str] = None) -> str:
    """Return the build tool command, preferring project-local wrappers.

    For local execution, checks for ./mvnw or ./gradlew in repo_path and
    uses them if present (they pin the exact tool version the project needs).
    Falls back to system-installed mvn/gradle.

    For Docker execution, spec_java.py handles wrapper detection at the shell
    level via _wrapper_preamble() — pass repo_path=None here for fallback only.
    """
    if build_system == "gradle":
        if repo_path:
            wrapper = Path(repo_path) / "gradlew"
            if wrapper.is_file():
                return str(wrapper)
        return "gradle"
    else:
        if repo_path:
            wrapper = Path(repo_path) / "mvnw"
            if wrapper.is_file():
                return str(wrapper)
        return "mvn"


def detect_build_system(repo_path: str) -> str:
    """Detect Maven vs Gradle from project files."""
    p = Path(repo_path)
    has_maven = (p / "pom.xml").exists()
    has_gradle = (p / "build.gradle").exists() or (p / "build.gradle.kts").exists()

    if has_gradle and not has_maven:
        return "gradle"
    if has_maven and not has_gradle:
        return "maven"
    if has_maven and has_gradle:
        # Maven takes precedence (more common in benchmarking contexts)
        return "maven"
    raise ValueError(f"No build system detected in {repo_path}")


def detect_modules(repo_path: str, build_system: str) -> List[str]:
    """Detect submodules in a multi-module project."""
    p = Path(repo_path)
    if build_system == "maven":
        pom_path = p / "pom.xml"
        if not pom_path.exists():
            return []
        try:
            pom = ET.parse(pom_path)
        except ET.ParseError:
            return []
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        modules_el = pom.find(".//m:modules", ns)
        if modules_el is not None:
            return [m.text for m in modules_el.findall("m:module", ns) if m.text]
    elif build_system == "gradle":
        settings = p / "settings.gradle"
        if not settings.exists():
            settings = p / "settings.gradle.kts"
        if settings.exists():
            content = settings.read_text()
            return re.findall(r"include\s*['\"]([^'\"]+)['\"]", content)
    return []
