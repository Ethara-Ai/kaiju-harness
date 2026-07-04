"""OpenAI Codex (ChatGPT-auth) subscription bridge for kaiju-harness.

Routes OpenAI Responses-API traffic through a ChatGPT/Codex subscription (OAuth)
instead of a metered API key, mirroring agent.claude_code. See the module
docstrings in bridge.py / credentials.py and docs for setup + ToS caveats.
"""

from agent.openai_codex.credentials import (
    CodexCredentials,
    CredentialProvider,
    CredentialsError,
    load_credentials,
    refresh_credentials,
)

__all__ = [
    "CodexCredentials",
    "CredentialProvider",
    "CredentialsError",
    "load_credentials",
    "refresh_credentials",
]
