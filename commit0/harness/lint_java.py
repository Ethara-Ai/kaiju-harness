"""Java linting — Checkstyle for style, compilation check for correctness."""
import hashlib
import os
import subprocess
import logging
import defusedxml.ElementTree as ET
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass

from commit0.harness.constants_java import resolve_build_cmd
from commit0.harness.spec_java import Commit0JavaSpec

logger = logging.getLogger(__name__)


@dataclass
class JavaLintResult:
    file: str
    line: int
    column: int
    severity: str
    message: str
    rule: str


CHECKSTYLE_VERSIONS: Dict[str, Dict[str, str]] = {
    "10.12.5": {
        "url": "https://github.com/checkstyle/checkstyle/releases/download/"
               "checkstyle-10.12.5/checkstyle-10.12.5-all.jar",
        "sha256": "2ef529cfe82580d71b2db22a3c58c1bc0a57cc9a7e73e834ee9d9f2c53d1e21e",
    },
    "10.21.4": {
        "url": "https://github.com/checkstyle/checkstyle/releases/download/"
               "checkstyle-10.21.4/checkstyle-10.21.4-all.jar",
        "sha256": "3c1d94d6ecc83e02dff587c9ba5b6b4ec4fec38c7a958eb587efec9b28e2f318",
    },
}

DEFAULT_CHECKSTYLE_VERSION = "10.12.5"


def _get_checkstyle_version() -> str:
    return os.environ.get("COMMIT0_CHECKSTYLE_VERSION", DEFAULT_CHECKSTYLE_VERSION)


def _get_checkstyle_url(version: str) -> str:
    if version in CHECKSTYLE_VERSIONS:
        return CHECKSTYLE_VERSIONS[version]["url"]
    return (
        f"https://github.com/checkstyle/checkstyle/releases/download/"
        f"checkstyle-{version}/checkstyle-{version}-all.jar"
    )


def _get_checkstyle_sha256(version: str) -> str:
    """Return expected SHA-256 hex digest, or empty string if unknown."""
    info = CHECKSTYLE_VERSIONS.get(version, {})
    return info.get("sha256", "")



def lint_java_checkstyle(
    repo_path: str,
    config: str = "google_checks.xml",
    checkstyle_version: Optional[str] = None,
) -> List[JavaLintResult]:
    """Run Checkstyle on Java source files (Google Java Style by default)."""
    version = checkstyle_version or _get_checkstyle_version()
    checkstyle_jar = _ensure_checkstyle(version)
    cmd = [
        "java", "-jar", str(checkstyle_jar),
        "-c", f"/{config}",
        "-f", "xml",
        repo_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return _parse_checkstyle_xml(result.stdout)


def lint_java_compilation(repo_path: str, build_system: str) -> bool:
    """Run a compilation check. Returns True if compilation succeeds."""
    build_cmd = resolve_build_cmd(build_system, repo_path)
    if build_system == "gradle":
        cmd = [build_cmd, "compileJava", "--no-daemon", "-q"]
    else:
        cmd = [build_cmd, "compile", "-q", "-B"] + Commit0JavaSpec._MVN_SKIP_FLAGS.split()
    result = subprocess.run(
        cmd, cwd=repo_path, capture_output=True, text=True, timeout=300,
    )
    return result.returncode == 0



def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_checkstyle(version: Optional[str] = None) -> Path:
    """Download Checkstyle JAR if missing; verify SHA-256 when known."""
    version = version or _get_checkstyle_version()
    jar_dir = Path.home() / ".commit0" / "tools"
    jar_path = jar_dir / f"checkstyle-{version}.jar"

    if jar_path.exists():
        expected = _get_checkstyle_sha256(version)
        if expected:
            actual = _sha256_file(jar_path)
            if actual != expected:
                logger.warning(
                    "Checkstyle JAR checksum mismatch (expected %s, got %s). "
                    "Re-downloading.",
                    expected[:16],
                    actual[:16],
                )
                jar_path.unlink()
            else:
                return jar_path
        else:
            return jar_path

    jar_dir.mkdir(parents=True, exist_ok=True)
    url = _get_checkstyle_url(version)
    logger.info("Downloading Checkstyle %s from %s", version, url)

    tmp_path = jar_path.with_suffix(".jar.tmp")
    try:
        urllib.request.urlretrieve(url, tmp_path)  # nosec B310 - URL is version-keyed from CHECKSTYLE_VERSIONS (hardcoded https://github.com/checkstyle); artifact SHA256-verified at lines 140-150 before use
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    expected = _get_checkstyle_sha256(version)
    if expected:
        actual = _sha256_file(tmp_path)
        if actual != expected:
            tmp_path.unlink()
            raise RuntimeError(
                f"Checkstyle JAR checksum verification failed.\n"
                f"  Expected: {expected}\n"
                f"  Actual:   {actual}\n"
                f"  URL:      {url}"
            )

    tmp_path.rename(jar_path)

    legacy = jar_dir / "checkstyle.jar"
    if legacy.exists() and legacy != jar_path:
        legacy.unlink(missing_ok=True)

    return jar_path



def _parse_checkstyle_xml(xml_output: str) -> List[JavaLintResult]:
    """Parse Checkstyle XML output into structured results."""
    results: List[JavaLintResult] = []
    if not xml_output or not xml_output.strip():
        return results
    try:
        root = ET.fromstring(xml_output)
        for file_el in root.findall("file"):
            filepath = file_el.get("name", "")
            for error in file_el.findall("error"):
                results.append(JavaLintResult(
                    file=filepath,
                    line=int(error.get("line", 0)),
                    column=int(error.get("column", 0)),
                    severity=error.get("severity", "warning"),
                    message=error.get("message", ""),
                    rule=error.get("source", ""),
                ))
    except ET.ParseError as e:
        logger.warning("Failed to parse Checkstyle XML output: %s", e)
    return results
