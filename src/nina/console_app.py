"""Hosted NINA Console (hybrid model) for onboarding and key management.

This module intentionally ships as a lightweight in-memory control plane so
teams can run and extend it without external dependencies.
"""

from __future__ import annotations

import argparse
import hmac
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .console_deps import POOL, STORE, _db_url
from .crypto import is_production
from .console_infra import METRICS, logger, _request_id_var
from .console_routes_admin import router as _admin_router
from .console_routes_auth import router as _auth_router
from .console_routes_wizard import router as _wizard_router
from .console_routes_tools import router as _tools_router
from .console_routes_query import router as _query_router
from .console_routes_channels import router as _channels_router


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


    # Operator/admin control-plane CRUD (/v1/orgs|sites|keys|tokens|…) lives in console_routes_admin.
    app.include_router(_admin_router)
    # Merchant dashboard routes (/v1/auth/*) live in console_routes_auth.
    app.include_router(_auth_router)
    # Onboarding wizard routes (/v1/wizard/*) live in console_routes_wizard.
    app.include_router(_wizard_router)
    # Developer/registrar/seo tooling routes live in console_routes_tools.
    app.include_router(_tools_router)
    # Widget hot path (POST /v1/query) lives in console_routes_query.
    app.include_router(_query_router)
    # WhatsApp channel + Razorpay billing webhooks live in console_routes_channels.
    app.include_router(_channels_router)


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

