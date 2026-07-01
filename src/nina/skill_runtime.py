"""Runtime helpers — read structured skill metadata for non-LLM pipeline stages."""

from __future__ import annotations

from typing import Any

_DEFAULT_CART_FLOW: dict[str, Any] = {
    "enabled": True,
    "steps": [
        {
            "field": "size",
            "prompt": "Pick a size:",
            "promptOnPdp": "Pick a size:",
            "promptNavigate": "Opening {productName}. Pick a size:",
            "promptRetry": "Tap your size:",
            "chipsFrom": "productOptions.sizes",
            "chipsDefault": ["XS", "S", "M", "L", "XL", "XXL"],
        },
        {
            "field": "quantity",
            "prompt": "Size {size} — how many?",
            "chips": ["1", "2", "3"],
        },
    ],
    "complete": {
        "reply": "Added {productName} ({size} × {quantity}) to your cart.",
        "chips": ["What's in my cart?", "Continue shopping"],
    },
}

_DEFAULT_SEARCH_UX = {
    "emptyStrict": "I couldn't find anything matching that in the catalog.",
    "emptyAlternatives": (
        "I couldn't find an exact match, but here are some similar options "
        "you can try instead."
    ),
}


def skills_for_action(skills: list[dict[str, Any]], action_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for skill in skills or []:
        if action_id in (skill.get("appliesTo") or []):
            out.append(skill)
    return out


def skill_body_for_action(skills: list[dict[str, Any]], action_id: str) -> str:
    bodies = [s.get("body", "") for s in skills_for_action(skills, action_id) if s.get("body")]
    return "\n\n".join(bodies)


def clarification_flow_for_action(
    skills: list[dict[str, Any]],
    action_id: str,
) -> dict[str, Any] | None:
    for skill in skills_for_action(skills, action_id):
        flow = skill.get("clarificationFlow")
        if isinstance(flow, dict) and flow.get("enabled"):
            return flow
    if action_id in ("add_to_cart", "add_item_to_cart"):
        return dict(_DEFAULT_CART_FLOW)
    return None


def compose_guidance_for_action(skills: list[dict[str, Any]], action_id: str) -> str:
    parts: list[str] = []
    for skill in skills_for_action(skills, action_id):
        text = skill.get("composeGuidance")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def clarify_guidance_for_action(skills: list[dict[str, Any]], action_id: str) -> str:
    parts: list[str] = []
    for skill in skills_for_action(skills, action_id):
        text = skill.get("clarifyGuidance")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        body = skill.get("body")
        if isinstance(body, str) and body.strip():
            parts.append(body.strip())
    return "\n\n".join(parts)


def search_ux_messages(skills: list[dict[str, Any]]) -> dict[str, str]:
    ux = dict(_DEFAULT_SEARCH_UX)
    for skill in skills or []:
        raw = skill.get("searchUX")
        if not isinstance(raw, dict):
            continue
        for key in ("emptyStrict", "emptyAlternatives"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                ux[key] = val.strip()
    return ux


def hint_path_get(hints: dict[str, Any] | None, path: str) -> Any:
    """Resolve dotted path like productOptions.sizes on session_hints."""
    node: Any = hints or {}
    for part in (path or "").split("."):
        if not part or not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def format_skill_template(template: str, values: dict[str, Any]) -> str:
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out
