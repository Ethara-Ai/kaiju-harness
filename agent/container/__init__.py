"""Fully-containerized inference: run the whole agent (aider + capture hooks)
inside the repo image's container instead of on the host.

Modules:
- agent_image: build the agent-capable image (repo image + Python + aider + the
  kaiju packages) and derive its cache key.
"""
