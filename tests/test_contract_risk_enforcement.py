"""resolve_intent's contract-level risk.confirmActions / risk.blockActions
must be a hard gate enforced by chat.py, not just something the LLM is asked
to respect. Before this fix, resolve_intent computed "confirm"/"blocked"
instructions but chat.py only ever inspected "needs_login", so a contract's
declared risk policy was silently dropped — the action would execute on the
first ask, on confidence and the LLM's own (fallible) judgement alone.
"""
from __future__ import annotations

import asyncio

from nina import Nina

CONTRACT = {
    "site": {"id": "shop", "name": "Shop", "baseUrl": "http://x"},
    "apis": {"default": {"baseUrl": "http://x"}},
    "actions": [
        {"id": "checkout", "description": "Place an order.", "parameters": {},
         "execute": {"type": "message", "steps": []}},
        {"id": "export_all_data", "description": "Export everything.", "parameters": {},
         "execute": {"type": "message", "steps": []}},
    ],
    "risk": {"confirmActions": ["checkout"], "blockActions": ["export_all_data"]},
}


def run(coro):
    return asyncio.run(coro)


def _adapter_resolving_to(action_name, confidence=1.0):
    def adapter(payload):
        if payload.get("mode") == "compose":
            return "Done."
        return {"resolution": "action", "action": action_name, "input": {}, "confidence": confidence}
    return adapter


async def _make_nina(adapter):
    nina = Nina()
    await nina.init({"llm": {"provider": "custom", "adapter": adapter}})
    await nina.register({
        "name": "checkout",
        "description": "Place an order and complete checkout for the user.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda inp, ctx: {"orderId": "order-1"},
    })
    await nina.register({
        "name": "export_all_data",
        "description": "Export all account data to a file for the user.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda inp, ctx: {"ok": True},
    })
    nina._core.config = {"_agentContract": CONTRACT}
    return nina


def test_confirm_action_is_not_executed_on_first_ask_even_at_full_confidence():
    nina = run(_make_nina(_adapter_resolving_to("checkout")))
    envelope = run(nina.chat("checkout please", "s1"))
    data = envelope["data"]
    assert data["intent"] == "confirmation"
    assert data["actionResult"] is None
    assert data["actionCalled"] is None


def test_confirm_action_executes_after_user_says_yes_no_infinite_loop():
    nina = run(_make_nina(_adapter_resolving_to("checkout")))
    run(nina.chat("checkout please", "s1"))
    envelope = run(nina.chat("yes", "s1"))
    data = envelope["data"]
    assert data["actionCalled"] == "checkout"
    assert data["actionResult"] == {"orderId": "order-1"}
    assert data["intent"] != "confirmation"


def test_confirm_action_executes_when_widget_sends_confirmed_with_empty_message():
    """Widget Confirm button sends confirmed=true; must not hit NINA_MESSAGE_INVALID."""
    nina = run(_make_nina(_adapter_resolving_to("checkout")))
    run(nina.chat("checkout please", "s1"))
    envelope = run(nina.chat("", "s1", confirmed=True))
    assert envelope.get("ok") is True, envelope
    data = envelope["data"]
    assert data["actionCalled"] == "checkout"
    assert data["actionResult"] == {"orderId": "order-1"}


def test_block_action_is_refused_outright():
    nina = run(_make_nina(_adapter_resolving_to("export_all_data")))
    envelope = run(nina.chat("export all my data", "s1"))
    data = envelope["data"]
    assert data["actionCalled"] is None
    assert data["actionResult"] is None
    assert data["intent"] == "blocked"


def test_confirm_gate_still_applies_below_resolve_intents_hardcoded_default_threshold():
    """resolve_intent has its own hardcoded confidence_threshold default
    (0.75), separate from a merchant's configured behavior.confidenceThreshold.
    A confidence of 0.6 with a merchant threshold of 0.5 already passed
    chat.py's own clarify gate -- resolve_intent must honor that same
    threshold, not silently bail out on LOW_CONFIDENCE before ever reaching
    the confirmActions check (which would skip confirmation entirely)."""
    nina = Nina()
    run(nina.init({
        "llm": {"provider": "custom", "adapter": _adapter_resolving_to("checkout", confidence=0.6)},
        "behavior": {"confidenceThreshold": 0.5},
    }))
    run(nina.register({
        "name": "checkout",
        "description": "Place an order and complete checkout for the user.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda inp, ctx: {"orderId": "order-1"},
    }))
    nina._core.config = {"_agentContract": CONTRACT}

    envelope = run(nina.chat("checkout please", "s1"))
    data = envelope["data"]
    assert data["intent"] == "confirmation", (
        "action executed without confirmation -- resolve_intent's own "
        "confidence_threshold default likely overrode the merchant's configured one"
    )
    assert data["actionCalled"] is None


def test_action_not_wrongly_blocked_when_page_context_is_absent():
    """An action with a declared availableOn list, called with no page_id
    set (core.config['_pageId'] absent -- a common real case: tests, simple
    sidecars, or auth-replay/plan-resume flows that don't track page
    context), must still execute. Only risk.blockActions should hard-block;
    a page-availability mismatch is a softer, pre-existing, intentionally
    ignored-by-chat.py signal and must not regress into a hard block."""
    contract = {
        **CONTRACT,
        "actions": [
            {"id": "search_products", "description": "Search.", "parameters": {},
             "availableOn": ["home"], "execute": {"type": "message", "steps": []}},
        ],
        "risk": {},
    }

    def adapter(payload):
        if payload.get("mode") == "compose":
            return "Done."
        return {"resolution": "action", "action": "search_products", "input": {}, "confidence": 1.0}

    nina = Nina()
    run(nina.init({"llm": {"provider": "custom", "adapter": adapter}}))
    run(nina.register({
        "name": "search_products",
        "description": "Search the product catalog by keyword for the user.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda inp, ctx: {"results": []},
    }))
    nina._core.config = {"_agentContract": contract}  # no _pageId set

    envelope = run(nina.chat("search for shoes", "s1"))
    data = envelope["data"]
    assert data["intent"] != "blocked"
    assert data["actionCalled"] == "search_products"
