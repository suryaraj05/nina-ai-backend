"""Grounded search must not navigate with the full natural-language query."""

from __future__ import annotations

from nina.catalog_rail import storefront_browse_url
from nina.instructions import turn_to_instructions


def test_storefront_browse_url_strips_price_clause():
    url = storefront_browse_url("hoodies under 3000", "https://shop.test")
    assert url == "https://shop.test/shop?search=hoodies"


def test_grounded_search_instructions_use_keyword_url():
    contract = {
        "site": {"baseUrl": "https://shop.test"},
        "actions": [{
            "id": "search_products",
            "description": "Search",
            "parameters": {"query": {"type": "string", "required": True}},
            "execute": {
                "type": "dom",
                "steps": [{"op": "navigate", "url": "/shop?search={query}"}],
            },
        }],
    }
    turn = {
        "actionCalled": "search_products",
        "actionInput": {"query": "hoodies under 3000"},
        "actionResult": {"grounded": True, "count": 2, "results": [{}, {}]},
        "confidence": 0.9,
    }
    steps = turn_to_instructions(contract, turn)
    assert len(steps) == 1
    assert steps[0]["type"] == "navigate"
    assert steps[0]["url"] == "https://shop.test/shop?search=hoodies"


def test_grounded_zero_results_no_navigate():
    contract = {
        "site": {"baseUrl": "https://shop.test"},
        "actions": [{
            "id": "search_products",
            "parameters": {"query": {"type": "string"}},
            "execute": {"type": "dom", "steps": [{"op": "navigate", "url": "/shop?search={query}"}]},
        }],
    }
    turn = {
        "actionCalled": "search_products",
        "actionInput": {"query": "pink elephants"},
        "actionResult": {"grounded": True, "count": 0},
        "confidence": 0.9,
    }
    assert turn_to_instructions(contract, turn) == []
