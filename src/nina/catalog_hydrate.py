"""Lazy product-catalog hydration when a site has no stored catalog rows."""

from __future__ import annotations

import logging
from typing import Any

from .catalog_probe import pull_product_catalog

_log = logging.getLogger(__name__)


def ensure_site_catalog(store: Any, site: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return catalog rows for *site*, pulling from the storefront on first miss.

    Persists non-empty pulls via ``store.attach_product_catalog`` so later
    requests (and Postgres/JSON store) keep the data until redeploy/migration.
    """
    site_id = site.get("id") or ""
    existing = list(site.get("productCatalog") or [])
    if existing:
        return existing, {"hydrated": False, "productCount": len(existing), "source": "stored"}

    base_url = (site.get("baseUrl") or "").strip()
    if not base_url:
        return [], {"hydrated": False, "productCount": 0, "source": "none", "reason": "no_base_url"}

    hint = (site.get("firestoreProject") or "").strip() or None
    rows, meta = pull_product_catalog(base_url, firestore_project=hint)
    meta = {**meta, "hydrated": bool(rows)}
    if rows and site_id:
        try:
            store.attach_product_catalog(site_id, rows)
            if meta.get("firestoreProject"):
                try:
                    store.update_site_fields(site_id, firestoreProject=meta["firestoreProject"])
                except Exception:
                    _log.exception("firestoreProject persist failed site=%s", site_id)
            _log.info(
                "catalog hydrated site=%s count=%d source=%s",
                site_id,
                len(rows),
                meta.get("source"),
            )
        except Exception:
            _log.exception("catalog attach failed site=%s", site_id)
    elif not rows:
        _log.warning(
            "catalog pull empty site=%s base=%s meta=%s",
            site_id,
            base_url,
            meta,
        )
    return rows, meta
