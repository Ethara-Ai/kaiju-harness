"""Install ``sys.modules`` stubs for ``aider.*`` and ``import_deps`` when the
optional ``[agent]`` extras are not installed.

Tests here import ``agent.*`` modules (``test_stage_patch`` -> ``agent.run_rust_agent``
-> ``from import_deps import ModuleSet``; the recovery/guarded_io tests exercise
``agent.claude_code.recovery`` / ``agent.guarded_io``). Without the stub, collection
fails or — worse — the code takes its aider-ABSENT fallback path and tests assert
the wrong behavior. Installing the SAME shared stub as ``agent/tests`` and
``commit0/harness/tests`` makes this directory behave like production (aider
present), so the tests exercise real behavior. No-op when the extras ARE installed.
"""

from __future__ import annotations

from commit0.harness._optional_dep_stubs import install_missing_optional_dep_stubs

install_missing_optional_dep_stubs()
