"""Smoke tests for the gemini-pro / Vertex AI alias wiring.

Validates that:
1. `commit0/harness/resolve_model.sh` resolves the alias to `vertex_ai/gemini-3.1-pro`.
2. The `gemini`, `gemini-pro`, and `vertex-gemini` aliases all produce the same output.
3. `AgentConfig` accepts a `vertex_ai/*` model string (validation does not reject it).

The actual Vertex API probe lives in `preflight_model_api` (resolve_model.sh) and
is exercised by the pipeline scripts when credentials are present.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVE_SH = REPO_ROOT / "commit0/harness/resolve_model.sh"


def _source_and_resolve(alias: str) -> dict[str, str]:
    """Source resolve_model.sh in bash, call resolve_model <alias>, return env vars."""
    cmd = (
        f'source "{RESOLVE_SH}" && resolve_model {alias} && '
        f'echo "MODEL_NAME=$MODEL_NAME"; '
        f'echo "MODEL_SHORT=$MODEL_SHORT"; '
        f'echo "CACHE_PROMPTS=$CACHE_PROMPTS"'
    )
    result = subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, check=True
    )
    out: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


class TestGeminiAliasResolution:
    def test_gemini_pro_alias_resolves_to_vertex_model(self) -> None:
        env = _source_and_resolve("gemini-3.1-pro")
        assert env["MODEL_NAME"] == "vertex_ai/gemini-3.1-pro-preview"
        assert env["MODEL_SHORT"] == "gemini-3.1-pro"
        assert env["CACHE_PROMPTS"] == "true"

    def test_bare_gemini_alias_resolves_to_same_target(self) -> None:
        env = _source_and_resolve("gemini")
        assert env["MODEL_NAME"] == "vertex_ai/gemini-3.1-pro-preview"
        assert env["MODEL_SHORT"] == "gemini-3.1-pro"

    def test_vertex_gemini_alias_resolves_to_same_target(self) -> None:
        env = _source_and_resolve("gemini31")
        assert env["MODEL_NAME"] == "vertex_ai/gemini-3.1-pro-preview"
        assert env["MODEL_SHORT"] == "gemini-3.1-pro"


class TestAgentConfigAcceptsVertexModel:
    def test_agent_config_accepts_vertex_ai_model_string(self) -> None:
        from agent.class_types import AgentConfig

        cfg = AgentConfig(
            agent_name="aider",
            model_name="vertex_ai/gemini-3.1-pro",
            use_user_prompt=False,
            user_prompt="",
            use_topo_sort_dependencies=False,
            add_import_module_to_context=False,
            use_repo_info=False,
            max_repo_info_length=0,
            use_unit_tests_info=False,
            max_unit_tests_info_length=0,
            use_spec_info=False,
            max_spec_info_length=0,
            use_lint_info=False,
            run_entire_dir_lint=False,
            max_lint_info_length=0,
            pre_commit_config_path=".pre-commit-config.yaml",
            run_tests=False,
            max_iteration=1,
            record_test_for_each_commit=False,
        )
        assert cfg.model_name == "vertex_ai/gemini-3.1-pro"


class TestVertexCredentialDetection:
    """Verifies that AiderAgents.__init__ picks up Vertex env vars when present.

    Skipped when no Vertex credentials are configured in the test environment
    (the actual API call is gated by preflight_model_api, not this unit test).
    """

    @pytest.mark.skipif(
        # Gate on exactly the credentials AiderAgents.__init__ accepts for
        # vertex_ai/ models (VERTEX_AI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS).
        # A broader gate (VERTEX_PROJECT/VERTEX_CREDENTIALS) would un-skip this
        # test when only those are set — including via cross-test env leakage —
        # and then __init__ would (correctly) raise, causing a spurious failure.
        not (
            os.environ.get("VERTEX_AI_API_KEY")
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ),
        reason="Vertex credentials not set; auth-detection test skipped",
    )
    def test_aider_agents_accepts_vertex_model_with_creds(self) -> None:
        from agent.agents import AiderAgents

        a = AiderAgents(max_iteration=1, model_name="vertex_ai/gemini-3.1-pro")
        assert a.model_name == "vertex_ai/gemini-3.1-pro"

    def test_aider_agents_rejects_vertex_model_without_creds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AiderAgents.__init__ checks for VERTEX_AI_API_KEY or
        # GOOGLE_APPLICATION_CREDENTIALS for vertex_ai/ models; without either
        # it raises ValueError("API Key Error: ...").
        monkeypatch.delenv("VERTEX_AI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.delenv("VERTEX_CREDENTIALS", raising=False)
        monkeypatch.delenv("VERTEX_PROJECT", raising=False)

        from agent import agents as agents_mod
        from agent.agents import AiderAgents

        # The real credential-check is the logic under test. Neutralize the
        # aider-Model construction and resilience wiring (they touch the aider
        # stub's Model.extra_params, which is not exercisable here) so __init__
        # reaches the real credential check.
        monkeypatch.setattr(agents_mod, "register_bedrock_arn_pricing", lambda *a, **k: None)
        monkeypatch.setattr(agents_mod, "apply_llm_resilience", lambda *a, **k: None)
        monkeypatch.setattr(AiderAgents, "_load_model_settings", staticmethod(lambda *a, **k: None))

        with pytest.raises(ValueError, match="API Key Error"):
            AiderAgents(max_iteration=1, model_name="vertex_ai/gemini-3.1-pro")
