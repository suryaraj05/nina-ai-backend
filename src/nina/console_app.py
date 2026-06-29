"""Hosted NINA Console (hybrid model) for onboarding and key management.

This module intentionally ships as a lightweight in-memory control plane so
teams can run and extend it without external dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .console_deps import (
    POOL,
    STORE,
    _db_url,
    _require_dashboard_token,
    _require_site_ownership,
)
from .console_schemas import (
    CliTokenIn,
    KeyIssueIn,
    KeyVerifyIn,
    MultiTenantQueryIn,
    OnboardingPackIn,
    OrgCreate,
    RegistrarExportIn,
    SeoEmbedHealthIn,
    SeoSitemapIn,
    SiteContractIn,
    SiteCreate,
    SiteLlmConfigIn,
    WizardApiConnectIn,
    WizardGenerateIn,
    WizardInitIn,
    WizardValidateIn,
)
from .crypto import is_production, unseal_llm_config
from .store_util import issue_key, parse_origin
from .console_pack import (
    build_onboarding_pack_files,
    resolve_site_fields,
    zip_onboarding_pack,
)
from .contract_validate import validate_executable
from .generator.pipeline import run_pipeline
from .console_infra import (
    METRICS,
    logger,
    _IP_LIMITER,
    _KEY_LIMITER,
    _request_id_var,
    _validate_external_url,
    _validate_local_path,
)
from .console_routes_auth import router as _auth_router
from .console_routes_wizard import router as _wizard_router


# Store helpers shared with PgStore (see store_util).
_parse_origin = parse_origin
_issue_key = issue_key


from .plans import PLAN_LIMITS as _PLAN_LIMITS, current_period as _current_period, VALID_PLANS as _VALID_PLANS


def create_app() -> FastAPI:
    # Fail closed: in production the admin secret is mandatory. Without it the
    # /v1/* control plane (create org, issue keys, run scans) would be open to
    # anonymous callers. Refuse to start rather than boot insecurely.
    if is_production() and not os.environ.get("NINA_CONSOLE_ADMIN_SECRET"):
        raise RuntimeError(
            "NINA_CONSOLE_ADMIN_SECRET is required when NINA_ENV=production "
            "(refusing to start with an unauthenticated admin API)."
        )

    app = FastAPI(title="NINA Console", version="0.1.0")

    # ── Request logging middleware (outermost — captures all requests) ─────────
    @app.middleware("http")
    async def _request_logger(request: Request, call_next):
        # Honor an inbound request id (e.g. from a gateway), else mint one, so
        # every log line for this request shares a correlation id.
        req_id = request.headers.get("X-NINA-Request-Id") or uuid.uuid4().hex
        token = _request_id_var.set(req_id)
        start = time.time()
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers["X-NINA-Request-Id"] = req_id
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "%s %s %d",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "ip": (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                       or (request.client.host if request.client else "unknown")),
            },
        )
        return response

    # ── Admin auth: protect all /v1/* except the widget query endpoint ────────
    # Set NINA_CONSOLE_ADMIN_SECRET in production. When unset (local dev) the
    # middleware is a no-op so everything works without configuration.
    @app.middleware("http")
    async def _admin_auth(request: Request, call_next):
        path = request.url.path
        # Public: health check, widget query, merchant auth, static assets
        if path in ("/health",) or path == "/v1/query" or path.startswith("/v1/auth/") or not path.startswith("/v1/"):
            return await call_next(request)
        secret = os.environ.get("NINA_CONSOLE_ADMIN_SECRET")
        if not secret:
            # No secret configured. In production this is a hard deny (the boot
            # check should already have prevented startup); in dev it's a no-op.
            if is_production():
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Admin API is not configured."}},
                )
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {secret}"
        if not hmac.compare_digest(
            auth.encode() if auth else b"",
            expected.encode(),
        ):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Console admin secret required. Set Authorization: Bearer <NINA_CONSOLE_ADMIN_SECRET>."}},
            )
        return await call_next(request)

    # ── CORS: widget must be callable from any merchant domain ────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*", "Authorization"],
        expose_headers=["X-NINA-Request-Id"],
    )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        # Release pooled LLM HTTP clients so connections aren't leaked on reload.
        await POOL.aclose_all()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "nina-console",
            "store": {
                "orgs":  STORE.count_orgs(),
                "sites": STORE.count_sites(),
                "keys":  STORE.count_keys(),
                "backend": "postgresql" if _db_url else "json-file",
            },
            "pool": {
                "cached":  len(POOL._instances),
                "max":     int(os.environ.get("NINA_POOL_MAX_SITES", "100")),
                "circuits_open": len(POOL._circuit_until),
            },
        }

    @app.get("/v1/metrics")
    def get_metrics() -> dict[str, Any]:
        return {"ok": True, "data": METRICS.snapshot()}

    @app.post("/v1/orgs")
    def create_org(body: OrgCreate) -> dict[str, Any]:
        return {"ok": True, "data": STORE.create_org(body.name, body.ownerEmail)}

    @app.get("/v1/orgs")
    def list_orgs() -> dict[str, Any]:
        return {"ok": True, "data": STORE.list_orgs()}

    @app.post("/v1/sites")
    def create_site(body: SiteCreate) -> dict[str, Any]:
        site = STORE.create_site(
            body.orgId,
            body.name,
            body.baseUrl,
            locales=body.locales,
            markets=body.markets,
            allowed_origins=body.allowedOrigins,
            currency=body.currency,
        )
        return {"ok": True, "data": site}

    @app.get("/v1/sites")
    def list_sites(org_id: str | None = None) -> dict[str, Any]:
        return {"ok": True, "data": STORE.list_sites(org_id)}

    @app.put("/v1/sites/{site_id}/contract")
    def put_site_contract(site_id: str, body: SiteContractIn) -> dict[str, Any]:
        try:
            STORE.attach_contract(site_id, body.contract)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, "contractAttached": True}}

    @app.put("/v1/sites/{site_id}/llm-config")
    def put_site_llm_config(site_id: str, body: SiteLlmConfigIn) -> dict[str, Any]:
        try:
            STORE.attach_llm_config(site_id, body.llmConfig)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, "llmConfigAttached": True}}

    @app.post("/v1/query")
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
        )
        if envelope.get("ok") and envelope.get("data"):
            from .instructions import turn_to_instructions
            turn = dict(envelope["data"])
            if turn.get("intent") != "blocked":
                turn["instructions"] = turn_to_instructions(
                    contract, turn, page_context=page_context,
                    session_hints=body.session_hints, confirmed=body.confirmed,
                )
            envelope = {**envelope, "data": turn}
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

    @app.get("/v1/sites/{site_id}/usage")
    def site_usage(site_id: str) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        plan = site.get("plan", "free")
        limit = _PLAN_LIMITS.get(plan)
        usage = STORE.get_usage(site_id)
        return {"ok": True, "data": {
            "siteId": site_id,
            "plan": plan,
            "limit": limit,
            "remaining": (limit - usage["calls"]) if limit is not None else None,
            **usage,
        }}

    @app.put("/v1/sites/{site_id}/plan")
    def set_site_plan(site_id: str, body: dict[str, Any]) -> dict[str, Any]:
        plan = body.get("plan", "")
        try:
            STORE.set_plan(site_id, plan)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": {"siteId": site_id, "plan": plan, "limit": _PLAN_LIMITS[plan]}}

    # Merchant dashboard routes (/v1/auth/*) live in console_routes_auth.
    app.include_router(_auth_router)
    # Onboarding wizard routes (/v1/wizard/*) live in console_routes_wizard.
    app.include_router(_wizard_router)

    @app.post("/v1/rotate-token")
    def auth_rotate_token(body: dict[str, Any]) -> dict[str, Any]:
        """Rotate a merchant's dashboard token. Operator action — lives under
        /v1/ (NOT /v1/auth/) so the admin-secret middleware protects it. Issuing
        a new login token for an arbitrary org must never be unauthenticated."""
        org_id = body.get("orgId", "")
        try:
            rec = STORE.rotate_dashboard_token(org_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.post("/v1/keys/issue")
    def issue_key(body: KeyIssueIn) -> dict[str, Any]:
        try:
            rec = STORE.issue_api_key(body.siteId, body.environment, body.kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.post("/v1/keys/verify")
    def verify_key(body: KeyVerifyIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        expected_secret = os.environ.get("NINA_CONSOLE_VERIFY_SECRET")
        if expected_secret:
            if authorization != f"Bearer {expected_secret}":
                return {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Invalid verifier secret."}}
        origin = body.origin or _parse_origin(body.pageUrl)
        ok, err = STORE.verify_publishable_key(body.apiKey, body.siteId, origin)
        return {"ok": ok, "error": err}

    @app.post("/v1/keys/{key_id}/revoke")
    def revoke_key(key_id: str) -> dict[str, Any]:
        try:
            STORE.revoke_api_key(key_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "data": {"keyId": key_id, "revoked": True}}

    @app.get("/v1/sites/{site_id}/keys")
    def list_site_keys(site_id: str) -> dict[str, Any]:
        if not STORE.get_site(site_id):
            raise HTTPException(status_code=404, detail="Unknown site_id")
        return {"ok": True, "data": STORE.list_api_keys_for_site(site_id)}

    @app.get("/v1/orgs/{org_id}")
    def get_org(org_id: str) -> dict[str, Any]:
        org = STORE.get_org(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Unknown org_id")
        return {"ok": True, "data": {k: v for k, v in org.items() if k != "dashboardTokenDigest"}}

    @app.get("/v1/sites/{site_id}")
    def get_site(site_id: str) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        return {"ok": True, "data": site}

    @app.post("/v1/tokens/cli")
    def issue_cli_token(body: CliTokenIn) -> dict[str, Any]:
        try:
            rec = STORE.issue_cli_token(body.orgId, body.label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.get("/v1/embed/snippet")
    def embed_snippet(site_id: str, api_url: str, manifest_url: str, key_id: str) -> dict[str, Any]:
        rec = STORE.get_api_key(key_id)
        if not rec or rec["siteId"] != site_id:
            raise HTTPException(status_code=404, detail="Unknown key_id for site.")
        return {
            "ok": True,
            "data": {
                "snippet": STORE.embed_snippet(site_id, api_url, manifest_url, rec["prefix"] + "..."),
                "note": "Use the full token returned during issuance; prefix is shown for safety.",
            },
        }

    # Developer workspace: files, CLI token, webhooks
    @app.get("/v1/developer/files")
    def developer_files(config_dir: str) -> dict[str, Any]:
        base = _validate_local_path(config_dir)
        names = ["nina.site.yaml", "api.manifest.yaml", "auth.policy.yaml", "risk.policy.yaml"]
        out: dict[str, str] = {}
        for name in names:
            p = base / name
            if p.exists():
                out[name] = p.read_text(encoding="utf-8")
        return {"ok": True, "data": out}

    @app.post("/v1/developer/files")
    def developer_write_file(config_dir: str, filename: str, content: str) -> dict[str, Any]:
        if filename not in {"nina.site.yaml", "api.manifest.yaml", "auth.policy.yaml", "risk.policy.yaml"}:
            raise HTTPException(status_code=400, detail="Unsupported config file")
        base = _validate_local_path(config_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / filename
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "data": {"path": str(path)}}

    @app.post("/v1/webhooks/broken-selector")
    def webhook_broken_selector(payload: dict[str, Any]) -> dict[str, Any]:
        count = STORE.push_webhook_event("broken-selector", payload)
        return {"ok": True, "data": {"queued": True, "count": count}}

    @app.get("/v1/webhooks/broken-selector")
    def webhook_list_broken_selector() -> dict[str, Any]:
        return {"ok": True, "data": STORE.list_webhook_events("broken-selector")}

    # Registrar + GEO
    @app.post("/v1/registrar/verify-domain")
    def registrar_verify_domain(site_id: str, method: str, token: str | None = None) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        method = method.lower()
        if method not in {"dns_txt", "html_meta", "well_known"}:
            raise HTTPException(status_code=400, detail="Unsupported verification method")
        new_status = "verified" if token else "pending"
        verification = dict(site.get("verification") or {})
        verification["production"] = new_status
        STORE.update_site_fields(site_id, verification=verification)
        return {"ok": True, "data": {"siteId": site_id, "method": method, "status": new_status}}

    @app.post("/v1/registrar/export-nina-site")
    def registrar_export_nina_site(body: RegistrarExportIn) -> dict[str, Any]:
        site = STORE.get_site(body.siteId)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        data = {
            "site": {
                "id": site["id"],
                "name": site["name"],
                "baseUrl": site["baseUrl"],
                "locales": site["locales"],
                "allowedOrigins": site["allowedOrigins"],
            },
            "generator": {"sitemap": "sitemap.xml", "docsDir": "docs", "crawl": {"maxPages": 50, "respectRobots": True, "delayMs": 500}},
            "publish": {"outputDir": "dist", "uploadUrl": ""},
        }
        path = Path(body.outputPath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return {"ok": True, "data": {"path": str(path)}}

    # SEO toolkit
    @app.post("/v1/seo/sitemap")
    async def seo_sitemap(body: SeoSitemapIn) -> dict[str, Any]:
        xml = body.rawSitemapXml
        if not xml and body.sitemapUrl:
            _validate_external_url(body.sitemapUrl, "Sitemap URL")
            with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                resp = client.get(body.sitemapUrl)
                resp.raise_for_status()
                xml = resp.text
        if not xml:
            raise HTTPException(status_code=400, detail="No sitemap content provided")
        urls = [line.split("<loc>", 1)[1].split("</loc>", 1)[0].strip() for line in xml.splitlines() if "<loc>" in line]
        return {"ok": True, "data": {"siteId": body.siteId, "urlCount": len(urls), "urls": urls[:100]}}

    @app.post("/v1/seo/embed-health")
    def seo_embed_health(body: SeoEmbedHealthIn) -> dict[str, Any]:
        _validate_external_url(body.siteUrl, "Site URL")
        checks: dict[str, Any] = {"siteUrl": body.siteUrl}
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            try:
                resp = client.get(body.siteUrl)
                html = resp.text if resp.status_code < 500 else ""
                checks["pageStatus"] = resp.status_code
                checks["bootstrapPresent"] = "nina-bootstrap.js" in html
            except Exception as exc:
                return {"ok": False, "error": {"code": "FETCH_FAILED", "message": str(exc)}}
            manifest = body.siteUrl.rstrip("/") + "/agent.json"
            query = body.siteUrl.rstrip("/") + "/v1/query"
            for key, url in [("manifestStatus", manifest), ("queryStatus", query)]:
                try:
                    r = client.get(url)
                    checks[key] = r.status_code
                except Exception:
                    checks[key] = None
        checks["ok"] = bool(checks.get("bootstrapPresent")) and checks.get("manifestStatus") == 200
        return {"ok": checks["ok"], "data": checks}

    # ── Self-serve contract generation from site URL ──────────────────────────
    class GenerateFromUrlIn(BaseModel):
        apiBaseUrl: str | None = None
        openApiUrl: str | None = None

    @app.post("/v1/sites/{site_id}/generate-from-url")
    async def generate_from_url(site_id: str, body: GenerateFromUrlIn) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")

        import asyncio
        import tempfile
        from .console_pack import build_nina_site_yaml, build_api_manifest

        api_base = body.apiBaseUrl or site.get("baseUrl", "")
        locales = site.get("locales") or ["en"]
        allowed = site.get("allowedOrigins") or []

        site_yaml = build_nina_site_yaml(
            site_id=site["id"],
            name=site["name"],
            base_url=site["baseUrl"],
            locales=locales,
            allowed_origins=allowed,
        )

        def _run_pipeline(config_dir_str: str) -> "GenerationResult":
            from .generator.pipeline import run_pipeline as _rp
            cfg = Path(config_dir_str)
            (cfg / "nina.site.yaml").write_text(site_yaml, encoding="utf-8")
            manifest_yaml = build_api_manifest(api_base_url=api_base)
            (cfg / "api.manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
            return _rp(cfg, dry_run=False, strict=False, probe=False)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, _run_pipeline, tmp)
        except Exception as exc:
            return {"ok": False, "errors": [str(exc)], "data": None}

        if result.ok and result.contract:
            STORE.attach_contract(site_id, result.contract)
            POOL.evict(site_id)
            return {"ok": True, "data": {"siteId": site_id, "stats": result.stats}}

        return {"ok": False, "errors": result.errors, "data": {"stats": result.stats}}

    # ── WhatsApp channel (H1–H3) ─────────────────────────────────────────────
    # Set NINA_WHATSAPP_VERIFY_TOKEN for webhook verification handshake.
    # Set NINA_AISENSY_API_KEY to send replies via AiSensy BSP.
    # Map WhatsApp business numbers to site IDs in the site's waNumber field.

    class _WAWebhookIn(BaseModel):
        object: str = ""
        entry: list[dict[str, Any]] = Field(default_factory=list)

    @app.get("/v1/channels/whatsapp/webhook")
    def whatsapp_verify(
        hub_mode: str | None = None,
        hub_verify_token: str | None = None,
        hub_challenge: str | None = None,
    ) -> Any:
        """WhatsApp webhook verification handshake (Meta requirement)."""
        expected = os.environ.get("NINA_WHATSAPP_VERIFY_TOKEN", "")
        if hub_mode == "subscribe" and hub_verify_token == expected:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(hub_challenge or "")
        raise HTTPException(status_code=403, detail="Verification token mismatch.")

    @app.post("/v1/channels/whatsapp/webhook")
    async def whatsapp_webhook(body: _WAWebhookIn, request: Request) -> dict[str, Any]:
        """Receive WhatsApp messages from AiSensy BSP and route through NINA."""
        # Extract messages from the WhatsApp webhook payload
        for entry in body.entry:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                wa_number = value.get("metadata", {}).get("phone_number_id", "")

                # Find which site is registered to this WhatsApp number
                site = STORE.find_site_by_wa_number(wa_number)
                if not site:
                    logger.info("whatsapp: unknown wa_number_id=%s", wa_number)
                    continue

                for msg in value.get("messages", []):
                    msg_type = msg.get("type", "")
                    if msg_type != "text":
                        continue  # Only handle text messages for now

                    from_number = msg.get("from", "")
                    text = msg.get("text", {}).get("body", "")
                    session_id = f"wa_{from_number}"

                    sealed_llm = site.get("llmConfig")
                    if not sealed_llm:
                        _raw = os.environ.get("NINA_DEFAULT_LLM_CONFIG")
                        if _raw:
                            try:
                                sealed_llm = json.loads(_raw)
                            except json.JSONDecodeError:
                                pass
                    if not sealed_llm or not site.get("agentContract"):
                        continue

                    try:
                        llm_config = unseal_llm_config(sealed_llm)
                    except Exception:
                        continue

                    envelope = await POOL.run(
                        site["id"],
                        llm_config,
                        site["agentContract"],
                        text,
                        session_id,
                    )
                    reply = ""
                    if envelope.get("ok") and envelope.get("data"):
                        reply = (envelope["data"].get("naturalLanguageResponse") or "").strip()

                    if reply:
                        await _send_whatsapp_reply(wa_number, from_number, reply)

                    STORE.record_usage(site["id"])

        return {"ok": True}

    async def _send_whatsapp_reply(wa_number_id: str, to: str, text: str) -> None:
        """Send a text reply via the WhatsApp Cloud API / AiSensy."""
        api_key = os.environ.get("NINA_WHATSAPP_API_KEY", "")
        if not api_key:
            logger.info("whatsapp: NINA_WHATSAPP_API_KEY not set — reply not sent")
            return
        url = f"https://graph.facebook.com/v18.0/{wa_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4096]},  # WhatsApp max text length
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"})
        except Exception as exc:
            logger.info("whatsapp: send failed — %s", exc)

    @app.put("/v1/sites/{site_id}/whatsapp")
    def configure_whatsapp(site_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Associate a WhatsApp business number ID with a site."""
        if not STORE.get_site(site_id):
            raise HTTPException(status_code=404, detail="Unknown site_id")
        wa_number_id = body.get("waNumberId", "")
        if not wa_number_id:
            raise HTTPException(status_code=400, detail="waNumberId is required")
        STORE.update_site_fields(site_id, waNumberId=wa_number_id)
        return {"ok": True, "data": {"siteId": site_id, "waNumberId": wa_number_id}}

    # ── Razorpay billing webhooks (H4–H6) ────────────────────────────────────
    # Set NINA_RAZORPAY_WEBHOOK_SECRET to validate Razorpay webhook signatures.
    # Events handled: subscription.activated, subscription.charged,
    #                 subscription.cancelled, subscription.expired

    # Map Razorpay plan IDs to NINA plan names — set via env or hardcode
    _RAZORPAY_PLAN_MAP = {
        os.environ.get("RAZORPAY_PLAN_STARTER", "plan_starter"):    "starter",
        os.environ.get("RAZORPAY_PLAN_GROWTH",  "plan_growth"):     "growth",
        os.environ.get("RAZORPAY_PLAN_SCALE",   "plan_scale"):      "scale",
        os.environ.get("RAZORPAY_PLAN_ENTERPRISE", "plan_ent"):     "enterprise",
    }

    @app.post("/v1/billing/razorpay/webhook")
    async def razorpay_webhook(request: Request) -> dict[str, Any]:
        """Validate Razorpay webhook signature and apply plan changes."""
        body_bytes = await request.body()
        secret = os.environ.get("NINA_RAZORPAY_WEBHOOK_SECRET", "")

        if secret:
            # Razorpay signs with HMAC-SHA256 of the raw body
            import hmac as _hmac
            import hashlib as _hashlib
            sig = request.headers.get("x-razorpay-signature", "")
            expected_sig = _hmac.new(
                secret.encode(), body_bytes, _hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                raise HTTPException(status_code=400, detail="Invalid Razorpay signature.")

        try:
            payload = json.loads(body_bytes)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.")

        event = payload.get("event", "")
        entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        notes = entity.get("notes", {})
        site_id = notes.get("nina_site_id", "")
        plan_id = entity.get("plan_id", "")

        if not site_id:
            logger.info("razorpay: webhook %s — no nina_site_id in notes", event)
            return {"ok": True, "data": {"processed": False, "reason": "no nina_site_id"}}

        site = STORE.get_site(site_id)
        if not site:
            logger.info("razorpay: webhook %s — unknown site_id=%s", event, site_id)
            return {"ok": True, "data": {"processed": False, "reason": "unknown_site"}}

        plan = _RAZORPAY_PLAN_MAP.get(plan_id)

        if event in ("subscription.activated", "subscription.charged") and plan:
            STORE.set_plan(site_id, plan)
            logger.info("razorpay: site=%s upgraded to plan=%s", site_id, plan)
            return {"ok": True, "data": {"siteId": site_id, "plan": plan, "event": event}}

        if event in ("subscription.cancelled", "subscription.expired", "subscription.halted"):
            STORE.set_plan(site_id, "free")
            logger.info("razorpay: site=%s downgraded to free (event=%s)", site_id, event)
            return {"ok": True, "data": {"siteId": site_id, "plan": "free", "event": event}}

        return {"ok": True, "data": {"processed": False, "event": event}}

    # ── Named HTML pages (must be before the catch-all static mount) ─────────
    console_static = Path(__file__).resolve().parent / "console_static"

    @app.get("/dashboard", include_in_schema=False)
    def serve_dashboard() -> FileResponse:
        return FileResponse(console_static / "dashboard.html")

    # ── Static assets — SDK first, then console UI (catch-all must be last) ──
    sdk_dir = Path(__file__).resolve().parent / "sdk"
    if sdk_dir.exists():
        app.mount("/sdk", StaticFiles(directory=sdk_dir), name="nina-sdk")

    if console_static.exists():
        app.mount("/", StaticFiles(directory=console_static, html=True), name="console-ui")

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nina-console", description="Run NINA Console API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run("nina.console_app:app", host=args.host, port=args.port, reload=False)
    return 0

