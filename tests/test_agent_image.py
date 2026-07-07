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
