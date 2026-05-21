"""Java version detection for kaiju.

Replaces the regex-only ``_detect_java_version`` in
``tools/prepare_repo_java.py`` (which read pom.xml maven.compiler properties
and basic Gradle ``sourceCompatibility``, but missed modern Gradle toolchain
blocks, Kotlin DSL forms, GitHub Actions matrices, and never clamped to the
``SUPPORTED_JAVA_VERSIONS`` set).

Signal sources:

* **Tier A** — pom.xml properties (``maven.compiler.release/target``,
  ``java.version``), Gradle ``sourceCompatibility`` / ``targetCompatibility``,
  Gradle ``toolchain { languageVersion = JavaLanguageVersion.of(N) }``,
  ``.sdkmanrc``, ``.tool-versions``.
* **Tier B** — GitHub Actions matrix ``java-version`` / ``jdk``.
* **Tier C** — ``Dockerfile`` ``FROM (?:openjdk|eclipse-temurin|maven):X``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from tools._versioning import (
    DetectionResult,
    NoSignalsError,
    Signal,
    Tier,
    VersionConflictError,
    resolve_two_tier,
)

logger = logging.getLogger(__name__)

__all__ = [
    "collect_signals",
    "detect",
    "detect_from_signals",
    "normalize_java_version",
]


_POM_RELEASE = "pom.xml[maven.compiler.release]"
_POM_TARGET = "pom.xml[maven.compiler.target]"
_POM_JAVA_VERSION = "pom.xml[java.version]"
_GRADLE_SOURCE_COMPAT = "build.gradle[sourceCompatibility]"
_GRADLE_TARGET_COMPAT = "build.gradle[targetCompatibility]"
_GRADLE_TOOLCHAIN = "build.gradle[toolchain.languageVersion]"
_SDKMANRC = ".sdkmanrc[java]"
_TOOL_VERSIONS = ".tool-versions[java]"
_GHA_MATRIX = ".github/workflows/*.yml[matrix.java-version]"
_DOCKERFILE_FROM = "Dockerfile[FROM openjdk:X / eclipse-temurin:X]"


def normalize_java_version(raw: str) -> str | None:
    """Normalize a Java version string to a bare major (e.g. ``"1.8"`` → ``"8"``).

    Returns ``None`` if the string can't be interpreted as a Java version.
    """
    s = raw.strip().strip("\"'")
    if not s:
        return None
    # Strip vendor prefix from SDKMAN / asdf entries: "21.0.2-tem" → "21.0.2"
    s = re.split(r"[\-/]", s, maxsplit=1)[0]
    # Java 1.x → x (Java 1.8 = Java 8)
    m = re.match(r"^1\.(\d+)(?:\.\d+)?(?:_\d+)?$", s)
    if m:
        return m.group(1)
    # Plain "X" or "X.Y" or "X.Y.Z"
    m = re.match(r"^(\d+)(?:\.\d+){0,2}$", s)
    if m:
        return m.group(1)
    # JavaVersion.VERSION_X
    m = re.search(r"VERSION_(\d+)", s)
    if m:
        return m.group(1)
    return None


def _make_eq_signal(source: str, raw: str) -> Signal | None:
    """Build a Tier-A signal that pins to exactly one Java major version."""
    norm = normalize_java_version(raw)
    if norm is None:
        return None
    try:
        spec = SpecifierSet(f"=={norm}.*")
    except InvalidSpecifier:
        return None
    return Signal(source=source, tier=Tier.A_DECLARED, constraint=spec, raw=raw)


def _collect_pom(repo_root: Path) -> list[Signal]:
    pom = repo_root / "pom.xml"
    if not pom.is_file():
        return []
    try:
        content = pom.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Strip default namespace to make ElementTree XPath work without prefixes
    no_ns = re.sub(r'\sxmlns="[^"]+"', "", content, count=1)
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(no_ns)
    except ET.ParseError:
        return []

    signals: list[Signal] = []
    for xpath, label in [
        (".//maven.compiler.release", _POM_RELEASE),
        (".//maven.compiler.target", _POM_TARGET),
        (".//java.version", _POM_JAVA_VERSION),
    ]:
        for el in root.findall(xpath):
            if el.text and el.text.strip():
                sig = _make_eq_signal(label, el.text.strip())
                if sig is not None:
                    signals.append(sig)
                    break  # one per source — first occurrence wins
    return signals


def _collect_gradle(repo_root: Path) -> list[Signal]:
    signals: list[Signal] = []
    for gradle in [
        repo_root / "build.gradle",
        repo_root / "build.gradle.kts",
    ]:
        if not gradle.is_file():
            continue
        try:
            content = gradle.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # sourceCompatibility = JavaVersion.VERSION_17 / "17" / 17 / 1.8 / etc.
        m = re.search(
            r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?([\d.]+)",
            content,
        )
        if m:
            sig = _make_eq_signal(_GRADLE_SOURCE_COMPAT, m.group(1))
            if sig is not None:
                signals.append(sig)

        # targetCompatibility
        m = re.search(
            r"targetCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?([\d.]+)",
            content,
        )
        if m:
            sig = _make_eq_signal(_GRADLE_TARGET_COMPAT, m.group(1))
            if sig is not None:
                signals.append(sig)

        # toolchain { languageVersion = JavaLanguageVersion.of(17) }
        m = re.search(
            r"languageVersion\s*[=.]\s*JavaLanguageVersion\.of\s*\(\s*(\d+)\s*\)",
            content,
        )
        if m:
            sig = _make_eq_signal(_GRADLE_TOOLCHAIN, m.group(1))
            if sig is not None:
                signals.append(sig)
        # Once we find any signal, break — first build file wins
        if signals:
            break
    return signals


def _collect_sdkmanrc(repo_root: Path) -> Signal | None:
    f = repo_root / ".sdkmanrc"
    if not f.is_file():
        return None
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^\s*java\s*=\s*(\S+)", content, re.MULTILINE)
    if not m:
        return None
    return _make_eq_signal(_SDKMANRC, m.group(1))


def _collect_tool_versions(repo_root: Path) -> Signal | None:
    f = repo_root / ".tool-versions"
    if not f.is_file():
        return None
    try:
        content = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"^\s*java\s+(\S+)", content, re.MULTILINE)
    if not m:
        return None
    return _make_eq_signal(_TOOL_VERSIONS, m.group(1))


def _collect_gha_matrix(repo_root: Path) -> Signal | None:
    workflows = repo_root / ".github" / "workflows"
    if not workflows.is_dir():
        return None
    versions: set[str] = set()
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(
            r"(?:java|jdk)-version\s*:\s*(\[[^\]]+\]|(?:\n[ \t]+-[ \t]+['\"]?[^\n]+['\"]?)+|['\"]?[\d.]+['\"]?)",
            content,
        ):
            block = m.group(1)
            for v in re.findall(r"['\"]?(\d+(?:\.\d+){0,2})['\"]?", block):
                norm = normalize_java_version(v)
                if norm:
                    versions.add(norm)
    if not versions:
        return None
    return Signal(
        source=_GHA_MATRIX,
        tier=Tier.B_MATRIX,
        versions=tuple(sorted(versions, key=int)),
        raw=", ".join(sorted(versions, key=int)),
    )


def _collect_dockerfile(repo_root: Path) -> Signal | None:
    candidates = [
        repo_root / "Dockerfile",
        repo_root / "Dockerfile.dev",
        repo_root / "docker" / "Dockerfile",
    ]
    versions: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(
            r"^\s*FROM\s+(?:[\w./-]+/)?(?:openjdk|eclipse-temurin|amazoncorretto|maven):(\d+)",
            content,
            re.MULTILINE | re.IGNORECASE,
        ):
            versions.add(m.group(1))
    if not versions:
        return None
    return Signal(
        source=_DOCKERFILE_FROM,
        tier=Tier.C_HINT,
        versions=tuple(sorted(versions, key=int)),
        raw=", ".join(sorted(versions, key=int)),
    )


def collect_signals(repo_root: Path) -> list[Signal]:
    signals: list[Signal] = []
    signals.extend(_collect_pom(repo_root))
    signals.extend(_collect_gradle(repo_root))
    for collector in [
        _collect_sdkmanrc,
        _collect_tool_versions,
        _collect_gha_matrix,
        _collect_dockerfile,
    ]:
        try:
            sig = collector(repo_root)
        except Exception:  # noqa: BLE001
            logger.exception("Java collector %s crashed", collector.__name__)
            continue
        if sig is not None:
            signals.append(sig)
    return signals


def detect_from_signals(
    signals: list[Signal], supported: Iterable[str]
) -> DetectionResult:
    return resolve_two_tier(
        signals, supported, version_template="{}.0.0", pick="min"
    )


def detect(
    repo_root: Path,
    supported: Iterable[str],
    *,
    fallback: str | None = None,
    strict: bool = False,
) -> DetectionResult:
    signals = collect_signals(repo_root)
    result = detect_from_signals(signals, supported)
    if result.version is None:
        if strict:
            raise NoSignalsError(
                f"No Java version signals found in {repo_root}."
            )
        if fallback is not None:
            return DetectionResult(
                version=fallback,
                source="default",
                conflicts=[],
                all_signals=result.all_signals,
            )
    return result
