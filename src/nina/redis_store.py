"""Redis-backed session store for multi-instance NINA deployments.

Drop-in replacement for MemoryStore: implements the same get/set/delete
interface the SessionManager expects. Works with Upstash Redis (TLS
``rediss://`` URLs) and local Redis.

Usage:
    from nina.redis_store import RedisStore, redis_health, shared_redis_store
    store = shared_redis_store()  # or RedisStore(url=os.environ["NINA_REDIS_URL"])
    await nina.init({"llm": ..., "session": {"store": store}})
"""
from __future__ import annotations

import json
import logging
import os
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

_log = logging.getLogger(__name__)

_SHARED: "RedisStore | None" = None

_REDIS_ENV_KEYS = (
    "NINA_REDIS_URL",
    "UPSTASH_REDIS_URL",
    "REDIS_URL",
)


def normalize_redis_url(url: str) -> str:
    """Normalize Redis URLs (Upstash copy-paste, TLS, stray quotes)."""
    cleaned = (url or "").strip()
    if not cleaned:
        return cleaned

    # Upstash UI copies: REDIS_URL="rediss://..."
    if "=" in cleaned and cleaned.split("=", 1)[0].strip().upper().endswith("REDIS_URL"):
        cleaned = cleaned.split("=", 1)[1].strip()

    # Strip wrapping quotes from dashboard copy-paste
    while len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()

    if "upstash.io" in cleaned and cleaned.startswith("redis://"):
        cleaned = "rediss://" + cleaned[len("redis://") :]

    return cleaned


def redis_url_from_env() -> str | None:
    for key in _REDIS_ENV_KEYS:
        raw = os.environ.get(key)
        if raw and str(raw).strip():
            return normalize_redis_url(str(raw))
    return None


def shared_redis_store() -> "RedisStore | None":
    """Return a process-wide RedisStore, or None when Redis is not configured."""
    global _SHARED
    url = redis_url_from_env()
    if not url:
        return None
    if _SHARED is None:
        _SHARED = RedisStore(url=url)
        _log.info("NINA session store: Redis (%s)", _redacted_url(url))
    return _SHARED


async def close_shared_redis() -> None:
    global _SHARED
    if _SHARED is not None:
        await _SHARED.aclose()
        _SHARED = None


async def redis_health() -> dict[str, Any]:
    """Ping Redis for /health and startup checks."""
    url = redis_url_from_env()
    if not url:
        return {"configured": False, "ok": None}
    store = shared_redis_store()
    if store is None:
        return {"configured": False, "ok": None}
    try:
        await store.ping()
        return {
            "configured": True,
            "ok": True,
            "provider": "upstash" if "upstash.io" in url else "redis",
        }
    except Exception as exc:
        _log.warning("Redis health check failed: %s", exc)
        return {"configured": True, "ok": False, "error": str(exc)[:200]}


def _redacted_url(url: str) -> str:
    """Hide password in logs."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
            netloc = f"{parsed.username}:***@{host}{port}" if parsed.username else host
            return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))
    except Exception:
        pass
    return "redis://***"


def _ttl_from_state(state: dict) -> int:
    """Compute remaining TTL in seconds from state['expiresAt'] (ISO string)."""
    expires = state.get("expiresAt")
    if not expires:
        return 1800
    try:
        exp_dt = datetime.fromisoformat(expires)
        remaining = int((exp_dt - datetime.now(timezone.utc)).total_seconds())
        return max(remaining, 1)
    except (ValueError, TypeError):
        return 1800


def _client_kwargs(url: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "socket_connect_timeout": 10,
        "socket_timeout": 10,
        "health_check_interval": 30,
    }
    if url.startswith("rediss://"):
        import certifi

        kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
        kwargs["ssl_ca_certs"] = certifi.where()
    return kwargs


class RedisStore:
    """Async Redis session store.

    Parameters
    ----------
    url:
        redis:// or rediss:// URL.  Ignored when *client* is provided.
    prefix:
        Key prefix for all session keys (default ``nina:sess:``).
    client:
        Pre-built redis.asyncio client.  Pass in tests to avoid a real server.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        prefix: str = "nina:sess:",
        client: Any = None,
    ) -> None:
        raw = url or redis_url_from_env() or "redis://localhost:6379"
        self._url = normalize_redis_url(raw)
        self._prefix = prefix
        self._client = client

    def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, **_client_kwargs(self._url))
        return self._client

    def _reset_client(self) -> None:
        self._client = None

    async def _run(self, op: str, fn):
        try:
            return await fn()
        except Exception as exc:
            _log.warning("Redis %s failed (%s): %s", op, _redacted_url(self._url), exc)
            self._reset_client()
            try:
                return await fn()
            except Exception as retry_exc:
                _log.warning(
                    "Redis %s retry failed (%s): %s",
                    op,
                    _redacted_url(self._url),
                    retry_exc,
                )
                raise retry_exc from exc

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    async def ping(self) -> bool:
        return bool(await self._run("ping", lambda: self._redis().ping()))

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def get(self, session_id: str) -> dict | None:
        data = await self._run("get", lambda: self._redis().get(self._key(session_id)))
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    async def set(self, session_id: str, state: dict) -> None:
        ttl = _ttl_from_state(state)
        payload = json.dumps(state, default=str)

        async def _set() -> None:
            await self._redis().setex(self._key(session_id), ttl, payload)

        await self._run("set", _set)

    async def delete(self, session_id: str) -> None:
        await self._run("delete", lambda: self._redis().delete(self._key(session_id)))
