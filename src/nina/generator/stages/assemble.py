"""Assemble draft agent.json from pipeline artifacts."""

from __future__ import annotations

from typing import Any

from nina.generator.stages.action_infer import _PAGE_ACTIONS, url_pattern_for_type


def assemble_contract(
    site: dict[str, Any],
    crawled: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    selectors: dict[str, str],
    auth_policy: dict[str, Any] | None = None,
    risk_policy: dict[str, Any] | None = None,
    version: str = "1.0.0",
) -> dict[str, Any]:
    """Merge site config, pages, actions into agent.json."""
    auth_policy = auth_policy or {}
    risk_policy = risk_policy or {}

    by_type: dict[str, list[str]] = {}
    for page in crawled:
        ptype = page.get("pageType", "generic")
        by_type.setdefault(ptype, []).append(page["url"])

    pages: list[dict[str, Any]] = []
    action_id_set = {a["id"] for a in actions}
    for ptype, urls in sorted(by_type.items()):
        pattern = url_pattern_for_type(ptype, urls)
        action_ids = [a for a in _PAGE_ACTIONS.get(ptype, ["navigate"]) if a in action_id_set]
        if not action_ids:
            action_ids = [a["id"] for a in actions if ptype in (a.get("availableOn") or [])]
        pages.append({
            "id": ptype,
            "urlPattern": pattern,
            "label": ptype.replace("_", " ").title(),
            "actions": action_ids or ["navigate"],
        })

    if not pages:
        pages = [{"id": "home", "urlPattern": "/", "label": "Home", "actions": ["navigate", "search"]}]

    clean_actions = []
    for a in actions:
        ac = {k: v for k, v in a.items() if not k.startswith("_")}
        clean_actions.append(ac)

    contract: dict[str, Any] = {
        "site": {
            "id": site["id"],
            "name": site["name"],
            "baseUrl": site["baseUrl"],
            "locales": site.get("locales", ["en"]),
        },
        "version": version,
        "description": f"Generated contract for {site['name']}",
        "pages": pages,
        "actions": clean_actions,
        "selectors": selectors,
        "embed": {
            "panel": "right",
            "apiBase": "/v1/query",
        },
    }

    if auth_policy:
        auth_section: dict[str, Any] = {
            "loginUrl": auth_policy.get("loginUrl", "/login"),
            "gatedActions": auth_policy.get("gatedActions", []),
        }
        # The schema requires sessionIndicator to be an object when present
        # -- omit the key entirely rather than writing null when a policy
        # doesn't specify one (e.g. a hand-written nina.site.yaml that only
        # sets loginUrl/gatedActions), instead of failing validation.
        if auth_policy.get("sessionIndicator"):
            auth_section["sessionIndicator"] = auth_policy["sessionIndicator"]
        contract["auth"] = auth_section

    # Every action the inference engine marked risk:"high" must be enforced
    # (confirmation required) regardless of whether a human remembered to
    # also list its id in risk.policy.yaml -- the per-action risk field is
    # otherwise purely descriptive and silently unenforced at runtime.
    high_risk_ids = [a["id"] for a in actions if a.get("risk") == "high"]
    confirm_actions = sorted(set(high_risk_ids) | set(risk_policy.get("confirmActions") or []))
    block_actions = list(dict.fromkeys(risk_policy.get("blockActions") or []))
    if confirm_actions or block_actions:
        contract["risk"] = {
            "confirmActions": confirm_actions,
            "blockActions": block_actions,
        }

    return contract
