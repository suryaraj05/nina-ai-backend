"""Pull a merchant's real product catalog at onboarding (Firestore + JSON-LD)."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .catalog_rail import CatalogProduct, rows_from_products
from .static_site_probe import discover_paths, _origin

_FB_PROJECT = re.compile(r'["\']?projectId["\']?\s*[:=]\s*["\']([a-z0-9\-]+)["\']', re.IGNORECASE)
_FB_AUTH_DOMAIN = re.compile(
    r'["\']?authDomain["\']?\s*[:=]\s*["\']([a-z0-9\-]+)\.firebaseapp\.com["\']',
    re.IGNORECASE,
)
_FB_APIKEY = re.compile(r'["\']?apiKey["\']?\s*[:=]\s*["\'](AIza[0-9A-Za-z_\-]{20,})["\']')
_LD_BLOCK = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_SCRIPT_SRC = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.IGNORECASE)

_CANDIDATE_COLLECTIONS = ("products", "items", "inventory", "catalog", "shop", "Products")
_MAX_JSONLD_PAGES = 40


def _fs_val(v: dict[str, Any]) -> Any:
    for key in ("stringValue", "booleanValue"):
        if key in v:
            return v[key]
    for key in ("integerValue", "doubleValue"):
        if key in v:
            try:
                return float(v[key]) if key == "doubleValue" else int(v[key])
            except (TypeError, ValueError):
                return v[key]
    if "arrayValue" in v:
        return [_fs_val(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {kk: _fs_val(vv) for kk, vv in v["mapValue"].get("fields", {}).items()}
    return None


def _fs_docs_to_products(data: dict[str, Any]) -> list[CatalogProduct]:
    out: list[CatalogProduct] = []
    for doc in data.get("documents") or []:
        fields = doc.get("fields") or {}
        did = doc.get("name", "/x").split("/")[-1]
        name = _fs_val(fields.get("name", {})) or _fs_val(fields.get("title", {})) or did
        price = _fs_val(fields.get("price", {})) or _fs_val(fields.get("sellingPrice", {}))
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        currency = str(_fs_val(fields.get("currency", {})) or "INR")

        size_stock: dict[str, int] = {}
        stock_val = fields.get("stock", {})
        if isinstance(stock_val, dict) and "mapValue" in stock_val:
            for sz, vv in stock_val["mapValue"].get("fields", {}).items():
                try:
                    size_stock[sz] = int(_fs_val(vv) or 0)
                except (TypeError, ValueError):
                    size_stock[sz] = 0
        in_stock = any(v > 0 for v in size_stock.values()) if size_stock else True

        images = _fs_val(fields.get("images", {})) or []
        image = images[0] if isinstance(images, list) and images else None
        category = _fs_val(fields.get("category", {}))

        out.append(CatalogProduct(
            sku=did,
            name=str(name),
            price=price_f,
            currency=currency,
            in_stock=in_stock,
            image=str(image) if image else None,
            category=str(category) if category else None,
        ))
    return out


def _extract_jsonld_products(html: str) -> list[CatalogProduct]:
    out: list[CatalogProduct] = []
    for raw in _LD_BLOCK.findall(html or ""):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = obj.get("@graph") if isinstance(obj, dict) and "@graph" in obj else [obj]
        if isinstance(obj, list):
            nodes = obj
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            if t != "Product" and not (isinstance(t, list) and "Product" in t):
                continue
            offers = node.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            sku = str(node.get("sku") or node.get("productID") or node.get("name") or "")
            price = offers.get("price")
            try:
                price_f = float(price) if price is not None else None
            except (TypeError, ValueError):
                price_f = None
            avail = str(offers.get("availability") or "")
            in_stock = "outofstock" not in avail.lower()
            out.append(CatalogProduct(
                sku=sku,
                name=str(node.get("name") or sku),
                price=price_f,
                currency=str(offers.get("priceCurrency") or "INR"),
                availability=avail,
                url=node.get("url"),
                in_stock=in_stock,
            ))
    return out


def _firestore_project_in_text(text: str) -> str | None:
    if not text:
        return None
    match = _FB_PROJECT.search(text)
    if match:
        return match.group(1)
    match = _FB_AUTH_DOMAIN.search(text)
    if match:
        return match.group(1)
    return None


def _detect_firestore_project(html: str, bundle_text: str) -> str | None:
    return _firestore_project_in_text(f"{html}\n{bundle_text}")


def _detect_firestore_project_from_scripts(
    client: httpx.Client,
    storefront_url: str,
    html: str,
) -> str | None:
    """Scan homepage + linked JS without truncating large bundles."""
    project = _firestore_project_in_text(html or "")
    if project:
        return project
    origin = _origin(storefront_url)
    for src in _SCRIPT_SRC.findall(html or "")[:12]:
        js_url = src if src.startswith("http") else urljoin(origin + "/", src.lstrip("/"))
        try:
            resp = client.get(js_url, timeout=20.0)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        project = _firestore_project_in_text(resp.text)
        if project:
            return project
    return None


def _try_firestore_rest(client: httpx.Client, project: str) -> list[CatalogProduct]:
    for coll in _CANDIDATE_COLLECTIONS:
        url = (
            f"https://firestore.googleapis.com/v1/projects/{project}"
            f"/databases/(default)/documents/{coll}?pageSize=300"
        )
        try:
            resp = client.get(url, timeout=20.0)
            resp.raise_for_status()
            products = _fs_docs_to_products(resp.json())
            if products:
                return products
        except Exception:
            continue
    return []


def _collect_bundle_text(client: httpx.Client, storefront_url: str, html: str) -> str:
    origin = _origin(storefront_url)
    chunks = [html or ""]
    for src in _SCRIPT_SRC.findall(html or "")[:12]:
        js_url = src if src.startswith("http") else urljoin(origin + "/", src.lstrip("/"))
        try:
            resp = client.get(js_url, timeout=15.0)
            if resp.status_code == 200:
                chunks.append(resp.text[:500_000])
        except httpx.HTTPError:
            continue
    return "\n".join(chunks)


def _jsonld_from_paths(
    client: httpx.Client,
    storefront_url: str,
    paths: list[str],
) -> list[CatalogProduct]:
    origin = _origin(storefront_url)
    product_paths = [p for p in paths if "/product/" in p][: _MAX_JSONLD_PAGES]
    if not product_paths:
        product_paths = [p for p in paths if p.startswith("/category/")][:5]
    out: list[CatalogProduct] = []
    for path in product_paths:
        url = origin + path
        try:
            resp = client.get(url, timeout=20.0, follow_redirects=True)
        except httpx.HTTPError:
            continue
        if resp.status_code != 200:
            continue
        for product in _extract_jsonld_products(resp.text):
            product.url = product.url or url
            out.append(product)
    return out


def pull_product_catalog(
    storefront_url: str,
    *,
    firestore_project: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (catalog rows, meta). Never invents rows — empty list on failure."""
    meta: dict[str, Any] = {"source": "none", "productCount": 0}
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        try:
            home = client.get(storefront_url.rstrip("/"))
            html = home.text if home.status_code == 200 else ""
        except httpx.HTTPError as exc:
            meta["error"] = str(exc)
            return [], meta

        project = (firestore_project or "").strip() or None
        if not project:
            project = _detect_firestore_project_from_scripts(client, storefront_url, html)
        if not project:
            bundle_text = _collect_bundle_text(client, storefront_url, html)
            project = _detect_firestore_project(html, bundle_text)
        meta["firestoreProject"] = project

        products: list[CatalogProduct] = []
        if project:
            products = _try_firestore_rest(client, project)
            if products:
                meta["source"] = "firestore"

        if not products:
            paths = discover_paths(client, storefront_url)
            products = _jsonld_from_paths(client, storefront_url, paths)
            if products:
                meta["source"] = "jsonld"
                meta["pagesScanned"] = min(len(paths), _MAX_JSONLD_PAGES)

        # Dedupe by SKU
        uniq: dict[str, CatalogProduct] = {}
        for p in products:
            if p.sku:
                uniq[p.sku] = p
        rows = rows_from_products(list(uniq.values()))
        meta["productCount"] = len(rows)
        return rows, meta
