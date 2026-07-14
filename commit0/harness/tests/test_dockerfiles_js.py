from __future__ import annotations

import pytest

from commit0.harness.dockerfiles_js import (
    _extract_apt_packages_from_pre_install,
    get_dockerfile_base,
    get_dockerfile_repo,
)


class TestExtractAptPackages:
    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            ("apt-get install -y curl", ["curl"]),
            ("apt-get -y install curl", ["curl"]),
            ("  apt-get install x", ["x"]),
            ("apt-get install x && rm -rf /", ["x"]),
            ("apt-get install -y curl wget", ["curl", "wget"]),
            ("apt install x", ["x"]),
            ("apt-get install -y --no-install-recommends curl", ["curl"]),
            ("apt-get install x ; rm -rf /", ["x"]),
            ("apt-get install x || true", ["x"]),
            ("apt-get install x | tee log", ["x"]),
        ],
    )
    def test_extracts_pkgs(self, cmd: str, expected: list[str]) -> None:
        assert _extract_apt_packages_from_pre_install([cmd]) == expected

    def test_empty_list(self) -> None:
        assert _extract_apt_packages_from_pre_install([]) == []

    def test_none(self) -> None:
        assert _extract_apt_packages_from_pre_install(None) == []

    def test_sudo_prefix_still_extracts(self) -> None:
        result = _extract_apt_packages_from_pre_install(["sudo apt-get install x"])
        assert result == ["x"]

    def test_env_prefix_still_extracts(self) -> None:
        result = _extract_apt_packages_from_pre_install(
            ["DEBIAN_FRONTEND=noninteractive apt-get install x"]
        )
        assert result == ["x"]

    def test_unterminated_quote_skipped(self) -> None:
        result = _extract_apt_packages_from_pre_install(['apt-get install "x'])
        assert result == []

    def test_no_install_keyword_skipped(self) -> None:
        assert _extract_apt_packages_from_pre_install(["apt-get update"]) == []

    def test_multiple_commands(self) -> None:
        result = _extract_apt_packages_from_pre_install(
            ["apt-get install -y curl", "apt-get install wget"]
        )
        assert result == ["curl", "wget"]


class TestGetDockerfileBase:
    @pytest.mark.parametrize("v", [18, 20, 22, 24])
    def test_supported_versions(self, v: int) -> None:
        out = get_dockerfile_base(v)
        assert isinstance(out, str)
        assert len(out) > 0

    @pytest.mark.parametrize("v", [17, 19, 21, 23, 25, 99, 0, -1])
    def test_unsupported_versions_raise(self, v: int) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base(v)


class TestGetDockerfileRepo:
    def test_disallowed_apt_pkg_raises(self) -> None:
        with pytest.raises(ValueError, match="Disallowed apt packages"):
            get_dockerfile_repo(
                base_image_key="commit0.base.node20:latest",
                install_cmd="npm ci",
                pre_install=["apt-get install netcat"],
            )

    def test_allowed_apt_pkg_succeeds(self) -> None:
        out = get_dockerfile_repo(
            base_image_key="commit0.base.node20:latest",
            install_cmd="npm ci",
            pre_install=["apt-get install -y git"],
        )
        assert isinstance(out, str)
        assert len(out) > 0

    def test_no_pre_install_succeeds(self) -> None:
        out = get_dockerfile_repo(
            base_image_key="commit0.base.node20:latest",
            install_cmd="npm ci",
        )
        assert isinstance(out, str)
        assert len(out) > 0

    def test_chained_disallowed_raises(self) -> None:
        with pytest.raises(ValueError, match="Disallowed apt packages"):
            get_dockerfile_repo(
                base_image_key="commit0.base.node20:latest",
                install_cmd="npm ci",
                pre_install=["apt-get install -y git netcat"],
            )

    def test_sudo_prefix_still_gates(self) -> None:
        with pytest.raises(ValueError, match="Disallowed apt packages"):
            get_dockerfile_repo(
                base_image_key="commit0.base.node20:latest",
                install_cmd="npm ci",
                pre_install=["sudo apt-get install netcat"],
            )


class TestStrictNodeVersionTyping:
    @pytest.mark.parametrize("v", ["20", "22", " 20", "20 "])
    def test_string_node_version_rejected(self, v: str) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base(v)  # type: ignore[arg-type]

    def test_bool_true_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base(True)  # type: ignore[arg-type]

    def test_bool_false_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base(False)  # type: ignore[arg-type]

    def test_float_node_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base(20.0)  # type: ignore[arg-type]

    def test_none_node_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Node version"):
            get_dockerfile_base(None)  # type: ignore[arg-type]


class TestNonAptPreInstallPassedThrough:
    def test_npm_config_command_does_not_raise(self) -> None:
        out = get_dockerfile_repo(
            base_image_key="commit0.base.node20:latest",
            install_cmd="npm ci",
            pre_install=["npm config set registry https://registry.npmjs.org"],
        )
        assert isinstance(out, str)
        assert len(out) > 0

    def test_arbitrary_shell_pre_install_does_not_raise_at_apt_gate(self) -> None:
        out = get_dockerfile_repo(
            base_image_key="commit0.base.node20:latest",
            install_cmd="npm ci",
            pre_install=["echo hello", "mkdir -p /tmp/x"],
        )
        assert isinstance(out, str)
        assert len(out) > 0

    def test_mixed_apt_and_non_apt_gate_applies_only_to_apt(self) -> None:
        out = get_dockerfile_repo(
            base_image_key="commit0.base.node20:latest",
            install_cmd="npm ci",
            pre_install=[
                "npm config set registry https://registry.npmjs.org",
                "apt-get install -y git",
            ],
        )
        assert isinstance(out, str)
        assert len(out) > 0

    def test_mixed_apt_disallowed_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="Disallowed apt packages"):
            get_dockerfile_repo(
                base_image_key="commit0.base.node20:latest",
                install_cmd="npm ci",
                pre_install=[
                    "npm config set registry https://registry.npmjs.org",
                    "apt-get install netcat",
                ],
            )
