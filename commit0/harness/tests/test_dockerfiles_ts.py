from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from commit0.harness.dockerfiles_ts import (
    _BASE_APT_PACKAGES,
    detect_ts_system_dependencies,
    get_dockerfile_base_ts,
    get_dockerfile_repo_ts,
)


class TestDetectTsSystemDependencies:
    def test_empty_list(self) -> None:
        assert detect_ts_system_dependencies([]) == []

    def test_unknown_package_returns_empty(self) -> None:
        assert detect_ts_system_dependencies(["express"]) == []

    def test_known_package_returns_deps(self) -> None:
        result = detect_ts_system_dependencies(["sharp"])
        assert "libvips-dev" in result

    def test_canvas_returns_multiple_deps(self) -> None:
        result = detect_ts_system_dependencies(["canvas"])
        assert len(result) >= 3
        assert "libcairo2-dev" in result
        assert "libjpeg-dev" in result

    def test_types_packages_skipped(self) -> None:
        assert detect_ts_system_dependencies(["@types/node", "@types/jest"]) == []

    def test_scoped_package_version_stripped(self) -> None:
        result = detect_ts_system_dependencies(["sharp@0.33.0"])
        assert "libvips-dev" in result

    def test_scoped_npm_package_version_stripped(self) -> None:
        result = detect_ts_system_dependencies(["pg-native@3.0.0"])
        assert "libpq-dev" in result

    def test_version_with_caret(self) -> None:
        result = detect_ts_system_dependencies(["sharp^0.33"])
        assert "libvips-dev" in result

    def test_version_with_tilde(self) -> None:
        result = detect_ts_system_dependencies(["sharp~0.33"])
        assert "libvips-dev" in result

    def test_base_packages_excluded(self) -> None:
        result = detect_ts_system_dependencies(["bcrypt"])
        for pkg in _BASE_APT_PACKAGES:
            assert pkg not in result

    def test_deduplication(self) -> None:
        result = detect_ts_system_dependencies(["libxmljs", "libxmljs2"])
        assert result.count("libxml2-dev") == 1

    def test_result_sorted(self) -> None:
        result = detect_ts_system_dependencies(["canvas", "sharp"])
        assert result == sorted(result)

    def test_mixed_known_and_unknown(self) -> None:
        result = detect_ts_system_dependencies(["express", "sharp", "lodash"])
        assert result == ["libvips-dev"]

    def test_whitespace_stripped(self) -> None:
        result = detect_ts_system_dependencies(["  sharp  "])
        assert "libvips-dev" in result


class TestGetDockerfileBaseTs:
    @pytest.mark.parametrize("version", ["20", "22"])
    def test_valid_version_returns_content(self, version: str) -> None:
        result = get_dockerfile_base_ts(version)
        assert isinstance(result, str)
        assert len(result) > 0
        assert result.strip().startswith("FROM")

    def test_invalid_version_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base_ts("17")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base_ts("")

    def test_supported_version_no_template_raises_filenotfounderror(self) -> None:
        with patch.object(Path, "exists", return_value=False):
            with pytest.raises(
                FileNotFoundError, match="Node base Dockerfile template not found"
            ):
                get_dockerfile_base_ts("20")


class TestGetDockerfileRepoTs:
    def test_from_line(self) -> None:
        result = get_dockerfile_repo_ts("commit0.base.node20:latest")
        assert "FROM commit0.base.node20:latest" in result

    def test_has_proxy_args(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert 'ARG http_proxy=""' in result
        assert 'ARG https_proxy=""' in result
        assert 'ARG HTTP_PROXY=""' in result
        assert 'ARG HTTPS_PROXY=""' in result
        assert "ARG no_proxy=" in result
        assert "ARG NO_PROXY=" in result

    def test_has_setup_sh_copy(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert "COPY ./setup.sh /root/" in result
        assert "chmod +x /root/setup.sh" in result

    def test_has_node_modules_check(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert "node_modules" in result

    def test_has_dep_manifest(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert ".dep-manifest.txt" in result

    def test_workdir_testbed(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert "WORKDIR /testbed/" in result

    def test_with_pre_install_apt(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            pre_install=["apt-get install -y libxml2 libxslt1-dev"],
        )
        assert "apt-get update" in result
        assert "libxml2" in result
        assert "libxslt1-dev" in result

    def test_with_pre_install_non_apt(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            pre_install=["curl -O http://example.com/file.tar.gz"],
        )
        assert "RUN curl -O http://example.com/file.tar.gz" in result

    def test_with_packages_native_deps(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            packages=["sharp", "express"],
        )
        assert "apt-get update" in result
        assert "libvips-dev" in result

    def test_without_packages_no_apt(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert "apt-get update" not in result

    def test_no_optional_params_clean_output(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert "FROM img:tag" in result
        assert "WORKDIR /testbed/" in result

    def test_apt_deduplicates(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            pre_install=[
                "apt-get install -y libxml2-dev gcc",
                "apt-get install -y gcc libssl-dev",
            ],
        )
        assert result.count("apt-get update") == 1

    def test_pre_install_apt_install_variant(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            pre_install=["apt install -y libfoo-dev"],
        )
        assert "apt-get update" in result
        assert "libfoo-dev" in result


class TestDetectTsNativeDepsEdgeCases:
    def test_canvas_all_deps(self) -> None:
        result = detect_ts_system_dependencies(["canvas"])
        expected = {
            "libcairo2-dev",
            "libjpeg-dev",
            "libpango1.0-dev",
            "libgif-dev",
            "librsvg2-dev",
        }
        assert set(result) == expected

    def test_sharp_deps(self) -> None:
        result = detect_ts_system_dependencies(["sharp"])
        assert result == ["libvips-dev"]

    def test_sqlite3_deps(self) -> None:
        result = detect_ts_system_dependencies(["sqlite3"])
        assert result == ["libsqlite3-dev"]

    def test_better_sqlite3_deps(self) -> None:
        result = detect_ts_system_dependencies(["better-sqlite3"])
        assert result == ["libsqlite3-dev"]

    def test_pg_native_deps(self) -> None:
        result = detect_ts_system_dependencies(["pg-native"])
        assert result == ["libpq-dev"]

    def test_re2_deps(self) -> None:
        result = detect_ts_system_dependencies(["re2"])
        assert result == ["libre2-dev"]

    def test_libxmljs_deps(self) -> None:
        result = detect_ts_system_dependencies(["libxmljs"])
        assert result == ["libxml2-dev"]

    def test_libxmljs2_deps(self) -> None:
        result = detect_ts_system_dependencies(["libxmljs2"])
        assert result == ["libxml2-dev"]

    def test_bcrypt_returns_empty(self) -> None:
        result = detect_ts_system_dependencies(["bcrypt"])
        assert result == []

    def test_cpu_features_returns_empty(self) -> None:
        result = detect_ts_system_dependencies(["cpu-features"])
        assert result == []

    def test_scoped_package_at_version(self) -> None:
        result = detect_ts_system_dependencies(["@scope/sharp@1.2.3"])
        assert result == []  # @scope/sharp is not in the map

    def test_scoped_package_without_version(self) -> None:
        result = detect_ts_system_dependencies(["@scope/pkg"])
        assert result == []

    def test_version_with_gt_operator(self) -> None:
        result = detect_ts_system_dependencies(["sharp>0.30"])
        assert "libvips-dev" in result

    def test_version_with_lt_operator(self) -> None:
        result = detect_ts_system_dependencies(["sharp<1.0"])
        assert "libvips-dev" in result

    def test_version_with_eq_operator(self) -> None:
        result = detect_ts_system_dependencies(["sharp=0.33.0"])
        assert "libvips-dev" in result

    def test_all_native_deps_combined(self) -> None:
        all_pkgs = ["sharp", "canvas", "sqlite3", "pg-native", "re2", "libxmljs"]
        result = detect_ts_system_dependencies(all_pkgs)
        assert "libvips-dev" in result
        assert "libcairo2-dev" in result
        assert "libsqlite3-dev" in result
        assert "libpq-dev" in result
        assert "libre2-dev" in result
        assert "libxml2-dev" in result

    def test_case_insensitive_matching(self) -> None:
        result = detect_ts_system_dependencies(["Sharp"])
        assert "libvips-dev" in result

    def test_scoped_types_with_version_skipped(self) -> None:
        result = detect_ts_system_dependencies(["@types/node@20.0.0"])
        assert result == []


class TestGetDockerfileRepoTsExtraBranches:
    def test_pre_install_with_mixed_apt_and_non_apt(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            pre_install=[
                "apt-get install -y libfoo-dev",
                "curl -o /tmp/setup.sh http://example.com/setup.sh",
                "apt install -y libbar-dev",
            ],
        )
        assert "RUN curl -o /tmp/setup.sh" in result
        assert "libfoo-dev" in result
        assert "libbar-dev" in result
        assert result.count("apt-get update") == 1

    def test_packages_with_no_native_deps_no_apt_block(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            packages=["express", "lodash", "typescript"],
        )
        assert "apt-get update" not in result

    def test_packages_combined_with_pre_install_apt(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            packages=["sharp"],
            pre_install=["apt-get install -y libextra-dev"],
        )
        assert "apt-get update" in result
        assert "libvips-dev" in result
        assert "libextra-dev" in result

    def test_pre_install_apt_flags_excluded(self) -> None:
        result = get_dockerfile_repo_ts(
            "img:tag",
            pre_install=["apt-get install -y --no-install-recommends libtest-dev"],
        )
        assert "libtest-dev" in result
        assert (
            "--no-install-recommends"
            not in result.split("apt-get update")[1].split("apt-get install")[0]
            if "apt-get update" in result
            else True
        )

    def test_empty_pre_install_list(self) -> None:
        result = get_dockerfile_repo_ts("img:tag", pre_install=[])
        assert "apt-get update" not in result

    def test_empty_packages_list(self) -> None:
        result = get_dockerfile_repo_ts("img:tag", packages=[])
        assert "apt-get update" not in result

    def test_no_proxy_default_value(self) -> None:
        result = get_dockerfile_repo_ts("img:tag")
        assert 'ARG no_proxy="localhost,127.0.0.1,::1"' in result
        assert 'ARG NO_PROXY="localhost,127.0.0.1,::1"' in result
