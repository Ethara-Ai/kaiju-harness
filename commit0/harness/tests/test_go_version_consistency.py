"""Issue 20: the Go toolchain version is declared in two places that must agree —
``constants_go.GO_VERSION`` (drives the version health check + base image tag) and
the ``FROM golang:<ver>`` literal in ``Dockerfile.go`` (the real toolchain the eval
container ships). Editing one without the other silently desyncs them. This test is
the CI assertion that keeps them in sync.
"""

import re
from pathlib import Path

from commit0.harness.constants_go import GO_VERSION


def test_go_dockerfile_uses_version_template():
    """Dockerfile.go must use the ``__GO_VERSION__`` placeholder so ``spec_go``
    can substitute the version from ``GO_VERSION`` (constants_go.py) at build
    time. This is the single source of truth pattern (mirrors Dockerfile.rust /
    RUST_VERSION); a literal version tag would silently drift when GO_VERSION
    is bumped.
    """
    dockerfile = (
        Path(__file__).resolve().parents[1] / "dockerfiles" / "Dockerfile.go"
    )
    text = dockerfile.read_text()
    assert "__GO_VERSION__" in text, (
        f"{dockerfile} must contain '__GO_VERSION__' template placeholder "
        f"(mirrors Dockerfile.rust). Substituted by spec_go from "
        f"constants_go.GO_VERSION at build time."
    )
    # Belt-and-suspenders: after template substitution, the FROM tag must
    # produce a well-formed golang:<major>.<minor>[.<patch>]-bookworm line.
    substituted = text.replace("__GO_VERSION__", GO_VERSION)
    m = re.search(r"^FROM\s+golang:(\d+\.\d+)(?:\.\d+)?", substituted, re.MULTILINE)
    assert m, (
        f"After substituting GO_VERSION={GO_VERSION!r} into __GO_VERSION__ the "
        f"Dockerfile.go FROM tag is not well-formed. First 200 chars:\n"
        f"{substituted[:200]!r}"
    )


def test_go_version_constant_is_valid_semver_prefix():
    """GO_VERSION must be a valid Go release like ``1.25.0`` (MAJOR.MINOR.PATCH)
    so ``FROM golang:{GO_VERSION}-bookworm`` resolves to a real Docker Hub tag."""
    parts = GO_VERSION.split(".")
    assert 2 <= len(parts) <= 3, (
        f"GO_VERSION={GO_VERSION!r} must be MAJOR.MINOR or MAJOR.MINOR.PATCH"
    )
    for p in parts:
        int(p)  # raises ValueError if not numeric
