"""Onboarding wizard routes (``/v1/wizard/*``).

The guided setup flow: create org+site+key, probe a merchant's API, generate and
validate a contract, and build the downloadable onboarding pack. Mounted on the
app via ``include_router`` in ``console_app.create_app``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Response

from .console_deps import STORE
from .console_infra import _validate_external_url
from .console_pack import build_onboarding_pack_files, resolve_site_fields, zip_onboarding_pack
from .console_schemas import (
    OnboardingPackIn,
    WizardApiConnectIn,
    WizardGenerateIn,
    WizardInitIn,
    WizardValidateIn,
)
from .contract_validate import validate_executable
from .generator.pipeline import run_pipeline

router = APIRouter()


@router.post("/v1/wizard/init")
def wizard_init(body: WizardInitIn) -> dict[str, Any]:
    org = STORE.create_org(body.orgName, body.ownerEmail)
    site = STORE.create_site(
        org["id"],
        body.siteName,
        body.baseUrl,
        locales=body.languages,
        markets=[body.country],
        currency=body.currency,
    )
    key = STORE.issue_api_key(site["id"], "test", "pk")
    return {"ok": True, "data": {"org": org, "site": site, "publishableKey": key}}

@router.post("/v1/wizard/connect-apis")
def wizard_connect_apis(body: WizardApiConnectIn) -> dict[str, Any]:
    _validate_external_url(body.apiBaseUrl, "API base URL")
    checks: list[dict[str, Any]] = []
    paths = [body.searchPath, body.listCategoriesPath]
    with httpx.Client(timeout=5.0, follow_redirects=True) as client:
        for path in paths:
            url = f"{body.apiBaseUrl.rstrip('/')}/{path.lstrip('/')}"
            try:
                resp = client.options(url)
                checks.append({"url": url, "ok": resp.status_code < 500, "status": resp.status_code})
            except Exception as exc:
                checks.append({"url": url, "ok": False, "error": str(exc)})
    return {"ok": True, "data": {"checks": checks}}

@router.post("/v1/wizard/generate-contract")
def wizard_generate_contract(body: WizardGenerateIn) -> dict[str, Any]:
    result = run_pipeline(Path(body.configDir), dry_run=False, strict=body.strict, probe=body.probe)
    return {
        "ok": result.ok,
        "data": {
            "outputPath": str(result.output_path) if result.output_path else None,
            "stats": result.stats,
        },
        "errors": result.errors,
    }

@router.post("/v1/wizard/validate-contract")
def wizard_validate_contract(body: WizardValidateIn) -> dict[str, Any]:
    p = Path(body.agentPath)
    if not p.exists():
        raise HTTPException(status_code=404, detail="agent.json not found")
    contract = json.loads(p.read_text(encoding="utf-8"))
    ok, errors, warnings = validate_executable(contract, strict=body.strict, probe=body.probe)
    return {"ok": ok, "errors": errors, "warnings": warnings}

@router.post("/v1/wizard/onboarding-pack")
def wizard_onboarding_pack(body: OnboardingPackIn) -> Response:
    """Build and download zip: nina.site.yaml, api.manifest.yaml, sitemap.xml, policies."""
    site = STORE.get_site(body.siteId) if body.siteId else None
    if body.siteId and not site:
        raise HTTPException(status_code=404, detail="Unknown site_id")
    try:
        fields = resolve_site_fields(
            site,
            site_name=body.siteName,
            base_url=body.baseUrl,
            locales=body.locales,
            markets=body.markets,
            allowed_origins=body.allowedOrigins,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    files = build_onboarding_pack_files(
        site_id=fields["site_id"],
        site_name=fields["site_name"],
        base_url=fields["base_url"],
        locales=fields["locales"],
        markets=fields["markets"],
        allowed_origins=fields["allowed_origins"],
        api_base_url=body.apiBaseUrl,
        capabilities=body.capabilities,
        sitemap_url=body.sitemapUrl,
        raw_sitemap_xml=body.rawSitemapXml,
        include_auth=body.includeAuth,
        include_risk=body.includeRisk,
        include_skills=body.includeSkills,
    )
    payload, filename = zip_onboarding_pack(files, archive_name=fields["site_id"])
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@router.get("/v1/wizard/steps")
def wizard_steps() -> dict[str, Any]:
    steps = [
        "Welcome",
        "Your store",
        "Capabilities",
        "Connect APIs",
        "Verify domain",
        "Build contract",
        "Review actions",
        "Install NINA",
        "Test sandbox",
        "Go live",
    ]
    return {"ok": True, "data": steps}
