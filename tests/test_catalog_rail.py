from nina.catalog_rail import (
    CatalogGraph,
    execute_catalog_search,
    grounded_reply,
    parse_price_constraint,
    validate_catalog_mutation,
)

SAMPLE_ROWS = [
    {
        "sku": "h1",
        "name": "Black Fleece Hoodie",
        "price": 1299,
        "currency": "INR",
        "category": "Hoodies",
        "in_stock": True,
    },
    {
        "sku": "h2",
        "name": "Grey Zip Hoodie",
        "price": 3499,
        "currency": "INR",
        "category": "Hoodies",
        "in_stock": True,
    },
    {
        "sku": "t1",
        "name": "White Cotton Tee",
        "price": 899,
        "currency": "INR",
        "category": "T-Shirts",
        "in_stock": True,
    },
]


def test_parse_price_constraint_under_3000():
    text, cap = parse_price_constraint("hoodies under 3000")
    assert "hoodies" in text
    assert cap == 3000


def test_catalog_search_filters_by_price():
    out = execute_catalog_search({"query": "hoodies under 3000"}, SAMPLE_ROWS)
    assert out["grounded"] is True
    assert out["count"] == 1
    assert out["results"][0]["sku"] == "h1"


def test_catalog_search_price_only_generic_query():
    out = execute_catalog_search({"query": "show me products under 3000"}, SAMPLE_ROWS)
    assert out["grounded"] is True
    assert out["count"] == 2
    skus = {r["sku"] for r in out["results"]}
    assert skus == {"h1", "t1"}


def test_catalog_search_empty_is_honest():
    out = execute_catalog_search({"query": "unicorn jacket"}, SAMPLE_ROWS)
    assert out["grounded"] is True
    assert out["count"] == 0
    assert grounded_reply("search_products", out) == "I couldn't find anything matching that in the catalog."


def test_catalog_search_no_catalog():
    out = execute_catalog_search({"query": "hoodies"}, [])
    assert out["noCatalog"] is True
    assert "verified product data" in grounded_reply("search_products", out)


def test_mutation_allowed_without_catalog_for_api_stores():
    ok, _ = validate_catalog_mutation("add_to_cart", {"query": "anything"}, [])
    assert ok
    ok, reason = validate_catalog_mutation("add_to_cart", {"query": "fake thing"}, SAMPLE_ROWS)
    assert not ok
    assert "not in the verified catalog" in reason


def test_mutation_allowed_for_catalog_match():
    ok, _ = validate_catalog_mutation("add_to_cart", {"query": "Black Fleece Hoodie"}, SAMPLE_ROWS)
    assert ok


def test_graph_hoodies_category_match():
    graph = CatalogGraph().load_rows(SAMPLE_ROWS)
    hits = graph.search("hoodies")
    assert len(hits) == 2
