"""Cross-language Dockerfile ↔ SUPPORTED_*_VERSIONS consistency tests.

Each language ships a `SUPPORTED_*_VERSIONS` frozenset/set in its `constants*.py`
and one `Dockerfile.<lang><version>` per supported version. This test file
verifies the two are ALWAYS in sync: adding a version to the set MUST come with
a matching Dockerfile, and vice versa. Prevents silent drift where a repo
declaring an unsupported version falls through to a runtime error at build time.

Companion to `test_go_version_consistency.py` (Go) and `test_constants_js.py`
(JS Node version sanity). This file covers Python, Java, TS Node, Rust, CPP, C.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from commit0.harness.constants import DOCKERFILES_DIR, SUPPORTED_PYTHON_VERSIONS
from commit0.harness.constants_java import SUPPORTED_JAVA_VERSIONS
from commit0.harness.constants_js import SUPPORTED_NODE_VERSIONS as SUPPORTED_NODE_VERSIONS_JS
from commit0.harness.constants_ts import SUPPORTED_NODE_VERSIONS as SUPPORTED_NODE_VERSIONS_TS


class TestPythonDockerfileConsistency:
    def test_each_supported_python_has_dockerfile(self):
        for v in SUPPORTED_PYTHON_VERSIONS:
            df = DOCKERFILES_DIR / f"Dockerfile.python{v}"
            assert df.exists(), (
                f"SUPPORTED_PYTHON_VERSIONS declares {v!r} but "
                f"{df} does not exist. Add the Dockerfile or remove from set."
            )

    def test_each_python_dockerfile_is_declared(self):
        for df in sorted(DOCKERFILES_DIR.glob("Dockerfile.python*")):
            v = df.name.removeprefix("Dockerfile.python")
            assert v in SUPPORTED_PYTHON_VERSIONS, (
                f"{df.name} exists but Python {v!r} is not in "
                f"SUPPORTED_PYTHON_VERSIONS. Add it or delete the Dockerfile."
            )

    def test_each_python_dockerfile_from_tag_matches(self):
        for v in SUPPORTED_PYTHON_VERSIONS:
            df = DOCKERFILES_DIR / f"Dockerfile.python{v}"
            content = df.read_text()
            expected = f"FROM python:{v}-slim"
            assert expected in content, (
                f"{df.name} must FROM python:{v}-slim-<distro> "
                f"but got: {content.splitlines()[0]!r}"
            )


class TestJavaDockerfileConsistency:
    def test_each_supported_java_has_dockerfile(self):
        for v in SUPPORTED_JAVA_VERSIONS:
            df = DOCKERFILES_DIR / f"Dockerfile.java{v}"
            assert df.exists(), (
                f"SUPPORTED_JAVA_VERSIONS declares {v!r} but "
                f"{df} does not exist."
            )

    def test_each_java_dockerfile_is_declared(self):
        for df in sorted(DOCKERFILES_DIR.glob("Dockerfile.java*")):
            v = df.name.removeprefix("Dockerfile.java")
            assert v in SUPPORTED_JAVA_VERSIONS, (
                f"{df.name} exists but Java {v!r} is not in "
                f"SUPPORTED_JAVA_VERSIONS."
            )

    def test_each_java_dockerfile_from_tag_matches(self):
        for v in SUPPORTED_JAVA_VERSIONS:
            df = DOCKERFILES_DIR / f"Dockerfile.java{v}"
            content = df.read_text()
            expected = f"FROM eclipse-temurin:{v}-jdk"
            assert expected in content, (
                f"{df.name} must FROM eclipse-temurin:{v}-jdk-<distro> "
                f"but got: {content.splitlines()[0]!r}"
            )


class TestNodeDockerfileConsistency:
    def test_js_and_ts_node_versions_match(self):
        js_set = {str(v) for v in SUPPORTED_NODE_VERSIONS_JS}
        assert js_set == set(SUPPORTED_NODE_VERSIONS_TS), (
            "SUPPORTED_NODE_VERSIONS in constants_js.py must match constants_ts.py "
            f"(js={sorted(js_set)}, ts={sorted(SUPPORTED_NODE_VERSIONS_TS)})"
        )

    def test_each_supported_node_has_dockerfile(self):
        for v in SUPPORTED_NODE_VERSIONS_TS:
            df = DOCKERFILES_DIR / f"Dockerfile.node{v}"
            assert df.exists(), (
                f"SUPPORTED_NODE_VERSIONS declares {v!r} but "
                f"{df} does not exist."
            )

    def test_each_node_dockerfile_is_declared(self):
        for df in sorted(DOCKERFILES_DIR.glob("Dockerfile.node*")):
            v = df.name.removeprefix("Dockerfile.node")
            assert v in SUPPORTED_NODE_VERSIONS_TS, (
                f"{df.name} exists but Node {v!r} is not in "
                f"SUPPORTED_NODE_VERSIONS."
            )


class TestRustDockerfileTemplate:
    def test_rust_dockerfile_uses_version_template(self):
        df = DOCKERFILES_DIR / "Dockerfile.rust"
        assert df.exists()
        content = df.read_text()
        assert "__RUST_VERSION__" in content, (
            "Dockerfile.rust must contain __RUST_VERSION__ template variable "
            "(substituted by get_dockerfile_base_rust from RUST_VERSION constant). "
            "See __init__rust.py."
        )

    def test_rust_version_constant_is_valid_semver_prefix(self):
        from commit0.harness.constants_rust import RUST_VERSION
        parts = RUST_VERSION.split(".")
        assert 2 <= len(parts) <= 3, (
            f"RUST_VERSION={RUST_VERSION!r} must be MAJOR.MINOR or MAJOR.MINOR.PATCH"
        )
        for p in parts:
            int(p)  # raises ValueError if not numeric


class TestCppDockerfileBase:
    def test_cpp_dockerfile_exists(self):
        df = DOCKERFILES_DIR / "Dockerfile.cpp"
        assert df.exists(), "Dockerfile.cpp must exist (single-base for CPP)"

    def test_cpp_dockerfile_base_is_ubuntu(self):
        df = DOCKERFILES_DIR / "Dockerfile.cpp"
        content = df.read_text()
        first = content.splitlines()[0]
        assert first.startswith("FROM ubuntu:"), (
            f"Dockerfile.cpp must FROM ubuntu:<version>. Got: {first!r}"
        )
        # Must use the __CPP_UBUNTU_VERSION__ template so callers can select
        # Ubuntu 20.04 (GCC 9), 22.04 (GCC 11), or 24.04 (GCC 13) per repo.
        assert "__CPP_UBUNTU_VERSION__" in first, (
            f"Dockerfile.cpp must use __CPP_UBUNTU_VERSION__ template. "
            f"Got: {first!r}"
        )

    def test_cpp_ubuntu_version_constant_is_valid(self):
        from commit0.harness.constants_cpp import CPP_UBUNTU_VERSION
        parts = CPP_UBUNTU_VERSION.split(".")
        assert len(parts) == 2, f"CPP_UBUNTU_VERSION must be MAJOR.MINOR (e.g. 22.04), got {CPP_UBUNTU_VERSION!r}"
        for p in parts:
            int(p)


class TestCDockerfileBase:
    def test_c_dockerfile_exists(self):
        df = DOCKERFILES_DIR / "Dockerfile.c"
        assert df.exists(), "Dockerfile.c must exist (single-base for C)"

    def test_c_dockerfile_from_gcc_image(self):
        df = DOCKERFILES_DIR / "Dockerfile.c"
        content = df.read_text()
        first = content.splitlines()[0]
        assert first.startswith("FROM gcc:"), (
            f"Dockerfile.c must FROM gcc:<version>-<distro>. Got: {first!r}"
        )
        # Must use the __C_GCC_VERSION__ template placeholder so spec_c can
        # substitute the version from constants_c.C_GCC_VERSION.
        assert "__C_GCC_VERSION__" in first, (
            f"Dockerfile.c must use __C_GCC_VERSION__ template (like Rust/Go). "
            f"Got: {first!r}"
        )

    def test_c_gcc_version_constant(self):
        from commit0.harness.constants_c import C_GCC_VERSION
        int(C_GCC_VERSION)  # raises if not numeric
