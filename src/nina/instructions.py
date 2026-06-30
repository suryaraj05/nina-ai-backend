"""Generic contract-bound instruction builder (no site-specific handlers)."""

from __future__ import annotations

from typing import Any

from .catalog_rail import is_grounded_result, storefront_browse_url
from .contract import (
    expand_dom_steps,
    expand_execute_steps,
    get_action,
    get_execute_runtime,
    resolve_intent,
)

_SEARCH_ACTIONS = frozenset({"search", "search_products", "list_products", "browse_products"})


def _grounded_search_instructions(
    contract: dict[str, Any],
    action_id: str,
    turn: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Catalog-backed search: show cards in chat; browse store with keyword URL only."""
    result = turn.get("actionResult")
    if action_id not in _SEARCH_ACTIONS or not is_grounded_result(result or {}):
        return None
    count = int((result or {}).get("count") or 0)
    if count <= 0:
        return []
    params = turn.get("actionInput") or turn.get("params") or {}
    query = str(params.get("query") or "")
    base_url = (contract.get("site") or {}).get("baseUrl") or ""
    url = storefront_browse_url(query, base_url)
    if url:
        return [{"type": "navigate", "url": url}]
    return []


def turn_to_instructions(
    contract: dict[str, Any],
    turn: dict[str, Any],
    *,
    page_context: dict[str, Any] | None = None,
    session_hints: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> list[dict[str, Any]]:
    """Build client instructions from a turn using agent.json only."""
    if not contract or not turn:
        return []

    if turn.get("intent") == "blocked":
        return list(turn.get("instructions") or [])

    if turn.get("intent") == "auth_replay":
        return list(turn.get("instructions") or [])

    action_id = turn.get("actionCalled")
    if not action_id:
        return []

    action = get_action(contract, action_id)
    if not action:
        return []

    grounded = _grounded_search_instructions(contract, action_id, turn)
    if grounded is not None:
        return grounded

    page_id = (page_context or {}).get("pageId")
    params = turn.get("actionInput") or turn.get("params") or {}

    resolved = resolve_intent(
        contract,
        intent=action_id,
        params=params,
        confidence=turn.get("confidence", 1.0),
        page_id=page_id,
        session_hints=session_hints,
        confirmed=confirmed or turn.get("confirmed", False),
    )

    instructions = list(resolved.get("instructions") or [])
    first_type = instructions[0].get("type") if instructions else None

    if first_type in ("no_match", "confirm", "needs_login", "show_message"):
        return instructions

    execute = action.get("execute") or {}
    etype = execute.get("type", "dom")
    runtime = get_execute_runtime(execute)
    action_result = turn.get("actionResult")

    if etype == "api" and runtime == "server":
        ui_steps = expand_dom_steps(contract, action, params)
        if ui_steps:
            return ui_steps
        if isinstance(action_result, dict) and action_result.get("results"):
            return [{
                "type": "toast",
                "message": f"Found {len(action_result['results'])} result(s).",
                "level": "success",
            }]
        items = None
        if isinstance(action_result, dict):
            items = action_result.get("results") or action_result.get("data")
        if isinstance(items, list) and items:
            lines = []
            for i, item in enumerate(items[:8], 1):
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("name") or item.get("id")
                if not title:
                    continue
                price = item.get("price")
                suffix = f" — ₹{price}" if price is not None else ""
                lines.append(f"{i}. {title}{suffix}")
            if lines:
                return [{
                    "type": "show_message",
                    "message": "Here's what I found:\n" + "\n".join(lines),
                }]
        return []

    if etype == "api" and runtime == "browser":
        api_steps = expand_execute_steps(contract, action, params)
        return api_steps or instructions

    if etype == "hybrid":
        out = expand_execute_steps(contract, action, params)
        if out:
            return out

    dom_steps = expand_execute_steps(contract, action, params)
    if dom_steps:
        return dom_steps
    return instructions
