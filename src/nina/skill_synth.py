"""Synthesize per-site skills automatically from the agent contract.

NINA maintains skill *templates* in ``skills/*.md``. At runtime we map those
templates onto the site's actual action ids, enrich them with parameter hints,
and tune flows using contract signals (DOM crawl, locale, OpenAPI params).
Merchants do not author skills — the engine does.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .skill_loader import BUILTIN_SKILLS_DIR, load_skills, skills_by_action

# Template name (from skill frontmatter) → logical role.
_ROLE_BY_TEMPLATE: dict[str, str] = {
    "search-skill": "search",
    "cart-skill": "cart",
    "checkout-skill": "checkout",
    "product-detail-skill": "product_detail",
    "navigation-skill": "navigation",
    "view-cart-skill": "view_cart",
    "remove-from-cart-skill": "remove_from_cart",
    "order-tracking-skill": "order_tracking",
    "cod-skill": "cod",
    "support-skill": "support",
}

# Match contract action ids to a role (first match wins).
_ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "search": re.compile(
        r"(search|filter_product|browse|list_product|product_list)",
        re.I,
    ),
    "cart": re.compile(r"(add.*cart|cart_add|add_item)", re.I),
    "checkout": re.compile(r"(checkout|place_order|buy_now)", re.I),
    "product_detail": re.compile(
        r"(product_detail|open_product|get_product|product_info)",
        re.I,
    ),
    "navigation": re.compile(
        r"(^navigate$|open_page|open_category|list_categor|go_to)",
        re.I,
    ),
    "view_cart": re.compile(r"(view_cart|^cart$|get_cart|show_cart)", re.I),
    "remove_from_cart": re.compile(r"(remove.*cart|clear_cart|delete.*cart)", re.I),
    "order_tracking": re.compile(r"(track.*order|order_status|shipment)", re.I),
    "cod": re.compile(r"(cod|cash_on_delivery|cash.*delivery)", re.I),
    "support": re.compile(
        r"(show_message|contact_support|shipping|returns|size_guide|help)",
        re.I,
    ),
}

_APPAREL_HINTS = re.compile(
    r"\b(shirt|tee|t-shirt|hoodie|dress|pant|jean|jacket|kurta|saree|"
    r"apparel|clothing|footwear|shoe|size)\b",
    re.I,
)

_INDIA_MARKET = re.compile(r"(^IN$|india|en-IN|hi-IN|\bhi\b)", re.I)

_PINCODE_PARAM = re.compile(
    r"(pincode|pin_code|postal_?code|zip_?code|zipcode|postcode)",
    re.I,
)

_ORDER_ID_PARAM = re.compile(r"(order_?id|order_?number|order_?ref)", re.I)

_PRICE_PARAM = re.compile(r"(price|max_?price|min_?price|budget)", re.I)


def role_for_action(action_id: str) -> str | None:
    for role, pattern in _ROLE_PATTERNS.items():
        if pattern.search(action_id):
            return role
    return None


def _contract_has_cod_actions(contract_actions: dict[str, dict[str, Any]]) -> bool:
    return any(role_for_action(aid) == "cod" for aid in contract_actions)


def contract_skills_fingerprint(
    contract: dict[str, Any],
    *,
    catalog_size: int = 0,
) -> str:
    """Stable cache key for synthesized skills."""
    actions = sorted(
        str(a.get("id"))
        for a in (contract.get("actions") or [])
        if a.get("id")
    )
    confirm = sorted(
        str(x)
        for x in ((contract.get("risk") or {}).get("confirmActions") or [])
    )
    pages = sorted(
        str(p.get("id"))
        for p in (contract.get("pages") or [])
        if p.get("id")
    )
    signals = contract.get("signals") or {}
    pd = signals.get("productDetail") or {}
    size_labels = ",".join(pd.get("sizeLabels") or [])
    site = contract.get("site") or {}
    locales = ",".join(sorted(str(x) for x in (site.get("locales") or [])))
    markets = ",".join(sorted(str(x) for x in (site.get("markets") or [])))
    return (
        f"a:{','.join(actions)}|c:{','.join(confirm)}|p:{','.join(pages)}"
        f"|n:{catalog_size}|sz:{bool(pd.get('hasSizeOptions'))}|{size_labels}"
        f"|loc:{locales}|mkt:{markets}"
    )


def _is_india_market(contract: dict[str, Any]) -> bool:
    site = contract.get("site") or {}
    tokens: list[str] = []
    for key in ("locales", "markets", "defaultMarket", "country"):
        raw = site.get(key)
        if isinstance(raw, str):
            tokens.append(raw)
        elif isinstance(raw, list):
            tokens.extend(str(x) for x in raw)
    return any(_INDIA_MARKET.search(t) for t in tokens)


def _has_product_detail_page(contract: dict[str, Any]) -> bool:
    for page in contract.get("pages") or []:
        pid = str(page.get("id") or "").lower()
        if pid in ("product_detail", "pdp", "product"):
            return True
    return False


def _dom_signals_suggest_sizing(contract: dict[str, Any]) -> bool:
    signals = contract.get("signals") or {}
    pd = signals.get("productDetail") or {}
    if pd.get("hasSizeOptions"):
        return True
    return bool(pd.get("sizeLabels"))


def _dom_size_labels(contract: dict[str, Any]) -> list[str]:
    signals = contract.get("signals") or {}
    pd = signals.get("productDetail") or {}
    raw = pd.get("sizeLabels") or []
    return [str(x).strip().upper() for x in raw if str(x).strip()][:12]


def _selectors_suggest_sizing(contract: dict[str, Any]) -> bool:
    selectors = contract.get("selectors") or {}
    if not isinstance(selectors, dict):
        return False
    for key, val in selectors.items():
        blob = f"{key} {val}".lower()
        if any(token in blob for token in ("size", "variant", "sku")):
            return True
    return False


def _catalog_suggests_sizing(catalog: list[dict[str, Any]] | None) -> bool:
    if not catalog:
        return False
    sample = catalog[:40]
    hits = 0
    for row in sample:
        text = " ".join(
            str(row.get(k) or "")
            for k in ("name", "title", "category", "description")
        )
        if _APPAREL_HINTS.search(text):
            hits += 1
    return hits >= max(1, len(sample) // 4)


def _action_parameters(action: dict[str, Any]) -> dict[str, Any]:
    raw = action.get("parameters") or action.get("inputSchema", {}).get("properties")
    if isinstance(raw, dict):
        return raw
    return {}


def _all_param_names(
    actions: dict[str, dict[str, Any]],
    action_ids: list[str],
) -> list[str]:
    names: list[str] = []
    for aid in action_ids:
        names.extend(_action_parameters(actions.get(aid) or {}).keys())
    return names


def _action_needs_size_param(action: dict[str, Any]) -> bool:
    params = _action_parameters(action)
    return any(
        k.lower() in ("size", "variantid", "variant", "sku")
        for k in params
    )


def _cart_needs_size_flow(
    actions: dict[str, dict[str, Any]],
    action_ids: list[str],
    contract: dict[str, Any],
    catalog: list[dict[str, Any]] | None,
) -> bool:
    if _dom_signals_suggest_sizing(contract):
        return True
    if _selectors_suggest_sizing(contract):
        return True
    if _has_product_detail_page(contract):
        return True
    if _catalog_suggests_sizing(catalog):
        return True
    for aid in action_ids:
        action = actions.get(aid) or {}
        if _action_needs_size_param(action):
            return True
        execute = action.get("execute") or {}
        if execute.get("type") == "dom":
            return True
    return False


def _apply_dom_size_chips(skill: dict[str, Any], contract: dict[str, Any]) -> None:
    labels = _dom_size_labels(contract)
    if not labels:
        return
    flow = skill.get("clarificationFlow")
    if not isinstance(flow, dict):
        return
    for step in flow.get("steps") or []:
        if not isinstance(step, dict) or step.get("field") != "size":
            continue
        step["chipsDefault"] = labels


def _param_hints_section(actions: dict[str, dict[str, Any]], action_ids: list[str]) -> str:
    lines = ["## This site's parameters"]
    seen: set[str] = set()
    for aid in action_ids:
        action = actions.get(aid) or {}
        params = _action_parameters(action)
        if not params:
            continue
        lines.append(f"Action `{aid}`:")
        for name, spec in params.items():
            if not isinstance(spec, dict):
                continue
            key = f"{aid}:{name}"
            if key in seen:
                continue
            seen.add(key)
            req = "required" if spec.get("required") else "optional"
            desc = str(spec.get("description") or "").strip()
            lines.append(f"- **{name}** ({req}){': ' + desc if desc else ''}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _pincode_param_name(param_names: list[str]) -> str | None:
    for name in param_names:
        if _PINCODE_PARAM.search(name):
            return name
    return None


def _order_id_param_name(param_names: list[str]) -> str | None:
    for name in param_names:
        if _ORDER_ID_PARAM.search(name):
            return name
    return None


def _enrich_cod_skill(
    skill: dict[str, Any],
    actions: dict[str, dict[str, Any]],
    action_ids: list[str],
    contract: dict[str, Any],
) -> None:
    param_names = _all_param_names(actions, action_ids)
    pincode = _pincode_param_name(param_names)
    parts: list[str] = []
    if pincode:
        parts.append(
            f"When the user asks about COD, collect their **{pincode}** "
            "before calling the action if they have not provided it yet."
        )
    if _is_india_market(contract):
        parts.append(
            "Mirror Hindi/Hinglish when the user does (e.g. \"COD milega?\", "
            "\"cash on delivery hai?\")."
        )
    if parts:
        existing = str(skill.get("clarifyGuidance") or "").strip()
        merged = "\n".join(parts)
        skill["clarifyGuidance"] = f"{existing}\n{merged}".strip() if existing else merged


def _enrich_order_tracking_skill(
    skill: dict[str, Any],
    actions: dict[str, dict[str, Any]],
    action_ids: list[str],
) -> None:
    order_param = _order_id_param_name(_all_param_names(actions, action_ids))
    if not order_param:
        return
    skill["clarifyGuidance"] = (
        f"If the user has not given an order reference, ask for **{order_param}** "
        "before calling the tracking action."
    )


def _enrich_search_skill(
    skill: dict[str, Any],
    actions: dict[str, dict[str, Any]],
    action_ids: list[str],
) -> None:
    hints: list[str] = []
    for aid in action_ids:
        for name in _action_parameters(actions.get(aid) or {}):
            if _PRICE_PARAM.search(name):
                hints.append(
                    f"Use the `{name}` parameter when the user states a budget "
                    "or price limit; do not invent a field the schema lacks."
                )
    if hints:
        skill["body"] = (skill.get("body") or "").rstrip() + "\n\n" + "\n".join(hints)


def _enrich_support_for_india(
    skill: dict[str, Any],
    contract: dict[str, Any],
    contract_actions: dict[str, dict[str, Any]],
) -> None:
    if not _is_india_market(contract):
        return
    if _contract_has_cod_actions(contract_actions):
        return
    skill["body"] = (
        (skill.get("body") or "").rstrip()
        + "\n\n## India market\n"
        + "This store has no dedicated COD lookup action. For cash-on-delivery "
        + "questions, explain that payment options appear at checkout and offer "
        + "to navigate there if the user wants to proceed."
    )


def _match_actions_to_template(
    template: dict[str, Any],
    contract_actions: dict[str, dict[str, Any]],
) -> list[str]:
    role = _ROLE_BY_TEMPLATE.get(str(template.get("name") or ""))
    if not role:
        return []
    pattern = _ROLE_PATTERNS.get(role)
    canonical = set(template.get("appliesTo") or [])
    matched: list[str] = []
    for action_id in contract_actions:
        if action_id in canonical:
            matched.append(action_id)
            continue
        if pattern and pattern.search(action_id):
            matched.append(action_id)
    return sorted(set(matched))


def synthesize_skills(
    contract: dict[str, Any] | None,
    *,
    catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the skill set for one site's contract from NINA templates."""
    templates = load_skills(BUILTIN_SKILLS_DIR)
    if not contract:
        return templates

    contract_actions = {
        str(a["id"]): a
        for a in (contract.get("actions") or [])
        if a.get("id")
    }
    if not contract_actions:
        return templates

    confirm_actions = set(
        str(x)
        for x in ((contract.get("risk") or {}).get("confirmActions") or [])
    )
    synthesized: list[dict[str, Any]] = []

    for template in templates:
        role = _ROLE_BY_TEMPLATE.get(str(template.get("name") or ""))
        action_ids = _match_actions_to_template(template, contract_actions)
        if not action_ids:
            continue

        # COD template only attaches when the contract exposes a COD action.
        if role == "cod" and not action_ids:
            continue

        skill = copy.deepcopy(template)
        skill["appliesTo"] = action_ids
        skill["synthesized"] = True

        hints = _param_hints_section(contract_actions, action_ids)
        if hints:
            skill["body"] = (skill.get("body") or "").rstrip() + "\n\n" + hints

        if role == "cart":
            flow = skill.get("clarificationFlow")
            if isinstance(flow, dict):
                needs = _cart_needs_size_flow(
                    contract_actions, action_ids, contract, catalog,
                )
                flow["enabled"] = needs
                if needs:
                    _apply_dom_size_chips(skill, contract)

        if role == "checkout" and set(action_ids) & confirm_actions:
            skill["body"] = (
                (skill.get("body") or "").rstrip()
                + "\n\n## Site policy\n"
                + "This site's contract marks checkout as **confirm-required**. "
                + "Always resolve to `confirm` before the first execute."
            )

        if role == "cod":
            _enrich_cod_skill(skill, contract_actions, action_ids, contract)

        if role == "order_tracking":
            _enrich_order_tracking_skill(skill, contract_actions, action_ids)

        if role == "search":
            _enrich_search_skill(skill, contract_actions, action_ids)

        if role == "support":
            _enrich_support_for_india(skill, contract, contract_actions)

        synthesized.append(skill)

    return synthesized or templates


def apply_skills_for_contract(
    core: Any,
    contract: dict[str, Any] | None,
    *,
    catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Synthesize and attach skills for a site contract onto Nina _Core."""
    from .fast_path import compile_fast_path_patterns

    skills = synthesize_skills(contract, catalog=catalog)
    cache_key = contract_skills_fingerprint(
        contract or {},
        catalog_size=len(catalog or []),
    )
    core.skills = skills
    core.skills_by_action = skills_by_action(skills)
    core.fast_path_patterns = compile_fast_path_patterns(skills)
    core._skills_cache_key = cache_key
    return skills
