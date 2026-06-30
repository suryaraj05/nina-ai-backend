from nina.fast_path import try_catalog_search_fast_path, try_reference_cart_fast_path
from nina.session import seed_reference_map_from_client


def test_cart_fast_path_uses_widget_seeded_results():
    state: dict = {"referenceMap": {}}
    seed_reference_map_from_client(state, {
        "lastSearchResults": [
            {"id": "sku-2", "title": "Gray Zip Hoodie"},
        ],
    })
    actions = [{"name": "add_to_cart", "description": "add"}]
    match = try_reference_cart_fast_path("get the 1st one", state, actions)
    assert match is not None
    assert match["action"] == "add_to_cart"
    assert match["input"]["productId"] == "sku-2"


def test_search_fast_path_routes_catalog_query():
    actions = [{"name": "search_products", "description": "search"}]
    match = try_catalog_search_fast_path("Show me hoodies under ₹2000", actions)
    assert match is not None
    assert match["action"] == "search_products"
    assert "hoodies" in match["input"]["query"]
