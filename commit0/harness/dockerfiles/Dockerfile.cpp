FROM ubuntu:__CPP_UBUNTU_VERSION__

ARG TARGETARCH
ARG DEBIAN_FRONTEND=noninteractive
ARG http_proxy=""
ARG https_proxy=""
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG no_proxy="localhost,127.0.0.1,::1"
ARG NO_PROXY="localhost,127.0.0.1,::1"
ARG CA_CERT_PATH="/etc/ssl/certs/ca-certificates.crt"

ENV TZ=Etc/UTC \
    LANG=C.UTF-8 \
    http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    SSL_CERT_FILE=${CA_CERT_PATH} \
    REQUESTS_CA_BUNDLE=${CA_CERT_PATH} \
    CURL_CA_BUNDLE=${CA_CERT_PATH} \
    NODE_EXTRA_CA_CERTS=${CA_CERT_PATH} \
    CCACHE_DIR=/ccache \
    CCACHE_MAXSIZE=5G

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    ca-certificates \
    curl \
    wget \
    jq \
    locales \
    locales-all \
    tzdata \
    pkg-config \
    libssl-dev \
    cmake \
    ninja-build \
    meson \
    autoconf \
    automake \
    libtool \
    clang \
    clang-tidy \
    clang-format \
    libclang-dev \
    ccache \
    bear \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /ccache && \
    ln -s /usr/bin/ccache /usr/local/bin/gcc && \
    ln -s /usr/bin/ccache /usr/local/bin/g++ && \
    ln -s /usr/bin/ccache /usr/local/bin/cc && \
    ln -s /usr/bin/ccache /usr/local/bin/c++ && \
    g++ --version && cmake --version && ninja --version && meson --version

# Cross-distro SSL cert symlinks for libraries that look in RHEL/Fedora paths
RUN mkdir -p /etc/pki/tls/certs /etc/pki/tls /etc/pki/ca-trust/extracted/pem /etc/ssl/certs && \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt 2>/dev/null; \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/ssl/cert.pem 2>/dev/null; \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/cert.pem 2>/dev/null; \
    ln -sf /etc/ssl/certs/ca-certificates.crt /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem 2>/dev/null; \
    ln -sf /etc/ssl/certs /etc/pki/tls/certs 2>/dev/null; \
    true

# MITM CA cert injection via BuildKit secret (only applied if secret is provided)
RUN --mount=type=secret,id=mitm_ca,required=false \
    if [ -f /run/secrets/mitm_ca ]; then \
        cp /run/secrets/mitm_ca /usr/local/share/ca-certificates/mitm-ca.crt && \
        update-ca-certificates && \
        echo "MITM CA certificate installed successfully"; \
    else \
        echo "No MITM CA certificate found, skipping"; \
    fi
