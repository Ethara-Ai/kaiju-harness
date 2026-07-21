from __future__ import annotations

import logging
from typing import List, Optional

from commit0.harness.constants import DOCKERFILES_DIR, SUPPORTED_PYTHON_VERSIONS
from commit0.harness.health_check import pip_to_import


def _posix_single_quote(s: str) -> str:
    """POSIX-safe single-quote for embedding an arbitrary string in a shell word.

    Wraps *s* in single quotes and escapes any embedded single quote via the
    canonical ``'\\''`` idiom, so the string reaches the command VERBATIM — no
    word-splitting, no glob, no variable expansion, and (critically for PEP 508
    dependency markers) no consumption of embedded double quotes. This is what
    lets ``pkg ; python_version >= "3.10"`` survive Docker's ``/bin/sh -c`` layer
    intact instead of arriving as ``pkg ; python_version >= 3.10`` (unquoted
    marker), which uv/pip reject with "Failed to parse".
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _requirements_install_run(specs: List[str], req_path: str) -> str:
    """Emit a single ``RUN`` that materializes *specs* into a requirements file
    and installs it with uv (pip fallback).

    Passing PEP 508 specs as inline shell args is unsafe: markers carry double
    quotes (``python_version >= "3.10"``, ``platform_python_implementation ==
    "CPython"``) that the shell strips, corrupting the marker. Writing one
    single-quoted spec per line to a file and installing with ``-r`` sidesteps
    the shell entirely — the resolver reads the file bytes as written.
    """
    # `printf '%s\n' 'a' 'b' …` writes each (verbatim, single-quoted) arg on its
    # own line. `\\n` here becomes a literal `\n` in the Dockerfile, which the
    # in-container /bin/sh printf then interprets as a newline.
    quoted = " ".join(_posix_single_quote(s) for s in specs)
    return (
        f"RUN printf '%s\\n' {quoted} > {req_path} "
        f"&& (uv pip install --system -r {req_path} "
        f"|| pip install --no-cache-dir -r {req_path})"
    )

# ---------------------------------------------------------------------------
# System-library detection: Python pip package → Debian/Ubuntu apt packages.
# To add support for a new package, add one line to NATIVE_DEP_MAP.
# ---------------------------------------------------------------------------

# Python package name (normalized, lowercase) → required apt packages.
NATIVE_DEP_MAP: dict[str, list[str]] = {
    # C extensions / build tools
    "cython": [],  # build-essential already in base image
    "numpy": [],  # build-essential already in base image
    "scipy": ["gfortran", "libopenblas-dev"],
    # XML
    "lxml": ["libxml2-dev", "libxslt1-dev"],
    # Database
    "psycopg2": ["libpq-dev"],
    "psycopg2-binary": [],  # binary wheel, no system deps
    "mysqlclient": ["default-libmysqlclient-dev"],
    # Crypto / security
    "cryptography": ["libssl-dev", "libffi-dev"],
    "cffi": ["libffi-dev"],
    "pynacl": ["libsodium-dev"],
    "bcrypt": ["libffi-dev"],
    # Image processing
    "pillow": ["libjpeg-dev", "zlib1g-dev", "libpng-dev"],
    "matplotlib": ["libfreetype6-dev", "libpng-dev"],
    # Data formats
    "pyyaml": ["libyaml-dev"],
    "h5py": ["libhdf5-dev"],
    # Compression
    "python-snappy": ["libsnappy-dev"],
    "lz4": ["liblz4-dev"],
    # Networking
    "pycurl": ["libcurl4-openssl-dev"],
    # VCS
    "pygit2": ["libgit2-dev"],
    # FFI
    "greenlet": ["libffi-dev"],
    # Geospatial — rasterio/fiona bind GDAL and need gdal-config at build time.
    "rasterio": ["gdal-bin", "libgdal-dev"],
    "fiona": ["gdal-bin", "libgdal-dev"],
    # Numerical optimizer bindings — ipyopt needs Ipopt's C++ dev headers.
    "ipyopt": ["coinor-libipopt-dev"],
    # NOTE: these fire only when the package is a DIRECT dependency in the repo's
    # pip list. When pulled in transitively (e.g. rasterio via icevision[all],
    # ipyopt via zfit[all]) the name never reaches NATIVE_DEP_MAP — those cases are
    # a dataset-spec issue (the heavy extra should be excluded), not a mapping gap.
}

# Packages already installed in the base Dockerfile templates.
# Keep in sync with Dockerfile.python3.{7,8,9,10,11,12,13} apt block.
_BASE_APT_PACKAGES: frozenset[str] = frozenset(
    {
        "git",
        "build-essential",
        # Common C/C++-extension build toolchain (small, near-universally needed
        # for source builds). autotools+pkg-config fix packages like h5py (needs
        # pkg-config) and autotools C extensions (btclib_libsecp256k1 -> autoreconf);
        # cmake covers CMake-based builds. Added to the base so a TRANSITIVE build
        # dep — which never appears in the repo's pip list and so can't be resolved
        # by NATIVE_DEP_MAP — still finds its toolchain.
        "autoconf",
        "automake",
        "cmake",
        "libtool",
        "pkg-config",
        "ca-certificates",
        "curl",
        "jq",
        "libatomic1",
        "locales",
        "locales-all",
        "procps",
    }
)


def detect_system_dependencies(pip_packages: list[str]) -> list[str]:
    """Map pip package names to required apt system packages.

    Looks up each pip package in :data:`NATIVE_DEP_MAP` and returns the union
    of required apt packages, minus those already in the base Docker image.

    Args:
    ----
        pip_packages: List of pip package specs (version pins are stripped).

    Returns:
    -------
        Sorted, deduplicated list of apt package names to install.

    """
    apt_deps: set[str] = set()
    for pip_spec in pip_packages:
        # Normalize: strip version pins, extras, markers, whitespace
        name = (
            pip_spec.lower()
            .split("[")[0]
            .split(">")[0]
            .split("<")[0]
            .split("=")[0]
            .split("!")[0]
            .split("~")[0]
            .split(";")[0]
            .strip()
        )
        apt_deps.update(NATIVE_DEP_MAP.get(name, []))
    # Filter out packages already in the base image
    return sorted(apt_deps - _BASE_APT_PACKAGES)


_logger = logging.getLogger(__name__)


def get_dockerfile_base(python_version: str) -> str:
    if python_version not in SUPPORTED_PYTHON_VERSIONS:
        _logger.error(
            "Unsupported Python version: %s (supported: %s)",
            python_version,
            sorted(SUPPORTED_PYTHON_VERSIONS),
        )
        raise ValueError(
            f"Unsupported Python version: {python_version}. "
            f"Supported: {sorted(SUPPORTED_PYTHON_VERSIONS)}"
        )
    template_path = DOCKERFILES_DIR / f"Dockerfile.python{python_version}"
    if not template_path.exists():
        raise FileNotFoundError(f"Base Dockerfile template not found: {template_path}")
    return template_path.read_text()


def get_dockerfile_repo(
    base_image: str,
    pre_install: Optional[List[str]] = None,
    packages: Optional[str] = None,
    pip_packages: Optional[List[str]] = None,
    install_cmd: Optional[str] = None,
) -> str:
    lines = [
        f"FROM {base_image}",
        "",
        'ARG http_proxy=""',
        'ARG https_proxy=""',
        'ARG HTTP_PROXY=""',
        'ARG HTTPS_PROXY=""',
        'ARG no_proxy="localhost,127.0.0.1,::1"',
        'ARG NO_PROXY="localhost,127.0.0.1,::1"',
        "",
        "COPY ./setup.sh /root/",
        "RUN chmod +x /root/setup.sh && /bin/bash /root/setup.sh",
        "",
        "# Set workdir to repo root so relative paths (requirements.txt, -e .) resolve",
        "WORKDIR /testbed/",
        "",
    ]

    apt_packages: list[str] = []
    if pre_install:
        for cmd in pre_install:
            if cmd.startswith("apt-get install") or cmd.startswith("apt install"):
                pkgs = cmd.split("install", 1)[1].replace("-y", "").strip().split()
                apt_packages.extend(p for p in pkgs if not p.startswith("-"))
            else:
                lines.append(f"RUN {cmd}")

    if pip_packages:
        apt_packages.extend(detect_system_dependencies(pip_packages))

    if apt_packages:
        pkg_str = " \\\n    ".join(sorted(set(apt_packages)))
        lines.append(
            f"RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {pkg_str} \\\n"
            f"    && rm -rf /var/lib/apt/lists/*"
        )
        lines.append("")

    # Python installs go through uv (fast, reliable resolver) with a pip fallback so
    # a uv hiccup — or a non-pip installer — still works. `--system` targets the
    # image's system python (uv otherwise defaults to a venv). Requested: use uv for
    # python installation.
    if packages:
        lines.append(
            f"RUN uv pip install --system -r {packages} "
            f"|| pip install --no-cache-dir -r {packages}"
        )
        lines.append("")

    if pip_packages:
        # Install via a generated requirements file, NOT inline args. Inline
        # double-quoting each spec (the old approach) let Docker's /bin/sh strip
        # the inner quotes of PEP 508 markers — uv/pip then saw an unquoted marker
        # (`python_version >= 3.10`, `platform_python_implementation == CPython`)
        # and failed with "Failed to parse". The requirements file preserves every
        # spec byte-for-byte.
        lines.append(_requirements_install_run(pip_packages, "/tmp/kaiju-pip-requirements.txt"))
        lines.append("")

    if install_cmd:
        # Normalize to a bare `pip install …`, then prefer uv (pip fallback). A
        # non-pip installer (e.g. `python setup.py develop`) is left untouched.
        norm = install_cmd.replace("python -m pip install", "pip install").replace(
            "uv pip install", "pip install"
        )
        if norm.startswith("pip install"):
            args_str = norm[len("pip install"):]
            lines.append(
                f"RUN uv pip install --system{args_str} "
                f"|| pip install --no-cache-dir{args_str}"
            )
        else:
            lines.append(f"RUN {norm}")
        lines.append("")

    # Verify key dependencies are importable after install
    if pip_packages:
        skip_prefixes = ("pytest", "coverage", "pip", "setuptools", "wheel")
        importable = [
            pip_to_import(p)
            for p in pip_packages
            if not any(p.lower().startswith(s) for s in skip_prefixes)
        ]
        if importable:
            individual_checks = "; ".join(
                f'python -c "import {m}" 2>/dev/null || echo "WARN: cannot import {m}"'
                for m in importable
            )
            lines.append(f"RUN {individual_checks}")
            lines.append("")

    lines.append(
        "RUN pip install --no-cache-dir -U pytest pytest-cov coverage pytest-json-report"
    )
    lines.append("")

    # Dependency manifest for debugging
    lines.append("RUN pip freeze > /testbed/.dep-manifest.txt")
    lines.append("")

    lines.append("WORKDIR /testbed/")
    lines.append("")

    return "\n".join(lines)


__all__: list[str] = [
    "NATIVE_DEP_MAP",
    "detect_system_dependencies",
    "get_dockerfile_base",
    "get_dockerfile_repo",
]
