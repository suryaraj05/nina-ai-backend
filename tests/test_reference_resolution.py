"""Reference map and pronoun-resolution tests (Phase 2) — no live LLM."""
from __future__ import annotations

import asyncio
import json

import pytest

from nina import Nina
from nina.session import update_reference_map


def run(coro):
    return asyncio.run(coro)


class RoutingAdapter:
    """Deterministic custom adapter keyed off the current user message."""

    @staticmethod
    def _message(prompt: str) -> str:
        if "<<<UNTRUSTED_USER_BEGIN>>>" in prompt:
            return (
                prompt.split("<<<UNTRUSTED_USER_BEGIN>>>", 1)[1]
                .split("<<<UNTRUSTED_USER_END>>>", 1)[0]
                .strip()
                .lower()
            )
        marker = "CURRENT USER MESSAGE"
        if marker in prompt:
            tail = prompt.split(marker, 1)[1]
            return tail.split("\n", 1)[-1].strip().lower()
        return prompt.lower()

    def __call__(self, payload):
        mode = payload["mode"]
        prompt = payload["prompt"]
        if mode == "compose":
            return {"text": "Done."}
        if "NINA INTERNAL: PRE-REASONING" in prompt:
            return json.dumps({"needs_reasoning": False})
        if "NINA INTERNAL: CLARIFICATION" in prompt:
            return json.dumps(
                {
                    "question": "Which product did you mean?",
                    "strategy": "reference_disambiguation",
                }
            )

        msg = self._message(prompt)
        empty_ref = "(empty — no referable entities" in prompt

        if "add it to cart" in msg and empty_ref:
            return {
                "resolution": "clarify",
                "action": "add_to_cart",
                "input": {},
                "missing_fields": ["productId"],
                "confidence": 0.4,
                "user_reply": "Which item should I add?",
            }
        if "add it to cart" in msg:
            return {
                "resolution": "action",
                "action": "add_to_cart",
                "input": {"productId": "p02"},
                "missing_fields": [],
                "confidence": 0.95,
                "user_reply": "",
            }
        if "second one" in msg:
            return {
                "resolution": "action",
                "action": "get_detail",
                "input": {"productId": "p02"},
                "missing_fields": [],
                "confidence": 0.92,
                "user_reply": "",
            }
        if "remove that" in msg:
            return {
                "resolution": "action",
                "action": "remove_from_cart",
                "input": {"productId": "p01"},
                "missing_fields": [],
                "confidence": 0.9,
                "user_reply": "",
            }
        if "more like this" in msg:
            return {
                "resolution": "action",
                "action": "search_items",
                "input": {"query": "kurta"},
                "missing_fields": [],
                "confidence": 0.88,
                "user_reply": "",
            }
        if "weather in paris" in msg:
            return {
                "resolution": "unsupported",
                "action": None,
                "input": None,
                "missing_fields": [],
                "confidence": 0.8,
                "user_reply": "I cannot help with weather.",
            }
        return {
            "resolution": "action",
            "action": "search_items",
            "input": {"query": "hoodies"},
            "missing_fields": [],
            "confidence": 0.9,
            "user_reply": "",
        }


ACTIONS = [
    {
        "name": "search_items",
        "description": (
            "Searches the catalogue and returns matching items. Use for discovery "
            "and browsing. Do not use for a single known item detail lookup."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
            },
            "required": ["query"],
        },
        "handler": lambda inp, ctx: {
            "results": [
                {"id": "p01", "name": "Hoodie"},
                {"id": "p02", "name": "Kurta"},
            ],
            "count": 2,
        },
    },
    {
        "name": "get_detail",
        "description": (
            "Returns detail for one item by id. Use when the user asks about a "
            "specific item including pronoun references like the second one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "productId": {
                    "type": "string",
                    "description": "Product id from prior results.",
                },
            },
            "required": ["productId"],
        },
        "handler": lambda inp, ctx: {"id": inp["productId"], "name": "Kurta"},
    },
    {
        "name": "add_to_cart",
        "description": (
            "Adds one product to the cart by id. Use when the user wants to keep "
            "a specific item including pronoun references like add it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "productId": {
                    "type": "string",
                    "description": "Product id from reference map.",
                },
            },
            "required": ["productId"],
        },
        "handler": lambda inp, ctx: {
            "cart": {"items": [{"id": inp["productId"], "qty": 1}], "total": 100}
        },
    },
    {
        "name": "remove_from_cart",
        "description": (
            "Removes a product from the cart by id. Use when the user wants to "
            "drop a specific cart line including references like remove that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "productId": {
                    "type": "string",
                    "description": "Product id in the cart.",
                },
            },
            "required": ["productId"],
        },
        "handler": lambda inp, ctx: {"cart": {"items": [], "total": 0}},
    },
]


async def _nina_ready():
    nina = Nina()
    await nina.init(
        {
            "llm": {"provider": "custom", "adapter": RoutingAdapter()},
            "behavior": {"confidenceThreshold": 0.75},
        }
    )
    await nina.register(ACTIONS)
    return nina


def test_add_it_after_single_product():
    nina = run(_nina_ready())
    state = {"referenceMap": {}}
    update_reference_map(
        state,
        "get_detail",
        {"id": "p99", "name": "Solo Item"},
    )
    run(nina.chat("show solo", "s1"))
    turn = run(nina.chat("add it to cart", "s1"))
    assert turn["ok"]
    assert turn["data"]["intent"] == "cart_guidance"
    run(nina.chat("M", "s1"))
    turn = run(nina.chat("1", "s1"))
    assert turn["data"]["actionCalled"] == "add_to_cart"
    assert turn["data"]["actionInput"]["productId"] == "p02"


def test_second_one_after_search():
    nina = run(_nina_ready())
    run(nina.chat("show hoodies", "s2"))
    turn = run(nina.chat("tell me more about the second one", "s2"))
    assert turn["ok"]
    assert turn["data"]["actionCalled"] == "get_detail"
    assert turn["data"]["actionInput"]["productId"] == "p02"


def test_remove_after_cart():
    nina = run(_nina_ready())
    run(nina.chat("show hoodies", "s3"))
    run(nina.chat("add it to cart", "s3"))
    run(nina.chat("M", "s3"))
    run(nina.chat("1", "s3"))
    turn = run(nina.chat("remove that from cart", "s3"))
    assert turn["ok"]
    assert turn["data"]["actionCalled"] == "remove_from_cart"


def test_more_like_this_after_detail():
    nina = run(_nina_ready())
    run(nina.chat("show hoodies", "s4"))
    run(nina.chat("tell me more about the second one", "s4"))
    turn = run(nina.chat("show me more like this", "s4"))
    assert turn["ok"]
    assert turn["data"]["actionCalled"] == "search_items"


def test_topic_change_clears_unrelated_search():
    state = {
        "referenceMap": {
            "lastSearchResults": [
                {"index": 1, "sourceAction": "search_items", "id": "p01"}
            ],
            "lastSingleItem": None,
            "cartContents": None,
            "lastActionResult": None,
        }
    }
    update_reference_map(
        state,
        "get_detail",
        {"id": "x9", "name": "Unrelated detail"},
    )
    assert state["referenceMap"]["lastSearchResults"] == []


def test_empty_reference_map_clarifies_not_hallucinates():
    nina = run(_nina_ready())
    turn = run(nina.chat("add it to cart", "s-empty"))
    assert turn["ok"]
    assert turn["data"]["intent"] == "clarification"
    assert turn["data"]["actionCalled"] is None
