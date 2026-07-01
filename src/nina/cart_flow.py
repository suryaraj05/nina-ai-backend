"""Guided add-to-cart: size → quantity → DOM, via tap chips (no long forms)."""

from __future__ import annotations

import re
from typing import Any

from .instructions import _open_product_instructions

_CART_ACTIONS = frozenset({"add_to_cart", "add_item_to_cart"})
_DEFAULT_SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
_QTY_CHIPS = ["1", "2", "3"]
_SIZE_RE = re.compile(
    r"^(?:size\s+)?(XXS|XS|S|M|L|XL|XXL|2XL|3XL)$",
    re.IGNORECASE,
)


def available_sizes(session_hints: dict[str, Any] | None) -> list[str]:
    opts = (session_hints or {}).get("productOptions") or {}
    raw = opts.get("sizes") or []
    if isinstance(raw, list) and raw:
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            label = str(item).strip().upper()
            if label and label not in seen:
                seen.add(label)
                out.append(label)
        if out:
            return out[:8]
    return list(_DEFAULT_SIZES)


def parse_size_choice(message: str, sizes: list[str]) -> str | None:
    text = (message or "").strip()
    if not text:
        return None
    match = _SIZE_RE.match(text)
    if match:
        return match.group(1).upper()
    upper = text.upper()
    allowed = {s.upper() for s in sizes}
    if upper in allowed:
        return upper
    return None


def parse_quantity_choice(message: str) -> int | None:
    text = (message or "").strip()
    if text in ("1", "2", "3", "4", "5"):
        return int(text)
    match = re.search(r"\b([1-5])\b", text)
    if match:
        return int(match.group(1))
    return None


def _product_label(action_input: dict[str, Any]) -> str:
    for key in ("name", "title", "query"):
        val = action_input.get(key)
        if val:
            return str(val).strip()
    return "this item"


def _navigate_instructions(
    contract: dict[str, Any],
    product_id: str,
    action_input: dict[str, Any],
) -> list[dict[str, Any]]:
    steps = _open_product_instructions(contract, product_id, action_input)
    return list(steps or [])


def _size_turn(pending: dict[str, Any], *, on_pdp: bool, retry: bool = False) -> dict[str, Any]:
    collected = pending.get("collectedInput") or {}
    name = collected.get("productName") or "this item"
    chips = list(pending.get("sizes") or _DEFAULT_SIZES)
    if on_pdp:
        reply = "Pick a size:" if not retry else "Tap your size:"
    else:
        reply = (
            f"Opening {name}. Pick a size:"
            if not retry
            else f"Tap a size for {name}:"
        )
    return {
        "intent": "cart_guidance",
        "reply": reply,
        "chips": chips,
        "instructions": [] if on_pdp else list(pending.get("navigateInstructions") or []),
    }


def _quantity_turn(pending: dict[str, Any]) -> dict[str, Any]:
    size = (pending.get("collectedInput") or {}).get("size") or ""
    reply = f"Size {size} — how many?"
    return {
        "intent": "cart_guidance",
        "reply": reply,
        "chips": list(_QTY_CHIPS),
        "instructions": [],
    }


def _complete_turn(collected: dict[str, Any]) -> dict[str, Any]:
    size = str(collected.get("size") or "M")
    quantity = int(collected.get("quantity") or 1)
    name = collected.get("productName") or "item"
    return {
        "intent": "action",
        "reply": f"Added {name} ({size} × {quantity}) to your cart.",
        "chips": ["What's in my cart?", "Continue shopping"],
        "instructions": [{
            "type": "cart_add",
            "size": size,
            "quantity": quantity,
            "productId": collected.get("productId"),
        }],
        "actionCalled": "add_to_cart",
        "actionInput": dict(collected),
        "actionResult": {"ok": True, "grounded": True, **collected},
    }


def begin_cart_add(
    state: dict[str, Any],
    action_input: dict[str, Any],
    *,
    session_hints: dict[str, Any] | None,
    contract: dict[str, Any],
    on_pdp: bool,
) -> dict[str, Any] | None:
    """Start a chip-guided cart flow instead of dumping the user on the PDP."""
    if action_input.get("size") and action_input.get("quantity"):
        return None
    product_id = str(
        action_input.get("productId")
        or action_input.get("sku")
        or action_input.get("variantId")
        or ""
    ).strip()
    if not product_id:
        return None

    collected = {
        "productId": product_id,
        "productName": _product_label(action_input),
    }
    if action_input.get("size"):
        collected["size"] = str(action_input["size"]).upper()

    navigate = [] if on_pdp else _navigate_instructions(contract, product_id, action_input)
    pending: dict[str, Any] = {
        "type": "cart_add",
        "action": "add_to_cart",
        "step": "quantity" if collected.get("size") else "size",
        "collectedInput": collected,
        "sizes": available_sizes(session_hints),
        "navigateInstructions": navigate,
        "attemptsUsed": 0,
    }
    state["pending"] = pending
    if pending["step"] == "quantity":
        return _quantity_turn(pending)
    return _size_turn(pending, on_pdp=on_pdp)


def continue_cart_add(state: dict[str, Any], message: str) -> dict[str, Any] | None:
    """Advance an in-progress cart_add pending flow."""
    pending = state.get("pending")
    if not isinstance(pending, dict) or pending.get("type") != "cart_add":
        return None

    step = pending.get("step")
    collected = dict(pending.get("collectedInput") or {})

    if step == "size":
        size = parse_size_choice(message, list(pending.get("sizes") or _DEFAULT_SIZES))
        if not size:
            pending["attemptsUsed"] = int(pending.get("attemptsUsed") or 0) + 1
            if pending["attemptsUsed"] > 2:
                state["pending"] = None
                return {
                    "intent": "unsupported",
                    "reply": "I didn't catch the size — try again from the product page.",
                    "chips": ["Browse all products"],
                    "instructions": [],
                }
            return _size_turn(pending, on_pdp=not pending.get("navigateInstructions"), retry=True)
        collected["size"] = size
        pending["collectedInput"] = collected
        pending["step"] = "quantity"
        pending["attemptsUsed"] = 0
        state["pending"] = pending
        return _quantity_turn(pending)

    if step == "quantity":
        qty = parse_quantity_choice(message)
        if not qty:
            pending["attemptsUsed"] = int(pending.get("attemptsUsed") or 0) + 1
            if pending["attemptsUsed"] > 2:
                state["pending"] = None
                return {
                    "intent": "unsupported",
                    "reply": "Tap 1, 2, or 3 for quantity.",
                    "chips": ["1", "2", "3"],
                    "instructions": [],
                }
            return _quantity_turn(pending)
        collected["quantity"] = qty
        state["pending"] = None
        return _complete_turn(collected)

    state["pending"] = None
    return None
