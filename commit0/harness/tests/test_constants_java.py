from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.constants_java import (
    JAVA_BASE_BRANCH,
    JAVA_BASE_IMAGE_PREFIX,
    JAVA_BUILD_DIRS,
    JAVA_CONTAINER_PREFIX,
    JAVA_REMOTE_BRANCH,
    JAVA_SKIP_FILENAMES,
    JAVA_SOURCE_EXT,
    JAVA_SPLIT,
    JAVA_SPLIT_LITE,
    JAVA_SRC_CONVENTION,
    JAVA_STUB_MARKER,
    JAVA_TEST_CONVENTION,
    JAVA_TEST_FILE_SUFFIX,
    JAVA_VERSION_DEFAULT,
    JavaRepoInstance,
    SUPPORTED_JAVA_VERSIONS,
    detect_build_system,
    detect_modules,
    resolve_build_cmd,
)

MODULE = "commit0.harness.constants_java"


class TestJavaRepoInstance:
    def test_default_fields(self) -> None:
        inst = JavaRepoInstance(
            instance_id="org/repo",
            repo="org-repo",
            base_commit="aaa",
            reference_commit="bbb",
            setup={},
            test={"cmd": "mvn test"},
            src_dir="src",
        )
        assert inst.language == "java"
        assert inst.java_version == "17"
        assert inst.build_system == "maven"
        assert inst.java_src_dir == "src/main/java"
        assert inst.test_dir == "src/test/java"
        assert inst.test_framework == "junit5"
        assert inst.has_modules is False
        assert inst.main_module is None

    def test_custom_fields(self) -> None:
        inst = JavaRepoInstance(
            instance_id="org/repo",
            repo="org-repo",
            base_commit="aaa",
            reference_commit="bbb",
            setup={},
            test={"cmd": "gradle test"},
            src_dir="src",
            language="java",
            java_version="21",
            build_system="gradle",
            java_src_dir="lib/src/main/java",
            test_dir="lib/src/test/java",
            test_framework="testng",
            has_modules=True,
            main_module="core",
        )
        assert inst.java_version == "21"
        assert inst.build_system == "gradle"
        assert inst.test_framework == "testng"
        assert inst.has_modules is True
        assert inst.main_module == "core"


class TestConstants:
    def test_supported_versions_are_set(self) -> None:
        assert isinstance(SUPPORTED_JAVA_VERSIONS, set)
        assert len(SUPPORTED_JAVA_VERSIONS) > 0


class TestResolveBuildCmd:
    def test_maven_default(self) -> None:
        result = resolve_build_cmd("maven")
        assert result == "mvn"

    def test_gradle_default(self) -> None:
        result = resolve_build_cmd("gradle")
        assert result == "gradle"

    def test_maven_wrapper(self, tmp_path: Path) -> None:
        wrapper = tmp_path / "mvnw"
        wrapper.touch()
        result = resolve_build_cmd("maven", str(tmp_path))
        assert result == str(wrapper)

    def test_gradle_wrapper(self, tmp_path: Path) -> None:
        wrapper = tmp_path / "gradlew"
        wrapper.touch()
        result = resolve_build_cmd("gradle", str(tmp_path))
        assert result == str(wrapper)


class TestDetectBuildSystem:
    def test_maven_only(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").touch()
        assert detect_build_system(str(tmp_path)) == "maven"

    def test_gradle_only(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").touch()
        assert detect_build_system(str(tmp_path)) == "gradle"

    def test_gradle_kts(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").touch()
        assert detect_build_system(str(tmp_path)) == "gradle"

    def test_both_prefer_maven(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").touch()
        (tmp_path / "build.gradle").touch()
        assert detect_build_system(str(tmp_path)) == "maven"

    def test_neither_raises_ValueError(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No build system detected"):
            detect_build_system(str(tmp_path))


class TestDetectModules:
    def test_maven_modules(self, tmp_path: Path) -> None:
        pom_content = (
            '<?xml version="1.0"?>'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<modules>"
            "<module>core</module>"
            "<module>api</module>"
            "</modules>"
            "</project>"
        )
        (tmp_path / "pom.xml").write_text(pom_content)
        result = detect_modules(str(tmp_path), "maven")
        assert result == ["core", "api"]

    def test_maven_no_modules(self, tmp_path: Path) -> None:
        pom_content = (
            '<?xml version="1.0"?>'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "</project>"
        )
        (tmp_path / "pom.xml").write_text(pom_content)
        result = detect_modules(str(tmp_path), "maven")
        assert result == []

    def test_gradle_modules(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.gradle"
        settings.write_text("include 'core'\ninclude 'api'\n")
        result = detect_modules(str(tmp_path), "gradle")
        assert result == ["core", "api"]

    def test_no_pom_returns_empty(self, tmp_path: Path) -> None:
        result = detect_modules(str(tmp_path), "maven")
        assert result == []


class TestEdgeCases:
    def test_unknown_build_system_defaults_mvn(self) -> None:
        result = resolve_build_cmd("unknown")
        assert result == "mvn"

    def test_none_repo_path_returns_system_cmd(self) -> None:
        result = resolve_build_cmd("maven", None)
        assert result == "mvn"

    def test_detect_build_system_nonexistent_raises(self) -> None:
        with pytest.raises(ValueError, match="No build system detected"):
            detect_build_system("/nonexistent/path/xyz")

    def test_detect_modules_malformed_xml_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<not valid xml>>>>")
        result = detect_modules(str(tmp_path), "maven")
        assert result == []

    def test_detect_modules_unknown_build_system(self, tmp_path: Path) -> None:
        result = detect_modules(str(tmp_path), "sbt")
        assert result == []

    def test_detect_build_system_none_path_raises(self) -> None:
        with pytest.raises((TypeError, ValueError)):
            detect_build_system(None)  # type: ignore[arg-type]

    def test_empty_pom_returns_empty_modules(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("")
        result = detect_modules(str(tmp_path), "maven")
        assert result == []

    def test_detect_modules_gradle_kts_settings(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.gradle.kts"
        settings.write_text('include "submod"\n')
        result = detect_modules(str(tmp_path), "gradle")
        assert result == ["submod"]

    def test_gradle_wrapper_not_executable_still_found(self, tmp_path: Path) -> None:
        wrapper = tmp_path / "gradlew"
        wrapper.write_text("#!/bin/sh\necho hi")
        result = resolve_build_cmd("gradle", str(tmp_path))
        assert result == str(wrapper)

    def test_maven_pom_no_namespace(self, tmp_path: Path) -> None:
        pom_content = (
            '<?xml version="1.0"?>'
            "<project>"
            "<modules>"
            "<module>core</module>"
            "</modules>"
            "</project>"
        )
        (tmp_path / "pom.xml").write_text(pom_content)
        result = detect_modules(str(tmp_path), "maven")
        # Without namespace, the namespace-qualified search won't find modules
        assert result == []


class TestBranchAndConventionConstants:
    def test_java_base_branch(self) -> None:
        assert JAVA_BASE_BRANCH == "commit0_java"

    def test_java_remote_branch(self) -> None:
        assert JAVA_REMOTE_BRANCH == "commit0_all"

    def test_java_stub_marker_contains_unsupported(self) -> None:
        assert "UnsupportedOperationException" in JAVA_STUB_MARKER

    def test_java_test_file_suffix(self) -> None:
        assert JAVA_TEST_FILE_SUFFIX == "Test.java"

    def test_java_source_ext(self) -> None:
        assert JAVA_SOURCE_EXT == ".java"

    def test_src_convention_ends_with_slash(self) -> None:
        assert JAVA_SRC_CONVENTION.endswith("/") is True

    def test_test_convention_ends_with_slash(self) -> None:
        assert JAVA_TEST_CONVENTION.endswith("/") is True

    def test_build_dirs_are_strings(self) -> None:
        for d in JAVA_BUILD_DIRS:
            assert isinstance(d, str) is True

    def test_skip_filenames_contains_module_info(self) -> None:
        assert "module-info.java" in JAVA_SKIP_FILENAMES

    def test_image_prefix_consistent(self) -> None:
        assert JAVA_BASE_IMAGE_PREFIX == JAVA_CONTAINER_PREFIX

    def test_version_default_in_supported(self) -> None:
        assert JAVA_VERSION_DEFAULT in SUPPORTED_JAVA_VERSIONS
