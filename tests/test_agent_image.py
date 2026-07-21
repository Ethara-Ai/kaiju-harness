"""Tests for the agent-image layer builder (Docker-free parts)."""

from __future__ import annotations

import tarfile

from agent.container.agent_image import (
    agent_image_key,
    build_context_tar,
    _agent_dockerfile,
)


REPO_IMAGE = "commit0.repo.bus.abcdef1234567890abcdef:v0"


def test_key_shape_and_tag_preserved():
    k = agent_image_key(REPO_IMAGE)
    assert k.endswith(":v0")
    assert "-agent." in k
    assert k == k.lower()


def test_key_is_deterministic():
    assert agent_image_key(REPO_IMAGE) == agent_image_key(REPO_IMAGE)


def test_key_varies_with_repo_image():
    other = "commit0.repo.kanal.0000001111112222223333:v0"
    assert agent_image_key(REPO_IMAGE) != agent_image_key(other)


def test_dockerfile_from_repo_image_and_installs_agent_extra():
    df = _agent_dockerfile(REPO_IMAGE)
    assert df.startswith(f"FROM {REPO_IMAGE}")
    assert 'uv pip install -e ".[agent]"' in df
    assert "PYTHONPATH=/opt/kaiju" in df
    assert "WORKDIR /testbed" in df


def test_context_excludes_bulk_and_includes_packages():
    names = tarfile.open(fileobj=build_context_tar()).getnames()
    assert any(n.startswith("agent/") for n in names)
    assert "pyproject.toml" in names
    assert ".aider.model.settings.yml" in names
    # Never ship bulk/artifacts.
    for n in names:
        assert not n.startswith("repos/")
        assert not n.startswith("dump/")
        assert "/.git/" not in n
        assert "__pycache__" not in n
        assert not n.endswith(".pyc")


def test_key_changes_when_baked_in_source_bytes_change(tmp_path):
    """A byte-change under agent/ MUST bust the cache key.

    Regression: pre-fix, agent_image_key hashed only path-list + pyproject/uv.lock,
    so a source edit shipped inside COPY /opt/kaiju was silently cache-hit and
    the stale image reran old code (memory: agent_image_stale_code.md).
    """
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "sample.py").write_bytes(b"x = 1\n")
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname='p'\n")

    before = agent_image_key(REPO_IMAGE, project_root=tmp_path)
    (tmp_path / "agent" / "sample.py").write_bytes(b"x = 2\n")
    after = agent_image_key(REPO_IMAGE, project_root=tmp_path)

    assert before != after, "byte change under agent/ must bust the image cache key"


def test_key_stable_across_pycache_and_pyc_noise(tmp_path):
    """__pycache__/*.pyc files must NOT bust the cache key (they're pruned from
    the tar context, so keying on them would cause spurious rebuilds)."""
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "sample.py").write_bytes(b"x = 1\n")

    before = agent_image_key(REPO_IMAGE, project_root=tmp_path)
    (tmp_path / "agent" / "__pycache__").mkdir()
    (tmp_path / "agent" / "__pycache__" / "sample.cpython-312.pyc").write_bytes(b"\x00\x00")
    (tmp_path / "agent" / "other.pyc").write_bytes(b"\x00\x00")
    after = agent_image_key(REPO_IMAGE, project_root=tmp_path)

    assert before == after, "pyc/__pycache__ noise must NOT change the image key"
