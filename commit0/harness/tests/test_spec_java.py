from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from commit0.harness.spec_java import (
    Commit0JavaSpec,
    make_java_spec,
)

MODULE = "commit0.harness.spec_java"


def _make_instance(**overrides: object) -> dict:
    defaults: dict = {
        "instance_id": "apache/commons-lang",
        "repo": "apache/commons-lang",
        "base_commit": "abc1230000000000000000000000000000000000",
        "reference_commit": "def4560000000000000000000000000000000000",
        "setup": {},
        "test": {"test_cmd": "mvn test"},
        "src_dir": "src",
    }
    defaults.update(overrides)
    return defaults


def _make_spec(
    build_system: str = "maven",
    java_version: str = "17",
    test_ids: list[str] | None = None,
    **instance_overrides: object,
) -> Commit0JavaSpec:
    instance = _make_instance(**instance_overrides)
    return Commit0JavaSpec(
        repo=instance["instance_id"],
        repo_directory="/testbed",
        instance=instance,
        absolute=True,
        java_version=java_version,
        build_system=build_system,
        test_framework="junit5",
        test_ids=test_ids,
    )


class TestBaseImageKey:
    def test_java17_format(self) -> None:
        spec = _make_spec(java_version="17")
        assert spec.base_image_key == "commit0-java17:latest"

    def test_java21_format(self) -> None:
        spec = _make_spec(java_version="21")
        assert spec.base_image_key == "commit0-java21:latest"


class TestRepoImageKey:
    def test_format_with_hash(self) -> None:
        spec = _make_spec()
        key = spec.repo_image_key
        assert key.startswith("commit0-java-")
        assert key.endswith(":latest")

    def test_format_with_slash_repo(self) -> None:
        spec = _make_spec(instance_id="org/myrepo", repo="org/myrepo")
        key = spec.repo_image_key
        assert "myrepo" in key


class TestRepoDockerfile:
    def test_contains_from_base(self) -> None:
        spec = _make_spec()
        df = spec.repo_dockerfile
        assert df.startswith(f"FROM {spec.base_image_key}")


class TestWrapperPreamble:
    def test_contains_mvnw_detection(self) -> None:
        lines = Commit0JavaSpec._wrapper_preamble()
        joined = "\n".join(lines)
        assert "mvnw" in joined

    def test_contains_gradlew_detection(self) -> None:
        lines = Commit0JavaSpec._wrapper_preamble()
        joined = "\n".join(lines)
        assert "gradlew" in joined


class TestBuildCommands:
    def test_maven_dependency_install(self) -> None:
        spec = _make_spec(build_system="maven")
        cmd = spec._get_dependency_install_cmd()
        assert "$MVN_CMD" in cmd
        assert "dependency:resolve" in cmd

    def test_gradle_dependency_install(self) -> None:
        spec = _make_spec(build_system="gradle")
        cmd = spec._get_dependency_install_cmd()
        assert "$GRADLE_CMD" in cmd
        assert "dependencies" in cmd

    def test_maven_compile(self) -> None:
        spec = _make_spec(build_system="maven")
        cmd = spec._get_compile_cmd()
        assert "$MVN_CMD" in cmd
        assert "compile" in cmd

    def test_gradle_compile(self) -> None:
        spec = _make_spec(build_system="gradle")
        cmd = spec._get_compile_cmd()
        assert "$GRADLE_CMD" in cmd
        assert "classes" in cmd


class TestMvnSkipFlags:
    def test_contains_expected_flags(self) -> None:
        flags = Commit0JavaSpec._MVN_SKIP_FLAGS
        assert "maven.test.skip" not in flags
        assert "gpg.skip=true" in flags
        assert "checkstyle.skip=true" in flags
        assert "javadoc.skip=true" in flags


class TestToolchainsXml:
    def test_contains_jdk_versions(self) -> None:
        cmds = Commit0JavaSpec._toolchains_xml_commands()
        joined = "\n".join(cmds)
        for v in ("8", "11", "17", "21"):
            assert f"<version>{v}</version>" in joined

    def test_valid_xml_structure(self) -> None:
        cmds = Commit0JavaSpec._toolchains_xml_commands()
        joined = "\n".join(cmds)
        assert "<toolchains>" in joined
        assert "</toolchains>" in joined


class TestReportDir:
    def test_maven_returns_surefire(self) -> None:
        spec = _make_spec(build_system="maven")
        assert spec._get_report_dir() == "target/surefire-reports"

    def test_gradle_returns_test_results(self) -> None:
        spec = _make_spec(build_system="gradle")
        assert spec._get_report_dir() == "build/test-results/test"


class TestMakeRepoScriptList:
    @patch.object(Commit0JavaSpec, "base_dockerfile", new_callable=lambda: property(lambda self: "FROM base"))
    def test_contains_clone(self, mock_df: MagicMock) -> None:
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        joined = "\n".join(scripts)
        assert "git clone" in joined

    @patch.object(Commit0JavaSpec, "base_dockerfile", new_callable=lambda: property(lambda self: "FROM base"))
    def test_contains_reset(self, mock_df: MagicMock) -> None:
        spec = _make_spec()
        scripts = spec.make_repo_script_list()
        joined = "\n".join(scripts)
        assert "git reset --hard" in joined


class TestMakeEvalScriptList:
    def test_contains_apply_patch(self) -> None:
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        joined = "\n".join(scripts)
        assert "git apply" in joined

    def test_contains_test_cmd(self) -> None:
        spec = _make_spec(build_system="maven")
        scripts = spec.make_eval_script_list()
        joined = "\n".join(scripts)
        assert "$MVN_CMD test" in joined

    def test_compilation_failed_detection(self) -> None:
        spec = _make_spec()
        scripts = spec.make_eval_script_list()
        joined = "\n".join(scripts)
        assert "COMPILATION_FAILED" in joined


class TestMakeJavaSpec:
    def test_factory_creates_spec(self) -> None:
        instance = _make_instance()
        spec = make_java_spec(instance)
        assert isinstance(spec, Commit0JavaSpec) is True

    def test_missing_key_raises(self) -> None:
        with pytest.raises(AttributeError, match="get"):
            make_java_spec(None)  # type: ignore[arg-type]

    def test_default_dataset_type(self) -> None:
        instance = _make_instance()
        spec = make_java_spec(instance)
        assert spec.repo_directory == "/testbed"


class TestCollectReportsCommands:
    def test_maven_report_consolidation(self) -> None:
        spec = _make_spec(build_system="maven")
        cmds = spec._collect_reports_commands()
        joined = "\n".join(cmds)
        assert "surefire-reports" in joined

    def test_gradle_report_consolidation(self) -> None:
        spec = _make_spec(build_system="gradle")
        cmds = spec._collect_reports_commands()
        joined = "\n".join(cmds)
        assert "test-results" in joined


class TestTestCmd:
    def test_gradle_test_with_ids(self) -> None:
        spec = _make_spec(build_system="gradle", test_ids=["com.Foo#bar"])
        cmd = spec._get_test_cmd(spec.test_ids)
        assert "--tests com.Foo#bar" in cmd

    def test_maven_test_without_ids(self) -> None:
        spec = _make_spec(build_system="maven", test_ids=None)
        cmd = spec._get_test_cmd(spec.test_ids)
        assert "-Dtest=" not in cmd
        assert "$MVN_CMD test" in cmd


class TestEdgeCases:
    def test_multiple_test_ids_in_cmd(self) -> None:
        spec = _make_spec(build_system="maven", test_ids=["com.A#t1", "com.B#t2"])
        cmd = spec._get_test_cmd(spec.test_ids)
        assert "com.A#t1" in cmd
        assert "com.B#t2" in cmd

    def test_empty_setup_defaults(self) -> None:
        instance = _make_instance(setup={})
        spec = make_java_spec(instance)
        assert spec.java_version == "17"
        assert spec.build_system == "maven"

    def test_dataset_type_simple(self) -> None:
        instance = _make_instance()
        spec = make_java_spec(instance, dataset_type="simple")
        assert isinstance(spec, Commit0JavaSpec) is True

    def test_empty_test_ids(self) -> None:
        spec = _make_spec(build_system="gradle", test_ids=None)
        cmd = spec._get_test_cmd(spec.test_ids)
        assert "$GRADLE_CMD test" in cmd
