"""Lazy catalog hydration on first query when store has no productCatalog."""

from __future__ import annotations

from unittest.mock import patch

from nina.catalog_hydrate import ensure_site_catalog
from nina.console_store import ConsoleStore


def test_ensure_site_catalog_returns_stored_without_pull():
    store = ConsoleStore()
    site_id = "site_stored"
    rows = [{"sku": "a", "name": "Hoodie", "price": 999}]
    store.sites[site_id] = {
        "id": site_id,
        "baseUrl": "https://shop.test",
        "productCatalog": rows,
    }
    out, meta = ensure_site_catalog(store, store.sites[site_id])
    assert out == rows
    assert meta["hydrated"] is False


def test_ensure_site_catalog_pulls_and_persists():
    store = ConsoleStore()
    site_id = "site_pull"
    store.sites[site_id] = {"id": site_id, "baseUrl": "https://tighthug.test"}

    fake_rows = [{"sku": "h1", "name": "Black Hoodie", "price": 1299}]
    fake_meta = {"source": "firestore", "productCount": 1}

    with patch("nina.catalog_hydrate.pull_product_catalog", return_value=(fake_rows, fake_meta)):
        out, meta = ensure_site_catalog(store, store.sites[site_id])

    assert out == fake_rows
    assert meta["hydrated"] is True
    assert store.sites[site_id]["productCatalog"] == fake_rows
