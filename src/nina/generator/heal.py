"""Generator heal loop — patch agent.json from broken-selector reports."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx

from nina.generator.stages.dom_extract import extract_dom_signals
from nina.generator.stages.sitemap import infer_page_type

_ACTION_CLICK_HINTS: dict[str, list[str]] = {
    "search": ["search", "submit", "find", "go"],
    "add_to_cart": ["add", "cart", "bag"],
    "checkout": ["checkout", "place order", "pay", "order"],
    "open_product": ["view", "product", "detail"],
}

_OP_DOM_POOL = {
    "fill": "searchInputs",
    "click": "buttons",
}


def normalize_reports(data: Any) -> list[dict[str, Any]]:
    """Accept a single report, a list of reports, or { reports: [...] }."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        if "failures" in data:
            return [data]
        nested = data.get("reports")
        if isinstance(nested, list):
            return [r for r in nested if isinstance(r, dict)]
    return []


def failure_records(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten reports into per-failure rows with page context."""
    rows: list[dict[str, Any]] = []
    for report in reports:
        page_url = report.get("pageUrl") or ""
        snapshot = report.get("snapshot") or {}
        for failure in report.get("failures") or []:
            if not isinstance(failure, dict):
                continue
            rows.append({
                **failure,
                "pageUrl": page_url,
                "pageId": report.get("pageId"),
                "snapshot": snapshot,
                "siteId": report.get("siteId"),
                "contractVersion": report.get("contractVersion"),
            })
    return rows


def prioritize_entries(
    entries: list[dict[str, Any]],
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Boost crawl priority for URLs mentioned in failure reports."""
    heal_urls = {r.get("pageUrl") for r in reports if r.get("pageUrl")}
    seen = {e["url"] for e in entries}
    merged = list(entries)
    for url in sorted(heal_urls):
        if url in seen:
            for entry in merged:
                if entry["url"] == url:
                    entry["priority"] = max(float(entry.get("priority", 0.5)), 1.0)
                    entry["healTarget"] = True
        else:
            merged.append({
                "url": url,
                "priority": 1.0,
                "changefreq": "weekly",
                "healTarget": True,
            })
            seen.add(url)
    merged.sort(key=lambda e: (-float(e.get("priority", 0.5)), e["url"]))
    return merged


def crawl_report_pages(
    reports: list[dict[str, Any]],
    *,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch HTML for each unique pageUrl in reports."""
    urls = sorted({r.get("pageUrl") for r in reports if r.get("pageUrl")})
    pages: list[dict[str, Any]] = []
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        for url in urls:
            try:
                resp = client.get(url)
                html = resp.text if resp.status_code == 200 else ""
                status = resp.status_code
            except httpx.HTTPError:
                html = ""
                status = 0
            pages.append({
                "url": url,
                "pageType": infer_page_type(url),
                "status": status,
                "html": html,
                "priority": 1.0,
                "healTarget": True,
            })
    return pages


def dom_signals_by_url(pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for page in pages:
        url = page.get("url")
        if not url:
            continue
        signals = page.get("domSignals") or extract_dom_signals(page.get("html", ""))
        out[url] = signals
        if page.get("pageType"):
            out.setdefault(f"pageType:{page['pageType']}", signals)
    return out


def _score_label(text: str, hints: list[str]) -> int:
    lower = text.lower()
    return sum(1 for hint in hints if hint in lower)


def _pick_from_pool(
    pool: list[dict[str, str]],
    hints: list[str],
    *,
    previous: str | None = None,
) -> str | None:
    if not pool:
        return None
    ranked = sorted(
        pool,
        key=lambda item: (
            -_score_label(
                " ".join(
                    filter(
                        None,
                        [item.get("label", ""), item.get("text", ""), item.get("name", "")],
                    )
                ),
                hints,
            ),
            item.get("selector") == previous,
        ),
    )
    for item in ranked:
        sel = item.get("selector")
        if sel and sel != previous:
            return sel
    return pool[0].get("selector")


def suggest_selector_replacement(
    failure: dict[str, Any],
    dom_signals: dict[str, Any],
) -> str | None:
    """Suggest a new selector for a failed step using fresh DOM + snapshot."""
    op = failure.get("op", "")
    action_id = failure.get("actionId") or ""
    previous = failure.get("selector")
    snapshot = failure.get("snapshot") or {}
    labels = list(snapshot.get("visibleLabels") or []) + list(
        snapshot.get("headings") or []
    )
    hints = list(_ACTION_CLICK_HINTS.get(action_id, []))
    for label in labels:
        hints.extend(label.lower().split())

    pool_key = _OP_DOM_POOL.get(op)
    if pool_key:
        candidate = _pick_from_pool(
            dom_signals.get(pool_key) or [],
            hints,
            previous=previous,
        )
        if candidate:
            return candidate

    if op == "click":
        for link in dom_signals.get("links") or []:
            href = (link.get("href") or "").lower()
            if any(h in href for h in hints):
                return link.get("selector")

    if previous and failure.get("reason") == "not_found":
        # Last resort: try data-testid derived from action id
        slug = action_id.replace("_", "-")
        if slug:
            return f'[data-testid="{slug}"]'
    return None


def _get_action(contract: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    for action in contract.get("actions") or []:
        if action.get("id") == action_id:
            return action
    return None


def _bump_patch_version(version: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version or "")
    if not match:
        return "1.0.1"
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    return f"{major}.{minor}.{patch + 1}"


def apply_heal_to_contract(
    contract: dict[str, Any],
    reports: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    *,
    bump_version: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    Patch selectors / inline step selectors in an existing contract.
    Returns (updated_contract, heal_log).
    """
    updated = deepcopy(contract)
    signals_map = dom_signals_by_url(pages)
    heal_log: list[dict[str, Any]] = []
    selectors = updated.setdefault("selectors", {})

    for failure in failure_records(reports):
        action_id = failure.get("actionId")
        step_index = int(failure.get("stepIndex") or 0)
        page_url = failure.get("pageUrl") or ""
        dom = signals_map.get(page_url) or signals_map.get(
            f"pageType:{failure.get('pageId')}", {}
        )
        replacement = suggest_selector_replacement(failure, dom)
        if not replacement:
            heal_log.append({
                "actionId": action_id,
                "stepIndex": step_index,
                "pageUrl": page_url,
                "status": "unresolved",
                "previous": failure.get("selector"),
                "reason": failure.get("reason"),
            })
            continue

        action = _get_action(updated, action_id or "")
        changed = False
        previous = failure.get("selector")

        if action:
            steps = ((action.get("execute") or {}).get("steps")) or []
            if 0 <= step_index < len(steps):
                step = steps[step_index]
                selector_id = step.get("selectorId")
                if selector_id:
                    previous = selectors.get(selector_id, previous)
                    if selectors.get(selector_id) != replacement:
                        selectors[selector_id] = replacement
                        changed = True
                elif step.get("selector") != replacement:
                    step["selector"] = replacement
                    changed = True

        if failure.get("selectorId"):
            sid = failure["selectorId"]
            previous = selectors.get(sid, previous)
            if selectors.get(sid) != replacement:
                selectors[sid] = replacement
                changed = True

        heal_log.append({
            "actionId": action_id,
            "stepIndex": step_index,
            "pageUrl": page_url,
            "status": "patched" if changed else "unchanged",
            "previous": previous,
            "replacement": replacement,
            "reason": failure.get("reason"),
        })

    if bump_version and any(entry.get("status") == "patched" for entry in heal_log):
        updated["version"] = _bump_patch_version(updated.get("version", "1.0.0"))

    return updated, heal_log


def load_existing_contract(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir / "agent.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_heal_log(output_dir: Path, heal_log: list[dict[str, Any]]) -> Path | None:
    if not heal_log:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "agent.heal.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump({"healed": heal_log}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def fetch_reports_from_api(api_base: str, *, site_id: str | None = None) -> list[dict[str, Any]]:
    """Pull reports from a running NINA demo/API (GET /v1/reports)."""
    base = api_base.rstrip("/")
    url = f"{base}/v1/reports"
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        body = resp.json()
    reports = body.get("data") if isinstance(body, dict) else body
    if not isinstance(reports, list):
        return []
    if site_id:
        reports = [r for r in reports if r.get("siteId") == site_id]
    return reports
