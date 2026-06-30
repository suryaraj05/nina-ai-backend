"""Deterministic product picks from lastSearchResults."""

from nina.session import resolve_product_reference, update_reference_map


def _state_with_results(results: list[dict]) -> dict:
    state: dict = {"referenceMap": {}}
    update_reference_map(state, "search_products", {"results": results, "count": len(results)})
    return state


def test_resolve_second_ordinal_to_product_id():
    state = _state_with_results([
        {"id": "sku-1", "name": "Black Hoodie", "price": 1299},
        {"id": "sku-2", "name": "Gray Zip Hoodie", "price": 2099},
    ])
    out = resolve_product_reference(
        state,
        "add_to_cart",
        {},
        "get the 2nd one",
    )
    assert out["productId"] == "sku-2"
    assert out["name"] == "Gray Zip Hoodie"


def test_resolve_product_name_from_add_to_cart_phrase():
    state = _state_with_results([
        {
            "id": "abc123",
            "name": "Distressed Cobalt Blue Puffer Jacket",
            "price": 3999,
        },
    ])
    out = resolve_product_reference(
        state,
        "add_to_cart",
        {},
        "Add Distressed Cobalt Blue Puffer Jacket to cart",
    )
    assert out["productId"] == "abc123"
    assert out["query"] == "Distressed Cobalt Blue Puffer Jacket"


def test_resolve_leaves_unknown_messages_unchanged():
    state = _state_with_results([{"id": "x", "name": "Tee", "price": 499}])
    out = resolve_product_reference(state, "add_to_cart", {}, "hello there")
    assert out == {}
