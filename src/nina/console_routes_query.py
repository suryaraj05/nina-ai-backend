"""The widget hot path: ``POST /v1/query``.

Resolves the publishable API key to a site, enforces per-IP/per-key rate limits
and the monthly quota, decrypts the site's LLM config, runs one turn through the
per-site NinaPool, attaches browser instructions, and records usage + metrics.
Mounted via ``include_router`` in ``console_app.create_app``. This path is
exempt from the admin-secret middleware (it's the public widget endpoint).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from .console_deps import POOL, STORE
from .console_infra import METRICS, logger, _IP_LIMITER, _KEY_LIMITER
from .console_schemas import MultiTenantQueryIn
from .crypto import unseal_llm_config

router = APIRouter()


@router.post("/v1/query")
async def multi_tenant_query(
    body: MultiTenantQueryIn,
    request: Request,
    x_nina_api_key: str | None = Header(default=None, alias="X-NINA-API-Key"),
) -> dict[str, Any]:
    # Rate limiting — per source IP and per API key
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "unknown"
    )
    if not _IP_LIMITER.allow(client_ip):
        return JSONResponse(status_code=429, content={"ok": False, "data": None, "error": {"code": "RATE_LIMITED", "message": "Too many requests. Please slow down."}})
    if x_nina_api_key and not _KEY_LIMITER.allow(x_nina_api_key[:32]):
        return JSONResponse(status_code=429, content={"ok": False, "data": None, "error": {"code": "RATE_LIMITED", "message": "API key rate limit exceeded."}})

    origin = request.headers.get("origin")
    ok_key, site, key_err = STORE.resolve_key_to_site(x_nina_api_key or "", origin)
    if not ok_key:
        return JSONResponse(status_code=401, content={"ok": False, "data": None, "error": key_err})

    # Quota enforcement — hard block when monthly limit is reached
    quota_ok, current_calls, limit = STORE.enforce_quota(site["id"])
    if not quota_ok:
        return JSONResponse(
            status_code=402,
            content={"ok": False, "data": None, "error": {
                "code": "QUOTA_EXCEEDED",
                "message": f"Monthly query limit of {limit:,} reached on the {site.get('plan','free')} plan. Upgrade to continue.",
            }},
        )

    contract = site.get("agentContract")
    if not contract:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "data": None, "error": {"code": "NO_CONTRACT", "message": "No agent contract configured for this site. Upload one via PUT /v1/sites/{id}/contract."}},
        )
    sealed_llm = site.get("llmConfig")
    if not sealed_llm:
        # Fall back to NINA_DEFAULT_LLM_CONFIG (operator's own key for free tier).
        # Format: JSON string, e.g. '{"provider":"openai","model":"gpt-4o-mini","apiKey":"sk-..."}'
        _default_raw = os.environ.get("NINA_DEFAULT_LLM_CONFIG")
        if _default_raw:
            try:
                sealed_llm = json.loads(_default_raw)
            except json.JSONDecodeError:
                pass
    if not sealed_llm:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "data": None, "error": {"code": "NO_LLM_CONFIG", "message": "No LLM config for this site. Upload one via PUT /v1/sites/{id}/llm-config."}},
        )
    try:
        llm_config = unseal_llm_config(sealed_llm)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "data": None, "error": {"code": "LLM_CONFIG_DECRYPT_FAILED", "message": str(exc)}},
        )

    page_context = body.page_context or {}
    from .catalog_hydrate import ensure_site_catalog

    catalog, _cat_meta = ensure_site_catalog(STORE, site)
    if _cat_meta.get("hydrated") and catalog:
        POOL.evict(site["id"])

    _t0 = time.time()
    envelope = await POOL.run(
        site["id"],
        llm_config,
        contract,
        body.transcript or body.message,
        body.sessionId,
        session_hints=body.session_hints,
        page_id=page_context.get("pageId"),
        replay_queued=body.replayQueued,
        product_catalog=catalog,
    )
    if envelope.get("ok") and envelope.get("data"):
        from .instructions import turn_to_instructions
        turn = dict(envelope["data"])
        if turn.get("intent") != "blocked":
            if not turn.get("instructions"):
                turn["instructions"] = turn_to_instructions(
                    contract, turn, page_context=page_context,
                    session_hints=body.session_hints, confirmed=body.confirmed,
                )
        envelope = {**envelope, "data": turn}
        try:
            from .conversation_log import entry_from_turn

            STORE.append_conversation_log(
                site["id"],
                entry_from_turn(
                    site["id"],
                    body.sessionId,
                    body.transcript or body.message or "",
                    turn,
                ),
            )
        except Exception:
            logger.exception("conversation log append failed site=%s", site["id"])
    _latency_ms = int((time.time() - _t0) * 1000)
    _err_code = (envelope.get("error") or {}).get("code", "")
    METRICS.record(ok=bool(envelope.get("ok")), latency_ms=_latency_ms, error_code=_err_code)
    logger.info(
        "query %s site=%s latency=%dms",
        "ok" if envelope.get("ok") else f"err:{_err_code}",
        site["id"],
        _latency_ms,
        extra={"site_id": site["id"], "duration_ms": _latency_ms, "error_code": _err_code or None},
    )
    STORE.record_usage(site["id"])
    return envelope
