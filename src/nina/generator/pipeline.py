"""NINA generator pipeline orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nina.generator.heal import (
    apply_heal_to_contract,
    crawl_report_pages,
    load_existing_contract,
    normalize_reports,
    prioritize_entries,
    write_heal_log,
)
from nina.generator.stages.action_infer import infer_actions
from nina.generator.stages.api_manifest import load_api_manifest, merge_api_manifest_into_contract
from nina.generator.stages.assemble import assemble_contract
from nina.generator.stages.crawler import crawl_urls
from nina.generator.stages.dom_extract import page_signals_from_crawl, summarize_contract_signals
from nina.generator.stages.dom_playwright import enrich_crawl_with_playwright
from nina.generator.stages.routes import build_routes_manifest, merge_routes_into_contract
from nina.generator.stages.publish import publish_contract
from nina.generator.stages.sitemap import parse_sitemap
from nina.generator.stages.validate import validate_contract


@dataclass
class GenerationResult:
    ok: bool
    contract: dict[str, Any] | None = None
    output_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    heal_log: list[dict[str, Any]] = field(default_factory=list)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_heal_only(
    config_dir: Path,
    reports: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> GenerationResult:
    """Patch existing dist/agent.json from failure reports without full regen."""
    config_dir = Path(config_dir)
    site_path = config_dir / "nina.site.yaml"
    if not site_path.exists():
        return GenerationResult(ok=False, errors=[f"Missing {site_path}"])
    site_cfg = _load_yaml(site_path)
    pub_cfg = site_cfg.get("publish") or {}
    out_dir = config_dir / pub_cfg.get("outputDir", "dist")
    existing = load_existing_contract(out_dir)
    if not existing:
        return GenerationResult(
            ok=False,
            errors=[f"No existing agent.json in {out_dir} — run a full generate first."],
        )
    if not reports:
        return GenerationResult(ok=False, errors=["No heal reports provided."])

    pages = crawl_report_pages(reports)
    contract, heal_log = apply_heal_to_contract(existing, reports, pages)
    ok, errors = validate_contract(contract)
    stats = {
        "mode": "heal-only",
        "failures": len(reports),
        "patched": sum(1 for h in heal_log if h.get("status") == "patched"),
        "unresolved": sum(1 for h in heal_log if h.get("status") == "unresolved"),
    }
    if not ok:
        return GenerationResult(
            ok=False,
            contract=contract,
            errors=errors,
            stats=stats,
            heal_log=heal_log,
        )

    output_path = None
    if not dry_run:
        output_path = publish_contract(contract, out_dir)
        write_heal_log(out_dir, heal_log)
    return GenerationResult(
        ok=True,
        contract=contract,
        output_path=output_path,
        stats=stats,
        heal_log=heal_log,
    )


def run_pipeline(
    config_dir: Path,
    *,
    dry_run: bool = False,
    heal_from: Path | None = None,
    heal_only: bool = False,
    fetch_reports_url: str | None = None,
    site_id: str | None = None,
    strict: bool = True,
    probe: bool = False,
) -> GenerationResult:
    """
    Run full generator: nina.site.yaml + sitemap → agent.json.

    config_dir should contain nina.site.yaml, sitemap.xml, optional policies.
    """
    config_dir = Path(config_dir)
    site_path = config_dir / "nina.site.yaml"
    if not site_path.exists():
        return GenerationResult(ok=False, errors=[f"Missing {site_path}"])

    site_cfg = _load_yaml(site_path)
    site = site_cfg.get("site") or {}
    gen_cfg = site_cfg.get("generator") or {}
    pub_cfg = site_cfg.get("publish") or {}

    sitemap_name = gen_cfg.get("sitemap", "sitemap.xml")
    sitemap_path = config_dir / sitemap_name
    if not sitemap_path.exists():
        return GenerationResult(ok=False, errors=[f"Missing sitemap: {sitemap_path}"])

    # auth.policy.yaml / risk.policy.yaml are the advanced, broken-out form.
    # The simple onboarding pack instead embeds these as nested "auth:" /
    # "risk:" keys directly in nina.site.yaml -- fall back to those when no
    # separate file is present, so the simple pack still ships a secure
    # default with one fewer file to manage.
    auth_path = config_dir / "auth.policy.yaml"
    auth_policy: dict[str, Any] = _load_yaml(auth_path) if auth_path.exists() else (site_cfg.get("auth") or {})

    risk_path = config_dir / "risk.policy.yaml"
    risk_policy: dict[str, Any] = _load_yaml(risk_path) if risk_path.exists() else (site_cfg.get("risk") or {})

    heal_hints: list[dict[str, Any]] = []
    if fetch_reports_url:
        from nina.generator.heal import fetch_reports_from_api

        heal_hints = fetch_reports_from_api(fetch_reports_url, site_id=site_id)
    elif heal_from and heal_from.exists():
        with heal_from.open(encoding="utf-8") as f:
            data = json.load(f)
        heal_hints = normalize_reports(data)

    if heal_only:
        return run_heal_only(config_dir, heal_hints, dry_run=dry_run)

    out_dir = config_dir / pub_cfg.get("outputDir", "dist")
    existing_contract = load_existing_contract(out_dir)

    entries = parse_sitemap(sitemap_path, site.get("baseUrl"))
    if heal_hints:
        entries = prioritize_entries(entries, heal_hints)
    crawl_cfg = gen_cfg.get("crawl") or {}
    crawled = crawl_urls(
        entries,
        max_pages=crawl_cfg.get("maxPages", 50),
        delay_ms=crawl_cfg.get("delayMs", 500),
    )

    if heal_hints:
        report_pages = crawl_report_pages(heal_hints)
        by_url = {p["url"]: p for p in crawled}
        for page in report_pages:
            by_url[page["url"]] = page
        crawled = list(by_url.values())

    if crawl_cfg.get("usePlaywright"):
        crawled = enrich_crawl_with_playwright(
            crawled,
            timeout_ms=crawl_cfg.get("playwrightTimeoutMs", 15000),
            max_pages=crawl_cfg.get("playwrightMaxPages", crawl_cfg.get("maxPages", 10)),
        )

    dom_by_type = page_signals_from_crawl(crawled)
    for page in crawled:
        live = page.get("domSignals")
        if not live:
            continue
        ptype = page.get("pageType", "generic")
        bucket = dom_by_type.setdefault(ptype, {})
        for key in ("searchInputs", "buttons", "forms", "links", "sizeOptions"):
            existing = bucket.setdefault(key, [])
            for item in live.get(key, []):
                if item not in existing:
                    existing.append(item)
    page_types = {p.get("pageType", "generic") for p in crawled} or {"home"}
    actions, selectors = infer_actions(page_types, dom_by_type, heal_hints)

    contract = assemble_contract(
        site,
        crawled,
        actions,
        selectors,
        auth_policy=auth_policy,
        risk_policy=risk_policy,
        page_signals=summarize_contract_signals(dom_by_type),
    )

    routes_manifest = build_routes_manifest(crawled, version=contract.get("version", "1.0.0"))
    contract = merge_routes_into_contract(contract, routes_manifest)

    api_manifest_path = config_dir / "api.manifest.yaml"
    api_manifest = load_api_manifest(api_manifest_path)
    if api_manifest:
        contract = merge_api_manifest_into_contract(contract, api_manifest)

    heal_log: list[dict[str, Any]] = []
    if heal_hints:
        heal_base = existing_contract if existing_contract else contract
        contract, heal_log = apply_heal_to_contract(heal_base, heal_hints, crawled)

    ok, errors = validate_contract(contract)
    stats = {
        "urlsInSitemap": len(entries),
        "pagesCrawled": len(crawled),
        "pageTypes": sorted(page_types),
        "actions": len(contract.get("actions", [])),
        "routes": len(routes_manifest.get("routes", [])),
        "healed": sum(1 for h in heal_log if h.get("status") == "patched"),
        "healUnresolved": sum(1 for h in heal_log if h.get("status") == "unresolved"),
    }

    if not ok:
        return GenerationResult(
            ok=False,
            contract=contract,
            errors=errors,
            stats=stats,
            heal_log=heal_log,
        )

    if strict:
        from nina.contract_validate import validate_executable

        exec_ok, exec_errors, exec_warnings = validate_executable(
            contract,
            strict=True,
            probe=probe,
        )
        stats["executableWarnings"] = len(exec_warnings)
        if not exec_ok:
            return GenerationResult(
                ok=False,
                contract=contract,
                errors=exec_errors,
                stats=stats,
                heal_log=heal_log,
            )

    output_path = None
    if not dry_run:
        output_path = publish_contract(
            contract,
            out_dir,
            routes_manifest=routes_manifest,
        )
        if heal_log:
            write_heal_log(out_dir, heal_log)

    return GenerationResult(
        ok=True,
        contract=contract,
        output_path=output_path,
        stats=stats,
        heal_log=heal_log,
    )
