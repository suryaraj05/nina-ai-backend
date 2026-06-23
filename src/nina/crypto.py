"""Encrypt/decrypt LLM config secrets at rest using Fernet symmetric encryption.

The encryption key comes from NINA_ENCRYPT_KEY env var (a URL-safe base64-
encoded 32-byte key, as produced by Fernet.generate_key()).  When the env var
is absent, seal/unseal are no-ops: configs are stored plaintext with a warning
so development works without a key configured.

To generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import warnings
from typing import Any

_SEAL_MARKER = "_sealed"

# ── API key / token hashing (canonical, shared by all stores) ─────────────────
# Both ConsoleStore (JSON) and PgStore MUST hash identically, or keys issued
# under one fail to validate under the other. The single algorithm is
# HMAC-SHA256 keyed on NINA_CONSOLE_KEY_HASH_SECRET.


def key_hash_secret() -> str:
    """Resolve the key-hashing secret, warning once if the env var is unset."""
    secret = os.environ.get("NINA_CONSOLE_KEY_HASH_SECRET")
    if not secret:
        warnings.warn(
            "NINA_CONSOLE_KEY_HASH_SECRET is not set — using insecure default. "
            "Set this env var before running in production.",
            RuntimeWarning,
            stacklevel=2,
        )
        secret = "nina-console-dev"
    return secret


def hash_key(raw: str, secret: str | None = None) -> str:
    """Canonical HMAC-SHA256 digest of an API key or token."""
    if secret is None:
        secret = key_hash_secret()
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _fernet() -> Any | None:
    raw_key = os.environ.get("NINA_ENCRYPT_KEY")
    if not raw_key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(raw_key.encode() if isinstance(raw_key, str) else raw_key)


def seal_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    """Encrypt *config* to a sealed blob.

    Returns ``{"_sealed": "<ciphertext>"}`` when a key is configured, or the
    original dict unchanged (with a one-time warning) when it is not.
    """
    if not config:
        return config
    f = _fernet()
    if f is None:
        warnings.warn(
            "NINA_ENCRYPT_KEY is not set — LLM config stored in plaintext. "
            "Set this env var in production to encrypt API keys at rest.",
            stacklevel=2,
        )
        return config
    plaintext = json.dumps(config, default=str).encode()
    return {_SEAL_MARKER: f.encrypt(plaintext).decode()}


def unseal_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    """Decrypt a sealed blob back to the original config dict.

    If the config is not sealed (no ``_sealed`` key), it is returned as-is so
    that plaintext configs from dev/test environments continue to work.
    """
    if not config or _SEAL_MARKER not in config:
        return config
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "Cannot decrypt sealed LLM config: NINA_ENCRYPT_KEY is not set. "
            "Set the same key that was used when the config was sealed."
        )
    plaintext = f.decrypt(config[_SEAL_MARKER].encode())
    return json.loads(plaintext)
