"""Grounded product catalog — anti-hallucination data rail (ported from Nina-Research).

Search and mutation gates use only rows pulled from the merchant's real catalog
(Firestore, JSON-LD, or API). The LLM never invents SKUs, prices, or inventory.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_SEARCH_ACTIONS = frozenset({"search", "search_products", "list_products", "browse_products"})
_MUTATING_ACTIONS = frozenset({
    "add_to_cart", "add_item_to_cart", "update_cart_item",
    "remove_from_cart", "start_checkout", "checkout", "confirm_purchase",
})
_PRODUCT_MUTATIONS = frozenset({
    "add_to_cart", "add_item_to_cart", "update_cart_item", "remove_from_cart",
})

_PRICE_CLAUSE = re.compile(
    r"(?:under|below|less than|upto|up to|max(?:imum)?|cheaper than)\s*"
    r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?)\s*(k|K)?",
    re.IGNORECASE,
)
_CURRENCY_NOISE = re.compile(r"(?:₹|rs\.?|inr)\s*", re.IGNORECASE)


@dataclass
class CatalogProduct:
    sku: str
    name: str
    price: float | None = None
    currency: str | None = "INR"
    availability: str | None = None
    url: str | None = None
    image: str | None = None
    category: str | None = None
    in_stock: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_result_row(self, *, base_url: str | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.sku,
            "sku": self.sku,
            "name": self.name,
            "title": self.name,
            "price": self.price,
            "currency": self.currency or "INR",
            "inStock": self.in_stock,
        }
        if self.url:
            row["url"] = self.url
        elif base_url:
            row["url"] = f"{base_url.rstrip('/')}/product/{self.sku}"
        if self.image:
            row["image"] = self.image
        if self.category:
            row["category"] = self.category
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> CatalogProduct:
        sku = str(row.get("sku") or row.get("id") or "").strip()
        name = str(row.get("name") or row.get("title") or sku).strip()
        price = row.get("price")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        avail = str(row.get("availability") or "").lower()
        in_stock = row.get("in_stock")
        if in_stock is None:
            in_stock = row.get("inStock")
        if in_stock is None:
            in_stock = ("outofstock" not in avail and "out_of_stock" not in avail) if avail else True
        return cls(
            sku=sku,
            name=name,
            price=price_f,
            currency=row.get("currency"),
            availability=row.get("availability"),
            url=row.get("url"),
            image=row.get("image"),
            category=row.get("category"),
            in_stock=bool(in_stock),
            extra={k: v for k, v in row.items() if k not in {
                "sku", "id", "name", "title", "price", "currency", "availability",
                "url", "image", "category", "in_stock", "inStock",
            }},
        )


class CatalogGraph:
    """In-memory catalog graph — all lookups are against stored rows only."""

    def __init__(self) -> None:
        self._by_sku: dict[str, CatalogProduct] = {}
        self._names: list[str] = []

    def load_rows(self, rows: list[dict[str, Any]]) -> CatalogGraph:
        self._by_sku.clear()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            product = CatalogProduct.from_row(row)
            if product.sku:
                self._by_sku[product.sku] = product
        self._names = [p.name for p in self._by_sku.values()]
        return self

    def __len__(self) -> int:
        return len(self._by_sku)

    def lookup_name(self, name_hint: str) -> CatalogProduct | None:
        hint = (name_hint or "").strip()
        if not hint or not self._names:
            return None
        if hint in self._by_sku:
            return self._by_sku[hint]
        hit = difflib.get_close_matches(hint, self._names, n=1, cutoff=0.6)
        if hit:
            for product in self._by_sku.values():
                if product.name == hit[0]:
                    return product
        return None

    def search(
        self,
        query: str,
        *,
        max_price: float | None = None,
        limit: int = 8,
    ) -> list[CatalogProduct]:
        text, price_cap = parse_price_constraint(query)
        if max_price is None:
            max_price = price_cap
        tokens = _query_tokens(text)
        if not tokens and not max_price:
            return []

        scored: list[tuple[float, CatalogProduct]] = []
        for product in self._by_sku.values():
            if max_price is not None and product.price is not None and product.price > max_price:
                continue
            score = _match_score(tokens, product)
            if score > 0:
                scored.append((score, product))

        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [p for _, p in scored[:limit]]


def rows_from_products(products: list[CatalogProduct]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for product in products:
        row = product.to_result_row()
        row.update(product.extra)
        rows.append(row)
    return rows


def graph_from_rows(rows: list[dict[str, Any]]) -> CatalogGraph:
    return CatalogGraph().load_rows(rows)


def parse_price_constraint(query: str) -> tuple[str, float | None]:
    text = (query or "").strip()
    match = _PRICE_CLAUSE.search(text)
    if not match:
        return _CURRENCY_NOISE.sub("", text).strip(), None
    amount = float(match.group(1))
    if match.group(2):
        amount *= 1000
    cleaned = (text[: match.start()] + text[match.end() :]).strip(" ,.-")
    cleaned = _CURRENCY_NOISE.sub("", cleaned).strip()
    return cleaned, amount


def _query_tokens(text: str) -> list[str]:
    stop = {"a", "an", "the", "me", "my", "for", "some", "any", "show", "find", "get"}
    return [
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 1 and t not in stop
    ]


def _match_score(tokens: list[str], product: CatalogProduct) -> float:
    if not tokens:
        return 0.0
    hay = " ".join(
        x for x in (product.name, product.category or "", product.sku) if x
    ).lower()
    hits = sum(1 for t in tokens if t in hay)
    if hits == 0:
        ratio = difflib.SequenceMatcher(None, " ".join(tokens), hay).ratio()
        return ratio if ratio >= 0.55 else 0.0
    return hits / len(tokens)


def storefront_browse_url(query: str, base_url: str) -> str | None:
    """Build a store search URL using keywords only — not the full NL phrase."""
    if not base_url:
        return None
    text, _ = parse_price_constraint(query or "")
    tokens = _query_tokens(text)
    root = base_url.rstrip("/")
    if not tokens:
        return f"{root}/shop"
    keyword = tokens[0] if len(tokens) == 1 else " ".join(tokens[:2])
    from urllib.parse import quote

    return f"{root}/shop?search={quote(keyword)}"


def execute_catalog_search(
    action_input: dict[str, Any],
    catalog_rows: list[dict[str, Any]],
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    if not catalog_rows:
        return {
            "results": [],
            "count": 0,
            "grounded": True,
            "noCatalog": True,
        }
    query = str((action_input or {}).get("query") or "").strip()
    graph = graph_from_rows(catalog_rows)
    products = graph.search(query, limit=8)
    results = [p.to_result_row(base_url=base_url) for p in products]
    return {
        "results": results,
        "count": len(results),
        "grounded": True,
        "query": query,
    }


def validate_catalog_mutation(
    action_name: str,
    action_input: dict[str, Any],
    catalog_rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    if action_name not in _PRODUCT_MUTATIONS:
        return True, ""
    if not catalog_rows:
        return True, ""
    graph = graph_from_rows(catalog_rows)
    hint = (
        (action_input or {}).get("productId")
        or (action_input or {}).get("sku")
        or (action_input or {}).get("query")
        or (action_input or {}).get("name")
        or ""
    )
    product = graph.lookup_name(str(hint)) if hint else None
    if not product:
        return False, "that product is not in the verified catalog"
    if not product.in_stock:
        return False, f"{product.name} is out of stock"
    quoted = (action_input or {}).get("price")
    if quoted is not None and product.price is not None:
        try:
            if abs(float(quoted) - product.price) > 0.01:
                return False, "quoted price does not match the catalog"
        except (TypeError, ValueError):
            pass
    return True, ""


def make_catalog_search_handler() -> Any:
    def handler(inp: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        rows = ctx.get("productCatalog") or []
        contract = ctx.get("agentContract") or {}
        base_url = (contract.get("site") or {}).get("baseUrl")
        return execute_catalog_search(inp, rows, base_url=base_url)

    return handler


def is_grounded_result(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("grounded"))


def grounded_reply(action_name: str, result: dict[str, Any]) -> str:
    if result.get("noCatalog"):
        return (
            "I don't have verified product data for this store yet. "
            "Browse the shop directly to see what's available."
        )
    count = int(result.get("count") or 0)
    if action_name in _SEARCH_ACTIONS:
        if count == 0:
            return "I couldn't find anything matching that in the catalog."
        return f"I found {count} item{'s' if count != 1 else ''} in the catalog."
    return _deterministic_short(result)


def _deterministic_short(result: dict[str, Any]) -> str:
    if result.get("blocked"):
        return str(result.get("message") or "I can't do that safely.")
    return "Done."


def catalog_rows_to_json(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False, default=str)
