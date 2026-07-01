"""Per-site Nina() instance pool for multi-tenant deployment."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)


async def _safe_aclose(nina: Nina) -> None:
    """Close a Nina instance's resources, swallowing any teardown error."""
    try:
        await nina.aclose()
    except Exception:  # pragma: no cover - best-effort cleanup
        pass

from . import Nina
from .redis_store import shared_redis_store

# Max cached Nina instances. LRU sites are evicted when this is exceeded.
# Raise with NINA_POOL_MAX_SITES if you have many high-traffic merchants.
_MAX_POOL_SIZE = int(os.environ.get("NINA_POOL_MAX_SITES", "100"))

# Circuit breaker config: trip after N consecutive LLM failures, reset after T seconds.
_CIRCUIT_TRIP_AFTER  = int(os.environ.get("NINA_CIRCUIT_TRIP_AFTER", "3"))
_CIRCUIT_OPEN_SECONDS = int(os.environ.get("NINA_CIRCUIT_OPEN_SECONDS", "30"))


class NinaPool:
    """Caches one initialized Nina() per site_id with LRU eviction and circuit breakers.

    Each site gets an isolated instance — no cross-site config bleed.
    A per-site asyncio.Lock serializes concurrent requests to the same
    instance; this avoids the shared _core.config race until per-request
    context threading is wired through run_turn.

    LRU eviction: when the pool exceeds _MAX_POOL_SIZE sites, the site that
    was least recently queried is evicted from memory. It will be re-initialized
    on the next request to that site.

    Circuit breaker: after _CIRCUIT_TRIP_AFTER consecutive LLM-level failures
    for a site, the circuit opens and requests to that site return an error
    immediately for _CIRCUIT_OPEN_SECONDS. This prevents a broken LLM key from
    hammering the LLM API and exhausting our rate limits.
    """

    def __init__(self) -> None:
        self._instances: dict[str, Nina] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()
        self._last_used: dict[str, float] = {}        # site_id -> unix timestamp
        self._failure_counts: dict[str, int] = {}     # consecutive LLM failures
        self._circuit_until: dict[str, float] = {}    # circuit open until timestamp

    async def _site_lock(self, site_id: str) -> asyncio.Lock:
        async with self._meta_lock:
            if site_id not in self._locks:
                self._locks[site_id] = asyncio.Lock()
            return self._locks[site_id]

    async def _evict_lru(self) -> None:
        """Evict the least-recently-used site when the pool is over capacity,
        closing its LLM HTTP client so connections aren't leaked."""
        if len(self._instances) <= _MAX_POOL_SIZE:
            return
        # Pick the site with the oldest last_used timestamp
        lru_site = min(
            (s for s in self._instances if s in self._last_used),
            key=lambda s: self._last_used[s],
            default=None,
        )
        if lru_site:
            nina = self._instances.get(lru_site)
            self.evict(lru_site)
            if nina is not None:
                await _safe_aclose(nina)

    async def aclose_all(self) -> None:
        """Close every cached instance's HTTP client. Call on app shutdown."""
        instances = list(self._instances.values())
        self._instances.clear()
        self._locks.clear()
        self._last_used.clear()
        for nina in instances:
            await _safe_aclose(nina)
        from .redis_store import close_shared_redis

        await close_shared_redis()

    def _circuit_open(self, site_id: str) -> bool:
        """Return True if the circuit breaker is tripped for this site."""
        until = self._circuit_until.get(site_id, 0.0)
        if time.time() < until:
            return True
        if site_id in self._circuit_until:
            del self._circuit_until[site_id]
        return False

    def _record_success(self, site_id: str) -> None:
        self._failure_counts.pop(site_id, None)

    def _record_failure(self, site_id: str) -> None:
        count = self._failure_counts.get(site_id, 0) + 1
        self._failure_counts[site_id] = count
        if count >= _CIRCUIT_TRIP_AFTER:
            self._circuit_until[site_id] = time.time() + _CIRCUIT_OPEN_SECONDS
            self._failure_counts.pop(site_id, None)
            # Evict the broken instance so it re-initializes when circuit closes
            self.evict(site_id)

    async def get(self, site_id: str, llm_config: dict[str, Any]) -> Nina | None:
        """Return an initialized Nina() for site_id, creating it if needed.

        Returns None if initialization fails (bad llm_config, unreachable
        provider, etc.).
        """
        if site_id in self._instances:
            return self._instances[site_id]

        lock = await self._site_lock(site_id)
        async with lock:
            if site_id in self._instances:
                return self._instances[site_id]
            session_store: Any = "memory"
            redis_store = shared_redis_store()
            if redis_store is not None:
                session_store = redis_store
            nina = Nina()
            result = await nina.init({"llm": llm_config, "session": {"store": session_store}})
            if not result.get("ok"):
                err = result.get("error", {})
                _log.error(
                    "NinaPool init failed site=%s code=%s msg=%s",
                    site_id, err.get("code"), err.get("message"),
                )
                return None
            self._instances[site_id] = nina
            self._last_used[site_id] = time.time()
            await self._evict_lru()
            return nina

    def evict(self, site_id: str) -> None:
        """Remove the cached instance for site_id (call after llmConfig changes)."""
        self._instances.pop(site_id, None)
        self._locks.pop(site_id, None)
        self._last_used.pop(site_id, None)

    async def _ensure_contract_registered(self, nina: Nina, contract: dict[str, Any] | None) -> None:
        """Register the contract's actions onto this instance, once per instance.

        Must be called under the per-site lock. Idempotent via a flag on the
        core; the flag resets naturally because a new contract evicts (rebuilds)
        the instance.
        """
        core = nina._core
        if getattr(core, "_contract_registered", False):
            return
        from .contract_registry import register_from_contract
        try:
            result = await register_from_contract(nina, contract or {})
            if result.get("failed"):
                _log.warning("contract action registration: %d failed", len(result["failed"]))
        except Exception:
            _log.exception("contract action registration failed")
        core._contract_registered = True

    async def run(
        self,
        site_id: str,
        llm_config: dict[str, Any],
        contract: dict[str, Any],
        message: str,
        session_id: str,
        *,
        session_hints: dict[str, Any] | None = None,
        page_id: str | None = None,
        replay_queued: bool = False,
        resume_plan: bool = False,
        confirmed: bool = False,
        product_catalog: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Get-or-create the Nina() for site_id and run one chat turn.

        Per-request context (contract, session hints, page_id) is injected
        into _core.config under the per-site lock so concurrent requests
        to the same site don't race on shared config.
        """
        from .errors import fail

        # Circuit breaker — return immediately if this site has had repeated failures
        if self._circuit_open(site_id):
            remaining = int(self._circuit_until.get(site_id, 0) - time.time())
            return fail(
                "SERVICE_UNAVAILABLE",
                f"Temporarily unavailable for this site (circuit open). Retry in ~{remaining}s.",
            )

        nina = await self.get(site_id, llm_config)
        if nina is None:
            self._record_failure(site_id)
            return fail("NINA_POOL_INIT_FAILED", "Could not initialize Nina for site.")

        lock = await self._site_lock(site_id)
        async with lock:
            self._last_used[site_id] = time.time()
            nina._core.config = {
                **(nina._core.config or {}),
                "_agentContract": contract,
                "_productCatalog": list(product_catalog or []),
                "_sessionHints": session_hints or {},
                "_pageId": page_id,
            }
            # Make the contract's actions resolvable/executable. The pool builds
            # a bare Nina() (init only), so without this the engine sees zero
            # registered actions and can only chitchat. Register once per cached
            # instance — every contract change calls POOL.evict(), which discards
            # the instance and forces a fresh registration on next use.
            await self._ensure_contract_registered(nina, contract)
            from .skill_synth import apply_skills_for_contract, contract_skills_fingerprint

            catalog = list(product_catalog or [])
            fp = contract_skills_fingerprint(contract, catalog_size=len(catalog))
            if getattr(nina._core, "_skills_cache_key", "__unset__") != fp:
                apply_skills_for_contract(nina._core, contract, catalog=catalog)
            result = await nina.chat(
                message,
                session_id,
                replay_queued=replay_queued,
                resume_plan=resume_plan,
                confirmed=confirmed,
            )

        # Update circuit breaker state based on result
        error_code = (result.get("error") or {}).get("code", "")
        is_infra_failure = error_code in (
            "NINA_LLM_UNREACHABLE", "NINA_LLM_AUTH_FAILED",
            "NINA_LLM_RATE_LIMITED", "NINA_POOL_INIT_FAILED",
        )
        if not result.get("ok") and is_infra_failure:
            self._record_failure(site_id)
        else:
            self._record_success(site_id)

        return result
