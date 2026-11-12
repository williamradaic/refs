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
