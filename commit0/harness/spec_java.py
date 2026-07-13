"""Java Spec implementation for Commit0.

Parallel to spec.py — does NOT modify spec.py.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from commit0.harness.spec import Spec
from commit0.harness.eval_hardening import (
    revert_and_clean_lines,
    guard_snapshot_lines,
    guard_heal_lines,
)
from commit0.harness.constants import ABSOLUTE_REPO_DIR, RELATIVE_REPO_DIR
from commit0.harness.constants_java import (
    JAVA_BASE_IMAGE_PREFIX,
    SUPPORTED_JAVA_VERSIONS,
)


@dataclass
class Commit0JavaSpec(Spec):
    """Java-specific Spec implementation.

    Overrides only the language-specific behavior while inheriting
    all shared infrastructure from Spec ABC.
    """

    java_version: str = "17"
    build_system: str = "maven"
    test_framework: str = "junit5"
    test_ids: Optional[List[str]] = None

    @property
    def base_image_key(self) -> str:
        return f"{JAVA_BASE_IMAGE_PREFIX}{self.java_version}:latest"

    @property
    def repo_image_key(self) -> str:
        # Docker image repository names MUST be lowercase — a repo like
        # stleary/JSON-java would otherwise yield 'commit0-java-JSON-java:latest'
        # and fail the build with "repository name must be lowercase". Lowercase
        # the whole tag (single source of truth, so the runner's agent-image FROM
        # and every reference stay consistent).
        repo_short = self.repo.split("/")[-1]
        return f"{JAVA_BASE_IMAGE_PREFIX}-{repo_short}:latest".lower()

    @property
    def base_dockerfile(self) -> str:
        """Read Dockerfile template directly — no __init__.py modification."""
        dockerfile_path = (
            Path(__file__).parent / "dockerfiles" / f"Dockerfile.java{self.java_version}"
        )
        if not dockerfile_path.exists():
            raise FileNotFoundError(
                f"No Dockerfile for Java {self.java_version}. "
                f"Supported: {SUPPORTED_JAVA_VERSIONS}"
            )
        return dockerfile_path.read_text()

    @property
    def repo_dockerfile(self) -> str:
        return (
            f"FROM {self.base_image_key}\n"
            f"COPY ./setup.sh /root/\n"
            f"RUN chmod +x /root/setup.sh && /bin/bash /root/setup.sh\n"
            f"WORKDIR {self.repo_directory}\n"
        )

    @staticmethod
    def _wrapper_preamble() -> List[str]:
        """Shell preamble that detects ./gradlew or ./mvnw wrappers.

        Sets $GRADLE_CMD and $MVN_CMD for use in subsequent commands.
        Wrappers are preferred because they pin the exact tool version the
        project needs — system gradle/mvn are fallbacks only.
        """
        return [
            '# Prefer project wrappers over system-installed build tools',
            'if [ -f ./gradlew ]; then chmod +x ./gradlew; GRADLE_CMD=./gradlew; else GRADLE_CMD=gradle; fi',
            'if [ -f ./mvnw ]; then chmod +x ./mvnw; MVN_CMD=./mvnw; else MVN_CMD=mvn; fi',
        ]

    def _get_dependency_install_cmd(self) -> str:
        if self.build_system == "gradle":
            return "$GRADLE_CMD dependencies --no-daemon -q || true"
        return "$MVN_CMD dependency:resolve -q -B || true"

    # Maven flags that skip common non-compilation plugins (license audits,
    # enforcer rules, Javadoc, source JARs, etc.) so we only care about
    # whether the code actually compiles and tests pass.
    _MVN_SKIP_FLAGS = (
        "-Drat.skip=true "
        "-Denforcer.skip=true "
        "-Dmaven.javadoc.skip=true "
        "-Dsource.skip=true "
        "-Djacoco.skip=true "
        "-Dcheckstyle.skip=true "
        "-Dspotbugs.skip=true "
        "-Dpmd.skip=true "
        "-Dcpd.skip=true "
        "-Danimal.sniffer.skip=true "
        "-Dmaven.buildnumber.skip=true "
        "-Dmaven.gitcommitid.skip=true "
        "-Dgpg.skip=true"
    )

    # Bound maven's HTTP waits so a slow/blocked registry (no network egress, MITM
    # proxy, uncached artifact) fails FAST instead of hanging the eval budget.
    _MVN_NET_FLAGS = (
        "-Dmaven.wagon.http.retryHandler.count=2 "
        "-Dmaven.wagon.httpconnectionManager.ttlSeconds=30 "
        "-Daether.connector.connectTimeout=15000 "
        "-Daether.connector.requestTimeout=60000"
    )

    # Surefire per-FORK timeout (forkedProcessTimeoutInSeconds): if the test fork
    # runs longer than this, surefire kills it and writes the reports produced so
    # far — so a single infinite-loop test can't consume the whole eval budget.
    # Overridable via env; default 300s.
    _SUREFIRE_FORK_TIMEOUT = "-Dsurefire.timeout=${KAIJU_JAVA_FORK_TIMEOUT:-300}"

    def _get_compile_cmd(self) -> str:
        if self.build_system == "gradle":
            return "$GRADLE_CMD classes testClasses --no-daemon -q"
        return (
            f"$MVN_CMD compile test-compile -q -B "
            f"{self._MVN_SKIP_FLAGS} {self._MVN_NET_FLAGS}"
        )

    def _get_test_cmd(self, test_ids: Optional[List[str]] = None) -> str:
        if self.build_system == "gradle":
            if test_ids:
                filters = " ".join(f"--tests {tid}" for tid in test_ids)
                return f"$GRADLE_CMD test {filters} --no-daemon"
            return "$GRADLE_CMD test --no-daemon"
        else:
            common = f"-B {self._MVN_SKIP_FLAGS} {self._MVN_NET_FLAGS} {self._SUREFIRE_FORK_TIMEOUT}"
            if test_ids:
                # Maven Surefire: -Dtest=TestClass#testMethod
                tests = ",".join(test_ids)
                return f'$MVN_CMD test -Dtest="{tests}" {common}'
            return f"$MVN_CMD test {common}"

    def _get_report_dir(self) -> str:
        if self.build_system == "gradle":
            return "build/test-results/test"
        return "target/surefire-reports"

    def _collect_reports_commands(self) -> List[str]:
        """Consolidate test reports from multi-module projects.

        Multi-module Maven/Gradle projects put reports in each submodule's
        target dir.  We merge them into the top-level report dir so a single
        ``files_to_collect`` path covers all modules.
        """
        report_dir = self._get_report_dir()
        return [
            f"mkdir -p {report_dir}",
            f'find . -mindepth 2 -path "*/{report_dir}/*.xml" '
            f'-exec cp -n {{}} {report_dir}/ \\;',
        ]

    def make_repo_script_list(self) -> List[str]:
        """Scripts to prepare the repo inside Docker.

        Mirrors Commit0Spec: clone repo, fetch commits, reset to base.
        """
        repo = self.instance["repo"]
        base_commit = self.instance.get("base_commit")
        if not base_commit:
            raise ValueError(
                f"'base_commit' is required in instance data for repo '{repo}'"
            )
        reference_commit = self.instance.get("reference_commit", base_commit)

        # `git fetch --depth 1 origin <tag>` only writes FETCH_HEAD, not a local
        # ref — `git reset --hard <tag>` then fails.  Fetch each ref separately
        # and create a local tag so later `git reset --hard` can resolve it.
        return [
            f"git clone --depth 1 -o origin https://github.com/{repo} {self.repo_directory}",
            f"chmod -R 777 {self.repo_directory}",
            f"cd {self.repo_directory}",
            f"git fetch --depth 1 origin {reference_commit} && git tag -f {reference_commit} FETCH_HEAD 2>/dev/null || true",
            f"git fetch --depth 1 origin {base_commit} && git tag -f {base_commit} FETCH_HEAD 2>/dev/null || true",
            f"git reset --hard {reference_commit}",
            "git submodule update --init --recursive 2>/dev/null || true",
            "git remote remove origin",
            f"git reset --hard {base_commit}",
            *self._wrapper_preamble(),
            *self._toolchains_xml_commands(),
            self._get_dependency_install_cmd(),
        ]

    @staticmethod
    def _toolchains_xml_commands() -> List[str]:
        """Create a fake ~/.m2/toolchains.xml mapping all common JDK versions to JAVA_HOME.

        Guava v33.5+ uses the Maven toolchains-maven-plugin which requires
        explicit toolchain definitions.  In Docker images with only one JDK
        installed we point every requested version at JAVA_HOME.
        """
        return [
            "mkdir -p ~/.m2",
            (
                'JAVA_HOME_DIR="${JAVA_HOME:-$(dirname $(dirname $(readlink -f $(which java))))}" && '
                'cat > ~/.m2/toolchains.xml << TOOLXML\n'
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<toolchains>\n"
                + "".join(
                    f'  <toolchain><type>jdk</type><provides><version>{v}</version>'
                    f"<vendor>temurin</vendor></provides>"
                    f"<configuration><jdkHome>$JAVA_HOME_DIR</jdkHome></configuration></toolchain>\n"
                    for v in ("8", "11", "17", "21", "22", "23", "24")
                )
                + "</toolchains>\nTOOLXML"
            ),
        ]

    def make_eval_script_list(self) -> List[str]:
        """Evaluation script: reset -> apply patch -> compile -> test -> capture.

        Matches the Spec ABC signature: make_eval_script_list(self) -> list[str].
        Test IDs are configured at spec construction time via instance data,
        not passed as a parameter (the ABC does not accept extra params).
        """
        report_dir = self._get_report_dir()
        compile_cmd = self._get_compile_cmd()
        test_cmd = self._get_test_cmd(self.test_ids)

        repo = self.instance["repo"]
        base_commit = self.instance.get("base_commit")
        if not base_commit:
            raise ValueError(
                f"'base_commit' is required in instance data for repo '{repo}'"
            )
        # Per-pathspec revert + delete model-added build/test files. Adds the
        # Maven/Gradle wrapper + settings (a wrapper or settings.xml can skip
        # tests or point at a poisoned repo). See eval_hardening.
        revert_lines = revert_and_clean_lines(
            base_commit,
            revert_targets=[
                "src/test/", "src/androidTest/", "src/integrationTest/",
                "*Test.java", "*Tests.java", "*IT.java", "*Spec.groovy",
                "pom.xml", "build.gradle", "build.gradle.kts",
                "settings.gradle", "settings.gradle.kts",
                "gradle.properties", "gradlew", "gradlew.bat", "gradle/",
                ".mvn/", "mvnw", "mvnw.cmd",
                "sitecustomize.py", "usercustomize.py",
                ".env", ".gitmodules", ".gitattributes",
            ],
            delete_added_globs=[
                "*Test.java", "**/*Test.java", "*Tests.java", "**/*Tests.java",
                "*IT.java", "**/*IT.java",
                "pom.xml", "**/pom.xml", "build.gradle", "**/build.gradle",
                "build.gradle.kts", "**/build.gradle.kts",
                "settings.gradle*", "**/settings.gradle*",
            ],
        )
        return [
            f"cd {self.repo_directory}",
            *self._wrapper_preamble(),
            *self._toolchains_xml_commands(),
            f"git remote add origin https://github.com/{repo} 2>/dev/null || true",
            # Bound the per-eval fetch so a slow/blocked network can't stall the eval
            # (the worktree is already at base for local_inplace; the fetch is a
            # best-effort refresh).
            f"timeout 180 git fetch --depth 1 origin {base_commit} && git tag -f {base_commit} FETCH_HEAD 2>/dev/null || true",
            f"git reset --hard {base_commit}",
            # A failed `git apply` must NOT fall through to compiling/testing the
            # UNPATCHED base tree — that scores a bad patch as a model 0% (or, if
            # base happens to build, silently tests the wrong code). Try exact then
            # --recount; on failure mark it a build failure and stop.
            "if [ -s /patch.diff ]; then",
            "  git apply -v /patch.diff || git apply --recount -v /patch.diff || { "
            "echo 'COMPILATION_FAILED' > test_exit_code.txt; "
            "echo 'PATCH_APPLY_FAILED: git apply failed' > compile_output.txt; exit 0; }",
            "fi",
            # Layer-2 guard: snapshot applied tree; heal any file the anti-cheat
            # rewrites corrupt before the compile, so a compiling submission is
            # never a false COMPILATION_FAILED from harness reconstruction.
            *guard_snapshot_lines(),
            *revert_lines,
            *guard_heal_lines(),
            # Compile first — Java fails fast on compile errors
            f"{compile_cmd} 2>&1 | tee compile_output.txt",
            "COMPILE_EXIT=${PIPESTATUS[0]}",
            'if [ "$COMPILE_EXIT" -ne 0 ]; then',
            '    echo "COMPILATION_FAILED" > test_exit_code.txt',
            '    exit 0',
            'fi',
            # Run tests, bounding the WHOLE test phase (belt-and-suspenders with the
            # surefire per-fork timeout baked into test_cmd): a hung/infinite-loop
            # test or a stuck maven download can no longer consume the entire eval
            # budget. On expiry (rc 124) the surefire reports written so far are
            # STILL collected below, so the eval yields partial results instead of a
            # 3600s hang -> 0/0. `-k 15` hard-kills if SIGTERM is ignored.
            f"timeout -k 15 ${{KAIJU_JAVA_TEST_TIMEOUT:-900}} {test_cmd} 2>&1 | tee test_output.txt",
            "echo $? > test_exit_code.txt",
            # Consolidate reports from all modules into the top-level report dir
            *self._collect_reports_commands(),
            # Capture test reports
            f"if [ -d {report_dir} ]; then",
            f"    ls {report_dir}/ > /dev/null",
            "fi",
        ]


def make_java_spec(
    instance: dict,
    dataset_type: str = "commit0",
    absolute: bool = True,
    test_ids: Optional[List[str]] = None,
) -> Commit0JavaSpec:
    """Factory to create a Commit0JavaSpec from a repo instance dict.

    Parallel to make_spec() — does NOT modify make_spec().
    """
    repo_directory = ABSOLUTE_REPO_DIR if absolute else RELATIVE_REPO_DIR
    setup = instance.get("setup", {})
    java_version = instance.get("java_version") or setup.get("java_version", "17")
    raw_build = instance.get("build_system") or setup.get("build_system", "maven")
    build_system = raw_build if raw_build not in (None, "", "auto") else "maven"
    test_framework = instance.get("test_framework", "junit5")

    return Commit0JavaSpec(
        repo=instance.get("instance_id", instance.get("repo", "")),
        repo_directory=repo_directory,
        instance=instance,
        absolute=absolute,
        java_version=java_version,
        build_system=build_system,
        test_framework=test_framework,
        test_ids=test_ids,
    )
