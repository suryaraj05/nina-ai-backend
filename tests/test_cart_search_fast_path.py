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


def test_show_me_hoodies_not_open_product_when_both_registered():
    from nina.fast_path import compile_fast_path_patterns, normalize_fast_match, try_fast_path
    from nina.skill_loader import BUILTIN_SKILLS_DIR, load_skills
    from nina.skill_synth import synthesize_skills

    contract = {
        "actions": [
            {"id": "search_products", "parameters": {"query": {"type": "string"}}},
            {"id": "open_product", "parameters": {
                "productUrl": {"type": "string", "required": True},
            }},
        ],
        "pages": [],
    }
    skills = synthesize_skills(contract)
    patterns = compile_fast_path_patterns(skills)
    actions = [
        {"name": "search_products", "examples": []},
        {"name": "open_product", "examples": []},
    ]
    msg = "show me hoodies under 3000"
    match = try_catalog_search_fast_path(msg, actions)
    assert match is not None
    assert match["action"] == "search_products"
    assert "hoodies" in match["input"]["query"]
    skill_match = try_fast_path(msg, actions, patterns)
    if skill_match:
        skill_match = normalize_fast_match(msg, skill_match, actions)
        assert skill_match["action"] == "search_products"
