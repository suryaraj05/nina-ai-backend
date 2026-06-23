"""Shared helpers for the console/Postgres stores.

These were previously copy-pasted across console_app.py and pg_store.py. Keeping
one implementation here guarantees both stores generate ids, slugs, origins and
key digests identically.
"""
from __future__ import annotations

import re
import secrets
import time
from urllib.parse import urlparse

from .crypto import hash_key


def now_ts() -> int:
    return int(time.time())


def rand_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def slug(text: str, fallback: str = "site") -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in (text or "").strip())
    return re.sub(r"-{2,}", "-", out).strip("-") or fallback


def parse_origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def issue_key(prefix: str, secret: str | None = None) -> tuple[str, str]:
    """Return (raw_key, digest). Digest is the canonical HMAC-SHA256."""
    raw = f"{prefix}{secrets.token_urlsafe(32)}"
    return raw, hash_key(raw, secret)
