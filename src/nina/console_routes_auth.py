"""Merchant dashboard routes (``/v1/auth/*``).

Dashboard-token-scoped endpoints a merchant uses to self-serve: usage, keys,
LLM config, contract upload, generate-from-URL, and site settings. Mounted on
the app via ``include_router`` in ``console_app.create_app``. Auth is enforced
per-route through the dashboard-token guard, not the admin middleware.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from .console_deps import POOL, STORE, _require_dashboard_token, _require_site_ownership
from .console_schemas import KeyIssueIn, SiteLlmConfigIn

router = APIRouter()


@router.get("/v1/auth/whoami")
def auth_whoami(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Validate a merchant dashboard token and return org info."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Dashboard token required.")
    raw = authorization.removeprefix("Bearer ").strip()
    org = STORE.verify_dashboard_token(raw)
    if not org:
        raise HTTPException(status_code=401, detail="Invalid or expired dashboard token.")
    sites = STORE.list_sites(org_id=org["id"])
    return {"ok": True, "data": {"org": {k: v for k, v in org.items() if k != "dashboardTokenDigest"}, "sites": sites}}

@router.get("/v1/auth/sites/{site_id}/usage")
def merchant_get_usage(site_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    usage = STORE.get_usage(site_id)
    plan = STORE.get_site(site_id).get("plan", "free")
    return {"ok": True, "data": {**(usage or {}), "plan": plan}}

@router.get("/v1/auth/sites/{site_id}/conversations")
def merchant_list_conversations(
    site_id: str,
    authorization: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session_id: str | None = Query(default=None, alias="sessionId"),
) -> dict[str, Any]:
    """Recent widget turns for merchant debugging (7-day retention)."""
    from .conversation_log import RETENTION_DAYS

    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    logs = STORE.list_conversation_logs(site_id, limit=limit, session_id=session_id)
    return {"ok": True, "data": {"logs": logs, "retentionDays": RETENTION_DAYS}}

@router.get("/v1/auth/sites/{site_id}/keys")
def merchant_list_keys(site_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    return {"ok": True, "data": STORE.list_api_keys_for_site(site_id)}

@router.put("/v1/auth/sites/{site_id}/llm-config")
def merchant_set_llm_config(site_id: str, body: SiteLlmConfigIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    try:
        STORE.attach_llm_config(site_id, body.llmConfig)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    POOL.evict(site_id)
    return {"ok": True, "data": {"siteId": site_id, "llmConfigAttached": True}}

@router.put("/v1/auth/sites/{site_id}/contract")
def merchant_set_contract(site_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    contract = body.get("contract")
    if not contract:
        raise HTTPException(status_code=400, detail="contract field required.")
    try:
        STORE.attach_contract(site_id, contract)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    POOL.evict(site_id)
    return {"ok": True, "data": {"siteId": site_id, "contractAttached": True}}

@router.post("/v1/auth/keys/issue")
def merchant_issue_key(body: KeyIssueIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, body.siteId)
    try:
        rec = STORE.issue_api_key(body.siteId, body.environment, body.kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "data": rec}

@router.post("/v1/auth/keys/{key_id}/revoke")
def merchant_revoke_key(key_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    key = STORE.get_api_key(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="Key not found.")
    _require_site_ownership(org, key["siteId"])
    try:
        STORE.revoke_api_key(key_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "data": {"keyId": key_id, "revoked": True}}

@router.post("/v1/auth/sites/{site_id}/generate-from-url")
async def merchant_generate_from_url(site_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    api_base_url = body.get("apiBaseUrl", "")
    if not api_base_url:
        raise HTTPException(status_code=400, detail="apiBaseUrl required.")
    runtime = body.get("runtime", "server")
    if runtime not in ("server", "browser"):
        raise HTTPException(status_code=400, detail="runtime must be 'server' or 'browser'.")
    site = STORE.get_site(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Unknown site_id")
    from .console_infra import _validate_external_url
    from .contract_generate import generate_contract_from_url
    from .crypto import is_production

    if is_production():
        _validate_external_url(api_base_url, "Store URL")
    try:
        contract, meta, catalog = generate_contract_from_url(site, api_base_url, runtime=runtime)
        STORE.attach_contract(site_id, contract)
        STORE.attach_product_catalog(site_id, catalog)
        catalog_fields: dict[str, Any] = {}
        if meta.get("firestoreProject"):
            catalog_fields["firestoreProject"] = meta["firestoreProject"]
        if meta.get("catalogSource"):
            catalog_fields["catalogSource"] = meta["catalogSource"]
        if catalog_fields:
            STORE.update_site_fields(site_id, **catalog_fields)
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, **meta}}
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}

@router.put("/v1/auth/sites/{site_id}/settings")
def merchant_update_settings(site_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
    org = _require_dashboard_token(authorization)
    _require_site_ownership(org, site_id)
    allowed = body.get("allowedOrigins")
    if allowed is not None:
        if not isinstance(allowed, list):
            raise HTTPException(status_code=400, detail="allowedOrigins must be a list of URL strings.")
        STORE.update_site_fields(site_id, allowedOrigins=allowed)
    return {"ok": True, "data": {"siteId": site_id, "updated": True}}
