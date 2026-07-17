"""QC cluster C8_build parity / regression tests.

Covers:
  * C8-003  Node base-image distro consistency (reproducibility drift guard).
  * C8-004  Cross-language USER parity (no partial non-root migration drift).
  * C8-005  build_java must FAIL LOUD (no synthetic HEAD instance).
  * C8-006  build_* tests must mock docker_client(), not the bare docker symbol.
  * C8-007  verify_inventory must FAIL under --strict when it enumerates no repos.
  * C8-011  verify_inventory freshness cross-check (stale / corrupt bz2).
"""
from __future__ import annotations

import bz2
import hashlib
import json
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[1]
_DOCKERFILES = _HARNESS / "dockerfiles"
_TESTS = Path(__file__).resolve().parent


def _from_line(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("FROM "):
            return line
    raise AssertionError(f"no FROM line in {path}")


# ---------------------------------------------------------------------------
# C8-003: Node base-image distro consistency.
# ---------------------------------------------------------------------------
class TestNodeDistroParity:
    def test_all_node_images_slim(self):
        for df in sorted(_DOCKERFILES.glob("Dockerfile.node*")):
            assert "-slim" in _from_line(df), f"{df.name} base image is not -slim"

    def test_node_distro_is_bookworm_except_documented_node14(self):
        """Every Node image uses bookworm EXCEPT node14, which is pinned to
        bullseye because ``node:14-bookworm-slim`` does not exist (Node 14 hit
        EOL before Debian bookworm shipped). That one deviation is a forced
        constraint, documented here so any *new* off-distro Node image fails."""
        for df in sorted(_DOCKERFILES.glob("Dockerfile.node*")):
            frm = _from_line(df)
            if df.name == "Dockerfile.node14":
                assert "bullseye" in frm, "node14 documented exception changed"
            else:
                assert "bookworm" in frm, (
                    f"{df.name} drifted off bookworm: {frm!r} (only node14 may "
                    "differ, and only to bullseye)"
                )


# ---------------------------------------------------------------------------
# C8-004: cross-language USER parity (no partial non-root migration).
# ---------------------------------------------------------------------------
class TestUserParity:
    def test_user_directive_is_all_or_none(self):
        """Until a coordinated non-root migration lands (de-scoped: needs an
        end-to-end build+eval run per Dockerfile.rust's comment and constants_*
        edits outside this cluster's ownership), the shipped Dockerfiles must be
        CONSISTENT: either EVERY base image drops to a non-root USER or NONE do.
        A partial, per-language migration is exactly the sibling-drift the QC
        finding warns about, so this guard fails on it."""
        with_user = []
        for df in sorted(_DOCKERFILES.glob("Dockerfile.*")):
            has_user = any(
                ln.startswith("USER ") for ln in df.read_text().splitlines()
            )
            if has_user:
                with_user.append(df.name)
        total = len(list(_DOCKERFILES.glob("Dockerfile.*")))
        assert len(with_user) in (0, total), (
            "Inconsistent USER directives across languages (partial non-root "
            f"migration): only {with_user} declare USER out of {total} images."
        )


# ---------------------------------------------------------------------------
# C8-005: build_java fail-loud, no synthetic HEAD instance.
# ---------------------------------------------------------------------------
class TestBuildJavaFailLoud:
    def test_no_synthetic_head_instance_in_source(self):
        src = (_HARNESS / "build_java.py").read_text()
        assert '"reference_commit": "HEAD"' not in src, (
            "build_java still synthesizes a HEAD reference_commit fallback "
            "(QC-C8-005) — a missing dataset entry must fail loud instead."
        )
        assert "raise RuntimeError" in src


# ---------------------------------------------------------------------------
# C8-006: build_* tests mock docker_client(), not the bare docker symbol.
# ---------------------------------------------------------------------------
class TestDockerMockTargets:
    def test_build_java_test_patches_docker_client(self):
        src = (_TESTS / "test_build_java.py").read_text()
        # No bare `.docker")` patch target may survive; docker_client is the call
        # site (prod uses docker_utils.docker_client()).
        assert '.docker")' not in src, (
            "test_build_java patches the bare docker symbol; the prod call site "
            "is docker_client() (QC-C8-006)."
        )
        assert '.docker_client")' in src

    def test_build_js_fixture_patches_docker_client(self):
        src = (_TESTS / "test_build_js.py").read_text()
        assert "build_js.docker_client" in src, (
            "test_build_js docker_env fixture must patch docker_client (QC-C8-006)."
        )

    def test_rust_build_main_test_patches_docker_client(self):
        src = (_TESTS / "test_rust_modules.py").read_text()
        assert '{BUILD_MODULE}.docker")' not in src, (
            "test_rust_modules TestBuildRustMain patches the bare docker symbol "
            "instead of docker_client (QC-C8-006)."
        )


# ---------------------------------------------------------------------------
# C8-007: verify_inventory strict-fail on empty enumeration.
# ---------------------------------------------------------------------------
class TestVerifyInventoryEmptyStrict:
    def test_strict_empty_enumeration_fails(self, monkeypatch):
        monkeypatch.delenv("KAIJU_REQUIRE_INVENTORY", raising=False)
        from kaiju.verify_inventory import main

        # python + no dataset + no split -> enumerate_repos() == [] -> must FAIL.
        rc = main(["--language", "python", "--strict"])
        assert rc == 2

    def test_no_strict_empty_enumeration_passes(self, monkeypatch):
        monkeypatch.delenv("KAIJU_REQUIRE_INVENTORY", raising=False)
        from kaiju.verify_inventory import main

        rc = main(["--language", "python", "--no-strict"])
        assert rc == 0

    def test_require_inventory_env_forces_warn_only(self, monkeypatch):
        monkeypatch.setenv("KAIJU_REQUIRE_INVENTORY", "0")
        from kaiju.verify_inventory import main

        rc = main(["--language", "python", "--strict"])
        assert rc == 0


# ---------------------------------------------------------------------------
# C8-011: verify_inventory bz2 freshness / provenance cross-check.
# ---------------------------------------------------------------------------
class TestFreshnessCrossCheck:
    def _make_bz2(self, d: Path, name: str, payload: bytes = b"tests") -> Path:
        p = d / f"{name}.bz2"
        p.write_bytes(bz2.compress(payload))
        return p

    def _write_meta(self, bz2_path: Path, *, reference_commit: str, content_sha256: str):
        meta = {
            "reference_commit": reference_commit,
            "content_sha256": content_sha256,
            "generated_at": "2026-01-01T00:00:00Z",
            "n_ids": 1,
        }
        bz2_path.with_name(bz2_path.name + ".meta.json").write_text(json.dumps(meta))

    def test_no_sidecar_is_warn_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KAIJU_TEST_IDS_DIR", str(tmp_path))
        self._make_bz2(tmp_path, "myrepo")
        from kaiju.verify_inventory import stale_inventory

        corrupt, drifted, no_prov = stale_inventory("python", ["myrepo"], None)
        assert corrupt == [] and drifted == []
        assert no_prov == ["myrepo"]

    def test_matching_hash_is_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KAIJU_TEST_IDS_DIR", str(tmp_path))
        p = self._make_bz2(tmp_path, "myrepo")
        good = hashlib.sha256(p.read_bytes()).hexdigest()
        self._write_meta(p, reference_commit="a" * 40, content_sha256=good)
        from kaiju.verify_inventory import stale_inventory

        corrupt, drifted, no_prov = stale_inventory("python", ["myrepo"], None)
        assert corrupt == [] and drifted == [] and no_prov == []

    def test_hash_mismatch_is_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KAIJU_TEST_IDS_DIR", str(tmp_path))
        p = self._make_bz2(tmp_path, "myrepo")
        self._write_meta(p, reference_commit="a" * 40, content_sha256="deadbeef")
        from kaiju.verify_inventory import stale_inventory

        corrupt, drifted, no_prov = stale_inventory("python", ["myrepo"], None)
        assert corrupt == ["myrepo"]

    def test_reference_commit_drift(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KAIJU_TEST_IDS_DIR", str(tmp_path))
        p = self._make_bz2(tmp_path, "myrepo")
        good = hashlib.sha256(p.read_bytes()).hexdigest()
        self._write_meta(p, reference_commit="a" * 40, content_sha256=good)
        dataset = tmp_path / "ds.json"
        dataset.write_text(
            json.dumps([{"repo": "org/myrepo", "reference_commit": "b" * 40}])
        )
        from kaiju.verify_inventory import stale_inventory

        corrupt, drifted, no_prov = stale_inventory(
            "python", ["myrepo"], str(dataset)
        )
        assert corrupt == [] and drifted == ["myrepo"]
