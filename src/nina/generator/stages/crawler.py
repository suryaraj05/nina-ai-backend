"""Fetch pages for DOM analysis (respects maxPages budget)."""

from __future__ import annotations

import time
from typing import Any

import httpx


def crawl_urls(
    entries: list[dict[str, Any]],
    *,
    max_pages: int = 50,
    delay_ms: int = 500,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """
    Fetch HTML for sitemap URLs. Returns [{ url, pageType, status, html }].
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        for entry in entries:
            if len(results) >= max_pages:
                break
            url = entry["url"]
            if url in seen:
                continue
            seen.add(url)
            try:
                resp = client.get(url)
                html = resp.text if resp.status_code == 200 else ""
            except httpx.HTTPError:
                html = ""
                resp = None
            from nina.generator.stages.sitemap import infer_page_type

            results.append({
                "url": url,
                "pageType": infer_page_type(url),
                "status": resp.status_code if resp else 0,
                "html": html,
                "priority": entry.get("priority", 0.5),
            })
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)

    return results
