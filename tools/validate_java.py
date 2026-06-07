import logging
import subprocess
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple
from dataclasses import dataclass

if TYPE_CHECKING:
    import docker

from commit0.harness.constants_java import (
    JAVA_BASE_IMAGE_PREFIX,
    detect_build_system,
    resolve_build_cmd,
)
from commit0.harness.docker_utils import (
    cleanup_container,
    create_container,
    exec_run_with_timeout,
)

logger = logging.getLogger(__name__)


@dataclass
class JavaValidationResult:
    repo: str
    valid: bool
    build_system: str
    java_version: str
    test_framework: str
    test_count: int
    compiles: bool
    tests_pass: bool
    estimated_test_time_seconds: float
    issues: List[str]
    warnings: List[str]


def validate_java_repo(
    repo_path: str,
    use_docker: bool = True,
    java_version: str = "17",
) -> JavaValidationResult:
    issues = []
    warnings = []
    p = Path(repo_path)

    build_system = _detect_build_system(p, issues)
    java_version = _detect_java_version(p, build_system, warnings) or java_version
    _check_source_layout(p, issues)
    test_framework = _detect_test_framework(p, build_system)

    test_count = _count_test_files(p)
    if test_count < 10:
        issues.append(f"Only {test_count} test files found (minimum: 10)")

    _check_license(p, warnings)
    _check_preview_features(p, build_system, warnings)

    docker_image = f"{JAVA_BASE_IMAGE_PREFIX}{java_version}:latest"
    runner = _get_runner(use_docker, docker_image, p)

    compiles = _try_compile(p, build_system, runner)
    if not compiles:
        issues.append("Project does not compile cleanly")

    tests_pass, test_time = _try_tests(p, build_system, runner)
    if not tests_pass:
        warnings.append("Some tests fail on clean checkout")

    return JavaValidationResult(
        repo=p.name,
        valid=len(issues) == 0,
        build_system=build_system,
        java_version=java_version,
        test_framework=test_framework,
        test_count=test_count,
        compiles=compiles,
        tests_pass=tests_pass,
        estimated_test_time_seconds=test_time,
        issues=issues,
        warnings=warnings,
    )


def _get_runner(
    use_docker: bool, docker_image: str, repo_path: Path
) -> Optional["_DockerRunner"]:
    if not use_docker:
        return None
    try:
        import docker
    except ImportError:
        logger.warning("docker package not installed, falling back to host execution")
        return None
    try:
        client = docker.from_env()
        client.images.get(docker_image)
        return _DockerRunner(client, docker_image, repo_path)
    except (docker.errors.ImageNotFound, docker.errors.APIError) as e:
        logger.warning(
            "Docker image %s not available (%s), falling back to host execution",
            docker_image,
            e,
        )
        return None


class _DockerRunner:
    def __init__(
        self,
        client: "docker.DockerClient",
        image: str,
        repo_path: Path,
    ):
        self.client = client
        self.image = image
        self.repo_path = repo_path
        self.container = None

    def run(self, cmd: str, timeout: int = 300) -> Tuple[int, str]:
        try:
            container = create_container(
                client=self.client,
                image_name=self.image,
                container_name=f"commit0-java-validate-{id(self) % 10000}",
                nano_cpus=int(2e9),
                logger=logger,
            )
            self.container = container

            from commit0.harness.docker_utils import copy_to_container
            copy_to_container(container, self.repo_path, Path("/workspace"))

            full_cmd = f"cd /workspace/{self.repo_path.name} && {cmd}"
            output, timed_out, elapsed = exec_run_with_timeout(
                container, full_cmd, timeout
            )
            if timed_out:
                return 1, output
            _FAILURE_MARKERS = ("BUILD FAILURE", "BUILD FAILED", "COMPILATION ERROR", "FAILED")
            if any(marker in output for marker in _FAILURE_MARKERS):
                return 1, output
            return 0, output
        except Exception as e:
            logger.error("Docker execution failed: %s", e)
            return 1, str(e)
        finally:
            if self.container:
                try:
                    cleanup_container(self.client, self.container, logger)
                except Exception:
                    pass
                self.container = None


def _detect_build_system(p: Path, issues: List[str]) -> str:
    try:
        return detect_build_system(str(p))
    except ValueError:
        issues.append("No Maven or Gradle build system detected")
        return "unknown"


def _detect_java_version(p: Path, build_system: str, warnings: List[str]) -> str:
    if build_system == "maven":
        try:
            pom = (p / "pom.xml").read_text()
        except (FileNotFoundError, OSError):
            warnings.append("Could not read pom.xml for Java version detection")
            return "17"
        match = re.search(r"<maven\.compiler\.source>(\d+)</maven\.compiler\.source>", pom)
        if match:
            return match.group(1)
        match = re.search(r"<release>(\d+)</release>", pom)
        if match:
            return match.group(1)
    elif build_system == "gradle":
        for fname in ["build.gradle", "build.gradle.kts"]:
            gfile = p / fname
            if gfile.exists():
                try:
                    content = gfile.read_text()
                except (FileNotFoundError, OSError):
                    continue
                match = re.search(r"sourceCompatibility\s*=\s*['\"]?(\d+)", content)
                if match:
                    return match.group(1)
    warnings.append("Could not detect Java version, defaulting to 17")
    return "17"


def _check_source_layout(p: Path, issues: List[str]) -> None:
    src_main = p / "src" / "main" / "java"
    src_test = p / "src" / "test" / "java"
    if not src_main.exists():
        issues.append("Missing standard source layout: src/main/java/")
    if not src_test.exists():
        issues.append("Missing standard test layout: src/test/java/")


def _detect_test_framework(p: Path, build_system: str) -> str:
    content = ""
    if build_system == "maven":
        content = (p / "pom.xml").read_text() if (p / "pom.xml").exists() else ""
    elif build_system == "gradle":
        for f in ["build.gradle", "build.gradle.kts"]:
            if (p / f).exists():
                content = (p / f).read_text()
                break
    if "testng" in content.lower():
        return "testng"
    if "junit-jupiter" in content or "junit5" in content.lower():
        return "junit5"
    return "junit4"


def _count_test_files(p: Path) -> int:
    test_dir = p / "src" / "test" / "java"
    if not test_dir.exists():
        return 0
    count = 0
    for f in test_dir.rglob("*.java"):
        name = f.stem
        if name.endswith("Test") or name.endswith("Tests") or name.endswith("TestCase") or name.startswith("Test"):
            count += 1
    return count


def _check_license(p: Path, warnings: List[str]) -> None:
    license_files = list(p.glob("LICENSE*")) + list(p.glob("LICENCE*"))
    if not license_files:
        warnings.append("No LICENSE file found")


def _check_preview_features(p: Path, build_system: str, warnings: List[str]) -> None:
    content = ""
    if build_system == "maven" and (p / "pom.xml").exists():
        content = (p / "pom.xml").read_text()
    elif build_system == "gradle":
        for f in ["build.gradle", "build.gradle.kts"]:
            if (p / f).exists():
                content = (p / f).read_text()
    if "--enable-preview" in content:
        warnings.append("Uses --enable-preview — may break across JDK versions")


def _try_compile(
    p: Path, build_system: str, runner: Optional[_DockerRunner] = None
) -> bool:
    # Docker runner: no repo_path (use system commands inside container)
    # Local: pass repo_path to prefer ./mvnw or ./gradlew wrappers
    build_cmd = resolve_build_cmd(build_system, None if runner else str(p))
    if build_system == "gradle":
        cmd = f"{build_cmd} classes --no-daemon -q"
    else:
        cmd = f"{build_cmd} compile -q -B"

    if runner:
        returncode, _ = runner.run(cmd, timeout=300)
        return returncode == 0

    try:
        result = subprocess.run(
            cmd.split(), cwd=p, capture_output=True, timeout=300
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _try_tests(
    p: Path,
    build_system: str,
    runner: Optional[_DockerRunner] = None,
) -> Tuple[bool, float]:
    build_cmd = resolve_build_cmd(build_system, None if runner else str(p))
    if build_system == "gradle":
        cmd = f"{build_cmd} test --no-daemon"
    else:
        cmd = f"{build_cmd} test -B"

    if runner:
        start = time.time()
        returncode, _ = runner.run(cmd, timeout=600)
        elapsed = time.time() - start
        return returncode == 0, elapsed

    try:
        start = time.time()
        result = subprocess.run(
            cmd.split(), cwd=p, capture_output=True, timeout=600
        )
        elapsed = time.time() - start
        return result.returncode == 0, elapsed
    except subprocess.TimeoutExpired:
        return False, 600.0
    except FileNotFoundError:
        return False, 0.0
