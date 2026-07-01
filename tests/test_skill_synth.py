"""Automatic skill synthesis from agent contracts."""
from __future__ import annotations

from nina.skill_loader import BUILTIN_SKILLS_DIR, load_skills
from nina.skill_synth import (
    apply_skills_for_contract,
    contract_skills_fingerprint,
    role_for_action,
    synthesize_skills,
)


def test_role_for_action():
    assert role_for_action("search_products") == "search"
    assert role_for_action("add_to_cart") == "cart"
    assert role_for_action("track_order") == "order_tracking"
    assert role_for_action("totally_custom_xyz") is None


def test_synthesize_maps_contract_action_ids():
    contract = {
        "actions": [
            {"id": "search_products", "parameters": {"query": {"type": "string", "required": True}}},
            {"id": "add_item_to_cart", "parameters": {"productId": {"type": "string"}}},
            {"id": "place_order", "risk": "high"},
        ],
        "risk": {"confirmActions": ["place_order"]},
        "pages": [{"id": "product_detail"}],
    }
    skills = synthesize_skills(contract)
    assert skills
    search = next(s for s in skills if s["name"] == "search-skill")
    assert "search_products" in search["appliesTo"]
    cart = next(s for s in skills if s["name"] == "cart-skill")
    assert "add_item_to_cart" in cart["appliesTo"]
    assert cart.get("clarificationFlow", {}).get("enabled") is True
    checkout = next(s for s in skills if s["name"] == "checkout-skill")
    assert "place_order" in checkout["appliesTo"]
    assert "confirm-required" in checkout["body"]


def test_cart_size_flow_disabled_without_pdp_or_apparel():
    contract = {
        "actions": [{"id": "add_to_cart", "execute": {"type": "api"}}],
        "pages": [{"id": "home"}],
    }
    skills = synthesize_skills(contract, catalog=[{"name": "API license key", "category": "Software"}])
    cart = next(s for s in skills if s["name"] == "cart-skill")
    assert cart.get("clarificationFlow", {}).get("enabled") is False


def test_synthesize_appends_parameter_hints():
    contract = {
        "actions": [{
            "id": "search_products",
            "parameters": {
                "query": {"type": "string", "required": True, "description": "Search terms"},
            },
        }],
        "pages": [],
    }
    skills = synthesize_skills(contract)
    search = next(s for s in skills if s["name"] == "search-skill")
    assert "This site's parameters" in search["body"]
    assert "query" in search["body"]


def test_fingerprint_changes_when_actions_change():
    a = contract_skills_fingerprint({"actions": [{"id": "search"}]})
    b = contract_skills_fingerprint({"actions": [{"id": "search"}, {"id": "checkout"}]})
    assert a != b


def test_apply_skills_for_contract_on_core():
    class _Core:
        pass

    core = _Core()
    contract = {"actions": [{"id": "search_products"}], "pages": []}
    apply_skills_for_contract(core, contract)
    assert core.skills
    assert "search_products" in core.skills_by_action
    assert core.fast_path_patterns is not None


def test_empty_contract_falls_back_to_templates():
    templates = load_skills(BUILTIN_SKILLS_DIR)
    assert len(synthesize_skills(None)) == len(templates)


def test_dom_signals_enable_cart_flow_without_pdp_page():
    contract = {
        "actions": [{"id": "add_to_cart", "execute": {"type": "api"}}],
        "pages": [{"id": "home"}],
        "signals": {
            "productDetail": {"hasSizeOptions": True, "sizeLabels": ["S", "M", "L"]},
        },
    }
    skills = synthesize_skills(
        contract,
        catalog=[{"name": "API license key", "category": "Software"}],
    )
    cart = next(s for s in skills if s["name"] == "cart-skill")
    assert cart.get("clarificationFlow", {}).get("enabled") is True
    size_step = next(
        s for s in cart["clarificationFlow"]["steps"] if s.get("field") == "size"
    )
    assert size_step.get("chipsDefault") == ["S", "M", "L"]


def test_cod_skill_gets_pincode_clarify_guidance():
    contract = {
        "site": {"locales": ["en-IN"]},
        "actions": [{
            "id": "check_cod",
            "parameters": {
                "pincode": {"type": "string", "required": True, "description": "6-digit PIN"},
            },
        }],
        "pages": [],
    }
    skills = synthesize_skills(contract)
    cod = next(s for s in skills if s["name"] == "cod-skill")
    assert "pincode" in cod.get("clarifyGuidance", "").lower()
    assert "hindi" in cod.get("clarifyGuidance", "").lower() or "hinglish" in cod.get(
        "clarifyGuidance", ""
    ).lower()


def test_india_support_note_when_no_cod_action():
    contract = {
        "site": {"locales": ["en-IN"]},
        "actions": [{"id": "show_message"}],
        "pages": [],
    }
    skills = synthesize_skills(contract)
    support = next(s for s in skills if s["name"] == "support-skill")
    assert "COD" in support["body"] or "cash-on-delivery" in support["body"].lower()
    assert not any(s["name"] == "cod-skill" for s in skills)


def test_search_skill_price_param_hint():
    contract = {
        "actions": [{
            "id": "search_products",
            "parameters": {
                "query": {"type": "string"},
                "max_price": {"type": "number"},
            },
        }],
        "pages": [],
    }
    skills = synthesize_skills(contract)
    search = next(s for s in skills if s["name"] == "search-skill")
    assert "max_price" in search["body"]


def test_order_tracking_asks_for_order_param():
    contract = {
        "actions": [{
            "id": "track_order",
            "parameters": {
                "order_id": {"type": "string", "required": True},
            },
        }],
        "pages": [],
    }
    skills = synthesize_skills(contract)
    tracking = next(s for s in skills if s["name"] == "order-tracking-skill")
    assert "order_id" in tracking.get("clarifyGuidance", "").lower()
