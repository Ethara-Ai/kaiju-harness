#!/usr/bin/env bash
# Build the cppstubber binary (Clang LibTooling primary path for the C++ stubber).
#
# Idempotent: if a working binary already exists, exits 0 without rebuilding.
# Cross-platform: detects LLVM install location on macOS (Homebrew), Ubuntu
# (apt / official LLVM APT repo), Amazon Linux, and generic Linux with
# llvm-config on PATH.
#
# Called by:
#   - scripts/bootstrap_ec2.sh (fresh-machine setup)
#   - CI (validate binary is buildable against the pinned LLVM)
#   - developers manually after `apt install llvm-dev clang-dev`
#
# Exit codes:
#   0   success (or binary already existed)
#   1   LLVM/Clang dev libs not found
#   2   cmake configure failed
#   3   make/build failed
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STUBBER_DIR="${REPO_ROOT}/tools/cppstubber"
BUILD_DIR="${STUBBER_DIR}/build"
BINARY="${BUILD_DIR}/cppstubber"

log() { printf '[cppstubber-build] %s\n' "$*" >&2; }

if [ -x "${BINARY}" ] && [ -z "${FORCE_REBUILD:-}" ]; then
    log "already built at ${BINARY}; skipping (set FORCE_REBUILD=1 to rebuild)"
    exit 0
fi

detect_llvm_dirs() {
    # macOS Homebrew (arm64 or intel)
    for prefix in /opt/homebrew/opt/llvm /usr/local/opt/llvm; do
        if [ -f "${prefix}/lib/cmake/llvm/LLVMConfig.cmake" ]; then
            LLVM_CMAKE_DIR="${prefix}/lib/cmake/llvm"
            CLANG_CMAKE_DIR="${prefix}/lib/cmake/clang"
            return 0
        fi
    done
    # Debian/Ubuntu versioned packages (llvm-<N>-dev). Prefer the highest.
    for v in 22 21 20 19 18 17 16 15 14; do
        d="/usr/lib/llvm-${v}"
        if [ -f "${d}/lib/cmake/llvm/LLVMConfig.cmake" ]; then
            LLVM_CMAKE_DIR="${d}/lib/cmake/llvm"
            CLANG_CMAKE_DIR="${d}/lib/cmake/clang"
            return 0
        fi
    done
    # Fallback: derive from llvm-config
    if command -v llvm-config >/dev/null 2>&1; then
        cmake_dir="$(llvm-config --cmakedir 2>/dev/null || true)"
        if [ -f "${cmake_dir}/LLVMConfig.cmake" ]; then
            LLVM_CMAKE_DIR="${cmake_dir}"
            CLANG_CMAKE_DIR="${cmake_dir%/llvm}/clang"
            return 0
        fi
    fi
    return 1
}

if ! detect_llvm_dirs; then
    cat >&2 <<'ERR'
[cppstubber-build] ERROR: LLVM/Clang development libraries not found.

Install them first, then re-run this script. Suggested commands:

  macOS (Homebrew):
    brew install llvm

  Ubuntu 22.04 / 24.04:
    sudo apt-get install -y llvm-dev libclang-dev clang cmake build-essential

  Amazon Linux 2023:
    sudo dnf install -y clang-devel llvm-devel cmake gcc-c++

  Generic (build from source): see https://llvm.org/docs/GettingStarted.html

The cppstubber build looks for LLVMConfig.cmake under:
  /opt/homebrew/opt/llvm/lib/cmake/llvm     (macOS Homebrew arm64)
  /usr/local/opt/llvm/lib/cmake/llvm         (macOS Homebrew x86_64)
  /usr/lib/llvm-<N>/lib/cmake/llvm           (Ubuntu llvm-N-dev)
  $(llvm-config --cmakedir)                  (any other install)
ERR
    exit 1
fi

log "using LLVM cmake dir: ${LLVM_CMAKE_DIR}"
log "using Clang cmake dir: ${CLANG_CMAKE_DIR}"

mkdir -p "${BUILD_DIR}"

if command -v nproc >/dev/null 2>&1; then
    JOBS="$(nproc)"
elif command -v sysctl >/dev/null 2>&1; then
    JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
else
    JOBS=4
fi

log "configuring (cmake)..."
(
    cd "${BUILD_DIR}"
    LLVM_DIR="${LLVM_CMAKE_DIR}" Clang_DIR="${CLANG_CMAKE_DIR}" cmake .. >/tmp/cppstubber_cmake.log 2>&1
) || {
    log "cmake configure FAILED. Last 30 lines of /tmp/cppstubber_cmake.log:"
    tail -30 /tmp/cppstubber_cmake.log >&2 || true
    exit 2
}

log "building (make -j${JOBS})..."
(
    cd "${BUILD_DIR}"
    make -j"${JOBS}" >/tmp/cppstubber_build.log 2>&1
) || {
    log "build FAILED. Last 40 lines of /tmp/cppstubber_build.log:"
    tail -40 /tmp/cppstubber_build.log >&2 || true
    exit 3
}

if [ ! -x "${BINARY}" ]; then
    log "ERROR: build completed but binary not found at ${BINARY}"
    exit 3
fi

log "success: ${BINARY}"
"${BINARY}" --help >/dev/null 2>&1 && log "binary responds to --help" || log "warning: binary exists but --help failed"
