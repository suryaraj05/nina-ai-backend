"""Checkout phrase routing and signed-in auth replay."""

from __future__ import annotations

import asyncio

from nina import Nina

CONTRACT = {
    "site": {"id": "shop", "name": "Shop", "baseUrl": "http://x"},
    "apis": {"default": {"baseUrl": "http://x"}},
    "actions": [
        {
            "id": "checkout",
            "description": "Proceed to checkout.",
            "parameters": {},
            "requiresAuth": True,
            "execute": {"type": "message", "steps": []},
        },
    ],
    "auth": {"loginUrl": "/login", "gatedActions": ["checkout"]},
    "risk": {"confirmActions": ["checkout"]},
}


def run(coro):
    return asyncio.run(coro)


async def _make_nina():
    nina = Nina()
    await nina.init({"llm": {"provider": "custom", "adapter": lambda p: "chitchat"}})
    await nina.register({
        "name": "checkout",
        "description": "Proceed to checkout for the current cart.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "handler": lambda _i, _c: {"ok": True},
    })
    nina._core.config = {
        "_agentContract": CONTRACT,
        "_sessionHints": {},
    }
    return nina


def test_buy_it_routes_to_checkout_login_when_logged_out():
    nina = run(_make_nina())
    envelope = run(nina.chat("buy it", "s-buy"))
    assert envelope["ok"]
    data = envelope["data"]
    assert data["intent"] == "needs_login"
    assert data.get("suggestionChips") == ["Sign in", "View cart"]


def test_buy_it_then_signed_in_replays_checkout():
    nina = run(_make_nina())
    run(nina.chat("buy it", "s-replay"))
    state = run(nina._core.sessions.get("s-replay"))
    assert state.get("queuedIntent")
    envelope = run(nina.chat("signed in already", "s-replay"))
    assert envelope["ok"]
    data = envelope["data"]
    assert data["actionCalled"] == "checkout"


def test_buy_it_authenticated_user_gets_confirmation():
    nina = run(_make_nina())
    nina._core.config = {
        **nina._core.config,
        "_sessionHints": {"authenticated": True},
    }
    envelope = run(nina.chat("buy it", "s-confirm"))
    assert envelope["ok"]
    data = envelope["data"]
    assert data["intent"] == "confirmation"
    assert data.get("suggestionChips") == ["Yes", "Not now"]
