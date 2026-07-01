import asyncio

from nina import Nina
from nina.cart_flow import (
    begin_cart_add,
    continue_cart_add,
    parse_quantity_choice,
    parse_size_choice,
    rehydrate_cart_pending_from_hints,
)


def test_parse_size_and_quantity():
    assert parse_size_choice("M", ["XS", "S", "M", "L"]) == "M"
    assert parse_size_choice("size L", ["XS", "S", "M", "L"]) == "L"
    assert parse_quantity_choice("2") == 2


def test_cart_add_flow_size_then_quantity():
    state: dict = {}
    flow = begin_cart_add(
        state,
        {"productId": "sku-1", "name": "Blue Hoodie"},
        session_hints={"productOptions": {"sizes": ["S", "M", "L"]}},
        contract={
            "site": {"baseUrl": "https://shop.test"},
            "actions": [{
                "id": "open_product",
                "execute": {"type": "dom", "steps": [{"op": "navigate", "url": "/product/{productId}"}]},
            }],
        },
        on_pdp=False,
    )
    assert flow is not None
    assert flow["intent"] == "cart_guidance"
    assert flow["chips"] == ["S", "M", "L"]
    assert flow["instructions"][0]["url"] == "/product/sku-1"

    qty_flow = continue_cart_add(state, "M")
    assert qty_flow is not None
    assert qty_flow["chips"] == ["1", "2", "3"]
    assert "Size M" in qty_flow["reply"]

    done = continue_cart_add(state, "1")
    assert done is not None
    assert done["intent"] == "action"
    assert done["instructions"][0]["type"] == "cart_add"
    assert done["instructions"][0]["size"] == "M"
    assert done["instructions"][0]["quantity"] == 1


def test_cart_add_via_chat_fast_path():
    async def scenario():
        nina = Nina()
        await nina.init({"llm": {"provider": "custom", "adapter": lambda p: {"resolution": "action", "action": "search", "input": {}, "confidence": 0.9}}})
        await nina.register([{
            "name": "add_to_cart",
            "description": "Add a product to the shopping cart with size and quantity.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "productId": {"type": "string", "description": "Catalog product id"},
                },
            },
            "handler": lambda inp, ctx: {"ok": True},
        }])
        nina._core.config = {
            "_productCatalog": [{"sku": "h1", "name": "Blue Hoodie", "price": 2000, "in_stock": True}],
            "_agentContract": {
                "site": {"baseUrl": "https://shop.test"},
                "actions": [{
                    "id": "open_product",
                    "execute": {"type": "dom", "steps": [{"op": "navigate", "url": "/product/{productId}"}]},
                }],
            },
            "_sessionHints": {"productOptions": {"sizes": ["S", "M"]}},
        }
        return await nina.chat("add Blue Hoodie to cart", "s-cart")

    envelope = asyncio.run(scenario())
    data = envelope["data"]
    assert data["intent"] == "cart_guidance"
    assert "size" in data["naturalLanguageResponse"].lower()
    assert data.get("suggestionChips")


def test_rehydrate_cart_pending_from_widget_hints():
    state: dict = {}
    ok = rehydrate_cart_pending_from_hints(
        state,
        {
            "cartFlow": {
                "step": "size",
                "productId": "oCVMI5Gy7cgGgOJDUG9A",
                "productName": "Cargo Midnight Black Fleece Hoodie",
                "sizes": ["XS", "S", "M", "L", "XL", "XXL"],
            },
        },
    )
    assert ok
    assert state["pending"]["step"] == "size"
    qty = continue_cart_add(state, "M")
    assert qty is not None
    assert "Size M" in qty["reply"]
