"""Developer / operations tooling routes.

Peripheral endpoints used during integration and operations: local config-file
read/write (``/v1/developer/*``), broken-selector webhooks, domain verification
and site export (``/v1/registrar/*``), and SEO/embed-health checks
(``/v1/seo/*``). Mounted via ``include_router`` in ``console_app.create_app``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException

from .console_deps import STORE
from .console_infra import _validate_external_url, _validate_local_path
from .console_schemas import RegistrarExportIn, SeoEmbedHealthIn, SeoSitemapIn

router = APIRouter()


@router.get("/v1/developer/files")
def developer_files(config_dir: str) -> dict[str, Any]:
    base = _validate_local_path(config_dir)
    names = ["nina.site.yaml", "api.manifest.yaml", "auth.policy.yaml", "risk.policy.yaml"]
    out: dict[str, str] = {}
    for name in names:
        p = base / name
        if p.exists():
            out[name] = p.read_text(encoding="utf-8")
    return {"ok": True, "data": out}

@router.post("/v1/developer/files")
def developer_write_file(config_dir: str, filename: str, content: str) -> dict[str, Any]:
    if filename not in {"nina.site.yaml", "api.manifest.yaml", "auth.policy.yaml", "risk.policy.yaml"}:
        raise HTTPException(status_code=400, detail="Unsupported config file")
    base = _validate_local_path(config_dir)
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "data": {"path": str(path)}}

@router.post("/v1/webhooks/broken-selector")
def webhook_broken_selector(payload: dict[str, Any]) -> dict[str, Any]:
    count = STORE.push_webhook_event("broken-selector", payload)
    return {"ok": True, "data": {"queued": True, "count": count}}

@router.get("/v1/webhooks/broken-selector")
def webhook_list_broken_selector() -> dict[str, Any]:
    return {"ok": True, "data": STORE.list_webhook_events("broken-selector")}

# Registrar + GEO
@router.post("/v1/registrar/verify-domain")
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

@router.post("/v1/registrar/export-nina-site")
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
@router.post("/v1/seo/sitemap")
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

@router.post("/v1/seo/embed-health")
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
