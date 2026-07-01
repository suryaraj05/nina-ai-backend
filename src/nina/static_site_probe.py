"""Build a DOM-only agent.json for storefronts without OpenAPI.

When a merchant pastes their shop URL, we first try OpenAPI (see openapi_probe).
If that fails — static SPAs, Vite/React shops, etc. — this module discovers
routes from sitemap.xml, homepage links, and bundled SPA route hints, then
reuses the generator's crawl → infer → assemble pipeline.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from nina.console_pack import fetch_or_build_sitemap
from nina.generator.stages.action_infer import infer_actions
from nina.generator.stages.assemble import assemble_contract
from nina.generator.stages.crawler import crawl_urls
from nina.generator.stages.dom_extract import page_signals_from_crawl, summarize_contract_signals
from nina.generator.stages.routes import build_routes_manifest, merge_routes_into_contract
from nina.generator.stages.sitemap import infer_page_type, parse_sitemap
from nina.generator.stages.validate import validate_contract

_ROUTE_IN_JS = re.compile(
    r"""(?:path|route)\s*[:=]\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_HREF_PATH = re.compile(r"""href=["'](/(?!/)[^"'#?]*)["']""", re.IGNORECASE)
_LINK_TO = re.compile(r"""\bto=["'](/(?!/)[^"'#?]*)["']""", re.IGNORECASE)
_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.IGNORECASE)

# Well-known ecommerce paths when discovery finds almost nothing.
_FALLBACK_PATHS = (
    "/",
    "/shop",
    "/new-arrivals",
    "/cart",
    "/checkout",
    "/login",
    "/signup",
    "/contact",
    "/track-order",
)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _same_origin(base: str, url: str) -> bool:
    return urlparse(base).netloc == urlparse(url).netloc


def _path_only(url: str) -> str:
    path = urlparse(url).path or "/"
    return path if path.startswith("/") else f"/{path}"


def _normalize_route_path(raw: str) -> str | None:
    path = raw.strip()
    if not path.startswith("/"):
        return None
    if "://" in path:
        return None
    # Drop React Router params for sitemap-style discovery; keep a sample slug path.
    if ":" in path:
        sample = path.replace(":categorySlug", "category").replace(":id", "item")
        sample = re.sub(r":\w+", "item", sample)
        return sample
    return path.rstrip("/") or "/"


def discover_paths(client: httpx.Client, storefront_url: str) -> list[str]:
    """Collect same-origin paths from sitemap, HTML links, and JS bundles."""
    base = storefront_url.rstrip("/")
    origin = _origin(base)
    paths: set[str] = set()

    xml, _source = fetch_or_build_sitemap(base)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as tmp:
        tmp.write(xml)
        tmp_path = Path(tmp.name)
    try:
        for entry in parse_sitemap(tmp_path, base_url=base):
            if _same_origin(base, entry["url"]):
                paths.add(_path_only(entry["url"]))
    finally:
        tmp_path.unlink(missing_ok=True)

    try:
        home = client.get(base)
        if home.status_code == 200 and home.text:
            html = home.text
            for pattern in (_HREF_PATH, _LINK_TO):
                for match in pattern.finditer(html):
                    paths.add(match.group(1).rstrip("/") or "/")
            for script_src in _SCRIPT_SRC.findall(html):
                if script_src.startswith("http") and not _same_origin(base, script_src):
                    continue
                js_url = script_src if script_src.startswith("http") else urljoin(origin + "/", script_src.lstrip("/"))
                try:
                    js_resp = client.get(js_url)
                except httpx.HTTPError:
                    continue
                if js_resp.status_code != 200:
                    continue
                for route_match in _ROUTE_IN_JS.finditer(js_resp.text):
                    normalized = _normalize_route_path(route_match.group(1))
                    if normalized:
                        paths.add(normalized)
    except httpx.HTTPError:
        pass

    if len(paths) < 3:
        paths.update(_FALLBACK_PATHS)

    return sorted(paths)


def _entries_from_paths(storefront_url: str, paths: list[str]) -> list[dict[str, Any]]:
    origin = _origin(storefront_url)
    entries: list[dict[str, Any]] = []
    for path in paths:
        url = origin + (path if path.startswith("/") else f"/{path}")
        entries.append({
            "url": url,
            "priority": 1.0 if path in ("/", "") else 0.7,
            "changefreq": "weekly",
        })
    return entries


def _enrich_spa_actions(contract: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    """Add navigate-based helpers common on SPAs (query search, category slugs)."""
    actions = list(contract.get("actions") or [])
    by_id = {a["id"]: a for a in actions}
    page_ids = [p["id"] for p in contract.get("pages", [])]

    def add(spec: dict[str, Any]) -> None:
        if spec["id"] not in by_id:
            actions.append(spec)
            by_id[spec["id"]] = spec

    path_set = set(paths)
    has_shop = any(p == "/shop" or p.startswith("/shop") for p in path_set)
    if has_shop and "search_products" not in by_id:
        add({
            "id": "search_products",
            "description": "Search the store catalog by keyword",
            "parameters": {
                "query": {"type": "string", "required": True, "description": "Search terms"},
            },
            "risk": "low",
            "requiresAuth": False,
            "availableOn": [p for p in page_ids if p in ("home", "product_list", "search", "generic")],
            "execute": {
                "type": "dom",
                "steps": [{"op": "navigate", "url": "/shop?search={query}"}],
            },
        })

    category_slugs = sorted({
        p.split("/category/", 1)[1]
        for p in path_set
        if p.startswith("/category/") and "/" not in p.split("/category/", 1)[1]
    })
    if category_slugs and "open_category" not in by_id:
        add({
            "id": "open_category",
            "description": "Browse a product category",
            "parameters": {
                "categorySlug": {
                    "type": "string",
                    "required": True,
                    "description": "Category URL slug",
                    "enum": category_slugs,
                },
            },
            "risk": "low",
            "requiresAuth": False,
            "availableOn": [p for p in page_ids if p in ("home", "product_list", "generic")],
            "execute": {
                "type": "dom",
                "steps": [{"op": "navigate", "url": "/category/{categorySlug}"}],
            },
        })

    if any(p.startswith("/product/") for p in path_set) and "open_product" not in by_id:
        add({
            "id": "open_product",
            "description": "Open a product detail page",
            "parameters": {
                "productId": {
                    "type": "string",
                    "required": True,
                    "description": "Product id from the catalog URL",
                },
            },
            "risk": "low",
            "requiresAuth": False,
            "availableOn": [p for p in page_ids if p in ("home", "product_list", "product_detail", "generic")],
            "execute": {
                "type": "dom",
                "steps": [{"op": "navigate", "url": "/product/{productId}"}],
            },
        })

    contract["actions"] = actions
    return contract


def build_contract_from_static_site(
    *,
    site_id: str,
    site_name: str,
    base_url: str,
    storefront_url: str | None = None,
    locales: list[str] | None = None,
    max_pages: int = 25,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (contract, stats). Raises ValueError when validation fails."""
    store_url = (storefront_url or base_url).rstrip("/")
    site = {
        "id": site_id,
        "name": site_name,
        "baseUrl": base_url.rstrip("/"),
        "locales": locales or ["en"],
    }

    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        paths = discover_paths(client, store_url)
        entries = _entries_from_paths(store_url, paths)
        crawled = crawl_urls(entries, max_pages=max_pages, delay_ms=100)

    if not crawled:
        crawled = [{
            "url": store_url + "/",
            "pageType": "home",
            "status": 200,
            "html": "",
            "priority": 1.0,
        }]

    dom_by_type = page_signals_from_crawl(crawled)
    page_types = {p.get("pageType", "generic") for p in crawled} or {"home"}
    actions, selectors = infer_actions(page_types, dom_by_type)

    contract = assemble_contract(
        site,
        crawled,
        actions,
        selectors,
        auth_policy={"loginUrl": "/login", "gatedActions": ["checkout"]},
        risk_policy={"confirmActions": ["checkout"]},
        page_signals=summarize_contract_signals(dom_by_type),
    )
    contract = _enrich_spa_actions(contract, paths)
    routes_manifest = build_routes_manifest(crawled)
    contract = merge_routes_into_contract(contract, routes_manifest)

    ok, errors = validate_contract(contract)
    stats = {
        "source": "static",
        "pathsDiscovered": len(paths),
        "pagesCrawled": len(crawled),
        "pageTypes": sorted(page_types),
        "actions": len(contract.get("actions", [])),
        "routes": len(contract.get("routes", [])),
    }
    if not ok:
        raise ValueError("; ".join(errors[:5]))
    return contract, stats
