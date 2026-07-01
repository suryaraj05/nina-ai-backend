"""Guided add-to-cart: size → quantity → DOM, via tap chips (no long forms)."""

from __future__ import annotations

import re
from typing import Any

from .instructions import _open_product_instructions
from .skill_runtime import (
    clarification_flow_for_action,
    format_skill_template,
    hint_path_get,
)

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


def _step_def(flow: dict[str, Any], field: str) -> dict[str, Any]:
    for step in flow.get("steps") or []:
        if isinstance(step, dict) and step.get("field") == field:
            return step
    return {}


def _size_turn(
    pending: dict[str, Any],
    *,
    on_pdp: bool,
    retry: bool = False,
) -> dict[str, Any]:
    flow = pending.get("flowSpec") or {}
    step = _step_def(flow, "size")
    collected = pending.get("collectedInput") or {}
    name = collected.get("productName") or "this item"
    values = {"productName": name, "size": collected.get("size") or ""}
    if on_pdp:
        template = (
            step.get("promptRetry") if retry else step.get("promptOnPdp")
        ) or step.get("prompt") or ("Tap your size:" if retry else "Pick a size:")
    else:
        template = (
            step.get("promptRetry") if retry else step.get("promptNavigate")
        ) or step.get("promptNavigate") or (
            f"Tap a size for {name}:" if retry else f"Opening {name}. Pick a size:"
        )
    reply = format_skill_template(str(template), values)
    chips = list(pending.get("sizes") or step.get("chipsDefault") or _DEFAULT_SIZES)
    return {
        "intent": "cart_guidance",
        "reply": reply,
        "chips": chips,
        "instructions": [] if on_pdp else list(pending.get("navigateInstructions") or []),
    }


def _quantity_turn(pending: dict[str, Any]) -> dict[str, Any]:
    flow = pending.get("flowSpec") or {}
    step = _step_def(flow, "quantity")
    collected = pending.get("collectedInput") or {}
    size = collected.get("size") or ""
    values = {"productName": collected.get("productName") or "item", "size": size}
    template = step.get("prompt") or "Size {size} — how many?"
    reply = format_skill_template(str(template), values)
    raw_chips = step.get("chips") or _QTY_CHIPS
    chips = [str(c) for c in raw_chips]
    return {
        "intent": "cart_guidance",
        "reply": reply,
        "chips": chips,
        "instructions": [],
    }


def _complete_turn(collected: dict[str, Any], flow: dict[str, Any]) -> dict[str, Any]:
    complete = flow.get("complete") or {}
    size = str(collected.get("size") or "M")
    quantity = int(collected.get("quantity") or 1)
    name = collected.get("productName") or "item"
    values = {"productName": name, "size": size, "quantity": quantity}
    template = complete.get("reply") or "Added {productName} ({size} × {quantity}) to your cart."
    reply = format_skill_template(str(template), values)
    raw_chips = complete.get("chips") or ["What's in my cart?", "Continue shopping"]
    chips = [str(c) for c in raw_chips]
    return {
        "intent": "action",
        "reply": reply,
        "chips": chips,
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


def _sizes_for_step(
    step: dict[str, Any],
    session_hints: dict[str, Any] | None,
) -> list[str]:
    chips_from = step.get("chipsFrom")
    if isinstance(chips_from, str):
        raw = hint_path_get(session_hints, chips_from)
        if isinstance(raw, list) and raw:
            return available_sizes({"productOptions": {"sizes": raw}})
    return available_sizes(session_hints)


def begin_cart_add(
    state: dict[str, Any],
    action_input: dict[str, Any],
    *,
    session_hints: dict[str, Any] | None,
    contract: dict[str, Any],
    on_pdp: bool,
    skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Start a chip-guided cart flow instead of dumping the user on the PDP."""
    flow = clarification_flow_for_action(skills or [], "add_to_cart")
    if not flow or not flow.get("enabled"):
        return None
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

    size_step = _step_def(flow, "size")
    navigate = [] if on_pdp else _navigate_instructions(contract, product_id, action_input)
    pending: dict[str, Any] = {
        "type": "cart_add",
        "action": "add_to_cart",
        "step": "quantity" if collected.get("size") else "size",
        "collectedInput": collected,
        "sizes": _sizes_for_step(size_step, session_hints),
        "navigateInstructions": navigate,
        "attemptsUsed": 0,
        "flowSpec": flow,
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

    flow = pending.get("flowSpec") or clarification_flow_for_action([], "add_to_cart") or {}
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
            return _size_turn(
                pending,
                on_pdp=not pending.get("navigateInstructions"),
                retry=True,
            )
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
                qty_chips = _step_def(flow, "quantity").get("chips") or _QTY_CHIPS
                return {
                    "intent": "unsupported",
                    "reply": "Tap 1, 2, or 3 for quantity.",
                    "chips": [str(c) for c in qty_chips],
                    "instructions": [],
                }
            return _quantity_turn(pending)
        collected["quantity"] = qty
        state["pending"] = None
        return _complete_turn(collected, flow)

    state["pending"] = None
    return None
