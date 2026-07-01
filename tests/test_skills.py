"""Skills: markdown playbooks injected into the resolution prompt per-action."""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

from nina import Nina
from nina.skill_loader import load_skills, skills_by_action, BUILTIN_SKILLS_DIR


def run(coro):
    return asyncio.run(coro)


def test_builtin_skills_load_and_map_to_their_actions():
    skills = load_skills(BUILTIN_SKILLS_DIR)
    names = {s["name"] for s in skills}
    assert {
        "search-skill",
        "cart-skill",
        "checkout-skill",
        "product-detail-skill",
        "navigation-skill",
        "view-cart-skill",
    } <= names

    by_action = skills_by_action(skills)
    assert "search_products" in by_action
    assert "last_search_results" in by_action["add_to_cart"] or "productId" in by_action["add_to_cart"]
    assert "confirm" in by_action["checkout"].lower()

    cart = next(s for s in skills if s["name"] == "cart-skill")
    assert isinstance(cart.get("clarificationFlow"), dict)
    assert cart["clarificationFlow"].get("enabled") is True

    search = next(s for s in skills if s["name"] == "search-skill")
    assert isinstance(search.get("searchUX"), dict)


def test_site_specific_skill_overrides_builtin_by_name(tmp_path: Path):
    override = tmp_path / "search-skill.md"
    override.write_text(
        textwrap.dedent("""\
            ---
            name: search-skill
            appliesTo: [search_products]
            description: Site override.
            ---
            SITE-SPECIFIC OVERRIDE TEXT
            """),
        encoding="utf-8",
    )
    skills = load_skills(BUILTIN_SKILLS_DIR, tmp_path)
    by_action = skills_by_action(skills)
    assert by_action["search_products"] == "SITE-SPECIFIC OVERRIDE TEXT"


def test_skill_guidance_reaches_the_actual_resolution_prompt():
    captured_prompts: list[str] = []

    def adapter(payload: dict):
        captured_prompts.append(payload.get("prompt", ""))
        return {"resolution": "action", "action": "add_to_cart",
                "input": {"variantId": "v1"}, "confidence": 0.95}

    async def scenario():
        nina = Nina()
        await nina.init({"llm": {"provider": "custom", "adapter": adapter}})
        await nina.register({
            "name": "add_to_cart",
            "description": "Add a product variant to the shopping cart for the user.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "variantId": {"type": "string", "description": "variant id"},
                },
                "required": ["variantId"],
            },
            "handler": lambda inp, ctx: {"ok": True, "cart": {"total": 10}},
        })
        await nina.chat("add it to my cart", "s1")

    run(scenario())
    assert captured_prompts, "LLM adapter was never called"
    resolution_prompts = [p for p in captured_prompts if "REGISTERED ACTIONS" in p]
    assert resolution_prompts, "main resolution prompt was never built"
    assert "variantId" in resolution_prompts[0]
    assert "last_search_results" in resolution_prompts[0]
