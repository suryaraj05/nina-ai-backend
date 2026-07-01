"""Tests for skill_runtime helpers."""
from __future__ import annotations

from nina.skill_loader import BUILTIN_SKILLS_DIR, load_skills
from nina.skill_runtime import (
    clarification_flow_for_action,
    compose_guidance_for_action,
    format_skill_template,
    search_ux_messages,
)


def test_clarification_flow_from_cart_skill():
    skills = load_skills(BUILTIN_SKILLS_DIR)
    flow = clarification_flow_for_action(skills, "add_to_cart")
    assert flow is not None
    assert flow.get("enabled") is True
    steps = flow.get("steps") or []
    assert any(s.get("field") == "size" for s in steps)
    assert any(s.get("field") == "quantity" for s in steps)


def test_search_ux_from_skill():
    skills = load_skills(BUILTIN_SKILLS_DIR)
    ux = search_ux_messages(skills)
    assert "catalog" in ux["emptyStrict"].lower()
    assert "similar" in ux["emptyAlternatives"].lower()


def test_compose_guidance_for_checkout():
    skills = load_skills(BUILTIN_SKILLS_DIR)
    text = compose_guidance_for_action(skills, "checkout")
    assert "order reference" in text.lower() or "confirmation" in text.lower()


def test_format_skill_template():
    out = format_skill_template(
        "Added {productName} ({size} × {quantity})",
        {"productName": "Hoodie", "size": "M", "quantity": 2},
    )
    assert out == "Added Hoodie (M × 2)"
