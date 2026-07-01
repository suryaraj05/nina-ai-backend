"""Compose natural-language replies from action results (spec §3 step 9)."""
from __future__ import annotations

import json

from .catalog_rail import grounded_reply, is_grounded_result
from .errors import LLMError
from .prompt import CHITCHAT_TEMPLATE, COMPOSE_TEMPLATE, render


def _deterministic_reply(action_name, result, action_error=None) -> str:
    if action_error:
        return (
            f"I ran into a problem with {action_name.replace('_', ' ')}: "
            f"{action_error['message']}"
        )
    if result is None:
        return "Done."
    if isinstance(result, dict):
        if "count" in result and "results" in result:
            n = result.get("count", len(result.get("results") or []))
            if result.get("grounded"):
                return grounded_reply(action_name, result)
            return f"I found {n} result{'s' if n != 1 else ''}."
        if "cart" in result:
            total = (result.get("cart") or {}).get("total")
            if total is not None:
                return f"Your cart is updated. Total: {total}."
        if "orderId" in result or "id" in result:
            oid = result.get("orderId") or result.get("id")
            return f"Order placed successfully. Reference: {oid}."
        if result.get("reset"):
            return "Conversation reset. How can I help?"
        if _is_navigation_only_result(result):
            return _navigation_reply(action_name, result)
    return "Done — let me know if you need anything else."


def _is_navigation_only_result(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    if isinstance(result.get("results"), list) or isinstance(result.get("data"), list):
        return False
    if result.get("cart") or result.get("orderId"):
        return False
    return bool(result.get("ok")) or any(
        key in result for key in ("query", "categorySlug", "url", "productId")
    )


def _navigation_reply(action_name: str, result: dict) -> str:
    if action_name in ("search", "search_products"):
        query = str(result.get("query") or "").strip()
        if query:
            return f"Opening search results for {query}."
        return "Opening search results for you."
    if action_name == "open_category":
        slug = str(result.get("categorySlug") or "").strip()
        if slug:
            return f"Taking you to {slug.replace('-', ' ')}."
        return "Taking you to that category."
    if action_name in ("navigate", "open_page"):
        return "Opening that page for you."
    return "On it — opening that for you."


async def compose_response(
    llm,
    identity,
    behavior,
    user_message,
    action_name,
    result,
    action_error=None,
) -> tuple[str, dict]:
    """Returns (natural_language_response, usage_dict)."""
    if not action_error and is_grounded_result(result or {}):
        return grounded_reply(action_name or "", result or {}), {}

    if not action_error and _is_navigation_only_result(result or {}):
        return _navigation_reply(action_name or "", result or {}), {}

    status = "error" if action_error else "success"
    payload = {
        "agent_name": identity["agentName"],
        "action_name": action_name or "none",
        "user_message": user_message,
        "result_status": status,
        "action_result_json": json.dumps(
            result if not action_error else action_error,
            ensure_ascii=False,
            default=str,
        ),
        "language": behavior.get("language", "auto"),
    }
    prompt = render(COMPOSE_TEMPLATE, payload)
    try:
        text, usage = await llm.compose(prompt)
        return text.strip(), usage
    except LLMError:
        return _deterministic_reply(action_name, result, action_error), {}


async def compose_chitchat(
    llm,
    identity,
    behavior,
    capabilities,
    user_message,
    fallback,
) -> tuple[str, dict]:
    """Generate a conversational reply via a clean completion.

    Used for chitchat / unsupported turns. Relying on the structured
    resolution's user_reply field is unreliable with weaker models (they
    often echo the user's message), so we ask for a plain reply instead.
    Returns (reply_text, usage_dict).
    """
    payload = {
        "agent_name": identity.get("agentName", "Assistant"),
        "persona": identity.get("persona") or identity.get("description") or "",
        "capabilities": capabilities or "General questions about this site.",
        "user_message": user_message,
        "language": behavior.get("language", "auto"),
    }
    prompt = render(CHITCHAT_TEMPLATE, payload)
    try:
        text, usage = await llm.compose(prompt)
        text = (text or "").strip()
        # Guard against an echo or empty result slipping through.
        if not text or text.lower() == (user_message or "").strip().lower():
            return fallback, usage
        return text, usage
    except LLMError:
        return fallback, {}
