from nina.action_input_coalesce import coalesce_action_input, infer_search_query


def test_infer_search_query_can_you_show_me():
    assert infer_search_query("can you show me hoodies under 3000") == "hoodies under 3000"


def test_infer_search_query_could_you_show_me():
    assert infer_search_query("Could you show me the jackets?") == "jackets"


def test_coalesce_fills_can_you_show_me():
    out = coalesce_action_input(
        "search_products",
        {},
        "can you show me hoodies under 3000",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    assert out == {"query": "hoodies under 3000"}


def test_infer_search_query_from_show_me():
    assert infer_search_query("Show me hoodies") == "hoodies"


def test_infer_search_query_from_search_for():
    assert infer_search_query("search for gaming laptops") == "gaming laptops"


def test_infer_search_query_skips_open_questions():
    assert infer_search_query("what laptops do you have") is None


def test_coalesce_fills_missing_search_query():
    out = coalesce_action_input(
        "search_products",
        {},
        "Show me hoodies",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    assert out == {"query": "hoodies"}


def test_coalesce_does_not_overwrite_existing_query():
    out = coalesce_action_input(
        "search_products",
        {"query": "jackets"},
        "Show me hoodies",
        {"type": "object", "properties": {"query": {"type": "string"}}},
    )
    assert out == {"query": "jackets"}


def test_coalesce_fills_open_category_slug():
    out = coalesce_action_input(
        "open_category",
        {},
        "show me hoodies",
        {
            "type": "object",
            "properties": {
                "categorySlug": {"type": "string", "enum": ["hoodies", "tees"]},
            },
            "required": ["categorySlug"],
        },
    )
    assert out == {"categorySlug": "hoodies"}


def test_llm_empty_search_params_coalesced_end_to_end():
    import asyncio

    from nina import Nina

    def adapter(payload):
        if payload.get("mode") == "compose":
            return {"reply": "Here are some hoodies for you."}
        return {
            "resolution": "action",
            "action": "search_products",
            "input": {},
            "confidence": 0.9,
        }

    async def scenario():
        nina = Nina()
        await nina.init({"llm": {"provider": "custom", "adapter": adapter}})
        await nina.register({
            "name": "search_products",
            "description": "Search the product catalog by keyword.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "search terms"}},
                "required": ["query"],
            },
            "handler": lambda inp, ctx: {"results": [], "count": 0},
        })
        return await nina.chat("Show me hoodies", "s1")

    envelope = asyncio.run(scenario())
    data = envelope["data"]
    assert data["actionCalled"] == "search_products"
    assert data["actionInput"] == {"query": "hoodies"}
