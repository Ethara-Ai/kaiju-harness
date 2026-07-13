#!/usr/bin/env bash
# Idempotent one-shot bootstrap for the kaiju-harness on a fresh EC2 box.
#
# Supported OS: Ubuntu 22.04 / 24.04 LTS, Amazon Linux 2023, Debian 12.
# Requires: sudo access (for apt/dnf install). Run as your normal user, not root.
#
# What it installs:
#   - System deps: git, curl, build-essential, cmake, pkg-config, python venv libs
#   - LLVM/Clang dev libraries (for building cppstubber)
#   - Docker Engine (for the harness eval containers)
#   - Node.js 20 LTS (for JS/TS repos + playwright browser deps)
#   - uv package manager, then Python 3.12 + all deps from pyproject.toml + uv.lock
#   - Playwright chromium browser (for spec scraping)
#   - cppstubber binary (Clang LibTooling C++ stubber)
#
# Safe to re-run: each step checks for existing install and skips if present.
#
# Env overrides:
#   LLVM_VERSION=20        pin a specific LLVM major version (default: 20)
#   SKIP_DOCKER=1          skip Docker install (useful when Docker is already managed)
#   SKIP_PLAYWRIGHT=1      skip Playwright browser install (~200 MB download)
#
# Exit codes:
#   0    success
#   1    unsupported OS
#   >1   step-specific failure (message printed)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LLVM_VERSION="${LLVM_VERSION:-20}"

log()  { printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[bootstrap][warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[bootstrap][fatal]\033[0m %s\n' "$*" >&2; exit "${2:-1}"; }

if [ "$(id -u)" -eq 0 ]; then
    warn "running as root — most steps expect an unprivileged user with sudo."
fi

if ! command -v sudo >/dev/null 2>&1; then
    die "sudo is required but not found on PATH"
fi

if [ ! -r /etc/os-release ]; then
    die "cannot read /etc/os-release — unsupported OS" 1
fi
. /etc/os-release
OS_ID="${ID:-unknown}"
OS_VERSION="${VERSION_ID:-unknown}"
log "detected OS: ${OS_ID} ${OS_VERSION}"

case "${OS_ID}" in
    ubuntu|debian) PKG_MGR=apt ;;
    amzn|rhel|centos|fedora) PKG_MGR=dnf ;;
    *) die "unsupported OS '${OS_ID}'. Supported: ubuntu, debian, amzn, rhel, fedora." 1 ;;
esac

install_apt_stack() {
    log "installing apt system packages..."
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg lsb-release software-properties-common \
        git build-essential cmake pkg-config \
        zlib1g-dev libssl-dev libzstd-dev libxml2-dev libedit-dev \
        python3-venv python3-pip \
        unzip jq

    if ! command -v "clang-${LLVM_VERSION}" >/dev/null 2>&1 && \
       ! [ -f "/usr/lib/llvm-${LLVM_VERSION}/lib/cmake/llvm/LLVMConfig.cmake" ]; then
        log "installing LLVM ${LLVM_VERSION} from apt.llvm.org..."
        wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key | \
            sudo gpg --dearmor -o /usr/share/keyrings/llvm-archive-keyring.gpg
        CODENAME="$(lsb_release -cs)"
        echo "deb [signed-by=/usr/share/keyrings/llvm-archive-keyring.gpg] https://apt.llvm.org/${CODENAME}/ llvm-toolchain-${CODENAME}-${LLVM_VERSION} main" | \
            sudo tee /etc/apt/sources.list.d/llvm-${LLVM_VERSION}.list >/dev/null
        sudo apt-get update -y
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
            "llvm-${LLVM_VERSION}-dev" "libclang-${LLVM_VERSION}-dev" \
            "clang-${LLVM_VERSION}" "clang-tools-${LLVM_VERSION}"
    else
        log "LLVM ${LLVM_VERSION} already installed"
    fi

    if [ -z "${SKIP_DOCKER:-}" ] && ! command -v docker >/dev/null 2>&1; then
        log "installing Docker Engine from official repo..."
        sudo install -m 0755 -d /etc/apt/keyrings
        wget -qO- https://download.docker.com/linux/${OS_ID}/gpg | \
            sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        sudo chmod a+r /etc/apt/keyrings/docker.gpg
        CODENAME="$(lsb_release -cs)"
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${OS_ID} ${CODENAME} stable" | \
            sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
        sudo apt-get update -y
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        sudo usermod -aG docker "$USER" || true
        warn "added $USER to docker group; log out and back in for it to take effect"
    fi

    if ! command -v node >/dev/null 2>&1 || [ "$(node -v 2>/dev/null | cut -d. -f1 | tr -d v)" -lt 20 ]; then
        log "installing Node.js 20 LTS from NodeSource..."
        curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs
    fi
}

install_dnf_stack() {
    log "installing dnf system packages..."
    sudo dnf install -y \
        ca-certificates curl wget \
        git gcc gcc-c++ make cmake pkgconfig \
        zlib-devel openssl-devel libzstd-devel libxml2-devel libedit-devel \
        python3-devel unzip jq

    if ! command -v clang >/dev/null 2>&1; then
        log "installing LLVM/Clang from dnf..."
        sudo dnf install -y clang-devel llvm-devel
    fi

    if [ -z "${SKIP_DOCKER:-}" ] && ! command -v docker >/dev/null 2>&1; then
        log "installing Docker from dnf..."
        sudo dnf install -y docker
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER" || true
        warn "added $USER to docker group; log out and back in for it to take effect"
    fi

    if ! command -v node >/dev/null 2>&1; then
        log "installing Node.js 20 LTS from NodeSource..."
        curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo -E bash -
        sudo dnf install -y nodejs
    fi
}

case "${PKG_MGR}" in
    apt) install_apt_stack ;;
    dnf) install_dnf_stack ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    log "installing uv package manager..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

log "syncing Python environment (uv sync)..."
cd "${REPO_ROOT}"
uv sync

if [ -z "${SKIP_PLAYWRIGHT:-}" ]; then
    log "installing Playwright chromium (idempotent)..."
    uv run playwright install --with-deps chromium || warn "playwright install failed — spec scraping will be unavailable"
fi

log "building cppstubber..."
bash "${REPO_ROOT}/scripts/build_cppstubber.sh"

log "verifying imports..."
uv run python -c "
import tree_sitter
import tools.stub_cpp as sc
assert sc._TS_AVAILABLE, 'tree-sitter fallback missing'
from pathlib import Path
assert Path('tools/cppstubber/build/cppstubber').exists(), 'cppstubber binary missing'
print('OK: tree-sitter fallback + cppstubber binary both available')
"

log "bootstrap complete."
log ""
log "next steps:"
log "  1. Log out and back in for docker group membership (if Docker was installed)."
log "  2. Verify Docker: docker run --rm hello-world"
log "  3. Run a smoke test: bash run_trajectory.sh --repo fmtlib/fmt --lang cpp --model opus48cc --iter 1 --org <your-fork-org>"
