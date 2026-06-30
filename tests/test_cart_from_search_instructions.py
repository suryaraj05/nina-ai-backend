from nina.instructions import turn_to_instructions


def test_add_to_cart_from_shop_navigates_to_product_page():
    contract = {
        "site": {"baseUrl": "https://shop.test"},
        "actions": [
            {
                "id": "add_to_cart",
                "execute": {
                    "type": "dom",
                    "steps": [{"op": "click", "selector": '[data-testid="add-to-cart"]'}],
                },
            },
            {
                "id": "open_product",
                "execute": {
                    "type": "dom",
                    "steps": [{"op": "navigate", "url": "/product/{productId}"}],
                },
            },
        ],
    }
    turn = {
        "actionCalled": "add_to_cart",
        "actionInput": {"productId": "sku-99"},
        "actionResult": {"ok": True, "productId": "sku-99"},
        "confidence": 1.0,
    }
    steps = turn_to_instructions(
        contract,
        turn,
        page_context={"pageId": "product_list"},
    )
    assert steps == [{"type": "navigate", "url": "/product/sku-99", "_actionId": "open_product"}]


def test_add_to_cart_with_product_url_template_contract():
    contract = {
        "site": {"baseUrl": "https://shop.test"},
        "actions": [
            {"id": "add_to_cart", "execute": {"type": "dom", "steps": []}},
            {
                "id": "open_product",
                "execute": {
                    "type": "dom",
                    "steps": [{"op": "navigate", "url": "{productUrl}"}],
                },
            },
        ],
    }
    turn = {
        "actionCalled": "add_to_cart",
        "actionInput": {"productId": "distressed-cobalt-hoodie"},
        "confidence": 1.0,
    }
    steps = turn_to_instructions(
        contract,
        turn,
        page_context={"pageId": "product_list"},
    )
    assert steps == [{
        "type": "navigate",
        "url": "/product/distressed-cobalt-hoodie",
        "_actionId": "open_product",
    }]
