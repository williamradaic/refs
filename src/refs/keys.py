"""Where the Anthropic API key lives: the macOS Keychain, never a file.

`refs setup-key` stores it once; everything else reads it through get_api_key().
An ANTHROPIC_API_KEY environment variable, when set, takes precedence.
"""

from __future__ import annotations

import os

import keyring

SERVICE = "refs"
ACCOUNT = "anthropic-api-key"


def get_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or keyring.get_password(SERVICE, ACCOUNT)


def set_api_key(key: str) -> None:
    keyring.set_password(SERVICE, ACCOUNT, key)


def problem_with(key: str) -> str | None:
    """Why this key cannot be used, or None if Anthropic accepts it. Asks for
    the model's description: free, and it only succeeds with a valid key."""
    import anthropic

    from . import llm

    key = key.strip()
    if not key.startswith("sk-ant-") or any(c.isspace() for c in key):
        return "that does not look like an Anthropic API key (it starts with sk-ant-)"
    try:
        anthropic.Anthropic(api_key=key).models.retrieve(llm.MODEL)
    except anthropic.AuthenticationError:
        return "Anthropic rejected that key"
    except anthropic.APIConnectionError:
        return "could not reach Anthropic to check the key; try again"
    return None
