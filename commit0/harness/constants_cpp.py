import os
from pathlib import Path
from typing import Dict, List

from pydantic import Field

from commit0.harness.constants import (
    DOCKERFILES_DIR,
    RepoInstance,
    TestStatus,
)

# Ubuntu base image version for the CPP toolchain. Selected to match the
# default GCC version bundled with each Ubuntu release:
#   ubuntu:20.04 -> GCC 9  (legacy repos, C++03/11 exact behavior)
    #   ubuntu:22.04 -> GCC 11 (default; supports C++98..C++20)
    #   ubuntu:24.04 -> GCC 13 (modern; supports C++20/23)
    # Override via CPP_UBUNTU_VERSION env var for repos needing a specific GCC.
CPP_UBUNTU_VERSION = os.environ.get("CPP_UBUNTU_VERSION", "22.04")

__all__ = [
    "CppRepoInstance",
    "CPP_STUB_MARKER",
    "CPP_STUB_MARKER_CONSTEXPR",
    "CPP_STUB_MARKER_NOEXCEPT",
    "CPP_SPLIT",
    "CPP_BASE_BRANCH",
    "CPP_GITIGNORE_ENTRIES",
    "CPP_BUILD_SYSTEMS",
    "CPP_TEST_FRAMEWORKS",
    "RUN_CPP_TESTS_LOG_DIR",
    "CPP_TEST_IDS_DIR",
    "DOCKERFILES_CPP_DIR",
    "DOCKERFILES_DIR",
    "HEAVY_PRE_INSTALL",
    "REPO_OVERRIDES",
    "TestStatus",
]

# __builtin_trap() (not std::abort()) so a stub compiles WITHOUT requiring the file
# to #include <cstdlib>: many C++ sources pull in <stdlib.h> (global `abort`) but
# not <cstdlib> (`std::abort`), so `std::abort()` fails to compile ("no member
# named 'abort' in namespace 'std'"). __builtin_trap() is a Clang/GCC builtin that
# needs no header and is `noreturn`, so it also satisfies non-void return paths.
CPP_STUB_MARKER = "__builtin_trap() /* STUB: not implemented */"
CPP_STUB_MARKER_CONSTEXPR = "return {}"
CPP_STUB_MARKER_NOEXCEPT = "__builtin_trap() /* STUB: not implemented */"

CPP_BASE_BRANCH = "commit0"

CPP_GITIGNORE_ENTRIES = [
    "build/",
    "cmake-build-*/",
    "builddir/",
    ".cache/",
    "compile_commands.json",
    ".aider*",
    "logs/",
]

CPP_BUILD_SYSTEMS = ["cmake", "meson", "autotools", "make"]

CPP_TEST_FRAMEWORKS = ["gtest", "catch2", "doctest", "boost_test", "ctest"]

# Curated subsets only — the "all" subset is derived dynamically from the
# loaded dataset by ``commit0.harness.split_utils.resolve_split``.
CPP_SPLIT: Dict[str, list[str]] = {}

RUN_CPP_TESTS_LOG_DIR = Path(os.environ.get("COMMIT0_CPP_LOG_DIR", "logs/cpp_tests"))

CPP_TEST_IDS_DIR = Path(__file__).parent.parent / "data" / "cpp_test_ids"

DOCKERFILES_CPP_DIR = Path(__file__).parent / "dockerfiles"


class CppRepoInstance(RepoInstance):
    """Repo instance with C++-specific metadata."""

    build_system: str = "cmake"
    cpp_standard: str = "17"
    test_framework: str = "gtest"
    cmake_options: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)
    compiler: str = "gcc"
    submodules: bool = False


HEAVY_PRE_INSTALL: Dict[str, List[str]] = {
    "protocolbuffers/protobuf": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y zlib1g-dev libssl-dev",
        "cd /tmp && git clone --depth 1 --branch 20250127.0 https://github.com/abseil/abseil-cpp.git",
        "cmake -S /tmp/abseil-cpp -B /tmp/abseil-cpp/build -DCMAKE_INSTALL_PREFIX=/usr/local -DABSL_PROPAGATE_CXX_STD=ON -DABSL_ENABLE_INSTALL=ON -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_CXX_STANDARD=17 -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release",
        "cmake --build /tmp/abseil-cpp/build -j$(nproc)",
        "cmake --install /tmp/abseil-cpp/build",
        "rm -rf /tmp/abseil-cpp",
        "ldconfig",
    ],
    "grpc/grpc": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y zlib1g-dev libssl-dev libc-ares-dev libre2-dev pkg-config",
        "cd /tmp && git clone --depth 1 --branch 20250127.0 https://github.com/abseil/abseil-cpp.git",
        "cmake -S /tmp/abseil-cpp -B /tmp/abseil-cpp/build -DCMAKE_INSTALL_PREFIX=/usr/local -DABSL_PROPAGATE_CXX_STD=ON -DABSL_ENABLE_INSTALL=ON -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_CXX_STANDARD=17 -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release && cmake --build /tmp/abseil-cpp/build -j$(nproc) && cmake --install /tmp/abseil-cpp/build && rm -rf /tmp/abseil-cpp",
        "ldconfig",
    ],
    "facebook/folly": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libevent-dev libssl-dev libdouble-conversion-dev libgoogle-glog-dev libgflags-dev libiberty-dev liblz4-dev liblzma-dev libsnappy-dev zlib1g-dev libjemalloc-dev libunwind-dev libfmt-dev libsodium-dev libaio-dev libzstd-dev binutils-dev libtool",
    ],
    "apache/brpc": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libssl-dev libgflags-dev libgoogle-glog-dev libprotobuf-dev protobuf-compiler libleveldb-dev libsnappy-dev zlib1g-dev",
    ],
    "drogonframework/drogon": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libjsoncpp-dev libssl-dev zlib1g-dev libbrotli-dev libc-ares-dev uuid-dev",
    ],
    "facebook/proxygen": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libevent-dev libssl-dev libdouble-conversion-dev libgoogle-glog-dev libgflags-dev libiberty-dev liblz4-dev liblzma-dev libsnappy-dev zlib1g-dev libjemalloc-dev libunwind-dev libfmt-dev libsodium-dev libzstd-dev binutils-dev libtool",
    ],
    "facebookincubator/velox": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libevent-dev libssl-dev libdouble-conversion-dev libgoogle-glog-dev libgflags-dev liblz4-dev libsnappy-dev libzstd-dev libfmt-dev libbz2-dev libxml2-dev libcurl4-openssl-dev libprotobuf-dev protobuf-compiler libre2-dev libjemalloc-dev",
    ],
    "facebook/wangle": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libevent-dev libssl-dev libdouble-conversion-dev libgoogle-glog-dev libgflags-dev libfmt-dev libsodium-dev libzstd-dev",
    ],
    "facebook/wdt": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libssl-dev libdouble-conversion-dev libgoogle-glog-dev libgflags-dev libfmt-dev",
    ],
    "facebook/fbthrift": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libevent-dev libssl-dev libdouble-conversion-dev libgoogle-glog-dev libgflags-dev libsnappy-dev libzstd-dev libkrb5-dev libsodium-dev libfmt-dev libunwind-dev",
    ],
    "userver-framework/userver": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev libyaml-cpp-dev libcurl4-openssl-dev libcrypto++-dev libpq-dev libhiredis-dev libssl-dev libz-dev zlib1g-dev libidn2-dev libc-ares-dev libfmt-dev libcctz-dev libzstd-dev",
    ],
    "eclipse-iceoryx/iceoryx": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libacl1-dev libncurses-dev",
    ],
    "TileDB-Inc/TileDB": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libssl-dev libcurl4-openssl-dev zlib1g-dev libzstd-dev libbz2-dev libhdf5-dev",
    ],
    "chronoxor/CppServer": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libssl-dev libasio-dev",
    ],
    "actor-framework/actor-framework": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libssl-dev",
    ],
    "Stiffstream/restinio": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libssl-dev libasio-dev libfmt-dev",
    ],
    "google/re2": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libssl-dev",
        "cd /tmp && git clone --depth 1 --branch 20250127.0 https://github.com/abseil/abseil-cpp.git && cmake -S /tmp/abseil-cpp -B /tmp/abseil-cpp/build -DCMAKE_INSTALL_PREFIX=/usr/local -DABSL_PROPAGATE_CXX_STD=ON -DABSL_ENABLE_INSTALL=ON -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_CXX_STANDARD=17 -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release && cmake --build /tmp/abseil-cpp/build -j$(nproc) && cmake --install /tmp/abseil-cpp/build && rm -rf /tmp/abseil-cpp",
        "cd /tmp && git clone --depth 1 --branch v1.14.0 https://github.com/google/googletest.git && cmake -S /tmp/googletest -B /tmp/googletest/build -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_CXX_STANDARD=17 -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release && cmake --build /tmp/googletest/build -j$(nproc) && cmake --install /tmp/googletest/build && rm -rf /tmp/googletest",
        "cd /tmp && git clone --depth 1 --branch v1.8.3 https://github.com/google/benchmark.git && cmake -S /tmp/benchmark -B /tmp/benchmark/build -DCMAKE_INSTALL_PREFIX=/usr/local -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_CXX_STANDARD=17 -DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release -DBENCHMARK_ENABLE_TESTING=OFF && cmake --build /tmp/benchmark/build -j$(nproc) && cmake --install /tmp/benchmark/build && rm -rf /tmp/benchmark",
        "ldconfig",
    ],
    "google/leveldb": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libsnappy-dev zlib1g-dev",
    ],
    "jbeder/yaml-cpp": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libboost-all-dev",
    ],
    "actor-framework/actor-framework": [
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y libssl-dev",
    ],
}


REPO_OVERRIDES: Dict[str, Dict[str, object]] = {
    "simdjson/simdjson":        {"install": "cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DSIMDJSON_DEVELOPER_MODE=ON && cmake --build build -j$(nproc)"},
    "grpc/grpc":                {"docker_timeout": 7200},
    "facebook/folly":           {"docker_timeout": 7200},
    "facebook/proxygen":        {"docker_timeout": 7200},
    "facebook/fbthrift":        {"docker_timeout": 7200},
    "facebookincubator/velox":  {"docker_timeout": 7200},
    "userver-framework/userver":{"docker_timeout": 7200},
    "protocolbuffers/protobuf": {"docker_timeout": 5400},
    "apache/brpc":              {"docker_timeout": 5400},
    "TileDB-Inc/TileDB":        {"docker_timeout": 5400},
    "facebook/wangle":          {"docker_timeout": 4800},
    "facebook/wdt":             {"docker_timeout": 4800},
    "drogonframework/drogon":   {"docker_timeout": 4800},
}
